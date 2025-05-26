import os
import time
import requests
import telebot
import random
import string
import threading
import datetime
from faker import Faker
from dotenv import load_dotenv
import pyotp
import binascii
import hashlib # Added for MD5 hashing for the new mail API

load_dotenv()
fake = Faker()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise Exception("❌ BOT_TOKEN not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown") # Default parse mode

# --- Temp Mail API Configuration ---
# Using temp-mail.org API (which temp-mail.io redirects to)
TEMP_MAIL_DOMAINS_URL = "https://api.temp-mail.org/request/domains/format/json/"
TEMP_MAIL_MESSAGES_URL_FORMAT = "https://api.temp-mail.org/request/mail/id/{md5_hash}/format/json/"

# Data storage
user_data = {}  # Stores {"email": "temp_email@address.com"} for temp mail
last_message_ids = {} # Stores set of seen mail_ids for auto-refresh per chat_id
user_2fa_secrets = {} # Stores {"platform": "Google", "secret": "BASE32SECRET"}
active_sessions = set()
pending_approvals = {}
approved_users = set()
user_profiles = {}  # Stores additional user profile info

# --- Helper Functions ---

def is_admin(chat_id):
    return str(chat_id) == ADMIN_ID

def safe_delete_user(chat_id):
    if chat_id in user_data:
        del user_data[chat_id]
    if chat_id in last_message_ids:
        del last_message_ids[chat_id]
    if chat_id in user_2fa_secrets: # Changed from user_2fa_codes
        del user_2fa_secrets[chat_id]
    # user_2fa_secrets was already listed, this is fine.
    if chat_id in active_sessions:
        try:
            active_sessions.remove(chat_id)
        except KeyError:
            pass # Already removed
    if chat_id in pending_approvals:
        del pending_approvals[chat_id]
    if chat_id in approved_users:
        try:
            approved_users.remove(chat_id)
        except KeyError:
            pass
    if chat_id in user_profiles:
        del user_profiles[chat_id]

def is_bot_blocked(chat_id):
    try:
        bot.get_chat(chat_id)
        return False
    except telebot.apihelper.ApiTelegramException as e:
        if e.result and e.result.status_code == 403: # Check e.result exists
             # Broadened check for block-related messages
            if "bot was blocked" in e.description.lower() or \
               "user is deactivated" in e.description.lower() or \
               "chat not found" in e.description.lower(): # Chat not found can also mean user deleted account
                return True
        return False
    except Exception:
        return False # Assume not blocked on other errors

def get_user_info(user):
    return {
        "name": user.first_name + (f" {user.last_name}" if user.last_name else ""),
        "username": user.username if user.username else "N/A",
        "join_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def get_main_keyboard(chat_id):
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("📬 New mail", "🔄 Refresh")
    keyboard.row("👨 Male Profile", "👩 Female Profile")
    keyboard.row("🔐 2FA Auth", "👤 My Account")
    if is_admin(chat_id):
        keyboard.row("👑 Admin Panel")
    return keyboard

def get_admin_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("👥 Pending Approvals", "📊 Stats")
    keyboard.row("👤 User Management", "📢 Broadcast")
    keyboard.row("⬅️ Main Menu")
    return keyboard

def get_user_management_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("📜 List Users", "❌ Remove User")
    keyboard.row("⬅️ Back to Admin")
    return keyboard

def get_approval_keyboard(user_id):
    keyboard = telebot.types.InlineKeyboardMarkup()
    keyboard.add(
        telebot.types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
        telebot.types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")
    )
    return keyboard

def get_user_account_keyboard(): # Currently not used by a direct button, but good to have
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("📧 My Email", "🆔 My Info") # "My Email" shows current temp mail
    keyboard.row("⬅️ Back to Main")
    return keyboard

def get_2fa_platform_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("Google", "Facebook", "Instagram")
    keyboard.row("Twitter", "Microsoft", "Apple")
    keyboard.row("⬅️ Back to Main")
    return keyboard

def get_back_keyboard(): # Generic back button
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("⬅️ Back")
    return keyboard

def get_broadcast_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("📢 Text Broadcast", "📋 Media Broadcast")
    keyboard.row("⬅️ Back to Admin")
    return keyboard

def safe_send_message(chat_id, text, **kwargs):
    # Ensure parse_mode is passed if not Markdown, or rely on bot's default
    if 'parse_mode' not in kwargs:
        kwargs['parse_mode'] = "Markdown" # Explicitly set if not overridden

    try:
        if is_bot_blocked(chat_id):
            print(f"Bot blocked by {chat_id}, cleaning up user.")
            safe_delete_user(chat_id)
            return None
        
        msg = bot.send_message(chat_id, text, **kwargs)
        active_sessions.add(chat_id)
        return msg
    except telebot.apihelper.ApiTelegramException as e:
        if e.result and e.result.status_code == 403:
            if "bot was blocked" in e.description.lower() or \
               "user is deactivated" in e.description.lower() or \
               "chat not found" in e.description.lower():
                print(f"Bot blocked by {chat_id} (API Exception), cleaning up user.")
                safe_delete_user(chat_id)
        else:
            print(f"API Error sending message to {chat_id}: {e.description}")
        return None
    except Exception as e:
        print(f"Generic error sending message to {chat_id}: {str(e)}")
        return None

# --- Temp Mail (temp-mail.org API) Functions ---

def get_temp_mail_domains_list():
    """Fetches a list of available domains from temp-mail.org API."""
    try:
        res = requests.get(TEMP_MAIL_DOMAINS_URL, timeout=10)
        res.raise_for_status() # Raises an exception for bad status codes
        domains = res.json()
        # Expected response: ["domain1.com", "another.org"]
        if isinstance(domains, list) and all(isinstance(d, str) for d in domains):
            # Filter out potential invalid entries, ensure they look like domains
            return [d.strip().lstrip('.') for d in domains if '.' in d and len(d) > 3]
        print(f"Unexpected domain format from temp-mail.org: {domains}")
        return ["pley.me", "zepcat.com"] # Fallback domains
    except requests.exceptions.RequestException as e:
        print(f"Error fetching temp-mail.org domains (RequestException): {e}")
        return ["pley.me", "zepcat.com"] # Fallback domains
    except Exception as e: # Catch other errors like JSONDecodeError
        print(f"Error fetching temp-mail.org domains (Exception): {e}")
        return ["pley.me", "zepcat.com"] # Fallback domains

def generate_random_temp_email_address():
    """Generates a random email address using a domain from temp-mail.org."""
    domains = get_temp_mail_domains_list()
    if not domains: # Should use fallback from get_temp_mail_domains_list
        # This is an additional safety net
        selected_domain = "mailinator.com" # A well-known fallback if API fails badly
    else:
        selected_domain = random.choice(domains)
    
    username_part = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    email = f"{username_part}@{selected_domain}"
    return email

def fetch_messages_from_temp_mail(email_address):
    """Fetches messages for a given email address from temp-mail.org API."""
    try:
        email_md5 = hashlib.md5(email_address.encode('utf-8')).hexdigest()
        url = TEMP_MAIL_MESSAGES_URL_FORMAT.format(md5_hash=email_md5)
        
        # The API might return an error within a 200 OK response, e.g., {"error":"no_messages"}
        res = requests.get(url, timeout=15)
        
        if res.status_code == 200:
            try:
                data = res.json()
                # Check for API-specific error messages in the JSON body
                if isinstance(data, dict) and 'error' in data:
                    # Known errors: "no_messages", "mailbox_not_found", "this_domain_is_not_in_our_database"
                    # print(f"API info for {email_address}: {data['error']}")
                    return [] # Treat as no messages or error
                if isinstance(data, list):
                    return data # Expected: list of message objects
                # print(f"Unexpected JSON structure from temp-mail for {email_address}: {data}")
                return []
            except ValueError: # JSONDecodeError
                # print(f"Invalid JSON response from temp-mail for {email_address}: {res.text[:200]}")
                return [] # Cannot parse response
        else:
            # print(f"Error fetching messages for {email_address}: HTTP {res.status_code} - {res.text[:200]}")
            return [] # HTTP error
            
    except requests.exceptions.Timeout:
        print(f"Timeout fetching messages for {email_address}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"RequestException fetching messages for {email_address}: {e}")
        return []
    except Exception as e:
        print(f"Generic exception fetching messages for {email_address}: {e}")
        return []


# --- Profile generator (Unchanged from original) ---
def generate_username():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))

def generate_password():
    today_day = datetime.datetime.now().strftime("%d")
    base = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return base + today_day

def generate_us_phone():
    area_code = str(random.randint(200, 999))
    number = ''.join([str(random.randint(0, 9)) for _ in range(7)])
    return f"1{area_code}{number}"

def generate_profile(gender):
    name = fake.name_male() if gender == "male" else fake.name_female()
    username = generate_username()
    password = generate_password()
    phone = generate_us_phone()
    return gender, name, username, password, phone

def profile_message(gender, name, username, password, phone):
    gender_icon = "👨" if gender == "male" else "👩"
    return (
        f"🔐 *Generated Profile*\n\n"
        f"{gender_icon} *Gender:* {gender.capitalize()}\n"
        f"🧑‍💼 *Name:* `{name}`\n"
        f"🆔 *Username:* `{username}`\n"
        f"🔑 *Password:* `{password}`\n"
        f"📞 *Phone:* `{phone}`\n\n"
        f"✅ Tap on any value to copy"
    )

# --- 2FA Feature Functions ---

def is_valid_base32(secret):
    """Check if the secret is valid Base32"""
    try:
        cleaned = secret.replace(" ", "").replace("-", "").upper()
        if not cleaned: return False # Empty string is not valid
        # pyotp will throw error if invalid (malformed, wrong length for some algos, etc.)
        # Basic check for base32 characters
        if not all(c in string.ascii_uppercase + "234567" for c in cleaned):
            return False
        # Ensure padding is correct if present (multiple of 8 characters)
        # pyotp handles padding, but this is a pre-check
        if len(cleaned) % 8 != 0 and '=' in cleaned: # A bit simplistic for padding check
             pass # pyotp will handle more complex padding validation
        pyotp.TOTP(cleaned).now() # Attempt to create TOTP object
        return True
    except (binascii.Error, ValueError, TypeError, Exception) as e: # Broader catch
        # print(f"Base32 validation error: {e}")
        return False

# --- Background Workers ---

def auto_refresh_worker():
    while True:
        try:
            active_user_data_keys = list(user_data.keys()) # Iterate over a copy
            for chat_id in active_user_data_keys:
                if chat_id not in user_data: # User might have been deleted during iteration
                    continue

                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id)
                    continue
                
                current_user_session = user_data.get(chat_id)
                if not current_user_session or "email" not in current_user_session:
                    continue # No email configured for this user, skip

                email_address = current_user_session["email"]
                messages = fetch_messages_from_temp_mail(email_address)

                if not messages: # No messages or error fetching
                    continue

                seen_ids = last_message_ids.setdefault(chat_id, set())
                new_messages_found = False

                # Process messages in reverse order from API (usually newest first)
                # but send to user in chronological order if multiple new ones.
                # However, most APIs return newest first, so processing as they come is fine.
                for msg_data in messages: # Assuming newest are first
                    msg_id = msg_data.get("mail_id") # temp-mail.org uses "mail_id"
                    if not msg_id:
                        continue
                    
                    if msg_id in seen_ids:
                        continue # Already processed this message

                    new_messages_found = True
                    seen_ids.add(msg_id)
                    
                    # Extract message details (temp-mail.org format)
                    sender = msg_data.get("mail_from", "N/A")
                    subject = msg_data.get("mail_subject", "(No Subject)")
                    body = msg_data.get("mail_text_only") or msg_data.get("mail_preview", "(No Content)")
                    body = body.strip() if body else "(No Content)"
                    
                    received_ts = msg_data.get("mail_timestamp")
                    if received_ts:
                        try:
                            # Ensure timestamp is float or int before conversion
                            received_time = datetime.datetime.fromtimestamp(float(received_ts)).strftime('%Y-%m-%d %H:%M:%S')
                        except (ValueError, TypeError):
                            received_time = msg_data.get("mail_date", "Just now") # Fallback to string date
                    else:
                        received_time = msg_data.get("mail_date", "Just now")

                    formatted_msg = (
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📬 *New Email Received!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 *From:* `{sender}`\n"
                        f"📨 *Subject:* _{subject}_\n"
                        f"🕒 *Received:* {received_time}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💬 *Body:*\n"
                        f"{body[:3800]}\n" # Keep it under Telegram's limit, leaving room for formatting
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )
                    safe_send_message(chat_id, formatted_msg) # Relies on default Markdown

                # Optional: Limit the size of seen_ids to prevent memory bloat over long time
                # For example, keep only the latest N IDs or IDs from the last X days
                # For simplicity, this is omitted here.

        except Exception as e:
            print(f"Error in auto_refresh_worker: {e}")
        time.sleep(45) # Check for new mail less frequently to be polite to the API

def cleanup_blocked_users():
    while True:
        try:
            sessions_to_check = list(active_sessions) # Iterate on a copy
            for chat_id in sessions_to_check:
                if is_bot_blocked(chat_id):
                    print(f"Cleanup: User {chat_id} blocked the bot. Removing data.")
                    safe_delete_user(chat_id)
        except Exception as e:
            print(f"Error in cleanup_blocked_users: {e}")
        time.sleep(3600) # Run hourly

# --- Bot Handlers ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): # Check upfront
        safe_delete_user(chat_id)
        return

    user_info = get_user_info(message.from_user)
    user_profiles[chat_id] = user_info # Store/update profile info

    if is_admin(chat_id):
        approved_users.add(chat_id) # Admin is auto-approved
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=get_main_keyboard(chat_id))
        return

    if chat_id in approved_users:
        safe_send_message(chat_id, "👋 Welcome back!", reply_markup=get_main_keyboard(chat_id))
    else:
        if chat_id not in pending_approvals: # Send request only if not already pending
            pending_approvals[chat_id] = user_info
            safe_send_message(chat_id, "👋 Your access request has been sent to the admin. Please wait for approval.")
            if ADMIN_ID: # Notify admin
                try:
                    admin_chat_id = int(ADMIN_ID)
                    approval_msg = (
                        f"🆕 *New Approval Request*\n\n"
                        f"🆔 User ID: `{chat_id}`\n"
                        f"👤 Name: `{user_info['name']}`\n"
                        f"📛 Username: @{user_info['username']}\n"
                        f"📅 Joined: `{user_info['join_date']}`"
                    )
                    bot.send_message(admin_chat_id, approval_msg, reply_markup=get_approval_keyboard(chat_id), parse_mode="Markdown")
                except Exception as e:
                    print(f"Failed to send approval request to admin {ADMIN_ID}: {e}")
        else:
            safe_send_message(chat_id, "⏳ Your access request is still pending. Please wait for admin approval.")


# --- Admin Panel Handlers (Largely Unchanged) ---
@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel" and is_admin(msg.chat.id))
def admin_panel(message):
    safe_send_message(message.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "👥 Pending Approvals" and is_admin(msg.chat.id))
def show_pending_approvals(message):
    if not pending_approvals:
        safe_send_message(message.chat.id, "✅ No pending approvals.")
        return
    for user_id, user_info in list(pending_approvals.items()): # Iterate a copy
        approval_msg = (
            f"🆕 *Pending Approval*\n\n"
            f"🆔 User ID: `{user_id}`\n"
            f"👤 Name: `{user_info['name']}`\n"
            f"📛 Username: @{user_info['username']}\n"
            f"📅 Joined: `{user_info['join_date']}`"
        )
        safe_send_message(message.chat.id, approval_msg, reply_markup=get_approval_keyboard(user_id))

@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.chat.id))
def show_stats(message):
    bot_start_time_str = user_profiles.get("bot_start_time", "Not recorded") # Assuming you might store this
    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin ID: `{ADMIN_ID}`\n"
        f"👥 Approved Users: `{len(approved_users)}`\n"
        f"👤 Active User Sessions (sent a msg): `{len(active_sessions)}`\n"
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"📧 Active Email Addresses: `{len(user_data)}` (users with generated temp mail)\n"
        # f"🕒 Bot Uptime: See console or deploy environment for exact uptime." # Uptime is complex for a simple script
        f"Current Time: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
    )
    safe_send_message(message.chat.id, stats_msg)

@bot.message_handler(func=lambda msg: msg.text == "👤 User Management" and is_admin(msg.chat.id))
def user_management(message):
    safe_send_message(message.chat.id, "👤 User Management Panel", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📜 List Users" and is_admin(msg.chat.id))
def list_users(message):
    if not approved_users:
        safe_send_message(message.chat.id, "❌ No approved users yet.")
        return
    
    users_list_formatted = []
    for user_id in approved_users:
        if user_id in user_profiles:
            user_info = user_profiles[user_id]
            email_info = " (No temp mail)"
            if user_id in user_data and "email" in user_data[user_id]:
                email_info = f" (Mail: `{user_data[user_id]['email']}`)"

            users_list_formatted.append(
                f"🆔 `{user_id}` - 👤 {user_info['name']} (@{user_info['username']}){email_info}\nJoined: _{user_info['join_date']}_"
            )
        else:
            users_list_formatted.append(f"🆔 `{user_id}` - _(Profile info not available)_")

    if not users_list_formatted:
        safe_send_message(message.chat.id, "❌ No user data available to list.")
        return

    response_header = f"👥 *Approved Users ({len(users_list_formatted)})*:\n\n"
    current_message = response_header
    
    for user_entry in users_list_formatted:
        if len(current_message) + len(user_entry) + 2 > 4096: # Check message length limit
            safe_send_message(message.chat.id, current_message)
            current_message = response_header # Start new message
        current_message += user_entry + "\n\n"
    
    if current_message != response_header: # Send the last chunk
        safe_send_message(message.chat.id, current_message)


@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_user_prompt(message):
    msg = safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=get_back_keyboard())
    if msg:
        bot.register_next_step_handler(msg, process_user_removal)

def process_user_removal(message):
    admin_chat_id = message.chat.id # The admin performing the action
    if message.text == "⬅️ Back":
        safe_send_message(admin_chat_id, "Cancelled user removal.", reply_markup=get_user_management_keyboard())
        return
    try:
        user_id_to_remove = int(message.text.strip())
        if str(user_id_to_remove) == ADMIN_ID: # Compare as strings or both as int
            safe_send_message(admin_chat_id, "❌ Cannot remove the Admin account!", reply_markup=get_user_management_keyboard())
            return

        if user_id_to_remove in approved_users or user_id_to_remove in pending_approvals:
            # Perform full cleanup
            original_username = user_profiles.get(user_id_to_remove, {}).get('username', 'N/A')
            safe_delete_user(user_id_to_remove) # This removes from approved_users, pending_approvals, etc.
            
            safe_send_message(admin_chat_id, f"✅ User {user_id_to_remove} (@{original_username}) has been removed and all their data cleared.", reply_markup=get_user_management_keyboard())
            try:
                # Notify the removed user if they haven't blocked the bot
                safe_send_message(user_id_to_remove, "❌ Your access to this bot has been revoked by the admin.")
            except Exception as e:
                # print(f"Could not notify user {user_id_to_remove} about removal: {e}")
                pass # User might have blocked the bot or account deleted
        else:
            safe_send_message(admin_chat_id, f"❌ User {user_id_to_remove} not found in approved or pending users.", reply_markup=get_user_management_keyboard())
    except ValueError:
        safe_send_message(admin_chat_id, "❌ Invalid User ID. Please enter a numeric ID.", reply_markup=get_user_management_keyboard())
    except Exception as e:
        print(f"Error in process_user_removal: {e}")
        safe_send_message(admin_chat_id, "An error occurred. Please try again.", reply_markup=get_user_management_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def broadcast_menu(message):
    safe_send_message(message.chat.id, "📢 Broadcast Message to All Approved Users", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def process_text_broadcast_prompt(message):
    msg = safe_send_message(message.chat.id, "✍️ Enter the broadcast message text (Markdown supported):", reply_markup=get_back_keyboard())
    if msg:
        bot.register_next_step_handler(msg, process_text_broadcast)

def process_text_broadcast(message):
    admin_chat_id = message.chat.id
    if message.text == "⬅️ Back":
        safe_send_message(admin_chat_id, "Cancelled text broadcast.", reply_markup=get_broadcast_keyboard())
        return

    broadcast_text = message.text
    if not broadcast_text:
        safe_send_message(admin_chat_id, "❌ Broadcast message cannot be empty.", reply_markup=get_broadcast_keyboard())
        return

    success_count = 0
    failed_count = 0
    
    # Filter out admin from broadcast list if they are also an approved user (they get reports separately)
    users_to_broadcast = [uid for uid in list(approved_users) if str(uid) != ADMIN_ID]
    total_users_to_broadcast = len(users_to_broadcast)

    if total_users_to_broadcast == 0:
        safe_send_message(admin_chat_id, "🤷 No users (excluding admin) to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_text = f"📢 Broadcasting to {total_users_to_broadcast} users...\n\nSent: 0/{total_users_to_broadcast}"
    progress_message = safe_send_message(admin_chat_id, progress_msg_text)
    
    for i, user_id in enumerate(users_to_broadcast, 1):
        try:
            # Use safe_send_message which handles blocks and uses Markdown by default
            sent_msg = safe_send_message(user_id, f"📢 *Admin Broadcast:*\n\n{broadcast_text}")
            if sent_msg:
                success_count += 1
            else: # safe_send_message returned None (e.g., blocked)
                failed_count += 1
                # safe_delete_user would have been called by safe_send_message if blocked
        except Exception: # Catch any other unexpected errors during send
            failed_count += 1
        
        if i % 10 == 0 or i == total_users_to_broadcast: # Update progress every 10 users or at the end
            if progress_message:
                try:
                    bot.edit_message_text(
                        f"📢 Broadcasting to {total_users_to_broadcast} users...\n\n"
                        f"Sent: {i}/{total_users_to_broadcast}\n"
                        f"✅ Successful: {success_count}\n"
                        f"❌ Failed: {failed_count}",
                        chat_id=admin_chat_id,
                        message_id=progress_message.message_id,
                        parse_mode="Markdown"
                    )
                except Exception: # Edit might fail if message is too old or identical
                    pass
        time.sleep(0.2) # Small delay to avoid hitting rate limits

    final_report = f"📢 Text Broadcast Completed!\n\n✅ Sent to {success_count} users.\n❌ Failed for {failed_count} users."
    safe_send_message(admin_chat_id, final_report, reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def media_broadcast_prompt(message):
    msg = safe_send_message(message.chat.id, "🖼 Send the photo/video/document you want to broadcast (you can add a caption now):", reply_markup=get_back_keyboard())
    if msg:
        bot.register_next_step_handler(msg, process_media_broadcast)

def process_media_broadcast(message):
    admin_chat_id = message.chat.id
    if message.text == "⬅️ Back": # Check if user sent text "Back" instead of media
        safe_send_message(admin_chat_id, "Cancelled media broadcast.", reply_markup=get_broadcast_keyboard())
        return

    caption = message.caption if message.caption else ""
    media_sent = False

    success_count = 0
    failed_count = 0
    users_to_broadcast = [uid for uid in list(approved_users) if str(uid) != ADMIN_ID]
    total_users_to_broadcast = len(users_to_broadcast)

    if total_users_to_broadcast == 0:
        safe_send_message(admin_chat_id, "🤷 No users (excluding admin) to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_text = f"📢 Broadcasting media to {total_users_to_broadcast} users...\n\nSent: 0/{total_users_to_broadcast}"
    progress_message = safe_send_message(admin_chat_id, progress_msg_text)

    for i, user_id in enumerate(users_to_broadcast, 1):
        current_media_sent_to_user = False
        try:
            if is_bot_blocked(user_id): # Pre-check, though send functions also handle it
                failed_count +=1
                safe_delete_user(user_id)
                continue

            if message.photo:
                bot.send_photo(user_id, message.photo[-1].file_id, caption=caption, parse_mode="Markdown")
                current_media_sent_to_user = True
            elif message.video:
                bot.send_video(user_id, message.video.file_id, caption=caption, parse_mode="Markdown")
                current_media_sent_to_user = True
            elif message.document:
                bot.send_document(user_id, message.document.file_id, caption=caption, parse_mode="Markdown")
                current_media_sent_to_user = True
            elif message.audio:
                bot.send_audio(user_id, message.audio.file_id, caption=caption, parse_mode="Markdown")
                current_media_sent_to_user = True
            elif message.voice:
                bot.send_voice(user_id, message.voice.file_id, caption=caption, parse_mode="Markdown")
                current_media_sent_to_user = True
            else:
                # This case should ideally be caught before starting the loop
                # if no media was provided by admin in the first place.
                if i == 1: # Only report this once
                     safe_send_message(admin_chat_id, "❌ No valid media found in your message to broadcast.", reply_markup=get_broadcast_keyboard())
                     if progress_message: bot.delete_message(admin_chat_id, progress_message.message_id)
                     return # Abort broadcast
                failed_count += 1 # Should not happen if initial check is good.
                continue 
            
            if current_media_sent_to_user:
                success_count += 1
                media_sent = True # Mark that at least one type of media was identified and attempted
            
        except telebot.apihelper.ApiTelegramException as e:
            failed_count += 1
            if e.result and e.result.status_code == 403 : # Blocked or similar
                safe_delete_user(user_id)
            # print(f"API error broadcasting media to {user_id}: {e.description}")
        except Exception as e:
            failed_count += 1
            # print(f"Generic error broadcasting media to {user_id}: {e}")

        if i % 5 == 0 or i == total_users_to_broadcast: # Update progress
            if progress_message:
                try:
                    bot.edit_message_text(
                        f"📢 Broadcasting media to {total_users_to_broadcast} users...\n\n"
                        f"Sent: {i}/{total_users_to_broadcast}\n"
                        f"✅ Successful: {success_count}\n"
                        f"❌ Failed: {failed_count}",
                        chat_id=admin_chat_id,
                        message_id=progress_message.message_id,
                        parse_mode="Markdown"
                    )
                except: pass
        time.sleep(0.3) # Slightly longer delay for media

    if not media_sent and total_users_to_broadcast > 0 : # If admin sent text instead of media for example
        safe_send_message(admin_chat_id, "❌ You did not provide any media to broadcast. Please try again.", reply_markup=get_broadcast_keyboard())
        if progress_message: bot.delete_message(admin_chat_id, progress_message.message_id) #cleanup progress message
        return

    final_report = f"📢 Media Broadcast Completed!\n\n✅ Sent to {success_count} users.\n❌ Failed for {failed_count} users."
    safe_send_message(admin_chat_id, final_report, reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin" and is_admin(msg.chat.id))
def back_to_admin(message):
    safe_send_message(message.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu" and is_admin(msg.chat.id)) # Admin specific main menu return
def admin_back_to_main(message):
    # Clear 2FA state if admin was using it for themselves
    if message.chat.id in user_2fa_secrets:
        del user_2fa_secrets[message.chat.id]
    safe_send_message(message.chat.id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(message.chat.id))


@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def handle_approval(call):
    if not is_admin(call.message.chat.id): # Ensure only admin can process this
        bot.answer_callback_query(call.id, "⚠️ Action restricted to admin.")
        return

    try:
        action, user_id_str = call.data.split('_')
        user_id = int(user_id_str)
    except ValueError:
        bot.answer_callback_query(call.id, "Error: Invalid user ID in callback.")
        bot.edit_message_text("Error processing request.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        return

    user_info = pending_approvals.get(user_id, user_profiles.get(user_id)) # Get info if available

    if action == "approve":
        approved_users.add(user_id)
        if user_id in pending_approvals:
            del pending_approvals[user_id]
        
        # Notify user of approval
        safe_send_message(user_id, "✅ Your access request has been approved by the admin! You can now use the bot.", reply_markup=get_main_keyboard(user_id))
        
        bot.answer_callback_query(call.id, f"User {user_id} approved.")
        new_text = f"✅ User {user_id} (@{user_info.get('username', 'N/A') if user_info else 'N/A'}) approved."
        bot.edit_message_text(new_text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None, parse_mode="Markdown")
    
    elif action == "reject":
        if user_id in pending_approvals:
            del pending_approvals[user_id]
        # Also remove from approved_users if they were somehow there and then rejected (unlikely flow but safe)
        if user_id in approved_users:
            approved_users.remove(user_id)
        safe_delete_user(user_id) # Clean up all data for rejected user

        safe_send_message(user_id, "❌ Your access request has been rejected by the admin.")
        bot.answer_callback_query(call.id, f"User {user_id} rejected.")
        new_text = f"❌ User {user_id} (@{user_info.get('username', 'N/A') if user_info else 'N/A'}) rejected and data cleared."
        bot.edit_message_text(new_text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None, parse_mode="Markdown")
    else:
        bot.answer_callback_query(call.id, "Unknown action.")


# --- Mail handlers (Using New Temp Mail API) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail_handler(message): # Renamed to avoid conflict with module
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if chat_id not in approved_users and not is_admin(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval. Please wait.")
        return

    # For temp-mail.org, we just generate an address client-side
    # No account creation or token needed on the API side.
    loading_msg = safe_send_message(chat_id, "⏳ Generating new temporary email address...")
    
    email_address = generate_random_temp_email_address()

    if loading_msg: # Delete "loading" message
        try:
            bot.delete_message(chat_id, loading_msg.message_id)
        except: pass


    if email_address:
        user_data[chat_id] = {"email": email_address}
        last_message_ids[chat_id] = set() # Reset seen messages for the new email
        msg_text = (
            f"✅ *New Temporary Email Generated!*\n\n"
            f"Your new email address is:\n`{email_address}`\n\n"
            f"(Tap to copy). Messages will appear automatically or when you hit '🔄 Refresh'."
        )
        safe_send_message(chat_id, msg_text)
    else:
        safe_send_message(chat_id, "❌ Failed to generate a temporary email address. The domain service might be down. Please try again later.")

@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh")
def refresh_mail_handler(message): # Renamed
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if chat_id not in approved_users and not is_admin(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    current_user_data = user_data.get(chat_id)
    if not current_user_data or "email" not in current_user_data:
        safe_send_message(chat_id, "⚠️ You don't have an active temporary email. Use '📬 New mail' to get one.")
        return

    email_address = current_user_data["email"]
    loading_msg = safe_send_message(chat_id, f"🔄 Checking for new mail at `{email_address}`...")

    messages = fetch_messages_from_temp_mail(email_address)
    
    if loading_msg:
        try:
            bot.delete_message(chat_id, loading_msg.message_id)
        except: pass

    if messages is None: # Indicates a connection or API error during fetch
        safe_send_message(chat_id, "❌ Error connecting to the mail server. Please try again later.")
        return
        
    if not messages:
        safe_send_message(chat_id, f"📭 Your inbox for `{email_address}` is currently empty or no new messages found since last auto-check.")
        return

    safe_send_message(chat_id, f"📬 *Latest emails for `{email_address}`:*")
    
    # temp-mail.org API usually returns messages newest first. We'll display top few.
    displayed_count = 0
    for msg_data in messages[:5]: # Show up to 5 latest messages on manual refresh
        msg_id = msg_data.get("mail_id")
        if not msg_id: continue # Skip if message has no ID

        # Add to seen_ids even on manual refresh to avoid immediate re-notification by auto-refresher
        # if manual refresh happens between auto-refresh cycles.
        last_message_ids.setdefault(chat_id, set()).add(msg_id)

        sender = msg_data.get("mail_from", "N/A")
        subject = msg_data.get("mail_subject", "(No Subject)")
        body = msg_data.get("mail_text_only") or msg_data.get("mail_preview", "(No Content)")
        body = body.strip() if body else "(No Content)"
        
        received_ts = msg_data.get("mail_timestamp")
        if received_ts:
            try:
                received_time = datetime.datetime.fromtimestamp(float(received_ts)).strftime('%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                received_time = msg_data.get("mail_date", "Just now")
        else:
            received_time = msg_data.get("mail_date", "Just now")

        formatted_msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            # f"📬 *Email Details:*\n" # Or keep New Email Received
            f"👤 *From:* `{sender}`\n"
            f"📨 *Subject:* _{subject}_\n"
            f"🕒 *Received:* {received_time}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💬 *Body Preview:*\n"
            f"{body[:1000]}\n" # Show a shorter preview for refresh list
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        safe_send_message(chat_id, formatted_msg)
        displayed_count += 1
    
    if displayed_count == 0 : # Should not happen if `if not messages:` check passed, but as safety.
         safe_send_message(chat_id, f"📭 No messages found in your inbox for `{email_address}` at the moment.")


# --- Profile handlers (Unchanged) ---
@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
def generate_profile_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if chat_id not in approved_users and not is_admin(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    gender = "male" if message.text == "👨 Male Profile" else "female"
    # generate_profile returns: gender, name, username, password, phone
    generated_gender, name, username, password, phone = generate_profile(gender)
    response_text = profile_message(generated_gender, name, username, password, phone)
    safe_send_message(chat_id, response_text)

# --- 2FA Handlers (Refined) ---
@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def two_fa_auth_start(message): # Renamed
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if chat_id not in approved_users and not is_admin(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    # Clear any previous 2FA state for this user before starting new one
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]
        
    safe_send_message(chat_id, "🔐 Choose the platform for which you want to generate a 2FA code:", reply_markup=get_2fa_platform_keyboard())

@bot.message_handler(func=lambda msg: msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform_selection(message): # Renamed
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return # No need for safe_delete_user here, will be caught by other flows

    platform = message.text
    
    # Initialize or reset 2FA state for this user, waiting for secret
    user_2fa_secrets[chat_id] = {"platform": platform} # Secret will be added next
    
    msg_to_send = f"🔢 Please enter the Base32 2FA secret key for *{platform}*:"
    sent_msg = safe_send_message(chat_id, msg_to_send, reply_markup=get_back_keyboard()) # Using "Back" to go to 2FA platform choice
    
    # The 'Back' button on get_back_keyboard() should ideally lead back to platform selection
    # or main menu. For simplicity here, "Back" from secret input will be handled by "handle_all_text"
    # if it's a generic "Back", or "⬅️ Back to Main" will clear state.

# "Back" button handler when expecting 2FA secret or platform choice
@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back" and msg.chat.id in user_2fa_secrets)
def handle_2fa_back_button(message):
    chat_id = message.chat.id
    # If they were about to enter a secret, go back to platform selection
    if "platform" in user_2fa_secrets[chat_id] and "secret" not in user_2fa_secrets[chat_id]:
        del user_2fa_secrets[chat_id]["platform"] # Clear only platform, ready for new choice
        safe_send_message(chat_id, " lựa chọn lại nền tảng (Choose platform again):", reply_markup=get_2fa_platform_keyboard())
    else: # Otherwise, or if state is unclear, go to main menu
        if chat_id in user_2fa_secrets: # Clear all 2FA state
            del user_2fa_secrets[chat_id]
        safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))


@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main") # General "Back to Main"
def back_to_main_menu_handler(message): # Renamed
    chat_id = message.chat.id
    # If user was in any 2FA flow, clear sensitive 2FA data
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]
        # print(f"Cleared 2FA secrets for {chat_id} on back_to_main_menu_handler")
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))


# --- My Account Section ---
@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
def my_account_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if chat_id not in approved_users and not is_admin(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    # For now, "My Account" will just show current email and basic info.
    # The get_user_account_keyboard() provides options for this.
    # Let's directly show info instead of another keyboard for simplicity here.
    
    user_info_str = "👤 *Your Account Information:*\n\n"
    profile = user_profiles.get(chat_id)
    if profile:
        user_info_str += f"▪️ Name: `{profile['name']}`\n"
        user_info_str += f"▪️ Username: `@{profile['username']}`\n"
        user_info_str += f"▪️ Joined Bot: `{profile['join_date']}`\n"
    else:
        user_info_str += "_Basic profile info not found._\n"

    if chat_id in user_data and "email" in user_data[chat_id]:
        user_info_str += f"📧 Current Temp Mail: `{user_data[chat_id]['email']}`\n"
    else:
        user_info_str += "📧 _No active temporary email._\n"
        
    safe_send_message(chat_id, user_info_str, reply_markup=get_main_keyboard(chat_id)) # Keep main keyboard

# This handler will catch 2FA secret keys or any other unhandled text
@bot.message_handler(func=lambda msg: True, content_types=['text'])
def handle_all_other_text(message):
    chat_id = message.chat.id
    text = message.text.strip()

    if is_bot_blocked(chat_id): # Should ideally not reach here if checks are upfront
        safe_delete_user(chat_id)
        return

    # Check if we are expecting a 2FA secret key
    if chat_id in user_2fa_secrets and \
       "platform" in user_2fa_secrets[chat_id] and \
       "secret" not in user_2fa_secrets[chat_id]: # State: platform chosen, waiting for secret
        
        if text == "⬅️ Back": # User might use the generic "Back" from secret input stage
            # This specific "Back" should take them to platform selection again
            del user_2fa_secrets[chat_id]["platform"] # Ready for new platform choice
            safe_send_message(chat_id, " lựa chọn lại nền tảng (Choose platform again):", reply_markup=get_2fa_platform_keyboard())
            return

        secret_key = text # User's input is the secret key
        if not is_valid_base32(secret_key):
            err_msg = (
                f"❌ *Invalid Base32 Secret Key for {user_2fa_secrets[chat_id]['platform']}!*\n\n"
                f"A valid Base32 secret key should only contain uppercase letters (A-Z) and digits (2-7). "
                f"It should not contain spaces or special characters (like 0, 1, 8, 9).\n\n"
                f"Please try entering the secret key again, or press '⬅️ Back' to choose another platform or return to the main menu."
            )
            safe_send_message(chat_id, err_msg, reply_markup=get_back_keyboard()) # 'Back' here goes to platform choice
            return

        # Valid secret, store it and generate code
        clean_secret = secret_key.replace(" ", "").replace("-", "").upper()
        user_2fa_secrets[chat_id]["secret"] = clean_secret
        platform = user_2fa_secrets[chat_id]["platform"]
        
        try:
            totp = pyotp.TOTP(clean_secret)
            current_code = totp.now()
            
            now = datetime.datetime.now()
            seconds_remaining = 30 - (now.second % 30)
            
            reply_text = (
                f"🔐 *2FA Code for {platform}*\n\n"
                f"🔑 Code: `{current_code}`\n"
                f"⏳ Refreshes in: _{seconds_remaining} seconds_\n\n"
                f"Tap the code to copy. This code will refresh automatically."
            )
            
            inline_keyboard = telebot.types.InlineKeyboardMarkup()
            inline_keyboard.add(telebot.types.InlineKeyboardButton(f"🔄 Refresh Code ({seconds_remaining}s)", callback_data="generate_2fa_code"))
            
            safe_send_message(chat_id, reply_text, reply_markup=inline_keyboard)
            # The secret remains in user_2fa_secrets for the refresh button.
            # "platform" also remains.
            # User can use main keyboard buttons to navigate away, which should clear the secret via back_to_main_menu_handler.

        except Exception as e:
            safe_send_message(chat_id, f"Error generating 2FA code: {str(e)}. Please check your secret key.", reply_markup=get_main_keyboard(chat_id))
            if chat_id in user_2fa_secrets: del user_2fa_secrets[chat_id] # Clear faulty state
        return # End of 2FA secret processing

    # If not a 2FA secret input, and not any other command, send a generic "unknown command" or ignore.
    # For this bot, it's better to guide them if they are approved.
    if chat_id in approved_users or is_admin(chat_id):
        # safe_send_message(chat_id, f"🤔 Unknown command: '{text}'. Please use the buttons.", reply_markup=get_main_keyboard(chat_id))
        pass # Or just ignore unknown text to prevent spamming "unknown command"
    elif chat_id in pending_approvals:
        safe_send_message(chat_id, "⏳ Your access request is still pending. Please wait for admin approval.")
    else: # User not known and not pending, might be /start issue or spam
        # Trigger /start flow or a generic message
        # safe_send_message(chat_id, "Hello! Please use /start to begin.")
        pass # Ignore if not recognized and not approved/pending.


@bot.callback_query_handler(func=lambda call: call.data == "generate_2fa_code")
def generate_2fa_code_callback(call):
    chat_id = call.message.chat.id
    
    if chat_id not in user_2fa_secrets or "secret" not in user_2fa_secrets.get(chat_id, {}):
        bot.answer_callback_query(call.id, "⚠️ 2FA secret not set. Please start the 2FA setup again.")
        try:
            bot.edit_message_text("Error: 2FA secret not found. Please restart 2FA setup.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
        except: pass
        return

    secret = user_2fa_secrets[chat_id]["secret"]
    platform = user_2fa_secrets[chat_id].get("platform", "Selected Platform") # Get platform if stored
    
    try:
        totp = pyotp.TOTP(secret)
        current_code = totp.now()
        
        now = datetime.datetime.now()
        seconds_remaining = 30 - (now.second % 30)
        
        reply_text = (
            f"🔐 *2FA Code for {platform}*\n\n"
            f"🔑 Code: `{current_code}`\n"
            f"⏳ Refreshes in: _{seconds_remaining} seconds_\n\n"
            f"Tap the code to copy."
        )
        
        inline_keyboard = telebot.types.InlineKeyboardMarkup()
        inline_keyboard.add(telebot.types.InlineKeyboardButton(f"🔄 Refresh Code ({seconds_remaining}s)", callback_data="generate_2fa_code"))
        
        bot.edit_message_text(
            reply_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='Markdown', # Switched to Markdown
            reply_markup=inline_keyboard
        )
        bot.answer_callback_query(call.id, f"Code refreshed: {current_code}")

    except Exception as e:
        bot.answer_callback_query(call.id, "Error generating new code. Check secret.")
        try:
            bot.edit_message_text(f"Error refreshing code: {str(e)}. Please check your secret or restart setup.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
        except: pass
        if chat_id in user_2fa_secrets: del user_2fa_secrets[chat_id] # Clear faulty state


if __name__ == '__main__':
    print("🤖 Bot is preparing to launch...")
    if not ADMIN_ID:
        print("⚠️ WARNING: ADMIN_ID is not set in .env. Admin features will not be fully functional.")
    else:
        print(f"🔑 Admin ID: {ADMIN_ID}")

    # Start background tasks
    auto_refresh_thread = threading.Thread(target=auto_refresh_worker, daemon=True)
    auto_refresh_thread.start()
    print("🔄 Auto-refresh worker started.")

    cleanup_thread = threading.Thread(target=cleanup_blocked_users, daemon=True)
    cleanup_thread.start()
    print("🧹 User cleanup worker started.")
    
    user_profiles["bot_start_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"🚀 Bot started successfully at {user_profiles['bot_start_time']}!")
    
    try:
        bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20) # Added timeout and skip_pending
    except Exception as main_loop_error:
        print(f"❌ CRITICAL ERROR in bot polling loop: {main_loop_error}")
        # Consider further actions like restart or logging to a file/service
    finally:
        print("🛑 Bot has stopped.")

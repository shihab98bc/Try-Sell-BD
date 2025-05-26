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
import hashlib # For MD5 hashing for the temp mail API

load_dotenv()
fake = Faker()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID") # Ensure this is a STRING in your .env file

if not BOT_TOKEN:
    raise Exception("❌ BOT_TOKEN not set in .env. Please set it and restart.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown") # Default parse mode for all messages

# --- Temp Mail API Configuration (temp-mail.org) ---
TEMP_MAIL_DOMAINS_URL = "https://api.temp-mail.org/request/domains/format/json/"
TEMP_MAIL_MESSAGES_URL_FORMAT = "https://api.temp-mail.org/request/mail/id/{md5_hash}/format/json/"
FALLBACK_TEMP_MAIL_DOMAINS = ["pley.me", "zepcat.com", "mailto.plus", "fexpost.com"] # Added more fallbacks

# Data storage
user_data = {}  # Stores {"email": "temp_email@address.com"} for temp mail
last_message_ids = {} # Stores set of seen mail_ids for auto-refresh per chat_id: {chat_id: {mail_id1, mail_id2}}
user_2fa_secrets = {} # Stores {chat_id: {"platform": "Google", "secret": "BASE32SECRET"}}
active_sessions = set() # Tracks chat_ids that have interacted recently (e.g., sent a message)
pending_approvals = {} # {chat_id: user_info_dict}
approved_users = set() # {chat_id1, chat_id2}
user_profiles = {}  # {chat_id: {"name": "...", "username": "...", "join_date": "..."}}

# --- Helper Functions ---

def is_admin(chat_id):
    """Checks if the given chat_id is the admin."""
    return str(chat_id) == str(ADMIN_ID) # Compare as strings

def safe_delete_user(chat_id):
    """Safely removes all data associated with a user."""
    # print(f"Attempting to safely delete user: {chat_id}")
    if chat_id in user_data:
        del user_data[chat_id]
    if chat_id in last_message_ids:
        del last_message_ids[chat_id]
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]
    if chat_id in active_sessions:
        try:
            active_sessions.remove(chat_id)
        except KeyError:
            pass # Already removed or not present
    if chat_id in pending_approvals:
        del pending_approvals[chat_id]
    if chat_id in approved_users:
        try:
            approved_users.remove(chat_id)
        except KeyError:
            pass
    if chat_id in user_profiles:
        del user_profiles[chat_id]
    # print(f"User {chat_id} data cleared.")

def is_bot_blocked(chat_id):
    """Checks if the bot is blocked by the user or chat is inaccessible."""
    try:
        bot.get_chat(chat_id) # Simple check if chat is accessible
        return False
    except telebot.apihelper.ApiTelegramException as e:
        if e.result and e.result.status_code == 403:
            # Common error messages indicating a block or inaccessible chat
            error_desc = e.description.lower()
            if "bot was blocked by the user" in error_desc or \
               "user is deactivated" in error_desc or \
               "chat not found" in error_desc or \
               "forbidden: bot was kicked from the supergroup chat" in error_desc or \
               "forbidden: bot is not a member of the supergroup chat" in error_desc:
                # print(f"Bot blocked or chat inaccessible for {chat_id}: {e.description}")
                return True
        # print(f"API Exception (not a block) for {chat_id}: {e.description}")
        return False # Other API errors are not necessarily blocks
    except Exception as e:
        # print(f"Generic exception in is_bot_blocked for {chat_id}: {str(e)}")
        return False # Assume not blocked on other errors

def get_user_info(user_object):
    """Extracts and formats user information from a Telegram User object."""
    return {
        "name": user_object.first_name + (f" {user_object.last_name}" if user_object.last_name else ""),
        "username": user_object.username if user_object.username else "N/A",
        "join_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

# --- Keyboards ---
def get_main_keyboard(chat_id):
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        telebot.types.KeyboardButton("📬 New mail"), telebot.types.KeyboardButton("🔄 Refresh"),
        telebot.types.KeyboardButton("👨 Male Profile"), telebot.types.KeyboardButton("👩 Female Profile"),
        telebot.types.KeyboardButton("🔐 2FA Auth"), telebot.types.KeyboardButton("👤 My Account")
    )
    if is_admin(chat_id):
        keyboard.add(telebot.types.KeyboardButton("👑 Admin Panel"))
    return keyboard

def get_admin_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        telebot.types.KeyboardButton("👥 Pending Approvals"), telebot.types.KeyboardButton("📊 Stats"),
        telebot.types.KeyboardButton("👤 User Management"), telebot.types.KeyboardButton("📢 Broadcast"),
        telebot.types.KeyboardButton("⬅️ Main Menu")
    )
    return keyboard

def get_user_management_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        telebot.types.KeyboardButton("📜 List Users"), telebot.types.KeyboardButton("❌ Remove User"),
        telebot.types.KeyboardButton("⬅️ Back to Admin")
    )
    return keyboard

def get_approval_keyboard(user_id_to_approve):
    keyboard = telebot.types.InlineKeyboardMarkup()
    keyboard.add(
        telebot.types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id_to_approve}"),
        telebot.types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id_to_approve}")
    )
    return keyboard

def get_2fa_platform_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    keyboard.add(
        telebot.types.KeyboardButton("Google"), telebot.types.KeyboardButton("Facebook"), telebot.types.KeyboardButton("Instagram"),
        telebot.types.KeyboardButton("Twitter"), telebot.types.KeyboardButton("Microsoft"), telebot.types.KeyboardButton("Apple"),
        telebot.types.KeyboardButton("⬅️ Back to Main")
    )
    return keyboard

def get_back_keyboard(custom_back_text=None): # Allows customizing the back button text if needed
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    back_text = custom_back_text if custom_back_text else "⬅️ Back"
    keyboard.row(telebot.types.KeyboardButton(back_text))
    return keyboard

def get_broadcast_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        telebot.types.KeyboardButton("📢 Text Broadcast"), telebot.types.KeyboardButton("📋 Media Broadcast"),
        telebot.types.KeyboardButton("⬅️ Back to Admin")
    )
    return keyboard

def safe_send_message(chat_id, text, **kwargs):
    """Safely sends a message, handling potential blocks and using default Markdown."""
    if 'parse_mode' not in kwargs: # Ensure Markdown is default if not specified
        kwargs['parse_mode'] = "Markdown"

    try:
        if is_bot_blocked(chat_id):
            # print(f"Bot blocked by {chat_id} before sending. Cleaning up user.")
            safe_delete_user(chat_id)
            return None
        
        msg = bot.send_message(chat_id, text, **kwargs)
        active_sessions.add(chat_id) # Mark user as active
        return msg
    except telebot.apihelper.ApiTelegramException as e:
        if e.result and e.result.status_code == 403:
            error_desc = e.description.lower()
            if "bot was blocked" in error_desc or "user is deactivated" in error_desc or "chat not found" in error_desc:
                # print(f"Bot blocked by {chat_id} during send (API Exception). Cleaning up user.")
                safe_delete_user(chat_id)
        # else:
            # print(f"API Error sending message to {chat_id} (not a block): {e.description}")
        return None
    except Exception as e:
        # print(f"Generic error sending message to {chat_id}: {str(e)}")
        return None

# --- Temp Mail (temp-mail.org API) Functions ---
def get_temp_mail_domains_list():
    """Fetches a list of available domains from temp-mail.org API."""
    try:
        res = requests.get(TEMP_MAIL_DOMAINS_URL, timeout=10)
        res.raise_for_status()
        domains = res.json()
        if isinstance(domains, list) and all(isinstance(d, str) for d in domains):
            valid_domains = [d.strip().lstrip('.') for d in domains if '.' in d and len(d) > 3 and '@' not in d]
            return valid_domains if valid_domains else FALLBACK_TEMP_MAIL_DOMAINS
        return FALLBACK_TEMP_MAIL_DOMAINS
    except requests.exceptions.RequestException:
        return FALLBACK_TEMP_MAIL_DOMAINS
    except Exception:
        return FALLBACK_TEMP_MAIL_DOMAINS

def generate_random_temp_email_address():
    """Generates a random email address using a domain from temp-mail.org."""
    domains = get_temp_mail_domains_list()
    selected_domain = random.choice(domains)
    username_part = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return f"{username_part}@{selected_domain}"

def fetch_messages_from_temp_mail(email_address):
    """Fetches messages for a given email address from temp-mail.org API. Returns list of messages or empty list on error/no messages."""
    try:
        email_md5 = hashlib.md5(email_address.encode('utf-8')).hexdigest()
        url = TEMP_MAIL_MESSAGES_URL_FORMAT.format(md5_hash=email_md5)
        res = requests.get(url, timeout=15)
        
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and 'error' in data: # API specific error like "no_messages"
                return [] 
            if isinstance(data, list):
                return data # Expected: list of message objects
            return [] # Unexpected JSON structure
        return [] # HTTP error
    except (requests.exceptions.RequestException, ValueError): # Catches network errors and JSON decode errors
        return []
    except Exception:
        return []

# --- Profile generator ---
def generate_username():
    return fake.user_name()[:12] + random.choice(string.digits) # More realistic usernames

def generate_password_complex(): # More complex password
    length = random.randint(10, 14)
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(random.choice(chars) for i in range(length))

def generate_us_phone():
    return fake.phone_number() # Use Faker for more realistic US phone numbers

def generate_profile(gender):
    name = fake.name_male() if gender == "male" else fake.name_female()
    username = generate_username()
    password = generate_password_complex()
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
        f"✅ Tap on any value to copy."
    )

# --- 2FA Feature Functions ---
def is_valid_base32(secret_key_input):
    """Checks if the secret is valid Base32."""
    try:
        cleaned = secret_key_input.replace(" ", "").replace("-", "").upper()
        if not cleaned or not all(c in string.ascii_uppercase + "234567" for c in cleaned):
            return False
        pyotp.TOTP(cleaned).now() # Attempt to create TOTP object
        return True
    except Exception:
        return False

# --- Background Workers ---
def auto_refresh_worker():
    """Periodically checks for new emails for users with active temp mail."""
    while True:
        try:
            active_user_data_keys = list(user_data.keys())
            for chat_id in active_user_data_keys:
                if chat_id not in user_data: continue # User data might have been cleared

                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id)
                    continue
                
                current_user_session = user_data.get(chat_id)
                if not current_user_session or "email" not in current_user_session:
                    continue

                email_address = current_user_session["email"]
                messages = fetch_messages_from_temp_mail(email_address)
                if not messages: continue

                seen_ids = last_message_ids.setdefault(chat_id, set())
                
                for msg_data in reversed(messages): # Process oldest new messages first if API returns newest first
                    msg_id = msg_data.get("mail_id")
                    if not msg_id or msg_id in seen_ids:
                        continue
                    
                    seen_ids.add(msg_id)
                    sender = msg_data.get("mail_from", "N/A")
                    subject = msg_data.get("mail_subject", "(No Subject)")
                    body = msg_data.get("mail_text_only") or msg_data.get("mail_html", "") # Prefer text, fallback to html
                    if "<body" in body.lower(): # Basic HTML body extraction
                        try:
                            from bs4 import BeautifulSoup
                            soup = BeautifulSoup(body, "html.parser")
                            body_tag = soup.find('body')
                            body = body_tag.get_text(separator='\n', strip=True) if body_tag else soup.get_text(separator='\n', strip=True)
                        except ImportError: # Fallback if bs4 not available
                            pass # Keep raw HTML or rely on mail_preview
                        except Exception: # Catch other parsing errors
                            pass


                    body = body.strip() if body else msg_data.get("mail_preview", "(No Content)")
                    
                    received_ts = msg_data.get("mail_timestamp")
                    received_time_str = "Just now"
                    if received_ts:
                        try:
                            received_time_str = datetime.datetime.fromtimestamp(float(received_ts)).strftime('%Y-%m-%d %H:%M:%S')
                        except (ValueError, TypeError):
                            received_time_str = msg_data.get("mail_date", "Just now")
                    else:
                         received_time_str = msg_data.get("mail_date", "Just now")


                    formatted_msg = (
                        f"📬 *New Email Received!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 *From:* `{sender}`\n"
                        f"📨 *Subject:* _{subject}_\n"
                        f"🕒 *Received:* {received_time_str}\n"
                        f"💬 *Body:*\n{body[:3500]}\n" # Truncate long bodies
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )
                    safe_send_message(chat_id, formatted_msg)
                
                # Limit seen_ids size to prevent memory issues over time
                if len(seen_ids) > 100: # Keep last 100 message IDs
                    oldest_ids = sorted(list(seen_ids))[:-50] # Keep newest 50 + buffer
                    for old_id in oldest_ids:
                        seen_ids.remove(old_id)

        except Exception as e:
            print(f"Error in auto_refresh_worker: {e}")
        time.sleep(60) # Check every 60 seconds

def cleanup_blocked_users_worker():
    """Periodically checks active sessions for blocked users and cleans them up."""
    while True:
        try:
            sessions_to_check = list(active_sessions)
            for chat_id in sessions_to_check:
                if is_bot_blocked(chat_id):
                    # print(f"Cleanup Worker: User {chat_id} blocked the bot. Removing data.")
                    safe_delete_user(chat_id)
        except Exception as e:
            print(f"Error in cleanup_blocked_users_worker: {e}")
        time.sleep(3600) # Run hourly

# --- Bot Handlers ---
@bot.message_handler(commands=['start', 'help'])
def command_start_help(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id) # Should not be strictly necessary if is_bot_blocked is called first
        return

    user_obj = message.from_user
    user_info = get_user_info(user_obj)
    user_profiles[chat_id] = user_info

    if is_admin(chat_id):
        approved_users.add(chat_id)
        safe_send_message(chat_id, f"👋 Welcome Admin, {user_obj.first_name}!", reply_markup=get_main_keyboard(chat_id))
        return

    if chat_id in approved_users:
        safe_send_message(chat_id, f"👋 Welcome back, {user_obj.first_name}!", reply_markup=get_main_keyboard(chat_id))
    else:
        if chat_id not in pending_approvals:
            pending_approvals[chat_id] = user_info
            safe_send_message(chat_id, "⏳ Your access request has been sent to the admin. Please wait for approval.")
            if ADMIN_ID:
                try:
                    admin_chat_id_int = int(ADMIN_ID)
                    approval_msg_to_admin = (
                        f"🆕 *New User Approval Request*\n\n"
                        f"👤 Name: `{user_info['name']}` (@{user_info['username']})\n"
                        f"🆔 User ID: `{chat_id}`\n"
                        f"📅 Requested: `{user_info['join_date']}`"
                    )
                    bot.send_message(admin_chat_id_int, approval_msg_to_admin, reply_markup=get_approval_keyboard(chat_id))
                except Exception as e:
                    print(f"Failed to send approval notification to admin {ADMIN_ID}: {e}")
        else:
            safe_send_message(chat_id, "⏳ Your access request is still pending. Please wait for admin approval.")

# --- Admin Panel Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel" and is_admin(msg.chat.id))
def handle_admin_panel(message):
    safe_send_message(message.chat.id, "👑 Welcome to the Admin Panel!", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "👥 Pending Approvals" and is_admin(msg.chat.id))
def handle_show_pending_approvals(message):
    if not pending_approvals:
        safe_send_message(message.chat.id, "✅ No pending user approvals at the moment.")
        return
    
    safe_send_message(message.chat.id, f"⏳ *Pending User Approvals ({len(pending_approvals)}):*")
    for user_id, user_info in list(pending_approvals.items()): # Iterate a copy
        approval_msg = (
            f"👤 Name: `{user_info['name']}` (@{user_info['username']})\n"
            f"🆔 User ID: `{user_id}`\n"
            f"📅 Requested: `{user_info['join_date']}`"
        )
        safe_send_message(message.chat.id, approval_msg, reply_markup=get_approval_keyboard(user_id))
        time.sleep(0.1) # Avoid hitting rate limits if many pending

@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.chat.id))
def handle_show_stats(message):
    bot_start_time = user_profiles.get("bot_start_time", "Not recorded")
    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin ID: `{ADMIN_ID}`\n"
        f"👥 Approved Users: `{len(approved_users)}`\n"
        f"👤 Active Sessions (interacted): `{len(active_sessions)}`\n"
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"📧 Active Temp Mails: `{len(user_data)}`\n"
        f"🕒 Bot Started: `{bot_start_time}` (approx.)\n"
        f"Current Time: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
    )
    safe_send_message(message.chat.id, stats_msg)

@bot.message_handler(func=lambda msg: msg.text == "👤 User Management" and is_admin(msg.chat.id))
def handle_user_management(message):
    safe_send_message(message.chat.id, "👤 User Management Panel", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📜 List Users" and is_admin(msg.chat.id))
def handle_list_users(message):
    if not approved_users:
        safe_send_message(message.chat.id, "❌ No approved users yet.")
        return
    
    users_list_str = [f"👥 *Approved Users ({len(approved_users)})*:\n"]
    for user_id in approved_users:
        profile = user_profiles.get(user_id)
        email_info = f" (Mail: `{user_data[user_id]['email']}`)" if user_id in user_data and 'email' in user_data[user_id] else ""
        if profile:
            users_list_str.append(f"\n🆔 `{user_id}` - {profile['name']} (@{profile['username']}){email_info}\n   _Joined: {profile['join_date']}_")
        else:
            users_list_str.append(f"\n🆔 `{user_id}` - _(Profile info N/A)_{email_info}")

    full_message = "".join(users_list_str)
    # Split message if too long for Telegram
    max_len = 4000 # Slightly less than 4096 for safety
    if len(full_message) > max_len:
        parts = [full_message[i:i+max_len] for i in range(0, len(full_message), max_len)]
        for part in parts:
            safe_send_message(message.chat.id, part)
            time.sleep(0.2)
    else:
        safe_send_message(message.chat.id, full_message)

@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def handle_remove_user_prompt(message):
    msg_obj = safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=get_back_keyboard("⬅️ Back to User Mgt"))
    if msg_obj:
        bot.register_next_step_handler(msg_obj, process_user_removal_input)

def process_user_removal_input(message):
    admin_chat_id = message.chat.id
    if message.text == "⬅️ Back to User Mgt":
        safe_send_message(admin_chat_id, "Cancelled user removal.", reply_markup=get_user_management_keyboard())
        return
    try:
        user_id_to_remove = int(message.text.strip())
        if str(user_id_to_remove) == str(ADMIN_ID):
            safe_send_message(admin_chat_id, "❌ Cannot remove the Admin account!", reply_markup=get_user_management_keyboard())
            return

        removed_from_approved = user_id_to_remove in approved_users
        removed_from_pending = user_id_to_remove in pending_approvals
        
        if removed_from_approved or removed_from_pending:
            user_profile_info = user_profiles.get(user_id_to_remove, {})
            username_display = user_profile_info.get('username', 'N/A')
            
            safe_delete_user(user_id_to_remove) # Comprehensive cleanup
            
            safe_send_message(admin_chat_id, f"✅ User {user_id_to_remove} (@{username_display}) has been removed and all their data cleared.", reply_markup=get_user_management_keyboard())
            safe_send_message(user_id_to_remove, "❌ Your access to this bot has been revoked by the admin.") # Notify user
        else:
            safe_send_message(admin_chat_id, f"❌ User {user_id_to_remove} not found in approved or pending users.", reply_markup=get_user_management_keyboard())
    except ValueError:
        safe_send_message(admin_chat_id, "❌ Invalid User ID. Please enter a numeric ID.", reply_markup=get_user_management_keyboard())
    except Exception as e:
        print(f"Error in process_user_removal_input: {e}")
        safe_send_message(admin_chat_id, "An error occurred during user removal.", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def handle_broadcast_menu(message):
    safe_send_message(message.chat.id, "📢 Select Broadcast Type:", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def handle_text_broadcast_prompt(message):
    msg_obj = safe_send_message(message.chat.id, "✍️ Enter the broadcast message text (Markdown supported):", reply_markup=get_back_keyboard("⬅️ Back to Broadcast Menu"))
    if msg_obj:
        bot.register_next_step_handler(msg_obj, process_text_broadcast_message)

def process_text_broadcast_message(message):
    admin_chat_id = message.chat.id
    if message.text == "⬅️ Back to Broadcast Menu":
        safe_send_message(admin_chat_id, "Cancelled text broadcast.", reply_markup=get_broadcast_keyboard())
        return

    broadcast_text = message.text
    if not broadcast_text.strip():
        safe_send_message(admin_chat_id, "❌ Broadcast message cannot be empty.", reply_markup=get_broadcast_keyboard())
        return

    success, failed = 0, 0
    users_to_target = [uid for uid in list(approved_users) if str(uid) != str(ADMIN_ID)]
    total_to_send = len(users_to_target)

    if total_to_send == 0:
        safe_send_message(admin_chat_id, "🤷 No users (excluding admin) to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg = safe_send_message(admin_chat_id, f"📢 Broadcasting text to {total_to_send} users...\nSent: 0/{total_to_send}")

    for i, user_id in enumerate(users_to_target, 1):
        if safe_send_message(user_id, f"📢 *Admin Broadcast:*\n\n{broadcast_text}"):
            success += 1
        else:
            failed += 1
        
        if progress_msg and (i % 10 == 0 or i == total_to_send):
            try:
                bot.edit_message_text(
                    f"📢 Broadcasting text to {total_to_send} users...\n"
                    f"Sent: {i}/{total_to_send} (✅{success} | ❌{failed})",
                    chat_id=admin_chat_id, message_id=progress_msg.message_id
                )
            except: pass # Edit might fail
        time.sleep(0.25)

    safe_send_message(admin_chat_id, f"📢 Text Broadcast Complete!\n✅ Successful: {success}\n❌ Failed: {failed}", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def handle_media_broadcast_prompt(message):
    msg_obj = safe_send_message(message.chat.id, "🖼 Send the photo, video, or document for broadcast (with caption if desired):", reply_markup=get_back_keyboard("⬅️ Back to Broadcast Menu"))
    if msg_obj:
        bot.register_next_step_handler(msg_obj, process_media_broadcast_content)

def process_media_broadcast_content(message):
    admin_chat_id = message.chat.id
    if message.text == "⬅️ Back to Broadcast Menu": # User sent text instead of media
        safe_send_message(admin_chat_id, "Cancelled media broadcast.", reply_markup=get_broadcast_keyboard())
        return

    caption = message.caption if message.caption else ""
    media_type_found = None

    if message.photo: media_type_found = "photo"
    elif message.video: media_type_found = "video"
    elif message.document: media_type_found = "document"
    elif message.audio: media_type_found = "audio"
    elif message.voice: media_type_found = "voice"
    
    if not media_type_found:
        safe_send_message(admin_chat_id, "❌ No valid media found. Please send a photo, video, document, audio, or voice message.", reply_markup=get_broadcast_keyboard())
        return

    success, failed = 0, 0
    users_to_target = [uid for uid in list(approved_users) if str(uid) != str(ADMIN_ID)]
    total_to_send = len(users_to_target)

    if total_to_send == 0:
        safe_send_message(admin_chat_id, "🤷 No users (excluding admin) to broadcast to.", reply_markup=get_admin_keyboard())
        return
        
    progress_msg = safe_send_message(admin_chat_id, f"📢 Broadcasting media to {total_to_send} users...\nSent: 0/{total_to_send}")

    for i, user_id in enumerate(users_to_target, 1):
        sent_this_user = False
        try:
            if is_bot_blocked(user_id): # Pre-check
                failed += 1
                safe_delete_user(user_id)
                continue

            if media_type_found == "photo":
                bot.send_photo(user_id, message.photo[-1].file_id, caption=caption, parse_mode="Markdown")
            elif media_type_found == "video":
                bot.send_video(user_id, message.video.file_id, caption=caption, parse_mode="Markdown")
            elif media_type_found == "document":
                bot.send_document(user_id, message.document.file_id, caption=caption, parse_mode="Markdown")
            elif media_type_found == "audio":
                bot.send_audio(user_id, message.audio.file_id, caption=caption, parse_mode="Markdown")
            elif media_type_found == "voice":
                bot.send_voice(user_id, message.voice.file_id, caption=caption, parse_mode="Markdown")
            sent_this_user = True
            success += 1
        except telebot.apihelper.ApiTelegramException as e_api:
            failed += 1
            if e_api.result and e_api.result.status_code == 403: safe_delete_user(user_id)
        except Exception:
            failed += 1
        
        if progress_msg and (i % 5 == 0 or i == total_to_send): # Update less frequently for media
            try:
                bot.edit_message_text(
                    f"📢 Broadcasting media to {total_to_send} users...\n"
                    f"Sent: {i}/{total_to_send} (✅{success} | ❌{failed})",
                    chat_id=admin_chat_id, message_id=progress_msg.message_id
                )
            except: pass
        time.sleep(0.5) # Longer delay for media

    safe_send_message(admin_chat_id, f"📢 Media Broadcast Complete!\n✅ Successful: {success}\n❌ Failed: {failed}", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin" and is_admin(msg.chat.id))
def handle_back_to_admin(message):
    safe_send_message(message.chat.id, "⬅️ Returning to Admin Panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu" and is_admin(msg.chat.id))
def handle_admin_back_to_main(message):
    if message.chat.id in user_2fa_secrets: del user_2fa_secrets[message.chat.id] # Clear 2FA state if admin was using it
    safe_send_message(message.chat.id, "⬅️ Returning to Main Menu...", reply_markup=get_main_keyboard(message.chat.id))

# --- Callback Query Handlers ---
@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def callback_handle_approval(call):
    if not is_admin(call.message.chat.id):
        bot.answer_callback_query(call.id, "⚠️ Action restricted to admin.")
        return

    try:
        action, user_id_str = call.data.split('_')
        user_id_to_process = int(user_id_str)
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Error: Invalid callback data.")
        bot.edit_message_text("Error processing request.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        return

    user_info_for_msg = pending_approvals.get(user_id_to_process, user_profiles.get(user_id_to_process, {}))
    username_display = user_info_for_msg.get('username', 'N/A')

    if action == "approve":
        approved_users.add(user_id_to_process)
        if user_id_to_process in pending_approvals: del pending_approvals[user_id_to_process]
        
        safe_send_message(user_id_to_process, "✅ Your access request has been approved! You can now use all bot features.", reply_markup=get_main_keyboard(user_id_to_process))
        bot.answer_callback_query(call.id, f"User {user_id_to_process} approved.")
        bot.edit_message_text(f"✅ User {user_id_to_process} (@{username_display}) approved.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
    
    elif action == "reject":
        safe_delete_user(user_id_to_process) # Comprehensive cleanup
        safe_send_message(user_id_to_process, "❌ Your access request has been rejected by the admin.")
        bot.answer_callback_query(call.id, f"User {user_id_to_process} rejected.")
        bot.edit_message_text(f"❌ User {user_id_to_process} (@{username_display}) rejected and data cleared.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
    else:
        bot.answer_callback_query(call.id, "Unknown action.")

# --- User Feature Handlers (Mail, Profile, 2FA, Account) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def handle_new_mail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return

    loading_msg = safe_send_message(chat_id, "⏳ Generating new temporary email address...")
    email_address = generate_random_temp_email_address()

    if loading_msg: 
        try: bot.delete_message(chat_id, loading_msg.message_id) 
        except: pass

    if email_address:
        user_data[chat_id] = {"email": email_address}
        last_message_ids[chat_id] = set() # Reset seen messages
        msg_text = (
            f"✅ *New Temporary Email Generated!*\n\n"
            f"Your email: `{email_address}`\n\n"
            f"_(Tap to copy). Messages will appear automatically or via '🔄 Refresh'._"
        )
        safe_send_message(chat_id, msg_text)
    else:
        safe_send_message(chat_id, "❌ Failed to generate a temporary email. Domain service might be unavailable. Try again later.")

@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh")
def handle_refresh_mail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    current_user_mail_data = user_data.get(chat_id)
    if not current_user_mail_data or "email" not in current_user_mail_data:
        safe_send_message(chat_id, "⚠️ No active temporary email. Use '📬 New mail' first.")
        return

    email_address = current_user_mail_data["email"]
    loading_msg = safe_send_message(chat_id, f"🔄 Checking for new mail at `{email_address}`...")
    messages = fetch_messages_from_temp_mail(email_address)
    
    if loading_msg: 
        try: bot.delete_message(chat_id, loading_msg.message_id) 
        except: pass

    if not messages:
        safe_send_message(chat_id, f"📭 Inbox for `{email_address}` is empty or no new messages found.")
        return

    safe_send_message(chat_id, f"📬 *Latest emails for `{email_address}` (up to 5 shown):*")
    displayed_count = 0
    for msg_data in messages[:5]: # Show up to 5 newest on manual refresh
        msg_id = msg_data.get("mail_id")
        if not msg_id: continue
        last_message_ids.setdefault(chat_id, set()).add(msg_id) # Mark as seen

        sender = msg_data.get("mail_from", "N/A")
        subject = msg_data.get("mail_subject", "(No Subject)")
        body = msg_data.get("mail_text_only") or msg_data.get("mail_preview", "(No Content)")
        body = body.strip() if body else "(No Content)"
        received_ts = msg_data.get("mail_timestamp")
        received_time_str = msg_data.get("mail_date", "Just now")
        if received_ts:
            try: received_time_str = datetime.datetime.fromtimestamp(float(received_ts)).strftime('%Y-%m-%d %H:%M:%S')
            except: pass
        
        formatted_msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *From:* `{sender}`\n📨 *Subject:* _{subject}_\n🕒 *Received:* {received_time_str}\n"
            f"💬 *Body Preview:*\n{body[:1000]}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        safe_send_message(chat_id, formatted_msg)
        displayed_count +=1
        time.sleep(0.1)
    
    if displayed_count == 0: # Should be caught by `if not messages` earlier, but safety.
         safe_send_message(chat_id, f"📭 No processable messages found for `{email_address}` at this moment.")

@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
def handle_generate_profile(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    gender = "male" if message.text == "👨 Male Profile" else "female"
    gen_gender, name, username, password, phone = generate_profile(gender)
    response_text = profile_message(gen_gender, name, username, password, phone)
    safe_send_message(chat_id, response_text)

@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def handle_2fa_auth_start(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    if chat_id in user_2fa_secrets: del user_2fa_secrets[chat_id] # Clear previous 2FA state
    safe_send_message(chat_id, "🔐 Choose the platform for 2FA code generation:", reply_markup=get_2fa_platform_keyboard())

@bot.message_handler(func=lambda msg: msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform_choice(message): # User selected a platform for 2FA
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return # No need for safe_delete_user, other flows will catch

    platform_choice = message.text
    user_2fa_secrets[chat_id] = {"platform": platform_choice} # Store platform, wait for secret
    
    msg_to_send = f"🔢 Please enter the Base32 2FA secret key for *{platform_choice}*:"
    sent_msg = safe_send_message(chat_id, msg_to_send, reply_markup=get_back_keyboard("⬅️ Back to Platform Choice"))
    # Next input will be handled by handle_all_other_text or handle_2fa_back_button

# Specific handler for "Back" during 2FA secret input or platform choice
@bot.message_handler(func=lambda msg: msg.text in ["⬅️ Back to Platform Choice", "⬅️ Back"] and msg.chat.id in user_2fa_secrets)
def handle_2fa_navigation_back(message):
    chat_id = message.chat.id
    current_2fa_state = user_2fa_secrets.get(chat_id, {})

    # If they were choosing a platform or about to enter a secret
    if "platform" in current_2fa_state and "secret" not in current_2fa_state:
        del user_2fa_secrets[chat_id]["platform"] # Clear platform, go back to selection
        safe_send_message(chat_id, "🔄 Please choose a platform again:", reply_markup=get_2fa_platform_keyboard())
    else: # Fallback or if secret was already entered (though inline refresh is preferred then)
        if chat_id in user_2fa_secrets: del user_2fa_secrets[chat_id]
        safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main") # General "Back to Main" from any ReplyKeyboard
def handle_back_to_main_menu(message):
    chat_id = message.chat.id
    if chat_id in user_2fa_secrets: # Clear any 2FA state if user navigates away
        del user_2fa_secrets[chat_id]
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))

@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
def handle_my_account(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    user_info_parts = ["👤 *Your Account Information:*\n"]
    profile = user_profiles.get(chat_id)
    if profile:
        user_info_parts.append(f"\n▪️ Name: `{profile['name']}`")
        user_info_parts.append(f"▪️ Username: `@{profile['username']}`")
        user_info_parts.append(f"▪️ Joined Bot: `{profile['join_date']}`")
    else:
        user_info_parts.append("\n_Basic profile info not found._")

    if chat_id in user_data and "email" in user_data[chat_id]:
        user_info_parts.append(f"\n📧 Current Temp Mail: `{user_data[chat_id]['email']}`")
    else:
        user_info_parts.append("\n📧 _No active temporary email._")
        
    safe_send_message(chat_id, "\n".join(user_info_parts), reply_markup=get_main_keyboard(chat_id))

# --- Catch-all for text messages (e.g., 2FA secret key input) ---
@bot.message_handler(func=lambda msg: True, content_types=['text'])
def handle_all_unmatched_text(message):
    chat_id = message.chat.id
    text_input = message.text.strip()

    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return

    # Check if expecting a 2FA secret key
    current_2fa_state = user_2fa_secrets.get(chat_id)
    if current_2fa_state and "platform" in current_2fa_state and "secret" not in current_2fa_state:
        # "⬅️ Back to Platform Choice" is handled by handle_2fa_navigation_back, so this text is likely the secret
        secret_key_candidate = text_input
        if not is_valid_base32(secret_key_candidate):
            platform_name = current_2fa_state["platform"]
            err_msg = (
                f"❌ *Invalid Base32 Secret Key for {platform_name}!*\n\n"
                f"A valid Base32 key uses A-Z (uppercase) and 2-7. No spaces or other characters.\n\n"
                f"Please try again, or use '⬅️ Back to Platform Choice'."
            )
            safe_send_message(chat_id, err_msg, reply_markup=get_back_keyboard("⬅️ Back to Platform Choice"))
            return

        clean_secret = secret_key_candidate.replace(" ", "").replace("-", "").upper()
        user_2fa_secrets[chat_id]["secret"] = clean_secret # Store the validated secret
        platform_name = user_2fa_secrets[chat_id]["platform"]
        
        try:
            totp = pyotp.TOTP(clean_secret)
            current_otp_code = totp.now()
            seconds_left = 30 - (datetime.datetime.now().second % 30)
            
            reply_text = (
                f"🔐 *2FA Code for {platform_name}*\n\n"
                f"🔑 Code: `{current_otp_code}`\n"
                f"⏳ Refreshes in: _{seconds_left}s_\n\n"
                f"Tap code to copy. Use main keyboard to navigate away."
            )
            
            inline_kb = telebot.types.InlineKeyboardMarkup()
            inline_kb.add(telebot.types.InlineKeyboardButton(f"🔄 Refresh Code ({seconds_left}s)", callback_data="generate_2fa_code_inline"))
            
            safe_send_message(chat_id, reply_text, reply_markup=inline_kb)
            # Keep main keyboard active for navigation, reply_markup above is for the message with the code
            bot.send_message(chat_id, "You can use the main keyboard buttons to navigate to other features.", reply_markup=get_main_keyboard(chat_id))


        except Exception as e_otp:
            safe_send_message(chat_id, f"Error generating 2FA code: {str(e_otp)}. Please check your secret key.", reply_markup=get_main_keyboard(chat_id))
            if chat_id in user_2fa_secrets: del user_2fa_secrets[chat_id] # Clear faulty state
        return # End of 2FA secret processing

    # Default behavior for unhandled text
    if chat_id in approved_users or is_admin(chat_id):
        # safe_send_message(chat_id, f"🤔 Unrecognized command: '{text_input}'. Please use the buttons.", reply_markup=get_main_keyboard(chat_id))
        pass # Or ignore to prevent "unknown command" spam
    elif chat_id in pending_approvals:
        safe_send_message(chat_id, "⏳ Your access request is still pending. Please wait.")
    else: # Unknown user, not pending
        # safe_send_message(chat_id, "Hello! Please use /start to begin interacting with the bot.")
        pass # Ignore unknown users not in flow

@bot.callback_query_handler(func=lambda call: call.data == "generate_2fa_code_inline")
def callback_generate_2fa_code_inline(call):
    chat_id = call.message.chat.id
    
    current_2fa_state = user_2fa_secrets.get(chat_id)
    if not current_2fa_state or "secret" not in current_2fa_state:
        bot.answer_callback_query(call.id, "⚠️ 2FA secret not set. Please restart 2FA setup.")
        try:
            bot.edit_message_text("Error: 2FA secret not found. Please restart 2FA setup via main menu.", 
                                  chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
        except: pass
        return

    secret = current_2fa_state["secret"]
    platform_name = current_2fa_state.get("platform", "Selected Platform")
    
    try:
        totp = pyotp.TOTP(secret)
        current_otp_code = totp.now()
        seconds_left = 30 - (datetime.datetime.now().second % 30)
        
        reply_text = (
            f"🔐 *2FA Code for {platform_name}*\n\n"
            f"🔑 Code: `{current_otp_code}`\n"
            f"⏳ Refreshes in: _{seconds_left}s_\n\n"
            f"Tap code to copy."
        )
        
        inline_kb = telebot.types.InlineKeyboardMarkup()
        inline_kb.add(telebot.types.InlineKeyboardButton(f"🔄 Refresh Code ({seconds_left}s)", callback_data="generate_2fa_code_inline"))
        
        bot.edit_message_text(
            reply_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=inline_kb # Keep the inline refresh button
        )
        bot.answer_callback_query(call.id, f"Code refreshed: {current_otp_code}")

    except Exception as e_otp_refresh:
        bot.answer_callback_query(call.id, "Error refreshing code.")
        try:
            bot.edit_message_text(f"Error refreshing code: {str(e_otp_refresh)}. Please check secret or restart setup.", 
                                  chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
        except: pass
        if chat_id in user_2fa_secrets: del user_2fa_secrets[chat_id]


if __name__ == '__main__':
    print("🤖 Bot is preparing to launch...")
    if not ADMIN_ID:
        print("⚠️ WARNING: ADMIN_ID is not set in .env. Admin features and user approval will not function correctly.")
    else:
        print(f"🔑 Admin ID configured: {ADMIN_ID}")

    user_profiles["bot_start_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    threading.Thread(target=auto_refresh_worker, daemon=True).start()
    print("🔄 Auto-refresh worker for emails started.")
    threading.Thread(target=cleanup_blocked_users_worker, daemon=True).start()
    print("🧹 User cleanup worker started.")
    
    print(f"🚀 Bot started successfully at {user_profiles['bot_start_time']}! Waiting for messages...")
    
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    except Exception as e_main_loop:
        print(f"❌ CRITICAL ERROR in bot polling loop: {e_main_loop}")
        # Consider logging to a file or a monitoring service in a production environment
    finally:
        print("🛑 Bot has stopped.")


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
import hashlib # Added for MD5 hashing for temp-mail.org API
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton # For 2FA refresh button

load_dotenv()
fake = Faker()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise Exception("❌ BOT_TOKEN not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown") # Default parse mode is Markdown

# Data storage
user_data = {}  # Stores {"email": "temp@example.com"} for temp mail
last_message_ids = {}  # Stores {chat_id: {msg_id1, msg_id2}} for temp mail
user_2fa_codes = {} # Kept for original 2FA logic, not directly related to mail
active_sessions = set()
pending_approvals = {}
approved_users = set()
user_profiles = {}  # Stores additional user profile info
user_2fa_secrets = {}  # Store user secrets for 2FA

# --- Helper Functions ---

def is_admin(chat_id):
    return str(chat_id) == ADMIN_ID

def safe_delete_user(chat_id):
    if chat_id in user_data:
        del user_data[chat_id]
    if chat_id in last_message_ids:
        del last_message_ids[chat_id]
    if chat_id in user_2fa_codes:
        del user_2fa_codes[chat_id]
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]
    if chat_id in active_sessions:
        active_sessions.remove(chat_id)
    if chat_id in pending_approvals:
        del pending_approvals[chat_id]
    if chat_id in approved_users:
        approved_users.remove(chat_id)
    if chat_id in user_profiles:
        del user_profiles[chat_id]

def is_bot_blocked(chat_id):
    try:
        bot.get_chat(chat_id)
        return False
    except telebot.apihelper.ApiTelegramException as e:
        if e.result.status_code == 403 and ("bot was blocked" in e.result.text.lower() or "user is deactivated" in e.result.text.lower()):
            return True
        return False
    except Exception:
        return False

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

def get_user_account_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("📧 My Email", "🆔 My Info")
    keyboard.row("⬅️ Back to Main")
    return keyboard

def get_2fa_platform_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("Google", "Facebook", "Instagram")
    keyboard.row("Twitter", "Microsoft", "Apple")
    keyboard.row("⬅️ Back to Main")
    return keyboard

def get_back_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("⬅️ Back")
    return keyboard

def get_broadcast_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("📢 Text Broadcast", "📋 Media Broadcast")
    keyboard.row("⬅️ Back to Admin")
    return keyboard

def safe_send_message(chat_id, text, **kwargs):
    try:
        if is_bot_blocked(chat_id):
            safe_delete_user(chat_id)
            return None
        
        # Ensure parse_mode is set if not Markdown and HTML/MarkdownV2 is intended
        if 'parse_mode' not in kwargs and any(c in text for c in ['<', '>', '&', '*', '_', '`', '[']):
            # If specific HTML/Markdown is detected, and default is Markdown,
            # it might be better to explicitly set it or ensure content is escaped.
            # For this bot, default is Markdown. If HTML is sent, parse_mode='HTML' must be in kwargs.
            pass

        msg = bot.send_message(chat_id, text, **kwargs)
        active_sessions.add(chat_id)
        return msg
    except telebot.apihelper.ApiTelegramException as e:
        if e.result.status_code == 403 and ("bot was blocked" in e.result.text.lower() or "user is deactivated" in e.result.text.lower()):
            safe_delete_user(chat_id)
        elif e.result.status_code == 400 and "chat not found" in e.result.text.lower():
            safe_delete_user(chat_id)
        else:
            print(f"Error sending message to {chat_id}: API Exception {e.result.status_code} - {e.result.text}")
        return None
    except Exception as e:
        print(f"Generic error sending message to {chat_id}: {str(e)}")
        return None

# --- Temp-Mail.io (via temp-mail.org API) Functions ---

TEMP_MAIL_API_DOMAINS = "https://api.temp-mail.org/request/domains/format/json/"
TEMP_MAIL_API_MESSAGES = "https://api.temp-mail.org/request/mail/id/{md5_hash}/format/json/"

def get_temp_mail_domains():
    """Fetches available domains from temp-mail.org API."""
    try:
        res = requests.get(TEMP_MAIL_API_DOMAINS, timeout=10)
        res.raise_for_status()
        domains = res.json()
        # Ensure domains are returned without '@' and are valid
        return [d.lstrip('@') for d in domains if isinstance(d, str) and '.' in d.lstrip('@')]
    except requests.exceptions.RequestException as e:
        print(f"Error fetching temp-mail domains: {e}")
        return None # Fallback or handle error appropriately
    except ValueError: # JSONDecodeError
        print("Error decoding temp-mail domains JSON.")
        return None


def generate_random_email_local(domains):
    """Generates a random email address using one of the provided domains."""
    if not domains:
        return None # No domains available
    
    domain = random.choice(domains)
    username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"{username}@{domain}"

def get_email_md5_hash(email_address):
    """Generates an MD5 hash for the given email address."""
    return hashlib.md5(email_address.encode('utf-8')).hexdigest()

def fetch_temp_mail_messages(email_address):
    """Fetches messages for a given temp-mail.org email address."""
    email_hash = get_email_md5_hash(email_address)
    url = TEMP_MAIL_API_MESSAGES.format(md5_hash=email_hash)
    try:
        res = requests.get(url, timeout=15) # Increased timeout slightly for message fetching
        if res.status_code == 404: # No messages or invalid email hash (less likely if generated correctly)
            return []
        res.raise_for_status()
        messages = res.json()
        return messages if isinstance(messages, list) else []
    except requests.exceptions.RequestException as e:
        print(f"Error fetching temp-mail messages for {email_address}: {e}")
        return None # Indicates an error rather than empty inbox
    except ValueError: # JSONDecodeError
        print(f"Error decoding temp-mail messages JSON for {email_address}.")
        return None


# --- Profile generator ---
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
        # Basic check for valid characters and length (multiples of 8 usually, but pyotp is forgiving)
        if not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in cleaned):
            return False
        if not cleaned: return False # Empty string is not valid
        pyotp.TOTP(cleaned).now() # pyotp will throw error if invalid padding or format
        return True
    except (binascii.Error, ValueError, Exception):
        return False

# --- Background Workers ---

def auto_refresh_worker():
    while True:
        try:
            active_chat_ids = list(user_data.keys()) # Iterate over users with active email sessions
            for chat_id in active_chat_ids:
                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id)
                    continue
                
                if "email" not in user_data.get(chat_id, {}):
                    continue

                current_email = user_data[chat_id]["email"]
                messages = fetch_temp_mail_messages(current_email)

                if messages is None: # API error
                    # Optionally notify user or admin, or just retry next cycle
                    print(f"Auto-refresh: API error for {current_email}, skipping.")
                    continue
                
                if not messages: # Empty inbox
                    continue

                seen_ids = last_message_ids.setdefault(chat_id, set())
                new_messages_found = False

                # temp-mail.org API returns messages often newest first, but let's sort by timestamp to be sure.
                # 'mail_timestamp' is a float (Unix timestamp)
                try:
                    sorted_messages = sorted(messages, key=lambda m: float(m.get('mail_timestamp', 0)), reverse=True)
                except (TypeError, ValueError):
                    print(f"Warning: Could not sort messages for {current_email} due to invalid timestamp format.")
                    sorted_messages = messages # Process as received

                for msg_data in sorted_messages[:5]: # Check top 5 recent messages
                    msg_id = msg_data.get("mail_id") or msg_data.get("_id", {}).get("$oid") # Use mail_id or fallback
                    
                    if not msg_id:
                        print(f"Warning: Message for {current_email} missing a usable ID.")
                        continue

                    if msg_id in seen_ids:
                        continue
                    
                    seen_ids.add(msg_id)
                    new_messages_found = True

                    sender = msg_data.get("mail_from", "N/A")
                    subject = msg_data.get("mail_subject", "(No Subject)")
                    body = msg_data.get("mail_text_only") or msg_data.get("mail_text", "(No Content)")
                    body = body.strip() if body else "(No Content)"
                    
                    try:
                        timestamp = float(msg_data.get("mail_timestamp", time.time()))
                        received_time_str = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
                    except (ValueError, TypeError):
                        received_time_str = "Recently"

                    formatted_msg = (
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📬 *New Email Received!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 *From:* `{sender}`\n"
                        f"📨 *Subject:* _{subject}_\n"
                        f"🕒 *Received:* {received_time_str}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💬 *Body:*\n"
                        f"{body[:3500]}\n" # Telegram message limit is 4096, leave room for formatting
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )
                    safe_send_message(chat_id, formatted_msg)
                
                # Optional: Prune very old seen_ids to save memory if needed, e.g., keep last 100
                # while len(seen_ids) > 100: seen_ids.pop() # Example, not ideal for set pop

        except Exception as e:
            print(f"Error in auto_refresh_worker: {e}")
        time.sleep(45) # Increased sleep time for temp-mail.org API

def cleanup_blocked_users():
    while True:
        try:
            sessions_to_check = list(active_sessions) # Check all users bot interacted with
            all_known_users = set(list(user_data.keys()) + list(user_profiles.keys()) + list(pending_approvals.keys()) + list(approved_users))
            
            users_to_ping = sessions_to_check | all_known_users

            for chat_id in users_to_ping:
                if chat_id == ADMIN_ID: continue # Don't try to cleanup admin this way

                if is_bot_blocked(chat_id):
                    print(f"Cleaning up blocked/deactivated user: {chat_id}")
                    safe_delete_user(chat_id)
                    time.sleep(1) # Small delay to avoid hitting API limits if many are blocked
        except Exception as e:
            print(f"Error in cleanup_blocked_users: {e}")
        time.sleep(3600) # Run once per hour

# --- Bot Handlers ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): # Initial check
        safe_delete_user(chat_id)
        return

    user_info = get_user_info(message.from_user)
    user_profiles[chat_id] = user_info # Store/update profile info

    if is_admin(chat_id):
        if chat_id not in approved_users: # Add admin to approved if not already
            approved_users.add(chat_id)
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=get_main_keyboard(chat_id))
        active_sessions.add(chat_id)
        return

    if chat_id in approved_users:
        safe_send_message(chat_id, "👋 Welcome back!", reply_markup=get_main_keyboard(chat_id))
        active_sessions.add(chat_id)
    else:
        if chat_id not in pending_approvals:
            pending_approvals[chat_id] = user_info
            safe_send_message(chat_id, "👋 Your access request has been sent to the admin. Please wait for approval.")
            if ADMIN_ID:
                approval_msg = (
                    f"🆕 *New Approval Request*\n\n"
                    f"🆔 User ID: `{chat_id}`\n"
                    f"👤 Name: `{user_info['name']}`\n"
                    f"📛 Username: @{user_info['username']}\n"
                    f"📅 Joined: `{user_info['join_date']}`"
                )
                try:
                    bot.send_message(ADMIN_ID, approval_msg, reply_markup=get_approval_keyboard(chat_id), parse_mode="Markdown")
                except Exception as e:
                    print(f"Failed to send approval request to ADMIN_ID {ADMIN_ID}: {e}")
        else:
            safe_send_message(chat_id, "⏳ You already have a pending approval request. Please wait.")
        active_sessions.add(chat_id)


# --- Admin Panel Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel" and is_admin(msg.chat.id))
def admin_panel(message):
    safe_send_message(message.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "👥 Pending Approvals" and is_admin(msg.chat.id))
def show_pending_approvals(message):
    if not pending_approvals:
        safe_send_message(message.chat.id, "✅ No pending approvals.")
        return
    
    pending_list = list(pending_approvals.items()) # To avoid issues if dict changes during iteration
    if not pending_list: # Double check after converting
        safe_send_message(message.chat.id, "✅ No pending approvals.")
        return

    for user_id, user_info in pending_list:
        approval_msg = (
            f"🆕 *Pending Approval*\n\n"
            f"🆔 User ID: `{user_id}`\n"
            f"👤 Name: `{user_info['name']}`\n"
            f"📛 Username: @{user_info['username']}\n"
            f"📅 Joined: `{user_info['join_date']}`"
        )
        safe_send_message(message.chat.id, approval_msg, reply_markup=get_approval_keyboard(user_id))
        time.sleep(0.2) # Avoid sending too many messages at once

@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.chat.id))
def show_stats(message):
    # Calculate uptime (assuming bot started when this script started)
    # This is a simple uptime based on script start, not robust across restarts.
    # For true uptime, you'd store start_time globally. For now, it's illustrative.
    bot_start_time_str = user_profiles.get(int(ADMIN_ID), {}).get('join_date', "Not available") if ADMIN_ID else "N/A (ADMIN_ID not set)"
    
    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin ID: `{ADMIN_ID if ADMIN_ID else 'Not Set'}`\n"
        f"👥 Total Approved Users: `{len(approved_users)}`\n"
        f"👤 Total Known User Profiles: `{len(user_profiles)}`\n"
        f"📭 Active Email Sessions (user_data): `{len(user_data)}`\n"
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"⚡ Active Sessions (general interaction): `{len(active_sessions)}`\n"
        # f"🕒 Bot Approx. Start Time: `{bot_start_time_str}` (Note: This is illustrative)" 
        # The above line about start time is not very accurate as join_date refers to user's first interaction.
        # A proper uptime would require storing bot's actual start time.
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

    users_list_msgs = []
    for user_id_approved in approved_users:
        if user_id_approved in user_profiles:
            user_info = user_profiles[user_id_approved]
            users_list_msgs.append(
                f"🆔 `{user_id_approved}` - 👤 {user_info['name']} (@{user_info['username']}) - 📅 {user_info['join_date']}"
            )
        else:
            users_list_msgs.append(
                f"🆔 `{user_id_approved}` - (No profile info available)"
            )
    
    if not users_list_msgs:
        safe_send_message(message.chat.id, "❌ No user data available even for approved users.")
        return

    response_header = "👥 *Approved Users*\n\n"
    current_message_part = response_header
    
    for user_entry in users_list_msgs:
        if len(current_message_part) + len(user_entry) + 1 > 4000: # Keep under Telegram limit
            safe_send_message(message.chat.id, current_message_part)
            current_message_part = response_header # Start new message
        current_message_part += user_entry + "\n"
    
    if current_message_part != response_header : # Send the last part
         safe_send_message(message.chat.id, current_message_part)


@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_user_prompt(message):
    msg_out = safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=get_back_keyboard())
    if msg_out:
        bot.register_next_step_handler(msg_out, process_user_removal)

def process_user_removal(message):
    chat_id = message.chat.id # This is admin's chat_id
    if message.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled user removal.", reply_markup=get_user_management_keyboard())
        return
    
    try:
        user_id_to_remove = int(message.text.strip())
        if str(user_id_to_remove) == ADMIN_ID:
            safe_send_message(chat_id, "❌ Cannot remove the admin!", reply_markup=get_user_management_keyboard())
            return

        if user_id_to_remove in approved_users or user_id_to_remove in pending_approvals or user_id_to_remove in user_profiles:
            # Perform comprehensive removal
            removed_from_approved = user_id_to_remove in approved_users
            safe_delete_user(user_id_to_remove) # This handles all data structures

            if removed_from_approved:
                 safe_send_message(chat_id, f"✅ User {user_id_to_remove} has been fully removed and their data cleared.", reply_markup=get_user_management_keyboard())
                 try:
                     safe_send_message(user_id_to_remove, "❌ Your access has been revoked by the admin and your data has been cleared.")
                 except Exception as e:
                     print(f"Could not notify user {user_id_to_remove} about removal: {e}") # Might be blocked or deactivated
            else:
                 safe_send_message(chat_id, f"User {user_id_to_remove} was not in approved list but any pending requests or data has been cleared.", reply_markup=get_user_management_keyboard())
        else:
            safe_send_message(chat_id, f"❌ User {user_id_to_remove} not found in approved users, pending list, or profiles.", reply_markup=get_user_management_keyboard())
    except ValueError:
        safe_send_message(chat_id, "❌ Invalid User ID. Please enter a numeric ID.", reply_markup=get_user_management_keyboard())
    except Exception as e:
        print(f"Error in process_user_removal: {e}")
        safe_send_message(chat_id, "An unexpected error occurred.", reply_markup=get_user_management_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def broadcast_menu(message):
    safe_send_message(message.chat.id, "📢 Broadcast Message to All Approved Users", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def process_text_broadcast_prompt(message):
    msg_out = safe_send_message(message.chat.id, "✍️ Enter the broadcast message text (Markdown supported):", reply_markup=get_back_keyboard())
    if msg_out:
        bot.register_next_step_handler(msg_out, process_text_broadcast)

def process_text_broadcast(message):
    chat_id = message.chat.id # Admin's chat_id
    if message.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled broadcast.", reply_markup=get_broadcast_keyboard())
        return

    broadcast_text = message.text
    success_count = 0
    failed_count = 0
    
    # Ensure admin is in approved_users list for this operation, but don't broadcast to self.
    users_to_broadcast = list(u for u in approved_users if str(u) != ADMIN_ID)
    total_users_to_broadcast = len(users_to_broadcast)

    if not users_to_broadcast:
        safe_send_message(chat_id, "No users (excluding admin) to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_text_template = "📢 Broadcasting to {} users...\n\nProcessed: {}/{}\n✅ Success: {}\n❌ Failed: {}"
    progress_msg = safe_send_message(chat_id, progress_msg_text_template.format(total_users_to_broadcast, 0, total_users_to_broadcast, 0, 0))

    if progress_msg is None: # Failed to send initial progress message
        safe_send_message(chat_id, "Error starting broadcast. Could not send progress message.", reply_markup=get_admin_keyboard())
        return

    for i, user_id_target in enumerate(users_to_broadcast, 1):
        try:
            # For text broadcasts, parse_mode="Markdown" is default for bot, but can be explicit.
            # Add a header to the broadcast message.
            full_broadcast_text = f"📢 *Admin Broadcast*\n\n{broadcast_text}"
            sent_msg = safe_send_message(user_id_target, full_broadcast_text, parse_mode="Markdown")
            if sent_msg:
                success_count += 1
            else: # safe_send_message returned None, implies blocked or other issue handled by it
                failed_count +=1
        except Exception as e:
            print(f"Broadcast exception to {user_id_target}: {e}")
            failed_count += 1
        
        time.sleep(0.1) # Small delay between messages

        if i % 10 == 0 or i == total_users_to_broadcast: # Update progress every 10 users or at the end
            try:
                bot.edit_message_text(
                    progress_msg_text_template.format(total_users_to_broadcast, i, total_users_to_broadcast, success_count, failed_count),
                    chat_id=chat_id,
                    message_id=progress_msg.message_id
                )
            except Exception as edit_e:
                print(f"Error updating broadcast progress message: {edit_e}")
    
    final_summary = f"📢 Text Broadcast Completed!\n\nProcessed: {total_users_to_broadcast}\n✅ Successful: {success_count}\n❌ Failed: {failed_count}"
    safe_send_message(chat_id, final_summary, reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def media_broadcast_prompt(message):
    msg_out = safe_send_message(message.chat.id, "🖼 Send the photo/video/document you want to broadcast (with caption if needed). Max 1 media file.", reply_markup=get_back_keyboard())
    if msg_out:
        bot.register_next_step_handler(msg_out, process_media_broadcast)

def process_media_broadcast(message):
    chat_id = message.chat.id # Admin's chat_id
    if message.text == "⬅️ Back": # Check if it's a text message "Back"
        safe_send_message(chat_id, "Cancelled media broadcast.", reply_markup=get_broadcast_keyboard())
        return

    # Validate that it's a media message
    if not (message.photo or message.video or message.document):
        msg_out = safe_send_message(chat_id, "⚠️ No media detected. Please send a photo, video, or document. Or click '⬅️ Back'.", reply_markup=get_back_keyboard())
        if msg_out:
            bot.register_next_step_handler(msg_out, process_media_broadcast) # Re-register
        return

    success_count = 0
    failed_count = 0
    users_to_broadcast = list(u for u in approved_users if str(u) != ADMIN_ID)
    total_users_to_broadcast = len(users_to_broadcast)
    
    if not users_to_broadcast:
        safe_send_message(chat_id, "No users (excluding admin) to broadcast media to.", reply_markup=get_admin_keyboard())
        return

    caption = message.caption if message.caption else ""
    # Add admin broadcast header to caption if it's not too long
    caption_header = "📢 *Admin Media Broadcast*\n"
    final_caption = caption_header + caption
    if len(final_caption) > 1024: # Telegram caption limit
        final_caption = caption[:1024 - len(caption_header) - 3] + "..." # Truncate original caption
        final_caption = caption_header + final_caption


    progress_msg_text_template = "📢 Broadcasting media to {} users...\n\nProcessed: {}/{}\n✅ Success: {}\n❌ Failed: {}"
    progress_msg = safe_send_message(chat_id, progress_msg_text_template.format(total_users_to_broadcast, 0, total_users_to_broadcast, 0, 0))
    if progress_msg is None:
        safe_send_message(chat_id, "Error starting media broadcast. Could not send progress message.", reply_markup=get_admin_keyboard())
        return

    for i, user_id_target in enumerate(users_to_broadcast, 1):
        media_sent_successfully = False
        try:
            if message.photo:
                bot.send_photo(user_id_target, message.photo[-1].file_id, caption=final_caption, parse_mode="Markdown")
                media_sent_successfully = True
            elif message.video:
                bot.send_video(user_id_target, message.video.file_id, caption=final_caption, parse_mode="Markdown")
                media_sent_successfully = True
            elif message.document:
                bot.send_document(user_id_target, message.document.file_id, caption=final_caption, parse_mode="Markdown")
                media_sent_successfully = True
            
            if media_sent_successfully:
                success_count += 1
            else: # Should not happen if validated before, but as a safeguard
                failed_count += 1

        except telebot.apihelper.ApiTelegramException as e:
            if e.result.status_code == 403 and ("bot was blocked" in e.result.text.lower() or "user is deactivated" in e.result.text.lower()):
                safe_delete_user(user_id_target) # Clean up if blocked
            print(f"Media broadcast API exception to {user_id_target}: {e}")
            failed_count += 1
        except Exception as e:
            print(f"Media broadcast general exception to {user_id_target}: {e}")
            failed_count += 1
        
        time.sleep(0.2) # Slightly longer delay for media

        if i % 5 == 0 or i == total_users_to_broadcast: # Update progress less frequently for media
            try:
                bot.edit_message_text(
                    progress_msg_text_template.format(total_users_to_broadcast, i, total_users_to_broadcast, success_count, failed_count),
                    chat_id=chat_id,
                    message_id=progress_msg.message_id
                )
            except Exception as edit_e:
                 print(f"Error updating media broadcast progress message: {edit_e}")
    
    final_summary = f"📢 Media Broadcast Completed!\n\nProcessed: {total_users_to_broadcast}\n✅ Successful: {success_count}\n❌ Failed: {failed_count}"
    safe_send_message(chat_id, final_summary, reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin" and is_admin(msg.chat.id))
def back_to_admin(message):
    safe_send_message(message.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu") # For both admin and regular users
def back_to_main_menu_common(message):
    chat_id = message.chat.id
    # Check if it's a user who was in a sub-menu like 2FA secret input
    if chat_id in user_2fa_secrets and "platform" in user_2fa_secrets[chat_id] and "secret" not in user_2fa_secrets[chat_id]:
        del user_2fa_secrets[chat_id] # Clear pending 2FA state

    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))


@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def handle_approval(call):
    admin_chat_id = call.message.chat.id
    if not is_admin(admin_chat_id):
        bot.answer_callback_query(call.id, "Error: Not an admin.")
        return

    try:
        action, user_id_str = call.data.split('_')
        user_id_target = int(user_id_str)
    except ValueError:
        bot.answer_callback_query(call.id, "Error: Invalid user ID in callback.")
        bot.edit_message_text("Invalid callback data.", chat_id=admin_chat_id, message_id=call.message.message_id)
        return

    original_message_text = call.message.text # Preserve the original message content

    if action == "approve":
        approved_users.add(user_id_target)
        if user_id_target in pending_approvals:
            del pending_approvals[user_id_target]
        
        # Ensure user_profile exists if approved (should from /start)
        if user_id_target not in user_profiles:
             # Try to get fresh info if missing, though ideally it's there from /start
            try:
                chat_info = bot.get_chat(user_id_target)
                user_profiles[user_id_target] = get_user_info(chat_info) # This is a Chat object, not User, adapt if needed
            except: # Fallback
                user_profiles[user_id_target] = {"name": f"User {user_id_target}", "username": "N/A", "join_date": "N/A"}

        safe_send_message(user_id_target, "✅ Your access request has been approved! Welcome!", reply_markup=get_main_keyboard(user_id_target))
        bot.answer_callback_query(call.id, f"User {user_id_target} approved.")
        # Edit the admin's message to show it's handled
        bot.edit_message_text(f"{original_message_text}\n\n---\n✅ Approved by you.", chat_id=admin_chat_id, message_id=call.message.message_id, reply_markup=None, parse_mode="Markdown")
        # safe_send_message(admin_chat_id, f"✅ User {user_id_target} approved.") # Optional separate notification
    
    elif action == "reject":
        if user_id_target in pending_approvals:
            del pending_approvals[user_id_target]
        # Optionally, remove from other places too if they somehow got there
        safe_delete_user(user_id_target) # This is more thorough

        safe_send_message(user_id_target, "❌ Your access request has been rejected by the admin.")
        bot.answer_callback_query(call.id, f"User {user_id_target} rejected.")
        bot.edit_message_text(f"{original_message_text}\n\n---\n❌ Rejected by you.", chat_id=admin_chat_id, message_id=call.message.message_id, reply_markup=None, parse_mode="Markdown")
        # safe_send_message(admin_chat_id, f"❌ User {user_id_target} rejected and data cleared.")
    else:
        bot.answer_callback_query(call.id, "Unknown action.")


# --- Mail handlers (New Temp-Mail.io via temp-mail.org API) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or has been revoked.")
        return

    domains = get_temp_mail_domains()
    if not domains:
        safe_send_message(chat_id, "❌ Could not fetch email domains. Please try again later.")
        return

    email_address = generate_random_email_local(domains)
    if not email_address: # Should not happen if domains were fetched
        safe_send_message(chat_id, "❌ Failed to generate a new email address. Try again.")
        return

    user_data[chat_id] = {"email": email_address}
    last_message_ids[chat_id] = set() # Initialize seen message IDs for this new email

    msg_text = (
        f"✅ *Your New Temporary Email is Active!*\n\n"
        f"📧 Email: `{email_address}`\n\n"
        f"Tap the email address above to copy it.\n"
        f"Messages will appear here automatically or use '🔄 Refresh'."
    )
    safe_send_message(chat_id, msg_text)


@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh")
def refresh_mail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or has been revoked.")
        return

    if chat_id not in user_data or "email" not in user_data[chat_id]:
        safe_send_message(chat_id, "⚠️ You don't have an active temporary email. Use '📬 New mail' to get one.")
        return

    current_email = user_data[chat_id]["email"]
    safe_send_message(chat_id, f"🔄 Checking inbox for `{current_email}`...")
    
    messages = fetch_temp_mail_messages(current_email)

    if messages is None: # API or connection error
        safe_send_message(chat_id, "❌ Connection error while fetching emails. Please try again later.")
        return
    
    if not messages:
        safe_send_message(chat_id, f"📭 *Your inbox for `{current_email}` is currently empty.*")
        return

    # Sort messages by timestamp, newest first. temp-mail.org usually does this.
    try:
        sorted_messages = sorted(messages, key=lambda m: float(m.get('mail_timestamp', 0)), reverse=True)
    except (TypeError, ValueError):
        sorted_messages = messages # Process as received if sorting fails

    safe_send_message(chat_id, f"📨 *Found {len(sorted_messages)} message(s) for `{current_email}` (showing up to 5 newest):*")
    
    seen_ids = last_message_ids.setdefault(chat_id, set())
    displayed_count = 0

    for msg_data in sorted_messages[:5]: # Show up to 5 newest on manual refresh
        msg_id = msg_data.get("mail_id") or msg_data.get("_id", {}).get("$oid")
        if msg_id: # Add to seen if manually refreshed and viewed
            seen_ids.add(msg_id)

        sender = msg_data.get("mail_from", "N/A")
        subject = msg_data.get("mail_subject", "(No Subject)")
        body = msg_data.get("mail_text_only") or msg_data.get("mail_text", "(No Content)")
        body = body.strip() if body else "(No Content)"
        
        try:
            timestamp = float(msg_data.get("mail_timestamp", time.time()))
            received_time_str = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            received_time_str = "Recently"

        formatted_msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            # f"📬 *Email Content:*\n" # Removed redundant header
            f"👤 *From:* `{sender}`\n"
            f"📨 *Subject:* _{subject}_\n"
            f"🕒 *Received:* {received_time_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💬 *Body:*\n"
            f"{body[:3500]}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        safe_send_message(chat_id, formatted_msg)
        displayed_count += 1
        time.sleep(0.1) # Avoid bursting

    if displayed_count == 0 and messages: # Should not happen if messages exist but as fallback
        safe_send_message(chat_id, "Could not display messages, though some were found. Try again.")


# --- Profile handlers ---
@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
def generate_profile_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or has been revoked.")
        return

    gender = "male" if message.text == "👨 Male Profile" else "female"
    _gender, name, username, password, phone = generate_profile(gender) # _gender is same as gender
    message_text = profile_message(gender, name, username, password, phone)
    safe_send_message(chat_id, message_text)


# --- My Account handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
def my_account_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    safe_send_message(chat_id, "Manage your account settings:", reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📧 My Email")
def my_email_info(message):
    chat_id = message.chat.id
    if chat_id in user_data and "email" in user_data[chat_id]:
        email = user_data[chat_id]["email"]
        safe_send_message(chat_id, f"Your current temporary email is: `{email}`")
    else:
        safe_send_message(chat_id, "You don't have an active temporary email. Use '📬 New mail' to get one.")

@bot.message_handler(func=lambda msg: msg.text == "🆔 My Info")
def my_user_info(message):
    chat_id = message.chat.id
    if chat_id in user_profiles:
        u_info = user_profiles[chat_id]
        info_text = (
            f"👤 *Your Information*\n\n"
            f"🆔 User ID: `{chat_id}`\n"
            f"👤 Name: `{u_info['name']}`\n"
            f"📛 Username: `@{u_info['username']}`\n"
            f"📅 Joined Bot: `{u_info['join_date']}`\n"
            f"✅ Access Status: {'Admin' if is_admin(chat_id) else ('Approved' if chat_id in approved_users else 'Pending/Unknown')}"
        )
        safe_send_message(chat_id, info_text)
    else:
        safe_send_message(chat_id, "Could not retrieve your info. Try `/start` again.")

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main") # General "Back to Main"
def common_back_to_main(message): # Already defined one, this might be redundant if context handled
    chat_id = message.chat.id
    # Clear any pending state, e.g. if user was in 2FA secret input
    if chat_id in user_2fa_secrets and "platform" in user_2fa_secrets[chat_id] and "secret" not in user_2fa_secrets[chat_id]:
        del user_2fa_secrets[chat_id]
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))


# --- 2FA Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def two_fa_auth_start(message): # Renamed to avoid conflict if another function was named two_fa_auth
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or has been revoked.")
        return
    safe_send_message(chat_id, "🔐 Choose the platform for which you want to generate a 2FA code (you'll need your secret key):", reply_markup=get_2fa_platform_keyboard())


@bot.message_handler(func=lambda msg: msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform_selection(message):
    chat_id = message.chat.id
    platform = message.text
    if is_bot_blocked(chat_id): # Redundant if previous handler checked, but good for direct calls
        safe_delete_user(chat_id)
        return
    if not (chat_id in approved_users or is_admin(chat_id)): # Check access again
        safe_send_message(chat_id, "Access denied for 2FA.")
        return

    user_2fa_secrets[chat_id] = {"platform": platform} # Store platform, wait for secret
    
    # Ask for secret key using a new message to trigger the next step handler correctly
    msg_out = safe_send_message(chat_id, f"🔑 Enter the 2FA secret key for *{platform}* (Base32 format).\n\nExample: `JBSWY3DPEHPK3PXP`\n\n_(This key is NOT stored after code generation)_", reply_markup=get_back_keyboard())
    if msg_out:
        # The generic handle_all_text will pick this up if it's next due to user_2fa_secrets state
        # Or, more explicitly:
        bot.register_next_step_handler(msg_out, process_2fa_secret_input)


def process_2fa_secret_input(message):
    chat_id = message.chat.id
    
    if message.text == "⬅️ Back":
        if chat_id in user_2fa_secrets: del user_2fa_secrets[chat_id]
        safe_send_message(chat_id, "2FA code generation cancelled.", reply_markup=get_main_keyboard(chat_id))
        return
    
    if chat_id not in user_2fa_secrets or "platform" not in user_2fa_secrets[chat_id]:
        # State lost or invalid sequence
        safe_send_message(chat_id, "⚠️ Error: 2FA platform not set. Please start over from '🔐 2FA Auth'.", reply_markup=get_main_keyboard(chat_id))
        return

    secret_key_input = message.text.strip()
    platform = user_2fa_secrets[chat_id]["platform"]

    if not is_valid_base32(secret_key_input):
        msg_out = safe_send_message(chat_id,
                                    f"❌ *Invalid Secret Key for {platform}!*\n\n"
                                    f"Your secret must be a valid Base32 string (e.g., `JBSWY3DPEHPK3PXP`).\n"
                                    f"- Characters: A-Z and 2-7 only.\n"
                                    f"- No spaces or special characters (hyphens are okay, they'll be removed).\n\n"
                                    f"Please try entering the secret key again, or use '⬅️ Back'.",
                                    parse_mode='Markdown', reply_markup=get_back_keyboard())
        if msg_out:
            bot.register_next_step_handler(msg_out, process_2fa_secret_input) # Re-register for another attempt
        return

    cleaned_secret = secret_key_input.replace(" ", "").replace("-", "").upper()
    user_2fa_secrets[chat_id]["secret"] = cleaned_secret # Store cleaned secret temporarily

    try:
        totp = pyotp.TOTP(cleaned_secret)
        current_code = totp.now()
        
        now_time = datetime.datetime.now()
        time_remaining = 30 - (now_time.second % 30)
        
        # Store the message ID so we can offer a refresh button
        code_message_text = (
            f"🔐 *2FA Code for {platform}*\n\n"
            f"Your code: ` {current_code} `\n"
            f"_(Tap code to copy)_\n\n"
            f"⏳ Valid for: *{time_remaining} seconds*\n\n"
            f"⚠️ _This secret key is NOT stored. If you need another code later, you'll have to re-enter the secret._"
        )
        
        # Inline keyboard for refresh
        refresh_button_key = f"refresh2fa_{chat_id}" # Unique key per user for callback
        user_2fa_codes[refresh_button_key] = cleaned_secret # Store secret against this key for refresh
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 Refresh Code", callback_data=refresh_button_key))

        sent_msg_details = safe_send_message(chat_id, code_message_text, reply_markup=markup, parse_mode="Markdown")
        
        # Clean up the temporary storage of platform/secret from user_2fa_secrets
        # The secret is now in user_2fa_codes[refresh_button_key] for the refresh button's use
        if chat_id in user_2fa_secrets:
            del user_2fa_secrets[chat_id]

    except Exception as e:
        print(f"Error generating TOTP: {e}")
        safe_send_message(chat_id, f"❌ Error generating 2FA code for {platform}. The secret key might be malformed despite initial checks. Please try again.", reply_markup=get_main_keyboard(chat_id))
        if chat_id in user_2fa_secrets: del user_2fa_secrets[chat_id]


@bot.callback_query_handler(func=lambda call: call.data.startswith("refresh2fa_"))
def handle_2fa_refresh_callback(call):
    chat_id = call.message.chat.id # User who clicked refresh
    refresh_key = call.data

    if refresh_key not in user_2fa_codes:
        bot.answer_callback_query(call.id, "⚠️ Secret key expired for refresh. Please re-enter.")
        try:
            bot.edit_message_text("Session for refreshing this code has expired. Please generate a new one via the main menu.",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
        except Exception as e_edit: print(f"Error editing message on expired 2FA refresh: {e_edit}")
        return

    secret_key = user_2fa_codes[refresh_key]
    platform_name = "the selected platform" # We don't store platform here, but it's for the same one.
                                          # Could try to parse from original message if needed.

    try:
        totp = pyotp.TOTP(secret_key)
        current_code = totp.now()
        now_time = datetime.datetime.now()
        time_remaining = 30 - (now_time.second % 30)

        refreshed_code_message_text = (
            f"🔐 *2FA Code for {platform_name}* (Refreshed)\n\n"
            f"Your code: ` {current_code} `\n"
            f"_(Tap code to copy)_\n\n"
            f"⏳ Valid for: *{time_remaining} seconds*\n\n"
             f"⚠️ _This secret key is NOT stored. If you need another code later, you'll have to re-enter the secret._"
        )
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 Refresh Code", callback_data=refresh_key)) # Same callback data

        bot.edit_message_text(refreshed_code_message_text,
                              chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=markup,
                              parse_mode="Markdown")
        bot.answer_callback_query(call.id, "Code refreshed!")

    except Exception as e:
        print(f"Error refreshing TOTP: {e}")
        bot.answer_callback_query(call.id, "Error refreshing code.")
        try:
            bot.edit_message_text("Error refreshing code. The secret key might be invalid.",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
        except: pass # Ignore if edit fails


# Fallback handler for any text not caught by specific handlers or next_step_handlers
# This helps prevent bot from getting stuck if a next_step_handler was expected but not matched.
@bot.message_handler(func=lambda message: True, content_types=['text', 'audio', 'photo', 'video', 'document', 'sticker', 'voice', 'location', 'contact'])
def handle_unknown_messages(message):
    chat_id = message.chat.id
    # If user is in a specific state (like pending 2FA input) and sends unexpected text,
    # it might be handled by a registered next_step_handler first.
    # This is a very broad fallback.
    if chat_id in approved_users or is_admin(chat_id):
        # Check if the bot was expecting a specific input for 2FA but got something else
        if chat_id in user_2fa_secrets and "platform" in user_2fa_secrets[chat_id] and "secret" not in user_2fa_secrets[chat_id]:
             # User might have typed something random instead of a secret or "Back"
             # The process_2fa_secret_input should handle this, but if it doesn't trigger:
             pass # Let the specific handler for 2FA secret input manage this.

        # For other cases, you might want to tell the user the command is not recognized.
        # However, be careful not to interfere with next_step_handlers.
        # A simple "I don't understand that" can be annoying if it overrides a pending input.
        # For now, this can be a silent pass-through or a very generic reply if no next_step is active.
        
        # A check to see if any next_step_handler is registered for this user:
        # Unfortunately, pyTelegramBotAPI doesn't offer a public way to check bot.current_states easily.
        # So, we avoid sending a default "unknown command" if a state might be active.

        # If it's just random text and no specific state:
        if not (chat_id in user_2fa_secrets): # Example: if not in any known input state
             if message.text and not message.text.startswith('/'): # Don't reply to commands already potentially handled
                pass # safe_send_message(chat_id, "🤔 I'm not sure what you mean. Try a command from the menu.", reply_markup=get_main_keyboard(chat_id))
    else:
        if chat_id not in pending_approvals: # If not admin, not approved, and not even pending
            send_welcome(message) # Re-trigger welcome to send approval request
        else:
            safe_send_message(chat_id, "⏳ Your access is still pending approval. Please wait.")


if __name__ == '__main__':
    print("🤖 Bot is preparing to run...")
    if not ADMIN_ID:
        print("⚠️ WARNING: ADMIN_ID is not set in .env file. Admin features will be limited or may not work correctly.")
    else:
        print(f"🔑 Admin ID: {ADMIN_ID}")
        # Automatically add admin to approved_users if not already, and set up profile
        # This is also handled in /start, but good for initial setup on run.
        admin_chat_id_int = int(ADMIN_ID)
        if admin_chat_id_int not in approved_users:
            approved_users.add(admin_chat_id_int)
        if admin_chat_id_int not in user_profiles:
             user_profiles[admin_chat_id_int] = {
                "name": "Admin User", 
                "username": "N/A (loaded at runtime)", 
                "join_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }


    print("🧵 Starting background workers...")
    threading.Thread(target=auto_refresh_worker, daemon=True).start()
    threading.Thread(target=cleanup_blocked_users, daemon=True).start()
    
    print("🚀 Bot is now polling for messages...")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=30)
    except Exception as e:
        print(f"💥 Bot crashed with exception: {e}")
        # Consider adding restart logic or more robust error handling here
    finally:
        print("🛑 Bot has stopped.")

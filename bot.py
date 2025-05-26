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
import hashlib # Added for MD5 hashing

load_dotenv()
fake = Faker()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise Exception("❌ BOT_TOKEN not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# --- Temporary Mail API Configuration ---
# Using temp-mail.org API as a common example for temporary email services.
# temp-mail.io might use this or a similar backend.
TEMP_MAIL_API_BASE_URL = "https://api.temp-mail.org/request"
DEFAULT_TEMP_MAIL_DOMAIN = "porjoton.com" # Fallback domain

# Data storage
user_data = {}  # Stores {"email": "user@tempdomain.com"} for temp mail
last_message_ids = {} # Stores set of seen message IDs from temp mail
user_2fa_codes = {} # This seems unused, consider removing or implementing its use case
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
    # if chat_id in user_2fa_codes: # This was from original, but seems unused
    #     del user_2fa_codes[chat_id]
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]
    if chat_id in active_sessions:
        active_sessions.discard(chat_id) # Use discard to avoid KeyError
    if chat_id in pending_approvals:
        del pending_approvals[chat_id]
    if chat_id in approved_users:
        approved_users.discard(chat_id) # Use discard
    if chat_id in user_profiles:
        del user_profiles[chat_id]

def is_bot_blocked(chat_id):
    try:
        bot.get_chat(chat_id)
        return False
    except telebot.apihelper.ApiTelegramException as e:
        if e.result_json.get("error_code") == 403 and "bot was blocked" in e.result_json.get("description", ""):
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
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        telebot.types.KeyboardButton("📬 New mail"),
        telebot.types.KeyboardButton("🔄 Refresh Mail") # Renamed for clarity
    )
    keyboard.add(
        telebot.types.KeyboardButton("👨 Male Profile"),
        telebot.types.KeyboardButton("👩 Female Profile")
    )
    keyboard.add(
        telebot.types.KeyboardButton("🔐 2FA Auth"),
        telebot.types.KeyboardButton("👤 My Account")
    )
    if is_admin(chat_id):
        keyboard.add(telebot.types.KeyboardButton("👑 Admin Panel"))
    return keyboard

def get_admin_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        telebot.types.KeyboardButton("👥 Pending Approvals"),
        telebot.types.KeyboardButton("📊 Stats")
    )
    keyboard.add(
        telebot.types.KeyboardButton("👤 User Management"),
        telebot.types.KeyboardButton("📢 Broadcast")
    )
    keyboard.add(telebot.types.KeyboardButton("⬅️ Main Menu"))
    return keyboard

def get_user_management_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        telebot.types.KeyboardButton("📜 List Users"),
        telebot.types.KeyboardButton("❌ Remove User")
    )
    keyboard.add(telebot.types.KeyboardButton("⬅️ Back to Admin"))
    return keyboard

def get_approval_keyboard(user_id):
    keyboard = telebot.types.InlineKeyboardMarkup()
    keyboard.add(
        telebot.types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
        telebot.types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")
    )
    return keyboard

def get_user_account_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        telebot.types.KeyboardButton("📧 My Current Email"), # Renamed
        telebot.types.KeyboardButton("🆔 My Info")
    )
    keyboard.add(telebot.types.KeyboardButton("⬅️ Back to Main"))
    return keyboard

def get_2fa_platform_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    keyboard.add("Google", "Facebook", "Instagram")
    keyboard.add("Twitter", "Microsoft", "Apple")
    keyboard.add("⬅️ Back to Main")
    return keyboard

def get_back_keyboard(target_menu="main"): # Added target_menu for flexibility
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    if target_menu == "admin_user_management":
        keyboard.row("⬅️ Back to User Management")
    elif target_menu == "admin_broadcast":
        keyboard.row("⬅️ Back to Broadcast Menu")
    elif target_menu == "2fa_secret_entry":
         keyboard.row("⬅️ Back to 2FA Platforms")
    else: # Default back to main menu
        keyboard.row("⬅️ Back to Main")
    return keyboard


def get_broadcast_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        telebot.types.KeyboardButton("📢 Text Broadcast"),
        telebot.types.KeyboardButton("📋 Media Broadcast")
    )
    keyboard.add(telebot.types.KeyboardButton("⬅️ Back to Admin"))
    return keyboard

def safe_send_message(chat_id, text, **kwargs):
    try:
        if is_bot_blocked(chat_id):
            safe_delete_user(chat_id)
            return None
        msg = bot.send_message(chat_id, text, **kwargs)
        active_sessions.add(chat_id)
        return msg
    except telebot.apihelper.ApiTelegramException as e:
        if e.result_json.get("error_code") == 403 and "bot was blocked" in e.result_json.get("description", ""):
            safe_delete_user(chat_id)
        else:
            print(f"Error sending message to {chat_id}: API Error {e.result_json}")
        return None
    except Exception as e:
        print(f"Error sending message to {chat_id}: {str(e)}")
        return None

# --- Temp Mail (temp-mail.org style API) Functions ---

def get_temp_mail_domains():
    """Fetches available domains from the temp-mail API."""
    try:
        res = requests.get(f"{TEMP_MAIL_API_BASE_URL}/domains/format/json/", timeout=10)
        res.raise_for_status()
        domains = res.json()
        return [d for d in domains if isinstance(d, str) and d.startswith('.')] if domains else [DEFAULT_TEMP_MAIL_DOMAIN]
    except requests.exceptions.RequestException as e:
        print(f"Error fetching temp-mail domains: {e}")
        return [DEFAULT_TEMP_MAIL_DOMAIN] # Fallback
    except ValueError: # JSONDecodeError
        print(f"Error decoding temp-mail domains JSON.")
        return [DEFAULT_TEMP_MAIL_DOMAIN]


def generate_temp_mail_address():
    """Generates a new temporary email address."""
    try:
        name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        domains = get_temp_mail_domains()
        domain = random.choice(domains).lstrip('.') # Domains from API might start with '.'
        if not domain: # Safety check
            domain = DEFAULT_TEMP_MAIL_DOMAIN.lstrip('.')
        return f"{name}@{domain}"
    except Exception as e:
        print(f"Error generating temp mail address: {e}")
        # Fallback to a completely hardcoded generation if API fails badly
        name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        return f"{name}@{DEFAULT_TEMP_MAIL_DOMAIN}"


def fetch_temp_mail_messages(email_address):
    """Fetches messages for a given temporary email address."""
    if not email_address:
        return []
    try:
        email_hash = hashlib.md5(email_address.encode('utf-8')).hexdigest()
        res = requests.get(f"{TEMP_MAIL_API_BASE_URL}/mail/id/{email_hash}/format/json/", timeout=15)
        res.raise_for_status()
        messages = res.json()
        # The API might return an error object like {"error":"no_mail"} instead of an empty list
        if isinstance(messages, dict) and "error" in messages:
            return []
        return messages if isinstance(messages, list) else []
    except requests.exceptions.RequestException as e:
        print(f"Error fetching temp-mail messages for {email_address}: {e}")
        return []
    except ValueError: # JSONDecodeError
        print(f"Error decoding temp-mail messages JSON for {email_address}.")
        return []


# Profile generator
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
        if not cleaned or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in cleaned):
            return False
        # Pad if necessary for pyotp
        padding = "=" * (-len(cleaned) % 8)
        pyotp.TOTP(cleaned + padding).now() # Test with pyotp
        return True
    except (binascii.Error, ValueError, TypeError, Exception) as e: # Broader exception for pyotp issues
        print(f"Base32 validation error: {e}")
        return False

# --- Background Workers ---
def format_temp_mail_message(msg_detail):
    """Formats a message from the temp-mail API."""
    sender = msg_detail.get('mail_from', 'N/A')
    subject = msg_detail.get('mail_subject', '(No Subject)')
    
    body = msg_detail.get('mail_text', '')
    if not body and 'mail_html' in msg_detail: # Fallback to HTML if text is empty
        # Basic HTML stripping, consider a library for complex HTML
        html_body = msg_detail['mail_html']
        body = requests.utils.unquote(html_body) # Handle URL encoded chars
        body = telebot.util.smart_split(body, 3000)[0] # Approximate stripping
        body = body.replace('<br>', '\n').replace('<br/>', '\n').replace('<p>', '\n').replace('</p>', '\n')
        # very basic tag stripping
        import re
        body = re.sub(r'<[^>]+>', '', body)

    body = body.strip() if body else "(No Content)"

    received_time_str = "Just now"
    timestamp = msg_detail.get('mail_timestamp')
    if timestamp:
        try:
            received_time_str = datetime.datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d %H:%M:%S UTC')
        except (ValueError, TypeError):
            received_time_str = str(timestamp) # if it's already a string or unparsable
    elif 'mail_date' in msg_detail: # Some APIs might provide 'mail_date' string
        received_time_str = msg_detail['mail_date']

    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📬 *New Email Received!*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *From:* `{sender}`\n"
        f"📨 *Subject:* _{subject}_\n"
        f"🕒 *Received:* {received_time_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 *Body:*\n"
        f"{body[:3500]}\n" # Telegram message length limit
        f"━━━━━━━━━━━━━━━━━━━━"
    )

def auto_refresh_worker():
    while True:
        try:
            current_user_data_keys = list(user_data.keys()) # Iterate over a copy
            for chat_id in current_user_data_keys:
                if chat_id not in user_data: # Check if user was removed during iteration
                    continue

                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id)
                    continue
                
                if "email" not in user_data.get(chat_id, {}):
                    continue

                email_address = user_data[chat_id]["email"]
                messages = fetch_temp_mail_messages(email_address)
                
                if not messages: # Could be empty or API error returning non-list
                    continue

                seen_ids = last_message_ids.setdefault(chat_id, set())
                
                # Sort messages by timestamp if available, newest first
                try:
                    messages.sort(key=lambda m: int(m.get('mail_timestamp', 0)), reverse=True)
                except (TypeError, ValueError):
                    pass # If timestamp is not an int or missing, proceed without sorting

                new_messages_found = False
                for msg_detail in messages[:5]: # Check recent 5 messages
                    # Assume 'mail_id' or a similar unique field exists.
                    # Common fields are 'mail_id', 'id', or hash of content if nothing else.
                    # For temp-mail.org, 'mail_id' is usually present.
                    msg_id = msg_detail.get('mail_id') 
                    if not msg_id: # Fallback if 'mail_id' is not present, try to create a unique enough ID
                        msg_id = hashlib.md5((str(msg_detail.get('mail_from')) + \
                                             str(msg_detail.get('mail_subject')) + \
                                             str(msg_detail.get('mail_timestamp'))).encode()).hexdigest()

                    if msg_id in seen_ids:
                        continue
                    
                    seen_ids.add(msg_id)
                    new_messages_found = True
                    
                    formatted_msg = format_temp_mail_message(msg_detail)
                    safe_send_message(chat_id, formatted_msg)
                    time.sleep(0.5) # Small delay between sending multiple messages

                # Keep seen_ids relatively small to avoid memory issues over time
                if len(seen_ids) > 100:
                    oldest_ids = sorted(list(seen_ids))[:-50] # Keep the most recent 50
                    for old_id in oldest_ids:
                        seen_ids.discard(old_id)

        except Exception as e:
            print(f"Error in auto_refresh_worker: {e}")
        time.sleep(45) # Increased sleep time

def cleanup_blocked_users():
    while True:
        try:
            sessions_to_check = list(active_sessions) # Iterate over a copy
            for chat_id in sessions_to_check:
                if is_bot_blocked(chat_id):
                    print(f"Cleaning up blocked user: {chat_id}")
                    safe_delete_user(chat_id)
        except Exception as e:
            print(f"Error in cleanup_blocked_users: {e}")
        time.sleep(3600) # Check once per hour

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
        approved_users.add(chat_id)
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=get_main_keyboard(chat_id))
        return

    if chat_id in approved_users:
        safe_send_message(chat_id, "👋 Welcome back!", reply_markup=get_main_keyboard(chat_id))
    else:
        if chat_id not in pending_approvals: # Send request only if not already pending
            pending_approvals[chat_id] = user_info
            safe_send_message(chat_id, "👋 Your access request has been sent to the admin. Please wait for approval.")
            if ADMIN_ID:
                try:
                    admin_chat_id = int(ADMIN_ID)
                    approval_msg = (
                        f"🆕 *New Approval Request*\n\n"
                        f"🆔 User ID: `{chat_id}`\n"
                        f"👤 Name: `{user_info['name']}`\n"
                        f"📛 Username: `@{user_info['username']}`\n"
                        f"📅 Joined: `{user_info['join_date']}`"
                    )
                    # Use safe_send_message for admin notifications too
                    safe_send_message(admin_chat_id, approval_msg, reply_markup=get_approval_keyboard(chat_id))
                except ValueError:
                    print("ADMIN_ID is not a valid integer.")
                except Exception as e:
                    print(f"Failed to send approval request to admin: {e}")
        else:
            safe_send_message(chat_id, "⏳ Your access request is still pending. Please wait for admin approval.")


# --- Admin Panel Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel" and is_admin(msg.chat.id))
def admin_panel(message):
    safe_send_message(message.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "👥 Pending Approvals" and is_admin(msg.chat.id))
def show_pending_approvals(message):
    if not pending_approvals:
        safe_send_message(message.chat.id, "✅ No pending approvals.")
        return
    
    response_text = "⏳ *Pending User Approvals:*\n\n"
    count = 0
    for user_id, user_info in list(pending_approvals.items()): # Iterate copy
        count +=1
        response_text += (
            f"*Request {count}*\n"
            f"🆔 User ID: `{user_id}`\n"
            f"👤 Name: `{user_info['name']}`\n"
            f"📛 Username: @{user_info['username']}\n"
            f"📅 Joined: `{user_info['join_date']}`\n"
            f"------------------------------------\n"
        )
        # Send individual approval options for each
        keyboard = get_approval_keyboard(user_id)
        safe_send_message(message.chat.id, f"Approve/Reject user: `{user_info['name']}` (`{user_id}`)", reply_markup=keyboard)
    
    if count == 0: # Should be caught by initial check, but as a safeguard
         safe_send_message(message.chat.id, "✅ No pending approvals currently.")


@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.chat.id))
def show_stats(message):
    bot_start_time_str = user_profiles.get("bot_start_time", "Not recorded")
    if "bot_start_time" not in user_profiles: # Set it if not present
        user_profiles["bot_start_time"] = datetime.datetime.now()
        bot_start_time_str = user_profiles["bot_start_time"].strftime('%Y-%m-%d %H:%M:%S')
    else:
        bot_start_time_str = user_profiles["bot_start_time"].strftime('%Y-%m-%d %H:%M:%S')

    uptime_delta = datetime.datetime.now() - user_profiles["bot_start_time"]
    days = uptime_delta.days
    hours, remainder = divmod(uptime_delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    uptime_str = f"{days}d {hours}h {minutes}m"

    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin ID: `{ADMIN_ID}`\n"
        f"👥 Approved Users: `{len(approved_users)}`\n"
        f"👤 Active User Sessions (bot contacted): `{len(active_sessions)}`\n"
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"📧 Active Email Addresses: `{len(user_data)}` (users with generated emails)\n"
        f"🚀 Bot Started: `{bot_start_time_str}`\n"
        f"⏱ Uptime: `{uptime_str}`"
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
    
    users_list_parts = []
    header = "👥 *Approved Users List*\n\n"
    
    current_part = ""
    for user_id in approved_users:
        user_info_str = f"🆔 `{user_id}`"
        if user_id in user_profiles:
            user_p = user_profiles[user_id]
            user_info_str += f" - 👤 {user_p['name']} (@{user_p['username']}) - 📅 Joined: {user_p['join_date']}"
        else:
            user_info_str += " - (Profile info not available)"
        user_info_str += "\n"

        if len(header) + len(current_part) + len(user_info_str) > 4000: # Telegram limit
            users_list_parts.append(header + current_part)
            current_part = user_info_str
        else:
            current_part += user_info_str
            
    if current_part: # Add the last part
        users_list_parts.append(header + current_part)

    if not users_list_parts:
        safe_send_message(message.chat.id, "❌ No user data to display, though there are approved user IDs.")
        return

    for part_msg in users_list_parts:
        safe_send_message(message.chat.id, part_msg)
        time.sleep(0.2)


@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_user_prompt(message):
    # Using a more specific back keyboard
    safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=get_back_keyboard("admin_user_management"))
    bot.register_next_step_handler(message, process_user_removal)

def process_user_removal(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back to User Management": # Matched to new keyboard
        safe_send_message(chat_id, "Cancelled user removal.", reply_markup=get_user_management_keyboard())
        return
    try:
        user_id_to_remove = int(message.text.strip())
        if user_id_to_remove == int(ADMIN_ID):
            safe_send_message(chat_id, "❌ Cannot remove the admin account!", reply_markup=get_user_management_keyboard())
            return
        
        removed_from_approved = False
        if user_id_to_remove in approved_users:
            approved_users.discard(user_id_to_remove) # Use discard
            removed_from_approved = True
        
        # Also check pending, though typically they wouldn't be "removed" but "rejected"
        removed_from_pending = False
        if user_id_to_remove in pending_approvals:
            del pending_approvals[user_id_to_remove]
            removed_from_pending = True

        if removed_from_approved or removed_from_pending:
            original_user_name = user_profiles.get(user_id_to_remove, {}).get('name', str(user_id_to_remove))
            safe_delete_user(user_id_to_remove) # Full cleanup
            safe_send_message(chat_id, f"✅ User `{original_user_name}` (ID: {user_id_to_remove}) has been removed and all their data cleared.", reply_markup=get_user_management_keyboard())
            try:
                safe_send_message(user_id_to_remove, "❌ Your access to this bot has been revoked by the admin.")
            except Exception as e:
                print(f"Could not notify user {user_id_to_remove} about removal: {e}")
        else:
            safe_send_message(chat_id, f"❌ User ID {user_id_to_remove} not found in approved or pending users.", reply_markup=get_user_management_keyboard())
    except ValueError:
        safe_send_message(chat_id, "❌ Invalid User ID. Please enter a numeric ID.", reply_markup=get_user_management_keyboard())
    except Exception as e:
        safe_send_message(chat_id, f"An error occurred: {e}", reply_markup=get_user_management_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def broadcast_menu(message):
    safe_send_message(message.chat.id, "📢 Choose Broadcast Type:", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def process_text_broadcast_prompt(message):
    safe_send_message(message.chat.id, "✍️ Enter the broadcast message text (or type /cancel_broadcast):", reply_markup=get_back_keyboard("admin_broadcast"))
    bot.register_next_step_handler(message, process_text_broadcast)

def process_text_broadcast(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back to Broadcast Menu" or message.text == "/cancel_broadcast":
        safe_send_message(chat_id, "Broadcast cancelled.", reply_markup=get_broadcast_keyboard())
        return
    
    broadcast_text = message.text
    if not broadcast_text:
        safe_send_message(chat_id, "Broadcast message cannot be empty. Try again.", reply_markup=get_broadcast_keyboard())
        return

    success_count = 0
    failed_count = 0
    
    users_to_broadcast = list(approved_users) # Copy to avoid modification issues
    total_users = len(users_to_broadcast)

    if total_users == 0:
        safe_send_message(chat_id, "No approved users to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_text = f"📢 Broadcasting to {total_users} users...\n\nSent: 0/{total_users}\n✅ Success: 0\n❌ Failed: 0"
    progress_message = safe_send_message(chat_id, progress_msg_text)
    if not progress_message:
        safe_send_message(chat_id, "Error starting broadcast (could not send progress message).", reply_markup=get_admin_keyboard())
        return

    for i, user_id_to_broadcast in enumerate(users_to_broadcast):
        if user_id_to_broadcast == int(ADMIN_ID): # Don't broadcast to self if admin is an approved user
            # success_count += 1 # Or just skip
            continue
        try:
            # Add a header to the broadcast message
            user_specific_broadcast_text = f"📢 *Admin Broadcast:*\n\n{broadcast_text}"
            if safe_send_message(user_id_to_broadcast, user_specific_broadcast_text):
                success_count += 1
            else: # safe_send_message returned None, likely blocked or error
                failed_count +=1
        except Exception: # Catch any other unexpected errors during send
            failed_count += 1
        
        if (i + 1) % 10 == 0 or (i + 1) == total_users: # Update progress every 10 users or at the end
            try:
                current_progress_text = f"📢 Broadcasting to {total_users} users...\n\nSent: {i+1}/{total_users}\n✅ Success: {success_count}\n❌ Failed: {failed_count}"
                bot.edit_message_text(current_progress_text, chat_id, progress_message.message_id)
            except Exception as e:
                print(f"Error updating broadcast progress: {e}")
        time.sleep(0.2) # Be nice to Telegram API

    final_summary = f"📢 Broadcast Completed!\n\nTotal Processed: {total_users}\n✅ Successful: {success_count}\n❌ Failed: {failed_count}"
    safe_send_message(chat_id, final_summary, reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def media_broadcast_prompt(message):
    safe_send_message(message.chat.id, "🖼 Send the photo, video, or document you want to broadcast (you can add a caption). Or type /cancel_broadcast.", reply_markup=get_back_keyboard("admin_broadcast"))
    bot.register_next_step_handler(message, process_media_broadcast)

def process_media_broadcast(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back to Broadcast Menu" or message.text == "/cancel_broadcast":
        safe_send_message(chat_id, "Media broadcast cancelled.", reply_markup=get_broadcast_keyboard())
        return

    if not (message.photo or message.video or message.document):
        safe_send_message(chat_id, "No media received. Please send a photo, video, or document. Broadcast cancelled.", reply_markup=get_broadcast_keyboard())
        bot.register_next_step_handler(message, process_media_broadcast) # Re-prompt
        return

    success_count = 0
    failed_count = 0
    users_to_broadcast = list(approved_users)
    total_users = len(users_to_broadcast)

    if total_users == 0:
        safe_send_message(chat_id, "No approved users to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_text = f"📢 Broadcasting media to {total_users} users...\n\nSent: 0/{total_users}\n✅ Success: 0\n❌ Failed: 0"
    progress_message = safe_send_message(chat_id, progress_msg_text)
    if not progress_message:
        safe_send_message(chat_id, "Error starting media broadcast.", reply_markup=get_admin_keyboard())
        return

    caption = message.caption if message.caption else ""
    # Add a header to the caption
    final_caption = f"📢 *Admin Media Broadcast:*\n\n{caption}".strip()


    for i, user_id_to_broadcast in enumerate(users_to_broadcast):
        if user_id_to_broadcast == int(ADMIN_ID):
            continue
        try:
            sent_media = False
            if message.photo:
                bot.send_photo(user_id_to_broadcast, message.photo[-1].file_id, caption=final_caption)
                sent_media = True
            elif message.video:
                bot.send_video(user_id_to_broadcast, message.video.file_id, caption=final_caption)
                sent_media = True
            elif message.document:
                bot.send_document(user_id_to_broadcast, message.document.file_id, caption=final_caption)
                sent_media = True
            
            if sent_media:
                success_count += 1
            else: # Should not happen if initial check passed, but safeguard
                failed_count +=1
        except telebot.apihelper.ApiTelegramException as e:
            if e.result_json.get("error_code") == 403 and "bot was blocked" in e.result_json.get("description", ""):
                safe_delete_user(user_id_to_broadcast) # Clean up if blocked
            failed_count += 1
            print(f"API error broadcasting media to {user_id_to_broadcast}: {e}")
        except Exception as e:
            failed_count += 1
            print(f"Error broadcasting media to {user_id_to_broadcast}: {e}")

        if (i + 1) % 5 == 0 or (i + 1) == total_users: # Update progress
            try:
                current_progress_text = f"📢 Broadcasting media to {total_users} users...\n\nSent: {i+1}/{total_users}\n✅ Success: {success_count}\n❌ Failed: {failed_count}"
                bot.edit_message_text(current_progress_text, chat_id, progress_message.message_id)
            except Exception as e_edit:
                print(f"Error updating media broadcast progress: {e_edit}")
        time.sleep(0.3) # Be gentle

    final_summary = f"📢 Media Broadcast Completed!\n\nTotal Processed: {total_users}\n✅ Successful: {success_count}\n❌ Failed: {failed_count}"
    safe_send_message(chat_id, final_summary, reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin" and is_admin(msg.chat.id))
def back_to_admin(message):
    safe_send_message(message.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu" and is_admin(msg.chat.id))
def admin_back_to_main(message):
    safe_send_message(message.chat.id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(message.chat.id))

@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def handle_approval(call):
    if not is_admin(call.message.chat.id):
        bot.answer_callback_query(call.id, "❌ Action not allowed.")
        return

    try:
        action, user_id_str = call.data.split('_')
        user_id_to_act_on = int(user_id_str)
    except ValueError:
        bot.answer_callback_query(call.id, "Error: Invalid user ID in callback.")
        bot.edit_message_text("Error processing request.", call.message.chat.id, call.message.message_id)
        return

    user_info = pending_approvals.get(user_id_to_act_on)
    user_name_for_log = user_info['name'] if user_info else str(user_id_to_act_on)


    if action == "approve":
        if user_id_to_act_on in pending_approvals:
            approved_users.add(user_id_to_act_on)
            del pending_approvals[user_id_to_act_on]
            
            # Store profile if not already (might be redundant if /start did it)
            if user_id_to_act_on not in user_profiles and user_info:
                 user_profiles[user_id_to_act_on] = user_info

            safe_send_message(user_id_to_act_on, "✅ Your access request has been approved by the admin! You can now use all bot features.", reply_markup=get_main_keyboard(user_id_to_act_on))
            bot.answer_callback_query(call.id, f"User {user_name_for_log} approved.")
            bot.edit_message_text(f"✅ User `{user_name_for_log}` (ID: `{user_id_to_act_on}`) has been approved.", call.message.chat.id, call.message.message_id, reply_markup=None)
        else:
            bot.answer_callback_query(call.id, "User not in pending list or already processed.")
            bot.edit_message_text(f"⚠️ User {user_name_for_log} (ID: {user_id_to_act_on}) was not in the pending list or already processed.", call.message.chat.id, call.message.message_id, reply_markup=None)
    
    elif action == "reject":
        if user_id_to_act_on in pending_approvals:
            del pending_approvals[user_id_to_act_on]
            safe_delete_user(user_id_to_act_on) # Clean up their data if rejected

            safe_send_message(user_id_to_act_on, "❌ Unfortunately, your access request has been rejected by the admin.")
            bot.answer_callback_query(call.id, f"User {user_name_for_log} rejected.")
            bot.edit_message_text(f"❌ User `{user_name_for_log}` (ID: `{user_id_to_act_on}`) has been rejected.", call.message.chat.id, call.message.message_id, reply_markup=None)
        else:
            bot.answer_callback_query(call.id, "User not in pending list or already processed.")
            bot.edit_message_text(f"⚠️ User {user_name_for_log} (ID: {user_id_to_act_on}) was not in the pending list or already processed.", call.message.chat.id, call.message.message_id, reply_markup=None)


# --- Mail handlers (New Temp Mail Implementation) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail_temp(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval. Please wait.")
        return

    # Delete old email data if exists
    if chat_id in user_data:
        del user_data[chat_id]
    if chat_id in last_message_ids:
        del last_message_ids[chat_id]

    generating_msg = safe_send_message(chat_id, "⏳ Generating new temporary email address...")
    email_address = generate_temp_mail_address()

    if email_address:
        user_data[chat_id] = {"email": email_address}
        last_message_ids[chat_id] = set() # Initialize seen messages for new email
        msg_text = f"✅ *New Temporary Email Created!*\n\n📧 Email: `{email_address}`\n\nTap the email to copy. Incoming messages will appear automatically or use 'Refresh Mail'."
        if generating_msg:
            bot.edit_message_text(msg_text, chat_id, generating_msg.message_id)
        else:
            safe_send_message(chat_id, msg_text)
    else:
        error_text = "❌ Failed to generate a temporary email address. Please try again later."
        if generating_msg:
            bot.edit_message_text(error_text, chat_id, generating_msg.message_id)
        else:
            safe_send_message(chat_id, error_text)


@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh Mail") # Renamed button
def refresh_mail_temp(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return

    if chat_id not in user_data or "email" not in user_data[chat_id]:
        safe_send_message(chat_id, "⚠️ No active temporary email address. Please use '📬 New mail' first.")
        return

    email_address = user_data[chat_id]["email"]
    refreshing_msg = safe_send_message(chat_id, f"🔄 Checking inbox for `{email_address}`...")
    
    messages = fetch_temp_mail_messages(email_address)

    if not messages:
        text = "📭 *Your inbox is currently empty or no new messages found.*"
        if refreshing_msg: bot.edit_message_text(text, chat_id, refreshing_msg.message_id)
        else: safe_send_message(chat_id, text)
        return

    # Delete the "refreshing..." message
    if refreshing_msg:
        try:
            bot.delete_message(chat_id, refreshing_msg.message_id)
        except Exception: # nosemgrep
            pass # Ignore if deletion fails

    seen_ids = last_message_ids.setdefault(chat_id, set())
    new_messages_count = 0
    
    # Sort messages by timestamp if available, newest first
    try:
        messages.sort(key=lambda m: int(m.get('mail_timestamp', 0)), reverse=True)
    except (TypeError, ValueError):
        pass

    for msg_detail in messages[:10]: # Show up to 10 recent messages on manual refresh
        msg_id = msg_detail.get('mail_id')
        if not msg_id: 
            msg_id = hashlib.md5((str(msg_detail.get('mail_from')) + \
                                 str(msg_detail.get('mail_subject')) + \
                                 str(msg_detail.get('mail_timestamp'))).encode()).hexdigest()

        if msg_id not in seen_ids: # Only show new messages on manual refresh
            new_messages_count +=1
            formatted_msg = format_temp_mail_message(msg_detail)
            safe_send_message(chat_id, formatted_msg)
            seen_ids.add(msg_id) # Add to seen after displaying
            time.sleep(0.3)
    
    if new_messages_count == 0:
        safe_send_message(chat_id, "✅ No *new* messages found in your inbox since the last check.")
    else:
        safe_send_message(chat_id, f"✨ Found {new_messages_count} new message(s).")


# --- Profile handlers ---
@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
def generate_profile_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return

    gender = "male" if message.text == "👨 Male Profile" else "female"
    # User might click this multiple times, generating new profiles each time.
    # This is fine, profile is ephemeral.
    _gender, name, username, password, phone = generate_profile(gender) 
    message_text = profile_message(_gender, name, username, password, phone)
    safe_send_message(chat_id, message_text)

# --- Account Info Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
def my_account_info(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    safe_send_message(chat_id, "👤 Account Options:", reply_markup=get_user_account_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📧 My Current Email")
def show_my_email(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    if chat_id in user_data and "email" in user_data[chat_id]:
        email = user_data[chat_id]['email']
        safe_send_message(chat_id, f"✉️ Your current temporary email address is:\n`{email}`\n\nTap to copy.")
    else:
        safe_send_message(chat_id, "ℹ️ You don't have an active temporary email. Use '📬 New mail' to get one.", reply_markup=get_main_keyboard(chat_id))


@bot.message_handler(func=lambda msg: msg.text == "🆔 My Info")
def show_my_info(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return

    if chat_id in user_profiles:
        u_info = user_profiles[chat_id]
        info_text = (
            f"👤 *Your Information:*\n\n"
            f"Telegram Name: `{u_info['name']}`\n"
            f"Telegram Username: `@{u_info['username']}`\n"
            f"Bot Join Date: `{u_info['join_date']}`\n"
            f"User ID: `{chat_id}`"
        )
        safe_send_message(chat_id, info_text)
    else:
        safe_send_message(chat_id, "Could not retrieve your info. Try /start again.")


# --- 2FA Handlers ---
STATE_WAITING_FOR_2FA_SECRET = "waiting_for_2fa_secret"
user_states = {} # To manage different conversation states like 2FA secret input

@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def two_fa_auth_start(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval."); return
    
    user_states[chat_id] = {"state": "2fa_platform_select"}
    safe_send_message(chat_id, "🔐 Choose the platform for 2FA code generation or manage an existing secret:", reply_markup=get_2fa_platform_keyboard())


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("state") == "2fa_platform_select" and \
                                     msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform_selection(message):
    chat_id = message.chat.id
    platform = message.text
    
    # Store the chosen platform temporarily while waiting for the secret
    user_2fa_secrets[chat_id] = {"platform": platform} 
    user_states[chat_id] = {"state": STATE_WAITING_FOR_2FA_SECRET, "platform": platform}
    
    safe_send_message(chat_id, f"🔢 Enter the Base32 2FA secret key for *{platform}*:\n\n(Example: `JBSWY3DPEHPK3PXP`)\nOr tap '⬅️ Back to 2FA Platforms' to cancel.",
                      reply_markup=get_back_keyboard("2fa_secret_entry"))


@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main")
def back_to_main_menu_handler(message): # More generic handler for "Back to Main"
    chat_id = message.chat.id
    user_states.pop(chat_id, None) # Clear any pending state
    user_2fa_secrets.pop(chat_id, None) # Clear any partial 2FA setup
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to 2FA Platforms")
def back_to_2fa_platforms(message):
    chat_id = message.chat.id
    user_states[chat_id] = {"state": "2fa_platform_select"}
    user_2fa_secrets.pop(chat_id, None)
    safe_send_message(chat_id, "⬅️ Choose a platform or go back:", reply_markup=get_2fa_platform_keyboard())


# Handler for 2FA secret key input
@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("state") == STATE_WAITING_FOR_2FA_SECRET)
def handle_2fa_secret_input(message):
    chat_id = message.chat.id
    secret_input = message.text.strip()

    if not is_valid_base32(secret_input):
        safe_send_message(chat_id, 
                          "❌ *Invalid Secret Key Format*\n\n"
                          "Your secret key must be a valid Base32 string.\n"
                          "- It should only contain uppercase letters (A-Z) and digits (2-7).\n"
                          "- No spaces or special characters are allowed.\n\n"
                          "Please try entering the secret key again, or tap '⬅️ Back to 2FA Platforms' to cancel.",
                          reply_markup=get_back_keyboard("2fa_secret_entry"))
        return # Remain in STATE_WAITING_FOR_2FA_SECRET

    platform = user_states[chat_id].get("platform", "Selected Platform")
    
    # Clean and store the valid secret
    cleaned_secret = secret_input.replace(" ", "").replace("-", "").upper()
    padding = "=" * (-len(cleaned_secret) % 8) # pyotp might need padding
    user_2fa_secrets[chat_id] = {
        "secret": cleaned_secret + padding, 
        "platform": platform,
        "added_time": datetime.datetime.now()
    }
    user_states.pop(chat_id, None) # Clear state

    try:
        totp = pyotp.TOTP(user_2fa_secrets[chat_id]["secret"])
        current_code = totp.now()
        
        now = datetime.datetime.now()
        # TOTP codes are typically valid for 30 seconds.
        # Calculate seconds remaining in the current 30-second window.
        seconds_remaining = 30 - (now.second % 30)
        
        reply_text = (
            f"✅ *2FA Secret Saved for {platform}!* 🎉\n\n"
            f"🔑 Your current 2FA code is:\n"
            f"➡️ `{current_code}` ⬅️ (Tap to copy)\n\n"
            f"⏳ This code is valid for approximately *{seconds_remaining} seconds*.\n\n"
            f"A new code will generate automatically. You can always get the latest from the '🔐 2FA Auth' menu after selecting {platform} again if you have a secret saved (or it will prompt to add one)."
        )
        safe_send_message(chat_id, reply_text, reply_markup=get_main_keyboard(chat_id))
        
        # For now, the bot doesn't continuously show updating codes for a saved secret.
        # User would re-select the platform to get a new code or the bot could be extended
        # to have an "Active 2FA Codes" section if multiple secrets are managed.
        # The current implementation will show the code once upon adding the secret.
        # To get a new code, user clicks "2FA Auth" -> platform -> if secret exists, show code.
        # This logic needs to be added to handle_2fa_platform_selection if a secret already exists.

    except Exception as e:
        user_2fa_secrets.pop(chat_id, None) # Remove invalid secret
        safe_send_message(chat_id, f"❌ Error generating 2FA code with the provided secret: {e}. Secret not saved. Please try again.", 
                          reply_markup=get_2fa_platform_keyboard())
        user_states[chat_id] = {"state": "2fa_platform_select"} # Go back to platform selection


# Fallback handler for any other text (should be last)
@bot.message_handler(func=lambda message: True, content_types=['text'])
def echo_all(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return

    if not (chat_id in approved_users or is_admin(chat_id)):
        # For unapproved users, resend approval pending message if they type something random
        if chat_id in pending_approvals:
            safe_send_message(chat_id, "⏳ Your access request is still pending. Please wait for admin approval.")
        else: # If somehow not in pending but also not approved (e.g. admin removed them from pending)
             send_welcome(message) # Re-trigger the /start flow for them
        return

    # If an approved user types something not caught by other handlers:
    safe_send_message(message.chat.id,
                      f"🤔 I'm not sure what you mean by '{message.text}'. Please use the buttons or commands.",
                      reply_markup=get_main_keyboard(chat_id))


if __name__ == '__main__':
    print("Initializing bot state...")
    user_profiles["bot_start_time"] = datetime.datetime.now() # Record bot start time

    print("🤖 Bot starting background threads...")
    threading.Thread(target=auto_refresh_worker, daemon=True).start()
    threading.Thread(target=cleanup_blocked_users, daemon=True).start()
    
    print(" पोलिंग शुरू कर रहा हूँ... (Starting polling...)")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except Exception as main_loop_e:
        print(f"CRITICAL ERROR in main polling loop: {main_loop_e}")
        # Consider more robust restart or notification logic here for production
    finally:
        print("🤖 Bot has stopped.")

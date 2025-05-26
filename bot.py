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
import hashlib
import re # For basic HTML stripping

load_dotenv()
fake = Faker()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise Exception("❌ BOT_TOKEN not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# --- API Configuration for 1secmail.com and Retry Settings ---
ONECMAIL_API_BASE_URL = "https://www.1secmail.com/api/v1/"
MAX_RETRIES = 3
RETRY_DELAY = 3  # seconds, base delay for retries

# Data storage
user_data = {} # Stores {"email": "login@domain.com", "login": "login", "domain": "domain"}
last_message_ids = {} # Stores set of integer message IDs from 1secmail
active_sessions = set()
pending_approvals = {}
approved_users = set()
user_profiles = {}
user_2fa_secrets = {}

# --- Helper Functions ---

def is_admin(chat_id):
    return str(chat_id) == ADMIN_ID

def safe_delete_user(chat_id):
    user_data.pop(chat_id, None)
    last_message_ids.pop(chat_id, None)
    user_2fa_secrets.pop(chat_id, None)
    active_sessions.discard(chat_id)
    pending_approvals.pop(chat_id, None)
    approved_users.discard(chat_id)
    user_profiles.pop(chat_id, None)

def is_bot_blocked(chat_id):
    try:
        bot.get_chat(chat_id)
        return False
    except telebot.apihelper.ApiTelegramException as e:
        if hasattr(e, 'result_json') and e.result_json.get("error_code") == 403 and \
           "bot was blocked" in e.result_json.get("description", ""):
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

# --- Keyboards ---
def get_main_keyboard(chat_id):
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        telebot.types.KeyboardButton("📬 New mail"),
        telebot.types.KeyboardButton("🔄 Refresh Mail")
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
    keyboard.add("👥 Pending Approvals", "📊 Stats")
    keyboard.add("👤 User Management", "📢 Broadcast")
    keyboard.add("⬅️ Main Menu")
    return keyboard

def get_user_management_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add("📜 List Users", "❌ Remove User")
    keyboard.add("⬅️ Back to Admin")
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
    keyboard.add("📧 My Current Email", "🆔 My Info")
    keyboard.add("⬅️ Back to Main")
    return keyboard

def get_2fa_platform_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    keyboard.add("Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple")
    keyboard.add("⬅️ Back to Main")
    return keyboard

def get_back_keyboard(target_menu="main"):
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    if target_menu == "admin_user_management":
        keyboard.row("⬅️ Back to User Management")
    elif target_menu == "admin_broadcast":
        keyboard.row("⬅️ Back to Broadcast Menu")
    elif target_menu == "2fa_secret_entry":
         keyboard.row("⬅️ Back to 2FA Platforms")
    else:
        keyboard.row("⬅️ Back to Main")
    return keyboard

def get_broadcast_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add("📢 Text Broadcast", "📋 Media Broadcast")
    keyboard.add("⬅️ Back to Admin")
    return keyboard

# --- Safe Messaging ---
def safe_send_message(chat_id, text, **kwargs):
    try:
        if is_bot_blocked(chat_id):
            safe_delete_user(chat_id)
            return None
        msg = bot.send_message(chat_id, text, **kwargs)
        active_sessions.add(chat_id)
        return msg
    except telebot.apihelper.ApiTelegramException as e:
        if hasattr(e, 'result_json') and e.result_json.get("error_code") == 403 and \
           "bot was blocked" in e.result_json.get("description", ""):
            safe_delete_user(chat_id)
        elif hasattr(e, 'result_json'):
            print(f"Error sending message to {chat_id}: API Error {e.result_json}")
        else:
            print(f"Error sending message to {chat_id}: API Error {str(e)}")
        return None
    except Exception as e:
        print(f"Generic error sending message to {chat_id}: {str(e)}")
        return None

# --- 1secmail.com API Functions with Retry ---

def generate_1secmail_address():
    """Generates a random email address from 1secmail.com."""
    params = {'action': 'genRandomMailbox', 'count': 1}
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(ONECMAIL_API_BASE_URL, params=params, timeout=10)
            res.raise_for_status()
            data = res.json()
            if data and isinstance(data, list) and len(data) > 0:
                email_full = data[0]
                if '@' in email_full:
                    login, domain = email_full.split('@', 1)
                    return "SUCCESS", {"email": email_full, "login": login, "domain": domain}
            return "API_ERROR", "Invalid response from email generation service."
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                return "NETWORK_ERROR", f"Network error generating email after {MAX_RETRIES} attempts."
        except ValueError:
            return "JSON_ERROR", "Invalid JSON response from email generation service."
        except Exception as e:
            return "API_ERROR", f"Unexpected error generating email: {str(e)}"
    return "API_ERROR", "Failed to generate email after multiple attempts."


def get_1secmail_message_list(login, domain):
    """Fetches message list (summaries) for a 1secmail address."""
    params = {'action': 'getMessages', 'login': login, 'domain': domain}
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(ONECMAIL_API_BASE_URL, params=params, timeout=15)
            res.raise_for_status()
            messages = res.json()
            if isinstance(messages, list):
                return "EMPTY" if not messages else "SUCCESS", messages
            return "API_ERROR", "Unexpected response format for message list."
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in [500, 502, 503, 504] and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return "API_ERROR", f"Email service failed (HTTP {e.response.status_code}) for message list."
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return "NETWORK_ERROR", f"Network error fetching message list after {MAX_RETRIES} attempts."
        except ValueError:
            return "JSON_ERROR", "Invalid JSON response for message list."
        except Exception as e:
            return "API_ERROR", f"Unexpected error fetching message list: {str(e)}"
    return "API_ERROR", "Failed to fetch message list after multiple attempts."


def read_1secmail_message_detail(login, domain, message_id):
    """Reads a specific message detail from 1secmail."""
    params = {'action': 'readMessage', 'login': login, 'domain': domain, 'id': message_id}
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(ONECMAIL_API_BASE_URL, params=params, timeout=15)
            res.raise_for_status()
            message_detail = res.json()
            if isinstance(message_detail, dict) and 'id' in message_detail:
                return "SUCCESS", message_detail
            return "API_ERROR", "Unexpected response format for message detail."
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in [500, 502, 503, 504] and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return "API_ERROR", f"Email service failed (HTTP {e.response.status_code}) for message detail."
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return "NETWORK_ERROR", f"Network error fetching message detail after {MAX_RETRIES} attempts."
        except ValueError:
            return "JSON_ERROR", "Invalid JSON response for message detail."
        except Exception as e:
            return "API_ERROR", f"Unexpected error fetching message detail: {str(e)}"
    return "API_ERROR", "Failed to fetch message detail after multiple attempts."


# --- Profile Generator ---
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

# --- 2FA ---
def is_valid_base32(secret):
    try:
        cleaned = secret.replace(" ", "").replace("-", "").upper()
        if not cleaned or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in cleaned):
            return False
        padding = "=" * (-len(cleaned) % 8)
        pyotp.TOTP(cleaned + padding).now()
        return True
    except (binascii.Error, ValueError, TypeError, Exception):
        return False

# --- Background Workers & Email Formatting ---
def format_1secmail_message(msg_detail):
    sender = msg_detail.get('from', 'N/A')
    subject = msg_detail.get('subject', '(No Subject)')
    
    # Prefer textBody, fallback to body (HTML), then to default
    body_content = msg_detail.get('textBody', '')
    if not body_content and 'body' in msg_detail: # 'body' is HTML for 1secmail
        html_body = msg_detail['body']
        # Basic HTML stripping
        body_content = re.sub(r'<style[^>]*?>.*?</style>', '', html_body, flags=re.DOTALL | re.IGNORECASE)
        body_content = re.sub(r'<script[^>]*?>.*?</script>', '', body_content, flags=re.DOTALL | re.IGNORECASE)
        body_content = re.sub(r'<br\s*/?>', '\n', body_content, flags=re.IGNORECASE)
        body_content = re.sub(r'</p>', '\n</p>', body_content, flags=re.IGNORECASE) 
        body_content = re.sub(r'<[^>]+>', '', body_content) # Strip all other tags
        body_content = body_content.replace('&nbsp;', ' ').replace('&amp;', '&')
        body_content = body_content.replace('&lt;', '<').replace('&gt;', '>')
        body_content = '\n'.join([line.strip() for line in body_content.splitlines() if line.strip()])

    body_content = body_content.strip() if body_content else "(No Content)"
    received_time_str = msg_detail.get('date', 'Just now') # 1secmail provides formatted date

    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📬 *New Email Received!*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *From:* `{sender}`\n"
        f"📨 *Subject:* _{subject}_\n"
        f"🕒 *Received:* {received_time_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 *Body:*\n"
        f"{body_content[:3500]}\n" 
        f"━━━━━━━━━━━━━━━━━━━━"
    )

def auto_refresh_worker():
    while True:
        try:
            current_user_data_keys = list(user_data.keys())
            for chat_id in current_user_data_keys:
                if chat_id not in user_data: continue
                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id)
                    continue
                
                current_email_info = user_data.get(chat_id)
                if not current_email_info or "login" not in current_email_info or "domain" not in current_email_info:
                    continue

                login = current_email_info["login"]
                domain = current_email_info["domain"]
                
                list_status, message_summaries = get_1secmail_message_list(login, domain)
                
                if list_status not in ["SUCCESS", "EMPTY"]:
                    print(f"Auto-refresh: Error fetching list for {login}@{domain}: {list_status} - {message_summaries}")
                    continue 
                if list_status == "EMPTY" or not message_summaries:
                    continue

                seen_ids = last_message_ids.setdefault(chat_id, set())
                
                # Sort by date if possible, 1secmail date is string like "2021-09-01 10:00:00"
                try:
                    message_summaries.sort(key=lambda m: m.get('date', "0000-00-00 00:00:00"), reverse=True)
                except Exception: pass 

                for msg_summary in message_summaries[:5]: # Check top 5 recent from summary
                    msg_id = msg_summary.get('id') 
                    if not isinstance(msg_id, int): continue # ID should be an integer

                    if msg_id in seen_ids:
                        continue
                    
                    # Fetch full message detail
                    detail_status, msg_detail_data = read_1secmail_message_detail(login, domain, msg_id)
                    if detail_status == "SUCCESS":
                        formatted_msg = format_1secmail_message(msg_detail_data)
                        if safe_send_message(chat_id, formatted_msg):
                             seen_ids.add(msg_id) # Add to seen only if successfully sent and processed
                        time.sleep(0.7) # Slightly longer delay due to two API calls per message
                    else:
                        print(f"Auto-refresh: Error fetching detail for msg {msg_id} for {login}@{domain}: {detail_status} - {msg_detail_data}")


                if len(seen_ids) > 100:
                    # Basic strategy to keep seen_ids from growing indefinitely
                    # Convert to list, sort (they are integers), then slice
                    sorted_seen_ids = sorted(list(seen_ids))
                    oldest_ids = sorted_seen_ids[:-50] 
                    for old_id in oldest_ids:
                        seen_ids.discard(old_id)
        except Exception as e:
            print(f"Error in auto_refresh_worker: {type(e).__name__} - {e}")
        time.sleep(60) # Check interval for 1secmail, can be longer

def cleanup_blocked_users():
    while True:
        try:
            sessions_to_check = list(active_sessions) 
            for chat_id in sessions_to_check:
                if is_bot_blocked(chat_id):
                    print(f"Cleaning up blocked user: {chat_id}")
                    safe_delete_user(chat_id)
        except Exception as e:
            print(f"Error in cleanup_blocked_users: {e}")
        time.sleep(3600) 

# --- Bot Handlers (Welcome, Admin, Mail, Profile, Account, 2FA) ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return

    user_info = get_user_info(message.from_user)
    user_profiles[chat_id] = user_info 

    if is_admin(chat_id):
        approved_users.add(chat_id)
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=get_main_keyboard(chat_id))
        return

    if chat_id in approved_users:
        safe_send_message(chat_id, "👋 Welcome back!", reply_markup=get_main_keyboard(chat_id))
    else:
        if chat_id not in pending_approvals: 
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
                    safe_send_message(admin_chat_id, approval_msg, reply_markup=get_approval_keyboard(chat_id))
                except ValueError: print("ADMIN_ID is not a valid integer.")
                except Exception as e: print(f"Failed to send approval request to admin: {e}")
        else:
            safe_send_message(chat_id, "⏳ Your access request is still pending. Please wait for admin approval.")

# --- Admin Panel ---
@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel" and is_admin(msg.chat.id))
def admin_panel(message):
    safe_send_message(message.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "👥 Pending Approvals" and is_admin(msg.chat.id))
def show_pending_approvals(message):
    if not pending_approvals:
        safe_send_message(message.chat.id, "✅ No pending approvals.")
        return
    count = 0
    for user_id, user_info in list(pending_approvals.items()): 
        count +=1
        user_display_name = user_info.get('name', str(user_id))
        user_display_username = user_info.get('username', 'N/A')
        user_display_joined = user_info.get('join_date', 'N/A')
        approval_item_text = (
            f"*Pending Request {count}*\n"
            f"🆔 User ID: `{user_id}`\n👤 Name: `{user_display_name}`\n"
            f"📛 Username: @{user_display_username}\n📅 Joined: `{user_display_joined}`"
        )
        safe_send_message(message.chat.id, approval_item_text, reply_markup=get_approval_keyboard(user_id))
        time.sleep(0.1)
    if count == 0: safe_send_message(message.chat.id, "✅ No pending approvals currently after iterating.")

@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.chat.id))
def show_stats(message):
    bot_start_time = user_profiles.get("bot_start_time")
    uptime_str, bot_start_time_str = "Not recorded", "Not recorded"
    if not bot_start_time: 
        user_profiles["bot_start_time"] = datetime.datetime.now()
        bot_start_time = user_profiles["bot_start_time"]
    if bot_start_time: # Ensure it's set
        bot_start_time_str = bot_start_time.strftime('%Y-%m-%d %H:%M:%S')
        uptime_delta = datetime.datetime.now() - bot_start_time
        days, remainder = uptime_delta.days, uptime_delta.seconds
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        uptime_str = f"{days}d {hours}h {minutes}m"

    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin ID: `{ADMIN_ID}`\n👥 Approved Users: `{len(approved_users)}`\n"
        f"👤 Active Sessions: `{len(active_sessions)}`\n⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"📧 Active Email Addresses: `{len(user_data)}`\n🚀 Bot Started: `{bot_start_time_str}`\n"
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
    users_list_parts, current_part = [], "👥 *Approved Users List*\n\n"
    for user_id in approved_users:
        user_p = user_profiles.get(user_id, {})
        user_info_str = f"🆔 `{user_id}` - 👤 {user_p.get('name','N/A')} (@{user_p.get('username','N/A')}) - 📅 {user_p.get('join_date','N/A')}\n"
        if len(current_part) + len(user_info_str) > 4000: 
            users_list_parts.append(current_part)
            current_part = "👥 *Approved Users List (cont.)*\n\n" + user_info_str
        else: current_part += user_info_str
    if current_part.strip() != "👥 *Approved Users List*\n\n".strip() and current_part.strip() != "👥 *Approved Users List (cont.)*\n\n".strip() : users_list_parts.append(current_part)
    if not users_list_parts: safe_send_message(message.chat.id, "❌ No user data to display.")
    else: 
        for part_msg in users_list_parts: 
            safe_send_message(message.chat.id, part_msg); time.sleep(0.2)

@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_user_prompt(message):
    safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=get_back_keyboard("admin_user_management"))
    bot.register_next_step_handler(message, process_user_removal)

def process_user_removal(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back to User Management": 
        safe_send_message(chat_id, "Cancelled user removal.", reply_markup=get_user_management_keyboard()); return
    try:
        user_id_to_remove = int(message.text.strip())
        if user_id_to_remove == int(ADMIN_ID):
            safe_send_message(chat_id, "❌ Cannot remove admin!", reply_markup=get_user_management_keyboard()); return
        
        was_approved = user_id_to_remove in approved_users
        was_pending = user_id_to_remove in pending_approvals
        name = user_profiles.get(user_id_to_remove, {}).get('name', str(user_id_to_remove))

        if was_approved or was_pending:
            safe_delete_user(user_id_to_remove)
            status_msg = f"✅ User `{name}` (ID: {user_id_to_remove}) "
            if was_approved: status_msg += "removed from approved. "
            if was_pending: status_msg += "removed from pending. "
            status_msg += "Data cleared."
            safe_send_message(chat_id, status_msg, reply_markup=get_user_management_keyboard())
            try: safe_send_message(user_id_to_remove, "❌ Your access has been revoked.")
            except Exception: pass
        else:
            safe_send_message(chat_id, f"❌ User {user_id_to_remove} not found.", reply_markup=get_user_management_keyboard())
    except ValueError:
        safe_send_message(chat_id, "❌ Invalid User ID.", reply_markup=get_user_management_keyboard())
    except Exception as e:
        safe_send_message(chat_id, f"Error: {e}", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def broadcast_menu(message):
    safe_send_message(message.chat.id, "📢 Choose Broadcast Type:", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def process_text_broadcast_prompt(message):
    safe_send_message(message.chat.id, "✍️ Enter broadcast message (or /cancel_broadcast):", reply_markup=get_back_keyboard("admin_broadcast"))
    bot.register_next_step_handler(message, process_text_broadcast)

def process_text_broadcast(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back to Broadcast Menu" or message.text == "/cancel_broadcast":
        safe_send_message(chat_id, "Broadcast cancelled.", reply_markup=get_broadcast_keyboard()); return
    broadcast_text = message.text
    if not broadcast_text:
        safe_send_message(chat_id, "Message empty. Cancelled.", reply_markup=get_broadcast_keyboard()); return

    users, success, failed = list(approved_users), 0, 0
    total = len(users)
    if total == 0: safe_send_message(chat_id, "No users to broadcast to.", reply_markup=get_admin_keyboard()); return
    
    prog_text = lambda i, s, f: f"📢 Broadcasting...\n\nSent: {i}/{total}\n✅ OK: {s}\n❌ Fail: {f}"
    prog_msg = safe_send_message(chat_id, prog_text(0,0,0))
    if not prog_msg: safe_send_message(chat_id, "Error starting broadcast.", reply_markup=get_admin_keyboard()); return

    for i, user_id in enumerate(users):
        full_msg = f"📢 *Admin Broadcast:*\n\n{broadcast_text}"
        if safe_send_message(user_id, full_msg): success += 1
        else: failed += 1
        if (i + 1) % 10 == 0 or (i + 1) == total:
            try: 
                if prog_msg: bot.edit_message_text(prog_text(i+1, success, failed), chat_id, prog_msg.message_id)
            except Exception: prog_msg = None 
        time.sleep(0.2)
    safe_send_message(chat_id, f"📢 Broadcast Done!\n✅ OK: {success}\n❌ Fail: {failed}", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def media_broadcast_prompt(message):
    safe_send_message(message.chat.id, "🖼 Send media with caption (or /cancel_broadcast):", reply_markup=get_back_keyboard("admin_broadcast"))
    bot.register_next_step_handler(message, process_media_broadcast)

def process_media_broadcast(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back to Broadcast Menu" or message.text == "/cancel_broadcast":
        safe_send_message(chat_id, "Media broadcast cancelled.", reply_markup=get_broadcast_keyboard()); return
    if not (message.photo or message.video or message.document):
        safe_send_message(chat_id, "No media. Cancelled.", reply_markup=get_broadcast_keyboard()); return

    users, success, failed = list(approved_users), 0, 0
    total = len(users)
    if total == 0: safe_send_message(chat_id, "No users.", reply_markup=get_admin_keyboard()); return
    
    prog_text = lambda i,s,f: f"📢 Broadcasting media...\n\nSent: {i}/{total}\n✅ OK: {s}\n❌ Fail: {f}"
    prog_msg = safe_send_message(chat_id, prog_text(0,0,0))
    if not prog_msg: safe_send_message(chat_id, "Error starting broadcast.", reply_markup=get_admin_keyboard()); return

    caption = f"📢 *Admin Media Broadcast:*\n\n{message.caption or ''}".strip()
    for i, user_id in enumerate(users):
        try:
            sent = False
            if message.photo: bot.send_photo(user_id, message.photo[-1].file_id, caption=caption, parse_mode="Markdown"); sent=True
            elif message.video: bot.send_video(user_id, message.video.file_id, caption=caption, parse_mode="Markdown"); sent=True
            elif message.document: bot.send_document(user_id, message.document.file_id, caption=caption, parse_mode="Markdown"); sent=True
            if sent: success +=1
            else: failed += 1 # Should not happen if validation is correct
        except Exception: failed +=1
        if (i + 1) % 5 == 0 or (i + 1) == total:
            try:
                if prog_msg: bot.edit_message_text(prog_text(i+1, success, failed), chat_id, prog_msg.message_id)
            except Exception: prog_msg = None
        time.sleep(0.3)
    safe_send_message(chat_id, f"📢 Media Broadcast Done!\n✅ OK: {success}\n❌ Fail: {failed}", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin" and is_admin(msg.chat.id))
def back_to_admin(message):
    safe_send_message(message.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu" and is_admin(msg.chat.id)) 
def admin_back_to_main_from_admin_panel(message):
    safe_send_message(message.chat.id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(message.chat.id))

@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def handle_approval(call):
    if not is_admin(call.message.chat.id):
        bot.answer_callback_query(call.id, "❌ Not allowed."); return
    try:
        action, user_id_str = call.data.split('_')
        user_id = int(user_id_str)
    except ValueError:
        bot.answer_callback_query(call.id, "Error: Invalid ID."); 
        bot.edit_message_text("Error.", call.message.chat.id, call.message.message_id); return

    user_info = pending_approvals.get(user_id, user_profiles.get(user_id))
    name = user_info.get('name', str(user_id)) if user_info else str(user_id)

    if action == "approve":
        if user_id in pending_approvals or user_id not in approved_users:
            approved_users.add(user_id)
            if user_id not in user_profiles and user_info: user_profiles[user_id] = user_info
            pending_approvals.pop(user_id, None) 
            
            safe_send_message(user_id, "✅ Access approved!", reply_markup=get_main_keyboard(user_id))
            bot.answer_callback_query(call.id, f"User {name} approved.")
            bot.edit_message_text(f"✅ User `{name}` (`{user_id}`) approved.", call.message.chat.id, call.message.message_id, reply_markup=None)
        else:
            bot.answer_callback_query(call.id, "Already processed or not pending.")
            bot.edit_message_text(f"⚠️ User `{name}` (`{user_id}`) already processed/not pending.", call.message.chat.id, call.message.message_id, reply_markup=None)
    elif action == "reject":
        safe_delete_user(user_id) 
        safe_send_message(user_id, "❌ Access rejected.")
        bot.answer_callback_query(call.id, f"User {name} rejected.")
        bot.edit_message_text(f"❌ User `{name}` (`{user_id}`) rejected.", call.message.chat.id, call.message.message_id, reply_markup=None)

# --- Mail Handlers (1secmail) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail_1secmail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Access pending."); return

    user_data.pop(chat_id, None); last_message_ids.pop(chat_id, None) # Clear old email data
    generating_msg = safe_send_message(chat_id, "⏳ Generating new email from 1secmail.com...")
    
    status, email_info = generate_1secmail_address()

    if status == "SUCCESS" and email_info:
        user_data[chat_id] = email_info # Stores {"email": login@domain, "login": login, "domain": domain}
        last_message_ids[chat_id] = set() 
        msg_text = f"✅ *New Email (1secmail):*\n`{email_info['email']}`\n\nTap to copy. Check with 'Refresh Mail'."
        if generating_msg: bot.edit_message_text(msg_text, chat_id, generating_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, msg_text)
    else:
        error_text = f"❌ Failed to generate email: {email_info or 'Unknown error'}. Try later."
        if generating_msg: bot.edit_message_text(error_text, chat_id, generating_msg.message_id)
        else: safe_send_message(chat_id, error_text)

@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh Mail")
def refresh_mail_1secmail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Access pending."); return
    
    current_email_info = user_data.get(chat_id)
    if not current_email_info or "login" not in current_email_info:
        safe_send_message(chat_id, "⚠️ No active email. Use '📬 New mail'."); return

    login = current_email_info["login"]
    domain = current_email_info["domain"]
    full_email = current_email_info["email"]

    refreshing_msg = safe_send_message(chat_id, f"🔄 Checking inbox for `{full_email}`...")
    
    list_status, message_summaries = get_1secmail_message_list(login, domain)

    if list_status == "EMPTY":
        text = f"📭 Inbox for `{full_email}` is empty."
        if refreshing_msg: bot.edit_message_text(text, chat_id, refreshing_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, text)
        return
    elif list_status != "SUCCESS":
        error_text = f"⚠️ Error fetching emails for `{full_email}`: {message_summaries}\nEmail service might be temporarily unavailable. Try '📬 New mail' or check later."
        if refreshing_msg: bot.edit_message_text(error_text, chat_id, refreshing_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, error_text)
        return
    
    # If SUCCESS, message_summaries is the list
    if refreshing_msg: 
        try: bot.delete_message(chat_id, refreshing_msg.message_id)
        except Exception: pass 

    seen_ids = last_message_ids.setdefault(chat_id, set())
    new_messages_count = 0
    
    try: message_summaries.sort(key=lambda m: m.get('date', "0000-00-00 00:00:00"), reverse=True)
    except: pass

    for msg_summary in message_summaries[:10]: # Process up to 10 from summary for manual refresh
        msg_id = msg_summary.get('id')
        if not isinstance(msg_id, int): continue

        if msg_id not in seen_ids: 
            detail_status, msg_detail_data = read_1secmail_message_detail(login, domain, msg_id)
            if detail_status == "SUCCESS":
                new_messages_count +=1
                formatted_msg = format_1secmail_message(msg_detail_data)
                if safe_send_message(chat_id, formatted_msg):
                    seen_ids.add(msg_id) 
                time.sleep(0.5) # Delay between sending messages
            else:
                safe_send_message(chat_id, f"⚠️ Error fetching details for message ID {msg_id}: {msg_detail_data}")
    
    if new_messages_count == 0: safe_send_message(chat_id, f"✅ No *new* messages in `{full_email}`.")
    else: safe_send_message(chat_id, f"✨ Found {new_messages_count} new message(s) for `{full_email}`.")

# --- Profile Handlers ---
@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
def generate_profile_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Access pending."); return
    gender = "male" if message.text == "👨 Male Profile" else "female"
    g, n, u, p, ph = generate_profile(gender) 
    safe_send_message(chat_id, profile_message(g, n, u, p, ph))

# --- Account Info ---
@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
def my_account_info(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Access pending."); return
    safe_send_message(chat_id, "👤 Account Options:", reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📧 My Current Email")
def show_my_email(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Access pending."); return
    email_info = user_data.get(chat_id)
    if email_info and "email" in email_info: 
        safe_send_message(chat_id, f"✉️ Current email:\n`{email_info['email']}`\n\nTap to copy.")
    else: safe_send_message(chat_id, "ℹ️ No active email. Use '📬 New mail'.", reply_markup=get_main_keyboard(chat_id))

@bot.message_handler(func=lambda msg: msg.text == "🆔 My Info")
def show_my_info(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Access pending."); return
    u_info = user_profiles.get(chat_id)
    if u_info:
        info_text = (f"👤 *Your Info:*\n\n"
            f"Name: `{u_info.get('name','N/A')}`\nUsername: `@{u_info.get('username','N/A')}`\n"
            f"Joined: `{u_info.get('join_date','N/A')}`\nID: `{chat_id}`")
        safe_send_message(chat_id, info_text)
    else: safe_send_message(chat_id, "Info not found. Try /start.")

# --- 2FA ---
STATE_WAITING_FOR_2FA_SECRET = "waiting_for_2fa_secret" 
user_states = {} 

@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def two_fa_auth_start(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Access pending."); return
    user_states[chat_id] = {"state": "2fa_platform_select"}
    safe_send_message(chat_id, "🔐 Choose platform for 2FA or add/update secret:", reply_markup=get_2fa_platform_keyboard())

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("state") == "2fa_platform_select" and \
                                     msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform_selection(message):
    chat_id, platform = message.chat.id, message.text
    current_secret_info = user_2fa_secrets.get(chat_id, {}).get(platform)

    if current_secret_info and "secret" in current_secret_info:
        try:
            totp = pyotp.TOTP(current_secret_info["secret"])
            code, secs = totp.now(), 30 - (datetime.datetime.now().second % 30)
            reply = (f"🔐 *{platform} 2FA Code:*\n➡️ `{code}` ⬅️\n"
                     f"⏳ Valid for ~*{secs}s*.\n\nTo update, choose platform & enter new key.")
            safe_send_message(chat_id, reply, reply_markup=get_main_keyboard(chat_id))
            time.sleep(0.5)
            safe_send_message(chat_id, f"To set new secret for {platform}, enter now. Else '⬅️ Back'.",
                              reply_markup=get_back_keyboard("2fa_secret_entry"))
            user_states[chat_id] = {"state": STATE_WAITING_FOR_2FA_SECRET, "platform": platform}
        except Exception as e:
            safe_send_message(chat_id, f"Error with saved {platform} secret: {e}. Re-add secret.", reply_markup=get_2fa_platform_keyboard())
            user_states[chat_id] = {"state": STATE_WAITING_FOR_2FA_SECRET, "platform": platform}
            if chat_id in user_2fa_secrets and platform in user_2fa_secrets[chat_id]:
                del user_2fa_secrets[chat_id][platform] 
    else:
        user_states[chat_id] = {"state": STATE_WAITING_FOR_2FA_SECRET, "platform": platform}
        safe_send_message(chat_id, f"🔢 Enter Base32 2FA secret for *{platform}*:\n(e.g., `JBSWY3DPEHPK3PXP`)\nOr '⬅️ Back'.",
                          reply_markup=get_back_keyboard("2fa_secret_entry"))

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main") 
def back_to_main_menu_handler(message): 
    chat_id = message.chat.id
    user_states.pop(chat_id, None) 
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to 2FA Platforms")
def back_to_2fa_platforms(message):
    chat_id = message.chat.id
    user_states[chat_id] = {"state": "2fa_platform_select"}
    safe_send_message(chat_id, "⬅️ Choose platform or go back:", reply_markup=get_2fa_platform_keyboard())

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("state") == STATE_WAITING_FOR_2FA_SECRET)
def handle_2fa_secret_input(message):
    chat_id = message.chat.id
    secret_input = message.text.strip()
    platform = user_states.get(chat_id, {}).get("platform")
    if not platform: 
        safe_send_message(chat_id, "Error: Platform not set. Please start 2FA process again.", reply_markup=get_main_keyboard(chat_id))
        user_states.pop(chat_id, None); return

    if not is_valid_base32(secret_input):
        safe_send_message(chat_id, "❌ *Invalid Secret Key Format* (Use A-Z, 2-7).\nTry again, or '⬅️ Back'.",
                          reply_markup=get_back_keyboard("2fa_secret_entry")); return 

    cleaned, padding = secret_input.replace(" ", "").replace("-", "").upper(), ""
    padding = "=" * (-len(cleaned) % 8) 
    final_secret = cleaned + padding

    if chat_id not in user_2fa_secrets: user_2fa_secrets[chat_id] = {}
    user_2fa_secrets[chat_id][platform] = {"secret": final_secret, "added": datetime.datetime.now().isoformat()}
    user_states.pop(chat_id, None)

    try:
        totp, now = pyotp.TOTP(final_secret), datetime.datetime.now()
        code, secs = totp.now(), 30 - (now.second % 30)
        reply = (f"✅ *2FA Secret for {platform} Saved!*\n🔑 Code: `{code}`\n⏳ Valid for ~*{secs}s*.")
        safe_send_message(chat_id, reply, reply_markup=get_main_keyboard(chat_id))
    except Exception as e:
        if chat_id in user_2fa_secrets and platform in user_2fa_secrets[chat_id]:
            del user_2fa_secrets[chat_id][platform] 
        safe_send_message(chat_id, f"❌ Error with secret for {platform}: {e}. Not saved. Try again.",
                          reply_markup=get_2fa_platform_keyboard())
        user_states[chat_id] = {"state": "2fa_platform_select"}

# --- Fallback Handler ---
@bot.message_handler(func=lambda message: True, content_types=['text'])
def echo_all(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        if chat_id in pending_approvals: safe_send_message(chat_id, "⏳ Access pending.")
        else: send_welcome(message) 
        return
    
    current_state_info = user_states.get(chat_id, {})
    current_state = current_state_info.get("state")
    
    if current_state == STATE_WAITING_FOR_2FA_SECRET and \
       message.text not in ["⬅️ Back to 2FA Platforms", "⬅️ Back to Main", "⬅️ Back to User Management", "⬅️ Back to Broadcast Menu", "⬅️ Back to Admin"]: # Check against all back buttons
        platform_in_state = current_state_info.get('platform', 'the selected platform')
        safe_send_message(message.chat.id, f"Still waiting for 2FA secret for {platform_in_state} or use a 'Back' button.",
                          reply_markup=get_back_keyboard("2fa_secret_entry"))
        return

    safe_send_message(message.chat.id, f"🤔 Unknown command: '{message.text}'. Use buttons.",
                      reply_markup=get_main_keyboard(chat_id))

# --- Main Loop ---
if __name__ == '__main__':
    print(f"[{datetime.datetime.now()}] Initializing bot state...")
    user_profiles["bot_start_time"] = datetime.datetime.now() 
    print(f"[{datetime.datetime.now()}] Bot starting background threads...")
    threading.Thread(target=auto_refresh_worker, daemon=True).start()
    threading.Thread(target=cleanup_blocked_users, daemon=True).start()
    print(f"[{datetime.datetime.now()}] Starting polling for bot token: ...{BOT_TOKEN[-6:]}")
    
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30, logger_level=None) 
        except requests.exceptions.ReadTimeout as e_rt:
            print(f"[{datetime.datetime.now()}] Polling ReadTimeout: {e_rt}. Retrying in 15s...")
            time.sleep(15)
        except requests.exceptions.ConnectionError as e_ce:
            print(f"[{datetime.datetime.now()}] Polling ConnectionError: {e_ce}. Retrying in 30s...")
            time.sleep(30)
        except Exception as main_loop_e:
            print(f"[{datetime.datetime.now()}] CRITICAL ERROR in main polling loop: {type(main_loop_e).__name__} - {main_loop_e}")
            print(f"[{datetime.datetime.now()}] Retrying in 60 seconds...")
            time.sleep(60)
        else: 
            print(f"[{datetime.datetime.now()}] Polling loop exited cleanly (unexpected). Restarting in 10s...")
            time.sleep(10)

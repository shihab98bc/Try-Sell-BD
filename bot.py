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
RETRY_DELAY = 4  # seconds, base delay for retries, slightly increased

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

# --- 1secmail.com API Functions with Retry & Enhanced Error Reporting ---

def generate_1secmail_address():
    """Generates a random email address from 1secmail.com."""
    params = {'action': 'genRandomMailbox', 'count': 1}
    request_url_debug = f"{ONECMAIL_API_BASE_URL}?action=genRandomMailbox&count=1" # For logging

    for attempt in range(MAX_RETRIES):
        try:
            # print(f"DEBUG: Attempt {attempt+1} to generate email. URL: {request_url_debug}")
            res = requests.get(ONECMAIL_API_BASE_URL, params=params, timeout=12) # Slightly increased timeout
            res.raise_for_status()
            data = res.json()
            if data and isinstance(data, list) and len(data) > 0:
                email_full = data[0]
                if '@' in email_full:
                    login, domain = email_full.split('@', 1)
                    # print(f"DEBUG: Email generated successfully: {email_full}")
                    return "SUCCESS", {"email": email_full, "login": login, "domain": domain}
            # print(f"DEBUG: Failed to parse email from 1secmail response: {data}")
            return "API_ERROR", "Invalid response from email generation service (1secmail)."
        except requests.exceptions.HTTPError as e: # Non-2xx status codes
            # print(f"DEBUG: HTTP error during email generation (attempt {attempt+1}): {e.response.status_code} - {e.response.text[:200]}")
            if e.response.status_code in [500, 502, 503, 504] and attempt < MAX_RETRIES - 1: # Server-side errors, retry
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            else: # Client-side error or max retries for server error
                 return "API_ERROR", f"Email service (1secmail) returned HTTP {e.response.status_code}."
        except requests.exceptions.RequestException as e: # Base class for connection errors, timeouts
            # print(f"DEBUG: Network error during email generation (attempt {attempt+1}): {type(e).__name__} - {e}. URL: {request_url_debug}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1)) # Exponential backoff
            else:
                # This is the final error message source
                return "NETWORK_ERROR", (f"Network error generating email from 1secmail after {MAX_RETRIES} attempts. "
                                         f"Please check your internet connection, firewall, or DNS. The service at {ONECMAIL_API_BASE_URL} might be unreachable from your location.")
        except ValueError: # JSONDecodeError
            # print(f"DEBUG: Invalid JSON response from 1secmail email generation.")
            return "JSON_ERROR", "Invalid JSON response from email generation service (1secmail)."
        except Exception as e: # Other unexpected errors
            # print(f"DEBUG: Unexpected error during email generation: {type(e).__name__} - {e}")
            return "API_ERROR", f"Unexpected error generating email (1secmail): {str(e)}"
    return "API_ERROR", "Failed to generate email from 1secmail after multiple attempts (unknown loop exit)."


def get_1secmail_message_list(login, domain):
    """Fetches message list (summaries) for a 1secmail address."""
    params = {'action': 'getMessages', 'login': login, 'domain': domain}
    request_url_debug = f"{ONECMAIL_API_BASE_URL}?action=getMessages&login={login}&domain={domain}"

    for attempt in range(MAX_RETRIES):
        try:
            # print(f"DEBUG: Attempt {attempt+1} to get message list for {login}@{domain}. URL: {request_url_debug}")
            res = requests.get(ONECMAIL_API_BASE_URL, params=params, timeout=15)
            res.raise_for_status()
            messages = res.json()
            if isinstance(messages, list):
                return "EMPTY" if not messages else "SUCCESS", messages
            # print(f"DEBUG: Unexpected response type for message list from 1secmail: {type(messages)}")
            return "API_ERROR", "Unexpected response format for message list (1secmail)."
        except requests.exceptions.HTTPError as e:
            # print(f"DEBUG: HTTP error getting message list (attempt {attempt+1}): {e.response.status_code} - {e.response.text[:200]}")
            if e.response.status_code in [500, 502, 503, 504] and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return "API_ERROR", f"Email service (1secmail) returned HTTP {e.response.status_code} for message list."
        except requests.exceptions.RequestException as e:
            # print(f"DEBUG: Network error getting message list (attempt {attempt+1}): {type(e).__name__} - {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                return "NETWORK_ERROR", f"Network error fetching message list from 1secmail after {MAX_RETRIES} attempts. Check network/firewall."
        except ValueError:
            # print(f"DEBUG: Invalid JSON for 1secmail message list.")
            return "JSON_ERROR", "Invalid JSON response for message list (1secmail)."
        except Exception as e:
            # print(f"DEBUG: Unexpected error fetching 1secmail message list: {type(e).__name__} - {e}")
            return "API_ERROR", f"Unexpected error fetching message list (1secmail): {str(e)}"
    return "API_ERROR", "Failed to fetch message list from 1secmail after multiple attempts."


def read_1secmail_message_detail(login, domain, message_id):
    """Reads a specific message detail from 1secmail."""
    params = {'action': 'readMessage', 'login': login, 'domain': domain, 'id': message_id}
    request_url_debug = f"{ONECMAIL_API_BASE_URL}?action=readMessage&login={login}&domain={domain}&id={message_id}"
    for attempt in range(MAX_RETRIES):
        try:
            # print(f"DEBUG: Attempt {attempt+1} to read message detail for ID {message_id} at {login}@{domain}. URL: {request_url_debug}")
            res = requests.get(ONECMAIL_API_BASE_URL, params=params, timeout=15)
            res.raise_for_status()
            message_detail = res.json()
            if isinstance(message_detail, dict) and 'id' in message_detail:
                return "SUCCESS", message_detail
            # print(f"DEBUG: Unexpected response for 1secmail message detail: {message_detail}")
            return "API_ERROR", "Unexpected response format for message detail (1secmail)."
        except requests.exceptions.HTTPError as e:
            # print(f"DEBUG: HTTP error reading message detail (attempt {attempt+1}): {e.response.status_code} - {e.response.text[:200]}")
            if e.response.status_code in [500, 502, 503, 504] and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return "API_ERROR", f"Email service (1secmail) returned HTTP {e.response.status_code} for message detail."
        except requests.exceptions.RequestException as e:
            # print(f"DEBUG: Network error reading message detail (attempt {attempt+1}): {type(e).__name__} - {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                return "NETWORK_ERROR", f"Network error fetching message detail from 1secmail after {MAX_RETRIES} attempts. Check network/firewall."
        except ValueError:
            # print(f"DEBUG: Invalid JSON for 1secmail message detail.")
            return "JSON_ERROR", "Invalid JSON response for message detail (1secmail)."
        except Exception as e:
            # print(f"DEBUG: Unexpected error reading 1secmail message detail: {type(e).__name__} - {e}")
            return "API_ERROR", f"Unexpected error fetching message detail (1secmail): {str(e)}"
    return "API_ERROR", "Failed to fetch message detail from 1secmail after multiple attempts."


# --- Profile Generator ---
# (No changes needed in this section)
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
        f"🧑‍💼 *Name:* `{name}`\n🆔 *Username:* `{username}`\n"
        f"🔑 *Password:* `{password}`\n📞 *Phone:* `{phone}`\n\n"
        f"✅ Tap on any value to copy"
    )

# --- 2FA ---
# (No changes needed in this section)
def is_valid_base32(secret):
    try:
        cleaned = secret.replace(" ", "").replace("-", "").upper()
        if not cleaned or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in cleaned): return False
        padding = "=" * (-len(cleaned) % 8); pyotp.TOTP(cleaned + padding).now(); return True
    except: return False

# --- Background Workers & Email Formatting ---
def format_1secmail_message(msg_detail): # Renamed and adapted
    sender = msg_detail.get('from', 'N/A')
    subject = msg_detail.get('subject', '(No Subject)')
    body_content = msg_detail.get('textBody', '')
    if not body_content and 'body' in msg_detail: # 'body' is HTML for 1secmail
        html_body = msg_detail['body']
        body_content = re.sub(r'<style[^>]*?>.*?</style>', '', html_body, flags=re.DOTALL | re.IGNORECASE)
        body_content = re.sub(r'<script[^>]*?>.*?</script>', '', body_content, flags=re.DOTALL | re.IGNORECASE)
        body_content = re.sub(r'<br\s*/?>', '\n', body_content, flags=re.IGNORECASE)
        body_content = re.sub(r'</p>', '\n</p>', body_content, flags=re.IGNORECASE) 
        body_content = re.sub(r'<[^>]+>', '', body_content)
        body_content = body_content.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        body_content = '\n'.join([line.strip() for line in body_content.splitlines() if line.strip()])
    body_content = body_content.strip() if body_content else "(No Content)"
    received_time_str = msg_detail.get('date', 'Just now')

    return (
        f"━━━━━━━━━━━━━━━━━━━━\n📬 *New Email Received!*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *From:* `{sender}`\n📨 *Subject:* _{subject}_\n🕒 *Received:* {received_time_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n💬 *Body:*\n{body_content[:3500]}\n━━━━━━━━━━━━━━━━━━━━"
    )

def auto_refresh_worker():
    while True:
        try:
            current_user_data_keys = list(user_data.keys())
            for chat_id in current_user_data_keys:
                if chat_id not in user_data: continue
                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id); continue
                current_email_info = user_data.get(chat_id)
                if not current_email_info or "login" not in current_email_info: continue

                login, domain = current_email_info["login"], current_email_info["domain"]
                list_status, message_summaries = get_1secmail_message_list(login, domain)
                
                if list_status not in ["SUCCESS", "EMPTY"]:
                    print(f"Auto-refresh: Error fetching list for {login}@{domain}: {list_status} - {message_summaries}"); continue 
                if list_status == "EMPTY" or not message_summaries: continue

                seen_ids = last_message_ids.setdefault(chat_id, set())
                try: message_summaries.sort(key=lambda m: m.get('date', "0"), reverse=True)
                except: pass 

                for msg_summary in message_summaries[:5]: 
                    msg_id = msg_summary.get('id') 
                    if not isinstance(msg_id, int): continue
                    if msg_id in seen_ids: continue
                    
                    detail_status, msg_detail_data = read_1secmail_message_detail(login, domain, msg_id)
                    if detail_status == "SUCCESS":
                        if safe_send_message(chat_id, format_1secmail_message(msg_detail_data)):
                             seen_ids.add(msg_id)
                        time.sleep(0.7)
                    else:
                        print(f"Auto-refresh: Error detail for msg {msg_id} ({login}@{domain}): {detail_status} - {msg_detail_data}")

                if len(seen_ids) > 100: # Prune old seen IDs
                    sorted_seen_ids = sorted(list(seen_ids)); oldest_ids = sorted_seen_ids[:-50] 
                    for old_id in oldest_ids: seen_ids.discard(old_id)
        except Exception as e: print(f"Error in auto_refresh_worker: {type(e).__name__} - {e}")
        time.sleep(60)

def cleanup_blocked_users():
    while True:
        try:
            for chat_id in list(active_sessions):
                if is_bot_blocked(chat_id):
                    print(f"Cleaning up blocked user: {chat_id}"); safe_delete_user(chat_id)
        except Exception as e: print(f"Error in cleanup_blocked_users: {e}")
        time.sleep(3600) 

# --- Bot Handlers (Welcome, Admin, Mail, Profile, Account, 2FA) ---
# (These handlers remain largely the same, with mail handlers adapted below)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    user_info = get_user_info(message.from_user); user_profiles[chat_id] = user_info 
    if is_admin(chat_id):
        approved_users.add(chat_id)
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=get_main_keyboard(chat_id)); return
    if chat_id in approved_users:
        safe_send_message(chat_id, "👋 Welcome back!", reply_markup=get_main_keyboard(chat_id))
    else:
        if chat_id not in pending_approvals: 
            pending_approvals[chat_id] = user_info
            safe_send_message(chat_id, "👋 Your access request is sent. Please wait for approval.")
            if ADMIN_ID:
                try:
                    admin_chat_id = int(ADMIN_ID)
                    approval_msg = (f"🆕 *New Approval Request*\n\nID: `{chat_id}`\nName: `{user_info['name']}`\nUsername: `@{user_info['username']}`\nJoined: `{user_info['join_date']}`")
                    safe_send_message(admin_chat_id, approval_msg, reply_markup=get_approval_keyboard(chat_id))
                except: print(f"Failed to send approval to admin {ADMIN_ID}")
        else: safe_send_message(chat_id, "⏳ Access request pending.")

# --- Admin Panel ---
@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel" and is_admin(msg.chat.id))
def admin_panel(message): safe_send_message(message.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "👥 Pending Approvals" and is_admin(msg.chat.id))
def show_pending_approvals(message):
    if not pending_approvals: safe_send_message(message.chat.id, "✅ No pending approvals."); return
    count = 0
    for user_id, info in list(pending_approvals.items()): 
        count +=1; name, uname, joined = info.get('name', str(user_id)), info.get('username', 'N/A'), info.get('join_date', 'N/A')
        text = (f"*Pending {count}*\nID: `{user_id}`\nName: `{name}`\nUser: @{uname}\nJoined: `{joined}`")
        safe_send_message(message.chat.id, text, reply_markup=get_approval_keyboard(user_id)); time.sleep(0.1)
    if count == 0: safe_send_message(message.chat.id, "✅ No pending approvals after iterating.")

@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.chat.id))
def show_stats(message):
    start_time = user_profiles.get("bot_start_time")
    up_str, start_str = "N/A", "N/A"
    if not start_time: user_profiles["bot_start_time"] = datetime.datetime.now(); start_time = user_profiles["bot_start_time"]
    if start_time:
        start_str = start_time.strftime('%Y-%m-%d %H:%M:%S')
        delta = datetime.datetime.now() - start_time
        d, r = delta.days, delta.seconds; h, r = divmod(r, 3600); m, _ = divmod(r, 60)
        up_str = f"{d}d {h}h {m}m"
    stats = (f"📊 *Bot Stats*\n\n👑 Admin: `{ADMIN_ID}`\n👥 Approved: `{len(approved_users)}`\n"
             f"👤 Active Sessions: `{len(active_sessions)}`\n⏳ Pending: `{len(pending_approvals)}`\n"
             f"📧 Emails Active: `{len(user_data)}`\n🚀 Started: `{start_str}`\n⏱ Uptime: `{up_str}`")
    safe_send_message(message.chat.id, stats)

@bot.message_handler(func=lambda msg: msg.text == "👤 User Management" and is_admin(msg.chat.id))
def user_management(message): safe_send_message(message.chat.id, "👤 User Management", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📜 List Users" and is_admin(msg.chat.id))
def list_users(message):
    if not approved_users: safe_send_message(message.chat.id, "❌ No approved users."); return
    parts, cur = [], "👥 *Approved Users*\n\n"
    for uid in approved_users:
        p = user_profiles.get(uid, {}); info = f"🆔 `{uid}` - 👤 {p.get('name','?')} (@{p.get('username','?')}) - 📅 {p.get('join_date','?')}\n"
        if len(cur) + len(info) > 4000: parts.append(cur); cur = "👥 *(cont.)*\n\n" + info
        else: cur += info
    if cur.strip() not in ["👥 *Approved Users*\n\n".strip(), "👥 *(cont.)*\n\n".strip()]: parts.append(cur)
    if not parts: safe_send_message(message.chat.id, "❌ No user data.")
    else: 
        for p_msg in parts: safe_send_message(message.chat.id, p_msg); time.sleep(0.2)

@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_user_prompt(message):
    safe_send_message(message.chat.id, "🆔 Enter User ID to remove:", reply_markup=get_back_keyboard("admin_user_management"))
    bot.register_next_step_handler(message, process_user_removal)

def process_user_removal(message):
    cid = message.chat.id
    if message.text == "⬅️ Back to User Management": safe_send_message(cid, "Cancelled.", reply_markup=get_user_management_keyboard()); return
    try:
        uid = int(message.text.strip())
        if uid == int(ADMIN_ID): safe_send_message(cid, "❌ Cannot remove admin!", reply_markup=get_user_management_keyboard()); return
        was_appr, was_pend = uid in approved_users, uid in pending_approvals
        name = user_profiles.get(uid, {}).get('name', str(uid))
        if was_appr or was_pend:
            safe_delete_user(uid); msg = f"✅ User `{name}` (ID:{uid}) removed. Data cleared."
            safe_send_message(cid, msg, reply_markup=get_user_management_keyboard())
            try: safe_send_message(uid, "❌ Access revoked.")
            except: pass
        else: safe_send_message(cid, f"❌ User {uid} not found.", reply_markup=get_user_management_keyboard())
    except ValueError: safe_send_message(cid, "❌ Invalid ID.", reply_markup=get_user_management_keyboard())
    except Exception as e: safe_send_message(cid, f"Error: {e}", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def broadcast_menu(message): safe_send_message(message.chat.id, "📢 Choose Broadcast:", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def text_broadcast_prompt(message):
    safe_send_message(message.chat.id, "✍️ Enter message (or /cancel):", reply_markup=get_back_keyboard("admin_broadcast"))
    bot.register_next_step_handler(message, process_text_broadcast)

def process_text_broadcast(message):
    cid = message.chat.id
    if message.text in ["⬅️ Back to Broadcast Menu", "/cancel"]: safe_send_message(cid, "Cancelled.", reply_markup=get_broadcast_keyboard()); return
    if not message.text: safe_send_message(cid, "Empty. Cancelled.", reply_markup=get_broadcast_keyboard()); return
    users, s, f, t = list(approved_users), 0,0, len(list(approved_users))
    if t == 0: safe_send_message(cid, "No users.", reply_markup=get_admin_keyboard()); return
    pt = lambda i,sc,fl: f"📢 Broadcasting...\nSent: {i}/{t}\n✅OK:{sc} ❌Fail:{fl}"
    pm = safe_send_message(cid, pt(0,0,0))
    if not pm: safe_send_message(cid, "Error starting.", reply_markup=get_admin_keyboard()); return
    for i, uid in enumerate(users):
        if safe_send_message(uid, f"📢 *Admin Broadcast:*\n\n{message.text}"): s+=1
        else: f+=1
        if (i+1)%10==0 or (i+1)==t: 
            try: 
                if pm: bot.edit_message_text(pt(i+1,s,f), cid, pm.message_id)
            except: pm=None
        time.sleep(0.2)
    safe_send_message(cid, f"📢 Done!\n✅OK:{s} ❌Fail:{f}", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def media_broadcast_prompt(message):
    safe_send_message(message.chat.id, "🖼 Send media & caption (or /cancel):", reply_markup=get_back_keyboard("admin_broadcast"))
    bot.register_next_step_handler(message, process_media_broadcast)

def process_media_broadcast(message):
    cid = message.chat.id
    if message.text in ["⬅️ Back to Broadcast Menu", "/cancel"]: safe_send_message(cid, "Cancelled.", reply_markup=get_broadcast_keyboard()); return
    if not (message.photo or message.video or message.document): safe_send_message(cid, "No media. Cancelled.", reply_markup=get_broadcast_keyboard()); return
    users, s, f, t = list(approved_users), 0,0, len(list(approved_users))
    if t == 0: safe_send_message(cid, "No users.", reply_markup=get_admin_keyboard()); return
    pt = lambda i,sc,fl: f"📢 Media Broadcast...\nSent: {i}/{t}\n✅OK:{sc} ❌Fail:{fl}"
    pm = safe_send_message(cid, pt(0,0,0))
    if not pm: safe_send_message(cid, "Error starting.", reply_markup=get_admin_keyboard()); return
    cap = f"📢 *Admin Media Broadcast:*\n\n{message.caption or ''}".strip()
    for i, uid in enumerate(users):
        try:
            sent = False
            if message.photo: bot.send_photo(uid, message.photo[-1].file_id, caption=cap, parse_mode="Markdown"); sent=True
            elif message.video: bot.send_video(uid, message.video.file_id, caption=cap, parse_mode="Markdown"); sent=True
            elif message.document: bot.send_document(uid, message.document.file_id, caption=cap, parse_mode="Markdown"); sent=True
            if sent: s+=1
            else: f+=1
        except: f+=1
        if (i+1)%5==0 or (i+1)==t: 
            try: 
                if pm: bot.edit_message_text(pt(i+1,s,f), cid, pm.message_id)
            except: pm=None
        time.sleep(0.3)
    safe_send_message(cid, f"📢 Media Done!\n✅OK:{s} ❌Fail:{f}", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin" and is_admin(msg.chat.id))
def back_to_admin(message): safe_send_message(message.chat.id, "⬅️ To admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu" and is_admin(msg.chat.id))
def admin_back_to_main_from_admin_panel(message): safe_send_message(message.chat.id, "⬅️ To main menu...", reply_markup=get_main_keyboard(message.chat.id))

@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def handle_approval(call):
    if not is_admin(call.message.chat.id): bot.answer_callback_query(call.id, "❌ Not allowed."); return
    try: action, user_id_str = call.data.split('_'); user_id = int(user_id_str)
    except: bot.answer_callback_query(call.id, "Error."); bot.edit_message_text("Error.", call.message.chat.id, call.message.message_id); return
    info = pending_approvals.get(user_id, user_profiles.get(user_id)); name = info.get('name', str(user_id)) if info else str(user_id)
    if action == "approve":
        if user_id in pending_approvals or user_id not in approved_users:
            approved_users.add(user_id)
            if user_id not in user_profiles and info: user_profiles[user_id] = info
            pending_approvals.pop(user_id, None)
            safe_send_message(user_id, "✅ Access approved!", reply_markup=get_main_keyboard(user_id))
            bot.answer_callback_query(call.id, f"User {name} approved.")
            bot.edit_message_text(f"✅ User `{name}` (`{user_id}`) approved.", call.message.chat.id, call.message.message_id, reply_markup=None)
        else: bot.answer_callback_query(call.id, "Processed."); bot.edit_message_text(f"⚠️ User `{name}` (`{user_id}`) processed.", call.message.chat.id, call.message.message_id, reply_markup=None)
    elif action == "reject":
        safe_delete_user(user_id); safe_send_message(user_id, "❌ Access rejected.")
        bot.answer_callback_query(call.id, f"User {name} rejected.")
        bot.edit_message_text(f"❌ User `{name}` (`{user_id}`) rejected.", call.message.chat.id, call.message.message_id, reply_markup=None)

# --- Mail Handlers (1secmail) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail_1secmail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    user_data.pop(chat_id, None); last_message_ids.pop(chat_id, None)
    gen_msg = safe_send_message(chat_id, "⏳ Generating new email (1secmail.com)...")
    status, email_info_data = generate_1secmail_address()

    if status == "SUCCESS" and email_info_data:
        user_data[chat_id] = email_info_data
        last_message_ids[chat_id] = set() 
        msg_txt = f"✅ *New Email:*\n`{email_info_data['email']}`\n\nTap to copy. Use 'Refresh Mail'."
        if gen_msg: bot.edit_message_text(msg_txt, chat_id, gen_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, msg_txt)
    else: # Network or API error
        error_txt = f"❌ Failed to generate email: {email_info_data}.\nThis often indicates a network problem from the bot's location trying to reach the email service. Please check your server's internet connection, firewall, DNS, or try again much later."
        if gen_msg: bot.edit_message_text(error_txt, chat_id, gen_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, error_txt)

@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh Mail")
def refresh_mail_1secmail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    email_info = user_data.get(chat_id)
    if not email_info or "login" not in email_info: safe_send_message(chat_id, "⚠️ No active email. Use '📬 New mail'."); return
    login, domain, email_full = email_info["login"], email_info["domain"], email_info["email"]
    refresh_msg = safe_send_message(chat_id, f"🔄 Checking inbox for `{email_full}`...")
    list_status, summaries = get_1secmail_message_list(login, domain)

    if list_status == "EMPTY":
        txt = f"📭 Inbox for `{email_full}` is empty."
        if refresh_msg: bot.edit_message_text(txt, chat_id, refresh_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, txt)
        return
    elif list_status != "SUCCESS":
        err_txt = f"⚠️ Error fetching emails for `{email_full}`: {summaries}\nEmail service might be temporarily unavailable. Check your server's connection or try '📬 New mail'."
        if refresh_msg: bot.edit_message_text(err_txt, chat_id, refresh_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, err_txt)
        return
    
    if refresh_msg: 
        try: bot.delete_message(chat_id, refresh_msg.message_id)
        except: pass 
    seen, new_count = last_message_ids.setdefault(chat_id, set()), 0
    try: summaries.sort(key=lambda m: m.get('date', "0"), reverse=True)
    except: pass
    for summary in summaries[:10]: 
        msg_id = summary.get('id')
        if not isinstance(msg_id, int) or msg_id in seen: continue
        detail_status, detail_data = read_1secmail_message_detail(login, domain, msg_id)
        if detail_status == "SUCCESS":
            new_count +=1
            if safe_send_message(chat_id, format_1secmail_message(detail_data)): seen.add(msg_id)
            time.sleep(0.5)
        else: safe_send_message(chat_id, f"⚠️ Error fetching detail for msg ID {msg_id}: {detail_data}")
    if new_count == 0: safe_send_message(chat_id, f"✅ No *new* messages in `{email_full}`.")
    else: safe_send_message(chat_id, f"✨ Found {new_count} new message(s) for `{email_full}`.")

# --- Profile & Account Handlers --- (Largely unchanged)
@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
def generate_profile_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    gender = "male" if message.text == "👨 Male Profile" else "female"
    g, n, u, p, ph = generate_profile(gender); safe_send_message(chat_id, profile_message(g, n, u, p, ph))

@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
def my_account_info(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    safe_send_message(chat_id, "👤 Account Options:", reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📧 My Current Email")
def show_my_email(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    email = user_data.get(chat_id, {}).get('email')
    if email: safe_send_message(chat_id, f"✉️ Current email:\n`{email}`\nTap to copy.")
    else: safe_send_message(chat_id, "ℹ️ No active email. Use '📬 New mail'.", reply_markup=get_main_keyboard(chat_id))

@bot.message_handler(func=lambda msg: msg.text == "🆔 My Info")
def show_my_info(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    u_info = user_profiles.get(chat_id)
    if u_info:
        text = (f"👤 *Your Info:*\nName: `{u_info.get('name','?')}`\nUser: `@{u_info.get('username','?')}`\n"
                f"Joined: `{u_info.get('join_date','?')}`\nID: `{chat_id}`")
        safe_send_message(chat_id, text)
    else: safe_send_message(chat_id, "Info not found. Try /start.")

# --- 2FA Handlers --- (Largely unchanged)
STATE_WAITING_FOR_2FA_SECRET = "waiting_for_2fa_secret" 
user_states = {} 
@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def two_fa_auth_start(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    user_states[chat_id] = {"state": "2fa_platform_select"}
    safe_send_message(chat_id, "🔐 Choose platform for 2FA:", reply_markup=get_2fa_platform_keyboard())

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("state") == "2fa_platform_select" and \
                                     msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform_selection(message):
    cid, plat = message.chat.id, message.text
    secret_info = user_2fa_secrets.get(cid, {}).get(plat)
    if secret_info and "secret" in secret_info:
        try:
            totp = pyotp.TOTP(secret_info["secret"]); code, secs = totp.now(), 30-(datetime.datetime.now().second%30)
            reply = (f"🔐 *{plat} 2FA Code:*\n➡️ `{code}` ⬅️\n⏳ Valid ~*{secs}s*.\n\nTo update, enter new key.")
            safe_send_message(cid, reply, reply_markup=get_main_keyboard(cid)); time.sleep(0.5)
            safe_send_message(cid, f"To set new key for {plat}, enter now. Else '⬅️ Back'.", reply_markup=get_back_keyboard("2fa_secret_entry"))
            user_states[cid] = {"state": STATE_WAITING_FOR_2FA_SECRET, "platform": plat}
        except Exception as e:
            safe_send_message(cid, f"Error with {plat} secret: {e}. Re-add.", reply_markup=get_2fa_platform_keyboard())
            user_states[cid] = {"state": STATE_WAITING_FOR_2FA_SECRET, "platform": plat}
            if cid in user_2fa_secrets and plat in user_2fa_secrets[cid]: del user_2fa_secrets[cid][plat] 
    else:
        user_states[cid] = {"state": STATE_WAITING_FOR_2FA_SECRET, "platform": plat}
        safe_send_message(cid, f"🔢 Enter Base32 2FA secret for *{plat}*:\n(e.g., `SECRETKEY123`)\nOr '⬅️ Back'.", reply_markup=get_back_keyboard("2fa_secret_entry"))

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main") 
def back_to_main_menu_handler(message): user_states.pop(message.chat.id, None); safe_send_message(message.chat.id, "⬅️ To main menu...", reply_markup=get_main_keyboard(message.chat.id))
@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to 2FA Platforms")
def back_to_2fa_platforms(message): user_states[message.chat.id]={"state":"2fa_platform_select"}; safe_send_message(message.chat.id, "⬅️ Choose platform:", reply_markup=get_2fa_platform_keyboard())

@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("state") == STATE_WAITING_FOR_2FA_SECRET)
def handle_2fa_secret_input(message):
    cid, secret_in = message.chat.id, message.text.strip()
    plat = user_states.get(cid, {}).get("platform")
    if not plat: safe_send_message(cid, "Error: Platform not set. Start 2FA again.", reply_markup=get_main_keyboard(cid)); user_states.pop(cid,None); return
    if not is_valid_base32(secret_in): safe_send_message(cid, "❌ *Invalid Secret Key* (A-Z, 2-7).\nTry again, or '⬅️ Back'.", reply_markup=get_back_keyboard("2fa_secret_entry")); return 
    cleaned = secret_in.replace(" ","").replace("-","").upper(); final_secret = cleaned + ("=" * (-len(cleaned)%8))
    if cid not in user_2fa_secrets: user_2fa_secrets[cid] = {}
    user_2fa_secrets[cid][plat] = {"secret": final_secret, "added": datetime.datetime.now().isoformat()}; user_states.pop(cid,None)
    try:
        totp, now = pyotp.TOTP(final_secret), datetime.datetime.now(); code, secs = totp.now(), 30-(now.second%30)
        safe_send_message(cid, f"✅ *2FA Secret for {plat} Saved!*\n🔑 Code: `{code}`\n⏳ Valid ~*{secs}s*.", reply_markup=get_main_keyboard(cid))
    except Exception as e:
        if cid in user_2fa_secrets and plat in user_2fa_secrets[cid]: del user_2fa_secrets[cid][plat] 
        safe_send_message(cid, f"❌ Error with secret for {plat}: {e}. Not saved.", reply_markup=get_2fa_platform_keyboard()); user_states[cid]={"state":"2fa_platform_select"}

# --- Fallback Handler ---
@bot.message_handler(func=lambda message: True, content_types=['text'])
def echo_all(message):
    cid = message.chat.id
    if is_bot_blocked(cid): safe_delete_user(cid); return
    if not (cid in approved_users or is_admin(cid)):
        if cid in pending_approvals: safe_send_message(cid, "⏳ Access pending.")
        else: send_welcome(message) 
        return
    state_info = user_states.get(cid, {}); state = state_info.get("state")
    back_buttons = ["⬅️ Back to 2FA Platforms", "⬅️ Back to Main", "⬅️ Back to User Management", "⬅️ Back to Broadcast Menu", "⬅️ Back to Admin"]
    if state == STATE_WAITING_FOR_2FA_SECRET and message.text not in back_buttons:
        safe_send_message(cid, f"Waiting for 2FA secret for {state_info.get('platform','platform')} or use 'Back'.", reply_markup=get_back_keyboard("2fa_secret_entry")); return
    safe_send_message(cid, f"🤔 Unknown: '{message.text}'. Use buttons.", reply_markup=get_main_keyboard(cid))

# --- Main Loop ---
if __name__ == '__main__':
    print(f"[{datetime.datetime.now()}] Initializing bot...")
    user_profiles["bot_start_time"] = datetime.datetime.now() 
    print(f"[{datetime.datetime.now()}] Starting background threads...")
    threading.Thread(target=auto_refresh_worker, daemon=True).start()
    threading.Thread(target=cleanup_blocked_users, daemon=True).start()
    print(f"[{datetime.datetime.now()}] Starting polling for bot token: ...{BOT_TOKEN[-6:] if BOT_TOKEN else 'NONE'}")
    
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

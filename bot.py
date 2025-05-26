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
import re # For basic HTML stripping

load_dotenv()
fake = Faker()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise Exception("❌ BOT_TOKEN not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# --- Temporary Mail API Configuration ---
TEMP_MAIL_API_BASE_URL = "https://api.temp-mail.org/request"
DEFAULT_TEMP_MAIL_DOMAIN = "porjoton.com" # Fallback domain

# Data storage
user_data = {}  # Stores {"email": "user@tempdomain.com"} for temp mail
last_message_ids = {} # Stores set of seen message IDs from temp mail
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
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]
    if chat_id in active_sessions:
        active_sessions.discard(chat_id)
    if chat_id in pending_approvals:
        del pending_approvals[chat_id]
    if chat_id in approved_users:
        approved_users.discard(chat_id)
    if chat_id in user_profiles:
        del user_profiles[chat_id]

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
        telebot.types.KeyboardButton("📧 My Current Email"),
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

# --- Temp Mail (temp-mail.org style API) Functions ---

def get_temp_mail_domains():
    """Fetches available domains from the temp-mail API."""
    try:
        res = requests.get(f"{TEMP_MAIL_API_BASE_URL}/domains/format/json/", timeout=10)
        res.raise_for_status()
        domains_from_api = res.json()
        # Filter for string domains, often they start with '.'
        valid_domains = [d.lstrip('.') for d in domains_from_api if isinstance(d, str) and d]
        return valid_domains if valid_domains else [DEFAULT_TEMP_MAIL_DOMAIN]
    except requests.exceptions.RequestException as e:
        print(f"Error fetching temp-mail domains (network): {e}")
        return [DEFAULT_TEMP_MAIL_DOMAIN]
    except ValueError: # JSONDecodeError
        print(f"Error decoding temp-mail domains JSON.")
        return [DEFAULT_TEMP_MAIL_DOMAIN]
    except Exception as e:
        print(f"Unexpected error fetching temp-mail domains: {e}")
        return [DEFAULT_TEMP_MAIL_DOMAIN]

def generate_temp_mail_address():
    """Generates a new temporary email address."""
    try:
        name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        domains = get_temp_mail_domains()
        domain = random.choice(domains)
        return f"{name}@{domain}"
    except Exception as e:
        print(f"Error generating temp mail address: {e}")
        name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        return f"{name}@{DEFAULT_TEMP_MAIL_DOMAIN}"

def fetch_temp_mail_messages(email_address):
    """
    Fetches messages for a given temporary email address.
    Returns a tuple: (status_string, data_or_error_message)
    status_string can be "SUCCESS", "EMPTY", "API_ERROR", "NETWORK_ERROR", "JSON_ERROR"
    """
    if not email_address:
        return "API_ERROR", "Email address not provided"
    
    request_url = "" 
    try:
        email_hash = hashlib.md5(email_address.encode('utf-8')).hexdigest()
        request_url = f"{TEMP_MAIL_API_BASE_URL}/mail/id/{email_hash}/format/json/"
        # print(f"DEBUG: Fetching mail from: {request_url}") 
        
        res = requests.get(request_url, timeout=15)
        # print(f"DEBUG: API Response Status Code: {res.status_code}")
        # print(f"DEBUG: API Response Raw Text: {res.text[:500]}")

        res.raise_for_status()  # Raises HTTPError for bad responses (4xx or 5xx)
        
        messages = res.json()
        
        if isinstance(messages, dict) and "error" in messages:
            # print(f"DEBUG: API returned error: {messages['error']} for {email_address}")
            if messages['error'] == 'no_mail': # Specific handling for "no_mail"
                 return "EMPTY", [] 
            return "API_ERROR", f"Service error: {messages['error']}"
        
        if isinstance(messages, list):
            if not messages:
                return "EMPTY", []
            return "SUCCESS", messages
        else:
            # print(f"DEBUG: Unexpected API response type: {type(messages)} for {email_address}")
            return "API_ERROR", "Unexpected response format from email service."

    except requests.exceptions.HTTPError as e:
        err_msg = f"Email service failed (HTTP {e.response.status_code})"
        # print(f"{err_msg} for {email_address} from {request_url}: {e}")
        if e.response.status_code == 404:
             err_msg = f"Email service endpoint not found (404)"
        return "API_ERROR", err_msg
    except requests.exceptions.RequestException as e:
        # print(f"Network error fetching temp-mail messages for {email_address} from {request_url}: {e}")
        return "NETWORK_ERROR", f"Network error connecting to email service."
    except ValueError:  # JSONDecodeError
        # print(f"JSON decoding error for temp-mail messages for {email_address} from {request_url}.")
        return "JSON_ERROR", f"Invalid response format from email service."
    except Exception as e:
        # print(f"Unexpected error in fetch_temp_mail_messages for {email_address} from {request_url}: {e}")
        return "API_ERROR", f"An unexpected error occurred with the email service."


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
    try:
        cleaned = secret.replace(" ", "").replace("-", "").upper()
        if not cleaned or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in cleaned):
            return False
        padding = "=" * (-len(cleaned) % 8)
        pyotp.TOTP(cleaned + padding).now()
        return True
    except (binascii.Error, ValueError, TypeError, Exception):
        return False

# --- Background Workers ---
def format_temp_mail_message(msg_detail):
    sender = msg_detail.get('mail_from', 'N/A')
    subject = msg_detail.get('mail_subject', '(No Subject)')
    
    body_content = msg_detail.get('mail_text', '')
    if not body_content and 'mail_html' in msg_detail:
        html_body = msg_detail['mail_html']
        try: # Basic unquote, might not be needed if API sends plain text in html field
            body_content = requests.utils.unquote(html_body) 
        except Exception:
            body_content = html_body # Fallback to raw if unquote fails
        
        # Basic HTML stripping (very rudimentary)
        body_content = re.sub(r'<style[^>]*?>.*?</style>', '', body_content, flags=re.DOTALL | re.IGNORECASE)
        body_content = re.sub(r'<script[^>]*?>.*?</script>', '', body_content, flags=re.DOTALL | re.IGNORECASE)
        body_content = re.sub(r'<br\s*/?>', '\n', body_content, flags=re.IGNORECASE)
        body_content = re.sub(r'</p>', '\n</p>', body_content, flags=re.IGNORECASE) # Add newline before stripping p
        body_content = re.sub(r'<[^>]+>', '', body_content) # Strip all other tags
        body_content = body_content.replace('&nbsp;', ' ').replace('&amp;', '&')
        body_content = body_content.replace('&lt;', '<').replace('&gt;', '>')
        body_content = '\n'.join([line.strip() for line in body_content.splitlines() if line.strip()])


    body_content = body_content.strip() if body_content else "(No Content)"

    received_time_str = "Just now"
    timestamp = msg_detail.get('mail_timestamp')
    if timestamp:
        try:
            received_time_str = datetime.datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d %H:%M:%S UTC')
        except (ValueError, TypeError):
            received_time_str = str(timestamp) 
    elif 'mail_date' in msg_detail:
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
        f"{body_content[:3500]}\n" 
        f"━━━━━━━━━━━━━━━━━━━━"
    )

def auto_refresh_worker():
    while True:
        try:
            current_user_data_keys = list(user_data.keys())
            for chat_id in current_user_data_keys:
                if chat_id not in user_data: 
                    continue

                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id)
                    continue
                
                if "email" not in user_data.get(chat_id, {}):
                    continue

                email_address = user_data[chat_id]["email"]
                status, mail_data = fetch_temp_mail_messages(email_address)
                
                if status != "SUCCESS" and status != "EMPTY":
                    print(f"Auto-refresh: Failed to fetch for {chat_id} ({email_address}): {status} - {mail_data}")
                    time.sleep(5) 
                    continue 
                
                if status == "EMPTY" or not mail_data:
                    continue

                messages = mail_data
                seen_ids = last_message_ids.setdefault(chat_id, set())
                
                try:
                    messages.sort(key=lambda m: int(m.get('mail_timestamp', 0)), reverse=True)
                except (TypeError, ValueError):
                    pass 

                new_messages_found_this_cycle = False
                for msg_detail in messages[:5]: 
                    msg_id = msg_detail.get('mail_id') 
                    if not msg_id: 
                        msg_id = hashlib.md5((str(msg_detail.get('mail_from')) + \
                                             str(msg_detail.get('mail_subject')) + \
                                             str(msg_detail.get('mail_timestamp'))).encode()).hexdigest()

                    if msg_id in seen_ids:
                        continue
                    
                    seen_ids.add(msg_id)
                    new_messages_found_this_cycle = True
                    
                    formatted_msg = format_temp_mail_message(msg_detail)
                    safe_send_message(chat_id, formatted_msg)
                    time.sleep(0.5) 

                if len(seen_ids) > 100:
                    oldest_ids = sorted(list(seen_ids), key=lambda x: (isinstance(x, tuple), x))[:-50] # Keep recent 50
                    for old_id in oldest_ids:
                        seen_ids.discard(old_id)
        except Exception as e:
            print(f"Error in auto_refresh_worker: {e}")
        time.sleep(45)

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

# --- Bot Handlers ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): 
        safe_delete_user(chat_id)
        return

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
    
    count = 0
    for user_id, user_info in list(pending_approvals.items()): 
        count +=1
        user_display_name = user_info.get('name', str(user_id))
        user_display_username = user_info.get('username', 'N/A')
        user_display_joined = user_info.get('join_date', 'N/A')
        
        approval_item_text = (
            f"*Pending Request {count}*\n"
            f"🆔 User ID: `{user_id}`\n"
            f"👤 Name: `{user_display_name}`\n"
            f"📛 Username: @{user_display_username}\n"
            f"📅 Joined: `{user_display_joined}`"
        )
        keyboard = get_approval_keyboard(user_id)
        safe_send_message(message.chat.id, approval_item_text, reply_markup=keyboard)
        time.sleep(0.2) # Avoid hitting rate limits if many pending
    
    if count == 0:
         safe_send_message(message.chat.id, "✅ No pending approvals currently after iterating.")

@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.chat.id))
def show_stats(message):
    bot_start_time = user_profiles.get("bot_start_time")
    uptime_str = "Not recorded"
    bot_start_time_str = "Not recorded"

    if not bot_start_time: 
        user_profiles["bot_start_time"] = datetime.datetime.now()
        bot_start_time = user_profiles["bot_start_time"]
    
    bot_start_time_str = bot_start_time.strftime('%Y-%m-%d %H:%M:%S')
    uptime_delta = datetime.datetime.now() - bot_start_time
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
        f"📧 Active Email Addresses: `{len(user_data)}`\n"
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
            user_info_str += f" - 👤 {user_p.get('name','N/A')} (@{user_p.get('username','N/A')}) - 📅 Joined: {user_p.get('join_date','N/A')}"
        else:
            user_info_str += " - (Profile info not fully available)"
        user_info_str += "\n"

        if len(header) + len(current_part) + len(user_info_str) > 4000: 
            users_list_parts.append(header + current_part)
            current_part = user_info_str
        else:
            current_part += user_info_str
            
    if current_part: 
        users_list_parts.append(header + current_part)

    if not users_list_parts:
        safe_send_message(message.chat.id, "❌ No user data to display for approved users.")
        return

    for part_msg in users_list_parts:
        safe_send_message(message.chat.id, part_msg)
        time.sleep(0.2)


@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_user_prompt(message):
    safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=get_back_keyboard("admin_user_management"))
    bot.register_next_step_handler(message, process_user_removal)

def process_user_removal(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back to User Management": 
        safe_send_message(chat_id, "Cancelled user removal.", reply_markup=get_user_management_keyboard())
        return
    try:
        user_id_to_remove = int(message.text.strip())
        if user_id_to_remove == int(ADMIN_ID):
            safe_send_message(chat_id, "❌ Cannot remove the admin account!", reply_markup=get_user_management_keyboard())
            return
        
        removed_from_approved = user_id_to_remove in approved_users
        removed_from_pending = user_id_to_remove in pending_approvals
        
        original_user_name = user_profiles.get(user_id_to_remove, {}).get('name', str(user_id_to_remove))

        if removed_from_approved or removed_from_pending:
            safe_delete_user(user_id_to_remove) # Full cleanup
            status_msg = f"✅ User `{original_user_name}` (ID: {user_id_to_remove}) "
            if removed_from_approved: status_msg += "removed from approved users "
            if removed_from_pending: status_msg += "removed from pending list "
            status_msg += "and all their data cleared."
            safe_send_message(chat_id, status_msg, reply_markup=get_user_management_keyboard())
            
            try: # Notify user if possible
                safe_send_message(user_id_to_remove, "❌ Your access to this bot has been revoked by the admin.")
            except Exception as e:
                print(f"Could not notify user {user_id_to_remove} about removal: {e}")
        else:
            safe_send_message(chat_id, f"❌ User ID {user_id_to_remove} not found in approved or pending users.", reply_markup=get_user_management_keyboard())
    except ValueError:
        safe_send_message(chat_id, "❌ Invalid User ID. Please enter a numeric ID.", reply_markup=get_user_management_keyboard())
    except Exception as e:
        safe_send_message(chat_id, f"An error occurred during user removal: {e}", reply_markup=get_user_management_keyboard())


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
        safe_send_message(chat_id, "Broadcast message cannot be empty. Try again.", reply_markup=get_broadcast_keyboard()) # Stay on broadcast menu
        return

    success_count = 0
    failed_count = 0
    
    users_to_broadcast = list(approved_users) 
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
        # Admin can choose to receive broadcast if their ID is in approved_users
        # if user_id_to_broadcast == int(ADMIN_ID): continue 

        try:
            user_specific_broadcast_text = f"📢 *Admin Broadcast:*\n\n{broadcast_text}"
            if safe_send_message(user_id_to_broadcast, user_specific_broadcast_text):
                success_count += 1
            else: 
                failed_count +=1
        except Exception: 
            failed_count += 1
        
        if (i + 1) % 10 == 0 or (i + 1) == total_users: 
            try:
                current_progress_text = f"📢 Broadcasting to {total_users} users...\n\nSent: {i+1}/{total_users}\n✅ Success: {success_count}\n❌ Failed: {failed_count}"
                if progress_message: # Check if message still exists
                    bot.edit_message_text(current_progress_text, chat_id, progress_message.message_id)
            except Exception as e:
                print(f"Error updating broadcast progress: {e}") # Progress message might have been deleted
                progress_message = None # Stop trying to edit if it fails
        time.sleep(0.2) 

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
        return # Stay on broadcast menu, don't re-register handler here

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
    final_caption = f"📢 *Admin Media Broadcast:*\n\n{caption}".strip()

    for i, user_id_to_broadcast in enumerate(users_to_broadcast):
        # if user_id_to_broadcast == int(ADMIN_ID): continue
        try:
            sent_media = False
            if message.photo:
                bot.send_photo(user_id_to_broadcast, message.photo[-1].file_id, caption=final_caption, parse_mode="Markdown")
                sent_media = True
            elif message.video:
                bot.send_video(user_id_to_broadcast, message.video.file_id, caption=final_caption, parse_mode="Markdown")
                sent_media = True
            elif message.document:
                bot.send_document(user_id_to_broadcast, message.document.file_id, caption=final_caption, parse_mode="Markdown")
                sent_media = True
            
            if sent_media:
                success_count += 1
            else: 
                failed_count +=1
        except telebot.apihelper.ApiTelegramException as e_api:
            if hasattr(e_api, 'result_json') and e_api.result_json.get("error_code") == 403 and \
               "bot was blocked" in e_api.result_json.get("description", ""):
                safe_delete_user(user_id_to_broadcast) 
            failed_count += 1
            print(f"API error broadcasting media to {user_id_to_broadcast}: {e_api}")
        except Exception as e:
            failed_count += 1
            print(f"Error broadcasting media to {user_id_to_broadcast}: {e}")

        if (i + 1) % 5 == 0 or (i + 1) == total_users: 
            try:
                current_progress_text = f"📢 Broadcasting media to {total_users} users...\n\nSent: {i+1}/{total_users}\n✅ Success: {success_count}\n❌ Failed: {failed_count}"
                if progress_message:
                    bot.edit_message_text(current_progress_text, chat_id, progress_message.message_id)
            except Exception as e_edit:
                print(f"Error updating media broadcast progress: {e_edit}")
                progress_message = None
        time.sleep(0.3) 

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
            if user_id_to_act_on not in user_profiles and user_info: # Ensure profile is stored
                 user_profiles[user_id_to_act_on] = user_info
            del pending_approvals[user_id_to_act_on]
            
            safe_send_message(user_id_to_act_on, "✅ Your access request has been approved by the admin! You can now use all bot features.", reply_markup=get_main_keyboard(user_id_to_act_on))
            bot.answer_callback_query(call.id, f"User {user_name_for_log} approved.")
            bot.edit_message_text(f"✅ User `{user_name_for_log}` (ID: `{user_id_to_act_on}`) has been approved.", call.message.chat.id, call.message.message_id, reply_markup=None)
        else:
            bot.answer_callback_query(call.id, "User not in pending list or already processed.")
            bot.edit_message_text(f"⚠️ User `{user_name_for_log}` (ID: `{user_id_to_act_on}`) was not in the pending list or already processed.", call.message.chat.id, call.message.message_id, reply_markup=None)
    
    elif action == "reject":
        if user_id_to_act_on in pending_approvals:
            safe_delete_user(user_id_to_act_on) # Removes from pending_approvals via safe_delete_user
            safe_send_message(user_id_to_act_on, "❌ Unfortunately, your access request has been rejected by the admin.")
            bot.answer_callback_query(call.id, f"User {user_name_for_log} rejected.")
            bot.edit_message_text(f"❌ User `{user_name_for_log}` (ID: `{user_id_to_act_on}`) has been rejected.", call.message.chat.id, call.message.message_id, reply_markup=None)
        else: # Also remove from approved_users if somehow they were there and rejected
            if user_id_to_act_on in approved_users:
                safe_delete_user(user_id_to_act_on)
            bot.answer_callback_query(call.id, "User not in pending list or already processed.")
            bot.edit_message_text(f"⚠️ User `{user_name_for_log}` (ID: `{user_id_to_act_on}`) was not in the pending list or already processed.", call.message.chat.id, call.message.message_id, reply_markup=None)


# --- Mail handlers (New Temp Mail Implementation) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail_temp(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval. Please wait."); return

    if chat_id in user_data: del user_data[chat_id]
    if chat_id in last_message_ids: del last_message_ids[chat_id]

    generating_msg = safe_send_message(chat_id, "⏳ Generating new temporary email address...")
    email_address = generate_temp_mail_address()

    if email_address:
        user_data[chat_id] = {"email": email_address}
        last_message_ids[chat_id] = set() 
        msg_text = f"✅ *New Temporary Email Created!*\n\n📧 Email: `{email_address}`\n\nTap the email to copy. Incoming messages will appear automatically or use 'Refresh Mail'."
        if generating_msg: bot.edit_message_text(msg_text, chat_id, generating_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, msg_text)
    else:
        error_text = "❌ Failed to generate a temporary email address. Please try again later."
        if generating_msg: bot.edit_message_text(error_text, chat_id, generating_msg.message_id)
        else: safe_send_message(chat_id, error_text)


@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh Mail")
def refresh_mail_temp(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval."); return

    if chat_id not in user_data or "email" not in user_data[chat_id]:
        safe_send_message(chat_id, "⚠️ No active temporary email address. Please use '📬 New mail' first."); return

    email_address = user_data[chat_id]["email"]
    refreshing_msg = safe_send_message(chat_id, f"🔄 Checking inbox for `{email_address}`...")
    
    status, mail_api_data = fetch_temp_mail_messages(email_address)

    if status == "EMPTY":
        text = f"📭 *Your inbox for `{email_address}` is currently empty.*"
        if refreshing_msg: bot.edit_message_text(text, chat_id, refreshing_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, text)
        return
    elif status != "SUCCESS":
        error_text = f"⚠️ Error fetching emails for `{email_address}`: {mail_api_data}\nPlease try '📬 New mail' for a different address or check later."
        if refreshing_msg: bot.edit_message_text(error_text, chat_id, refreshing_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, error_text)
        return
    
    messages = mail_api_data # data is the list of messages if SUCCESS
    if refreshing_msg: # Delete the "refreshing..." message if processing continues
        try: bot.delete_message(chat_id, refreshing_msg.message_id)
        except Exception: pass 

    seen_ids = last_message_ids.setdefault(chat_id, set())
    new_messages_count = 0
    
    try:
        messages.sort(key=lambda m: int(m.get('mail_timestamp', 0)), reverse=True)
    except (TypeError, ValueError): pass

    for msg_detail in messages[:10]: 
        msg_id = msg_detail.get('mail_id')
        if not msg_id: 
            msg_id = hashlib.md5((str(msg_detail.get('mail_from')) + \
                                 str(msg_detail.get('mail_subject')) + \
                                 str(msg_detail.get('mail_timestamp'))).encode()).hexdigest()

        if msg_id not in seen_ids: 
            new_messages_count +=1
            formatted_msg = format_temp_mail_message(msg_detail)
            safe_send_message(chat_id, formatted_msg)
            seen_ids.add(msg_id) 
            time.sleep(0.3)
    
    if new_messages_count == 0:
        safe_send_message(chat_id, f"✅ No *new* messages found in `{email_address}` since the last check.")
    else:
        safe_send_message(chat_id, f"✨ Found {new_messages_count} new message(s) for `{email_address}`.")


# --- Profile handlers ---
@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
def generate_profile_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval."); return

    gender = "male" if message.text == "👨 Male Profile" else "female"
    _gender, name, username, password, phone = generate_profile(gender) 
    message_text = profile_message(_gender, name, username, password, phone)
    safe_send_message(chat_id, message_text)

# --- Account Info Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
def my_account_info(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval."); return
    safe_send_message(chat_id, "👤 Account Options:", reply_markup=get_user_account_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📧 My Current Email")
def show_my_email(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval."); return
    
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
        safe_send_message(chat_id, "⏳ Your access is pending approval."); return

    if chat_id in user_profiles:
        u_info = user_profiles[chat_id]
        info_text = (
            f"👤 *Your Information:*\n\n"
            f"Telegram Name: `{u_info.get('name','N/A')}`\n"
            f"Telegram Username: `@{u_info.get('username','N/A')}`\n"
            f"Bot Join Date: `{u_info.get('join_date','N/A')}`\n"
            f"User ID: `{chat_id}`"
        )
        safe_send_message(chat_id, info_text)
    else:
        safe_send_message(chat_id, "Could not retrieve your info. Try /start again.")


# --- 2FA Handlers ---
STATE_WAITING_FOR_2FA_SECRET = "waiting_for_2fa_secret"
user_states = {} 

@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def two_fa_auth_start(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval."); return
    
    user_states[chat_id] = {"state": "2fa_platform_select"}
    # Check if a secret for this platform is already saved
    # This part needs refinement if we want to show existing codes directly
    safe_send_message(chat_id, "🔐 Choose the platform to get a 2FA code or add/update a secret:", reply_markup=get_2fa_platform_keyboard())


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("state") == "2fa_platform_select" and \
                                     msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform_selection(message):
    chat_id = message.chat.id
    platform = message.text
    
    # Check if a secret for this user & platform already exists
    current_secret_info = user_2fa_secrets.get(chat_id, {}).get(platform)

    if current_secret_info and "secret" in current_secret_info:
        try:
            totp = pyotp.TOTP(current_secret_info["secret"])
            current_code = totp.now()
            now = datetime.datetime.now()
            seconds_remaining = 30 - (now.second % 30)
            
            reply_text = (
                f"🔐 *{platform} 2FA Code:*\n\n"
                f"➡️ `{current_code}` ⬅️ (Tap to copy)\n\n"
                f"⏳ Valid for approx. *{seconds_remaining} seconds*.\n\n"
                f"To update or change the secret key for {platform}, choose the platform again and enter the new key when prompted after this message."
            )
            safe_send_message(chat_id, reply_text, reply_markup=get_main_keyboard(chat_id))
            # Prompt to add a new one anyway, or offer a "Change Secret" button
            time.sleep(0.5) # Small delay
            safe_send_message(chat_id, f"To set a new secret key for {platform} (or if this is the first time), enter it now. Otherwise, '⬅️ Back to Main'.",
                              reply_markup=get_back_keyboard("2fa_secret_entry"))
            user_states[chat_id] = {"state": STATE_WAITING_FOR_2FA_SECRET, "platform": platform}

        except Exception as e:
            safe_send_message(chat_id, f"Error generating code for saved {platform} secret: {e}. Please try re-adding the secret.",
                              reply_markup=get_2fa_platform_keyboard())
            user_states[chat_id] = {"state": STATE_WAITING_FOR_2FA_SECRET, "platform": platform} # Allow re-entry
            if chat_id in user_2fa_secrets and platform in user_2fa_secrets[chat_id]:
                del user_2fa_secrets[chat_id][platform] # Clear potentially corrupt secret
    else:
        user_states[chat_id] = {"state": STATE_WAITING_FOR_2FA_SECRET, "platform": platform}
        safe_send_message(chat_id, f"🔢 Enter the Base32 2FA secret key for *{platform}*:\n\n(Example: `JBSWY3DPEHPK3PXP`)\nOr tap '⬅️ Back to 2FA Platforms' to cancel.",
                          reply_markup=get_back_keyboard("2fa_secret_entry"))


@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main")
def back_to_main_menu_handler(message): 
    chat_id = message.chat.id
    user_states.pop(chat_id, None) 
    # user_2fa_secrets.pop(chat_id, None) # Don't clear all 2FA secrets on back to main, only if cancelling a specific input flow
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to 2FA Platforms")
def back_to_2fa_platforms(message):
    chat_id = message.chat.id
    user_states[chat_id] = {"state": "2fa_platform_select"}
    # Don't clear user_2fa_secrets[chat_id] fully, as it might hold other platform secrets.
    # The specific platform entry will be overwritten if a new secret is added.
    safe_send_message(chat_id, "⬅️ Choose a platform or go back:", reply_markup=get_2fa_platform_keyboard())


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("state") == STATE_WAITING_FOR_2FA_SECRET)
def handle_2fa_secret_input(message):
    chat_id = message.chat.id
    secret_input = message.text.strip()
    platform = user_states[chat_id].get("platform", "Selected Platform") # Should always exist here

    if not is_valid_base32(secret_input):
        safe_send_message(chat_id, 
                          "❌ *Invalid Secret Key Format*\n\n"
                          "Your secret key must be a valid Base32 string (uppercase A-Z, digits 2-7).\n"
                          "Please try again, or '⬅️ Back to 2FA Platforms'.",
                          reply_markup=get_back_keyboard("2fa_secret_entry"))
        return 

    cleaned_secret = secret_input.replace(" ", "").replace("-", "").upper()
    padding = "=" * (-len(cleaned_secret) % 8) 
    final_secret = cleaned_secret + padding

    # Store the secret per platform for the user
    if chat_id not in user_2fa_secrets:
        user_2fa_secrets[chat_id] = {}
    user_2fa_secrets[chat_id][platform] = {
        "secret": final_secret, 
        "added_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    user_states.pop(chat_id, None)

    try:
        totp = pyotp.TOTP(final_secret)
        current_code = totp.now()
        now = datetime.datetime.now()
        seconds_remaining = 30 - (now.second % 30)
        
        reply_text = (
            f"✅ *2FA Secret Saved for {platform}!* 🎉\n\n"
            f"🔑 Current 2FA code:\n"
            f"➡️ `{current_code}` ⬅️ (Tap to copy)\n\n"
            f"⏳ Valid for approx. *{seconds_remaining} seconds*."
        )
        safe_send_message(chat_id, reply_text, reply_markup=get_main_keyboard(chat_id))
        
    except Exception as e:
        if chat_id in user_2fa_secrets and platform in user_2fa_secrets[chat_id]:
            del user_2fa_secrets[chat_id][platform] # Remove invalid secret
        safe_send_message(chat_id, f"❌ Error with the provided secret for {platform}: {e}. Secret not saved. Please try again.", 
                          reply_markup=get_2fa_platform_keyboard())
        user_states[chat_id] = {"state": "2fa_platform_select"}


@bot.message_handler(func=lambda message: True, content_types=['text'])
def echo_all(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return

    if not (chat_id in approved_users or is_admin(chat_id)):
        if chat_id in pending_approvals:
            safe_send_message(chat_id, "⏳ Your access request is still pending. Please wait for admin approval.")
        else: 
             send_welcome(message) 
        return
    
    # Check if it's a "Back" button from a next_step_handler that wasn't caught
    if "Back to" in message.text and user_states.get(chat_id):
        # Try to intelligently route back if a specific back button was missed
        if user_states[chat_id].get("state") == STATE_WAITING_FOR_2FA_SECRET:
            back_to_2fa_platforms(message)
            return
        # Add more specific back navigations if needed
        user_states.pop(chat_id, None) # Clear state

    safe_send_message(message.chat.id,
                      f"🤔 I'm not sure what you mean by '{message.text}'. Please use the buttons or commands.",
                      reply_markup=get_main_keyboard(chat_id))


if __name__ == '__main__':
    print("Initializing bot state...")
    user_profiles["bot_start_time"] = datetime.datetime.now() 

    print("🤖 Bot starting background threads...")
    threading.Thread(target=auto_refresh_worker, daemon=True).start()
    threading.Thread(target=cleanup_blocked_users, daemon=True).start()
    
    print(" पोलिंग शुरू कर रहा हूँ... (Starting polling...)")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30, logger_level=None) # Set logger_level to reduce verbosity
        except requests.exceptions.ReadTimeout as e:
            print(f"Polling ReadTimeout: {e}. Retrying in 15 seconds...")
            time.sleep(15)
        except requests.exceptions.ConnectionError as e:
            print(f"Polling ConnectionError: {e}. Retrying in 30 seconds...")
            time.sleep(30)
        except Exception as main_loop_e:
            print(f"CRITICAL ERROR in main polling loop: {main_loop_e}")
            print("Retrying in 60 seconds...")
            time.sleep(60)
        finally:
            print("🤖 Bot polling loop iteration finished or exited.") # Should not happen with infinity_polling unless error

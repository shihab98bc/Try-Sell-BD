import os
import threading
import time
import datetime
import random
import string
import re
from faker import Faker
from dotenv import load_dotenv
import requests
import pyotp

# For Flask health check on Railway
if os.environ.get('RAILWAY_ENVIRONMENT'):
    from flask import Flask

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Load environment variables
load_dotenv()

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")  # e.g., '123456789'
PORT = int(os.environ.get('PORT', 5000))

if not BOT_TOKEN or not ADMIN_ID:
    raise Exception("Please set BOT_TOKEN and ADMIN_ID in environment variables.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
BOT_START_TIME = datetime.datetime.now()

# Data stores
user_data = {}          # {chat_id: {"email":..., "password":..., "token":...}}
last_message_ids = {}   # {chat_id: set(msg_ids)}
user_2fa_secrets = {}   # {chat_id: {"platform":..., "secret":...}}
active_sessions = set()
pending_approvals = {}  # {chat_id: user_info}
approved_users = set()
user_profiles = {}      # {chat_id: {"name":..., "username":..., "join_date":...}}

fake = Faker()

# Helper functions
def is_admin(chat_id):
    return str(chat_id) == str(ADMIN_ID)

def is_authorized(chat_id):
    return is_admin(chat_id) or chat_id in approved_users

def safe_delete_user(chat_id):
    user_data.pop(chat_id, None)
    last_message_ids.pop(chat_id, None)
    user_2fa_secrets.pop(chat_id, None)
    active_sessions.discard(chat_id)
    pending_approvals.pop(chat_id, None)
    approved_users.discard(chat_id)
    user_profiles.pop(chat_id, None)
    print(f"Safely deleted user data for chat_id: {chat_id}")

def is_bot_blocked(chat_id):
    try:
        bot.get_chat(chat_id)
        return False
    except telebot.apihelper.ApiTelegramException as e:
        if e.error_code == 403: # Forbidden: bot was blocked by the user
            print(f"Bot blocked by user {chat_id}. Error: {e}")
            return True
        print(f"Error checking chat {chat_id} status (is_bot_blocked): {e}")
        return True # Treat other errors as potential blocks/issues
    except Exception as e:
        print(f"Unexpected error in is_bot_blocked for {chat_id}: {e}")
        return True # Safer to assume blocked on unexpected error

def get_user_info(user):
    return {
        "name": user.first_name + (f" {user.last_name}" if user.last_name else ""),
        "username": user.username if user.username else "N/A",
        "join_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def get_main_keyboard(chat_id):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📬 New mail", "🔄 Refresh")
    kb.row("👨 Male Profile", "👩 Female Profile")
    kb.row("🔐 2FA Auth", "👤 My Account")
    if is_admin(chat_id):
        kb.row("👑 Admin Panel")
    return kb

def get_admin_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("👥 Pending Approvals", "📊 Stats")
    kb.row("👤 User Management", "📢 Broadcast")
    kb.row("⬅️ Main Menu")
    return kb

def get_user_management_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📜 List Users", "❌ Remove User")
    kb.row("⬅️ Back to Admin")
    return kb

def get_broadcast_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📢 Text Broadcast", "📋 Media Broadcast")
    kb.row("⬅️ Back to Admin")
    return kb

def get_approval_keyboard(user_id):
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")
    )
    return kb

def get_back_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("⬅️ Back")
    return kb

def safe_send_message(chat_id, text, **kwargs):
    try:
        if is_bot_blocked(chat_id): # Check again before sending
            safe_delete_user(chat_id)
            return None
        return bot.send_message(chat_id, text, **kwargs)
    except telebot.apihelper.ApiTelegramException as e:
        print(f"Telegram API error sending message to {chat_id}: {e}")
        if e.error_code == 403: # Forbidden: bot was blocked by the user or kicked from group
             safe_delete_user(chat_id)
        # Other errors (e.g. 400 bad request if markdown is malformed) might also occur.
        return None
    except Exception as e:
        print(f"Generic error sending message to {chat_id}: {e}")
        safe_delete_user(chat_id) # Fallback, could be network related on bot side
        return None

# --- Mail.tm API functions ---
def get_domain():
    try:
        res = requests.get("https://api.mail.tm/domains", timeout=10)
        res.raise_for_status() 
        domains_data = res.json()
        domains = domains_data.get("hydra:member", [])
        if domains and isinstance(domains, list) and len(domains) > 0 and "domain" in domains[0]:
            return domains[0]["domain"]
        print(f"Warning: No domains found or unexpected format from mail.tm API: {domains_data}. Defaulting to mail.tm")
        return "mail.tm"
    except requests.exceptions.RequestException as e:
        print(f"Error fetching domains from mail.tm: {e}. Defaulting to mail.tm")
        return "mail.tm"
    except ValueError as e: 
        print(f"Error decoding domains JSON from mail.tm: {e}. Defaulting to mail.tm")
        return "mail.tm"

def generate_email():
    domain = get_domain()
    name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    email = f"{name}@{domain}"
    return email, name

def create_account(email, password):
    try:
        res = requests.post("https://api.mail.tm/accounts",
                            json={"address": email, "password": password},
                            timeout=10)
        if res.status_code == 201:
            print(f"Account {email} created successfully on mail.tm.")
            return "created"
        if res.status_code == 422:
            try:
                error_data = res.json()
                if "This value is already used" in error_data.get("hydra:description", ""):
                    print(f"Account {email} already exists on mail.tm.")
                    return "exists"
                print(f"mail.tm create_account error (422) for {email}: {error_data}")
            except ValueError:
                print(f"mail.tm create_account error (422) for {email}, non-JSON: {res.text[:200]}")
            return "error" # Potentially a different 422 error
        
        print(f"Error creating mail.tm account {email}. Status: {res.status_code}, Response: {res.text[:200]}")
        return "error"
    except requests.exceptions.RequestException as e:
        print(f"Network error creating mail.tm account {email}: {e}")
        return "error"

def get_token(email, password):
    time.sleep(1.5) 
    try:
        res = requests.post("https://api.mail.tm/token",
                            json={"address": email, "password": password},
                            timeout=10)
        if res.status_code == 200:
            token_data = res.json()
            if "token" in token_data:
                print(f"Token obtained successfully for {email}.")
                return token_data["token"]
            else:
                print(f"Token not found in mail.tm response for {email}. Response: {token_data}")
                return None
        else:
            error_text = res.text[:200]
            try:
                error_json = res.json()
                error_text = error_json.get("hydra:description") or error_json.get("message") or str(error_json)
            except ValueError:
                pass 
            print(f"Failed to get token for {email} from mail.tm. Status: {res.status_code}, Error: {error_text}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Network error getting token for {email} from mail.tm: {e}")
        return None
    except ValueError as e: 
        print(f"Error decoding token JSON from mail.tm for {email}: {e}")
        return None

# Profile generator (remains the same)
def generate_profile(gender):
    name_func = fake.name_male if gender == "male" else fake.name_female
    name = name_func()
    username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    # More robust password
    password_chars = string.ascii_letters + string.digits + "!@#$%^&*"
    password = ''.join(random.choices(password_chars, k=12)) + datetime.datetime.now().strftime("%d")
    # Phone number generation can be improved for more realism if needed, but this is fine.
    phone = '1' + str(random.randint(200, 999)) + ''.join([str(random.randint(0, 9)) for _ in range(7)])
    return gender, name, username, password, phone

def profile_message(gender, name, username, password, phone):
    icon = "👨" if gender == "male" else "👩"
    return (
        f"🔐 *Generated Profile*\n\n"
        f"{icon} *Gender:* {gender.capitalize()}\n"
        f"🧑‍💼 *Name:* `{name}`\n"
        f"🆔 *Username:* `{username}`\n"
        f"🔑 *Password:* `{password}`\n"
        f"📞 *Phone:* `{phone}`\n\n"
        f"✅ Tap on any value to copy"
    )

# --- 2FA functions ---
def is_valid_base32(secret):
    try:
        clean_secret = secret.replace(" ", "").replace("-", "").upper()
        # Basic check for Base32 characters. pyotp will do a more thorough check.
        if not re.match(r"^[A-Z2-7]+=*$", clean_secret):
            return False
        pyotp.TOTP(clean_secret).now() # This will raise an exception if invalid
        return True
    except Exception: # Catches exceptions from pyotp or other issues
        return False

# --- Worker threads ---
def auto_refresh_worker():
    while True:
        active_user_data_keys = list(user_data.keys()) # Iterate over a copy of keys
        for chat_id in active_user_data_keys:
            try:
                if chat_id not in user_data: # User might have been deleted in another part of this loop iteration
                    continue

                if is_bot_blocked(chat_id) or (not is_admin(chat_id) and chat_id not in approved_users):
                    safe_delete_user(chat_id)
                    continue

                user_details = user_data.get(chat_id)
                if not user_details: # Should be caught by the previous check, but as safeguard
                    continue

                current_email = user_details.get("email")
                current_password = user_details.get("password")
                token = user_details.get("token")

                if not all([current_email, current_password, token]):
                    print(f"Worker: Incomplete mail data for {chat_id}. Skipping.")
                    continue

                def fetch_messages_for_worker(current_token_to_use):
                    h = {"Authorization": f"Bearer {current_token_to_use}"}
                    try:
                        return requests.get("https://api.mail.tm/messages?sort[createdAt]=desc&page=1", headers=h, timeout=15)
                    except requests.exceptions.RequestException as req_e:
                        print(f"Worker: Connection error for {chat_id} ({current_email}): {req_e}")
                        return None
                
                res = fetch_messages_for_worker(token)

                if res is None:
                    time.sleep(5) # Delay if connection error for this user
                    continue

                if res.status_code != 200:
                    print(f"Worker: Initial fetch failed for {chat_id} ({current_email}), Status: {res.status_code}. Attempting re-auth.")
                    new_token = get_token(current_email, current_password)
                    if new_token:
                        user_data[chat_id]["token"] = new_token # Update token
                        token = new_token # Use new token for this iteration
                        print(f"Worker: Re-authenticated {chat_id} ({current_email}). Retrying fetch.")
                        res = fetch_messages_for_worker(new_token)
                        if res is None or res.status_code != 200:
                            status_after_retry = res.status_code if res else "Connection Error"
                            print(f"Worker: Fetch failed for {chat_id} ({current_email}) even after re-auth (Status: {status_after_retry}).")
                            continue # Skip this user for this cycle
                    else:
                        print(f"Worker: Re-authentication failed for {chat_id} ({current_email}). Session likely invalid.")
                        # Consider notifying user or removing email session after several failures
                        continue
                
                # At this point, res should be a successful response (200 OK)
                messages = res.json().get("hydra:member", [])
                if not isinstance(messages, list):
                    print(f"Worker: Unexpected messages format for {chat_id} ({current_email}): {messages}")
                    messages = []
                    
                seen_ids = last_message_ids.setdefault(chat_id, set())
                
                for msg_data in messages[:5]: # Process up to 5 newest unseen messages
                    msg_id = msg_data.get("id")
                    if not msg_id or msg_id in seen_ids:
                        continue
                    
                    seen_ids.add(msg_id)
                    # Simple seen_ids cleanup
                    if len(seen_ids) > 50:
                        oldest_ids = sorted(list(seen_ids), key=lambda x: msg_data.get("createdAt", ""), reverse=True)[30:]
                        for old_id in oldest_ids:
                            seen_ids.discard(old_id)

                    try:
                        # Use the current token (which might have been refreshed)
                        detail_headers = {"Authorization": f"Bearer {user_data[chat_id]['token']}"} 
                        detail_res = requests.get(f"https://api.mail.tm/messages/{msg_id}", headers=detail_headers, timeout=10)
                        
                        if detail_res.status_code == 200:
                            msg_detail = detail_res.json()
                            sender = msg_detail.get("from", {}).get("address", "Unknown Sender")
                            subject = msg_detail.get("subject", "(No Subject)")
                            
                            body_text = msg_detail.get("text")
                            if not body_text and msg_detail.get("html"):
                                # Basic HTML to text conversion (very rudimentary)
                                html_body = " ".join(msg_detail.get("html", [])) if isinstance(msg_detail.get("html"), list) else msg_detail.get("html", "")
                                clean_body = re.sub(r'<[^>]+>', '', html_body) # Strip HTML tags
                                body_text = re.sub(r'\s+', ' ', clean_body).strip() # Normalize whitespace
                            
                            body = (body_text or "(No Content)").strip()
                            
                            intro_text = msg_detail.get('intro', 'Just now')
                            if not isinstance(intro_text, str): intro_text = 'Just now'

                            otp_match = re.search(r"\b\d{6,8}\b", body)
                            otp_text_part = ""
                            if otp_match:
                                otp_code = otp_match.group()
                                otp_text_part = f"\n\n🚨 OTP Detected: `{otp_code}` (Click to copy!)"

                            msg_to_send = (
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                "📬 *New Email Received!*\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                f"👤 *From:* `{sender}`\n"
                                f"📨 *Subject:* _{subject}_\n"
                                f"🕒 *Received:* {intro_text}\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                "💬 *Body:*\n"
                                f"{body[:3500]}{otp_text_part}\n" 
                                "━━━━━━━━━━━━━━━━━━━━"
                            )
                            safe_send_message(chat_id, msg_to_send)
                        else:
                            print(f"Worker: Failed to get msg detail {msg_id} for {chat_id} ({current_email}). Status: {detail_res.status_code}")
                    except Exception as e_detail:
                        print(f"Worker: Error processing msg detail {msg_id} for {chat_id} ({current_email}): {e_detail}")
            except Exception as e_outer_loop:
                print(f"Worker: Unhandled exception in main loop for user {chat_id}: {e_outer_loop}")
        time.sleep(25) # Interval between full user list scans

def cleanup_blocked_users(): # This might be redundant if is_bot_blocked is checked frequently elsewhere
    while True:
        time.sleep(3600) # Run hourly
        for chat_id in list(active_sessions): # active_sessions might not be the best list to iterate
                                            # approved_users or user_data.keys() might be more relevant
            if is_bot_blocked(chat_id):
                print(f"Cleanup: Found blocked user {chat_id}. Deleting data.")
                safe_delete_user(chat_id)


# --- Handlers ---
@bot.message_handler(commands=['start', 'help'])
def handle_start_help(m):
    chat_id = m.chat.id
    active_sessions.add(chat_id) # Track active session

    # It's better to fetch user info once and store it if needed.
    # The current get_user_info updates join_date every time it's called.
    # Store initial join date.
    if chat_id not in user_profiles or "initial_join_date" not in user_profiles[chat_id]:
        tg_user_info = get_user_info(m.from_user) # Gets current name, username
        user_profiles[chat_id] = {
            "name": tg_user_info["name"],
            "username": tg_user_info["username"],
            "initial_join_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    else: # Update last seen and potentially name/username if changed
        user_profiles[chat_id]["last_seen"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_tg_info = get_user_info(m.from_user)
        user_profiles[chat_id]["name"] = current_tg_info["name"]
        user_profiles[chat_id]["username"] = current_tg_info["username"]

    if is_admin(chat_id):
        approved_users.add(chat_id)
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=get_main_keyboard(chat_id))
    elif chat_id in approved_users:
        safe_send_message(chat_id, "👋 Welcome back!", reply_markup=get_main_keyboard(chat_id))
    else:
        # Use the stored profile info for the approval request
        profile_for_approval = user_profiles.get(chat_id, get_user_info(m.from_user))
        pending_approvals[chat_id] = profile_for_approval
        
        safe_send_message(chat_id, "👋 Your access request has been sent to the admin. Please wait for approval.")
        approval_msg = (
            f"🆕 *New Approval Request*\n\n"
            f"🆔 User ID: `{chat_id}`\n"
            f"👤 Name: `{profile_for_approval['name']}`\n"
            f"📛 Username: `@{profile_for_approval['username']}`\n"
            f"📅 Requested: `{profile_for_approval.get('initial_join_date', 'N/A')}`"
        )
        bot.send_message(ADMIN_ID, approval_msg, reply_markup=get_approval_keyboard(chat_id)) # Use bot.send_message directly for admin critical msgs

# --- Main Menu Handlers --- (Largely unchanged, ensure safe_send_message is used)

@bot.message_handler(func=lambda m: m.text == "👑 Admin Panel" and is_admin(m.chat.id))
def handle_admin_panel(m):
    safe_send_message(m.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "👥 Pending Approvals" and is_admin(m.chat.id))
def handle_pending_approvals(m):
    if not pending_approvals:
        safe_send_message(m.chat.id, "✅ No pending approvals.")
        return
    for uid, info in list(pending_approvals.items()): # Iterate over a copy
        # Ensure info has the required fields
        user_name = info.get('name', 'N/A')
        user_username = info.get('username', 'N/A')
        join_date = info.get('initial_join_date', info.get('join_date', 'N/A')) # Fallback for older join_date key
        msg = (
            f"🆕 *Pending Approval*\n\n"
            f"🆔 User ID: `{uid}`\n"
            f"👤 Name: `{user_name}`\n"
            f"📛 Username: `@{user_username}`\n"
            f"📅 Requested: `{join_date}`"
        )
        safe_send_message(m.chat.id, msg, reply_markup=get_approval_keyboard(uid))

@bot.message_handler(func=lambda m: m.text == "📊 Stats" and is_admin(m.chat.id))
def handle_stats(m):
    now = datetime.datetime.now()
    uptime_delta = now - BOT_START_TIME
    days = uptime_delta.days
    hours, remainder = divmod(uptime_delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"
    
    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin Chat ID: `{ADMIN_ID}`\n"
        f"👥 Approved Users: `{len(approved_users)}`\n"
        # f"📭 Active Sessions (general): `{len(active_sessions)}`\n" # active_sessions might not be accurate
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"📧 Active Email Sessions: `{len(user_data)}`\n"
        f"🕰️ Bot Uptime: `{uptime_str}`"
    )
    safe_send_message(m.chat.id, stats_msg)

@bot.message_handler(func=lambda m: m.text == "👤 User Management" and is_admin(m.chat.id))
def handle_user_management(m):
    safe_send_message(m.chat.id, "👤 User Management Panel", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda m: m.text == "📜 List Users" and is_admin(m.chat.id))
def handle_list_users(m):
    if not approved_users:
        safe_send_message(m.chat.id, "❌ No approved users.")
        return
    
    details_list = []
    for uid in approved_users:
        profile = user_profiles.get(uid, {})
        name = profile.get('name', 'N/A')
        username = profile.get('username', 'N/A')
        join_date = profile.get('initial_join_date', profile.get('join_date', 'N/A'))
        details = (
            f"🆔 `{uid}`\n"
            f"👤 Name: {name}\n"
            f"📛 Username: @{username}\n"
            f"📅 Joined: {join_date}\n"
            f"📧 Email Active: {'Yes' if uid in user_data else 'No'}"
        )
        details_list.append(details)
    
    msg_parts = []
    current_part = "👥 *Approved Users:*\n\n"
    for detail in details_list:
        if len(current_part) + len(detail) + 2 > 4096: # Telegram message limit
            msg_parts.append(current_part)
            current_part = ""
        current_part += detail + "\n--------------------\n"
    msg_parts.append(current_part)

    for part in msg_parts:
        safe_send_message(m.chat.id, part)


@bot.message_handler(func=lambda m: m.text == "❌ Remove User" and is_admin(m.chat.id))
def handle_remove_user_prompt(m):
    safe_send_message(m.chat.id, "🆔 Enter User ID to remove:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(m, process_user_removal) # Renamed for clarity

def process_user_removal(m): # Renamed for clarity
    chat_id = m.chat.id
    if m.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled user removal.", reply_markup=get_user_management_keyboard())
        return
    try:
        uid_to_remove = int(m.text.strip())
        if str(uid_to_remove) == str(ADMIN_ID):
            safe_send_message(chat_id, "❌ Cannot remove the admin!", reply_markup=get_user_management_keyboard())
            return
        
        if uid_to_remove in approved_users or uid_to_remove in pending_approvals or uid_to_remove in user_data:
            original_status = "Approved" if uid_to_remove in approved_users else \
                              ("Pending" if uid_to_remove in pending_approvals else "Active (Email)")
            
            safe_delete_user(uid_to_remove) # This handles all data stores
            
            safe_send_message(chat_id, f"✅ User {uid_to_remove} (was {original_status}) removed successfully.", reply_markup=get_user_management_keyboard())
            try:
                # Notify the user if possible
                safe_send_message(uid_to_remove, "❌ Your access to the bot has been revoked by the admin.")
            except Exception as e_notify:
                print(f"Could not notify user {uid_to_remove} about removal: {e_notify}")
        else:
            safe_send_message(chat_id, f"❌ User {uid_to_remove} not found in any active list.", reply_markup=get_user_management_keyboard())
    except ValueError:
        safe_send_message(chat_id, "❌ Invalid User ID format. Please enter a numeric ID.", reply_markup=get_user_management_keyboard())
    except Exception as e:
        print(f"Error in process_user_removal: {e}")
        safe_send_message(chat_id, "❌ An unexpected error occurred during user removal.", reply_markup=get_user_management_keyboard())


@bot.message_handler(func=lambda m: m.text == "📢 Broadcast" and is_admin(m.chat.id))
def handle_broadcast_menu(m):
    safe_send_message(m.chat.id, "📢 Broadcast Options", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda m: m.text == "📢 Text Broadcast" and is_admin(m.chat.id))
def handle_text_broadcast_prompt(m): # Renamed
    safe_send_message(m.chat.id, "✍️ Enter the message you want to broadcast to all approved users:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(m, process_text_broadcast) # Renamed

def process_text_broadcast(m): # Renamed
    admin_chat_id = m.chat.id
    if m.text == "⬅️ Back":
        safe_send_message(admin_chat_id, "Text broadcast cancelled.", reply_markup=get_broadcast_keyboard())
        return

    broadcast_text = m.text
    if not broadcast_text:
        safe_send_message(admin_chat_id, "❌ Broadcast message cannot be empty.", reply_markup=get_broadcast_keyboard())
        return

    users_to_broadcast = list(approved_users) # Send to approved users
    total_users = len(users_to_broadcast)
    if total_users == 0:
        safe_send_message(admin_chat_id, "📢 No approved users to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_obj = safe_send_message(admin_chat_id, f"📢 Broadcasting text to {total_users} users...\n\n0/{total_users} sent.")
    success_count, fail_count = 0, 0

    for i, user_id in enumerate(users_to_broadcast, 1):
        if str(user_id) == str(ADMIN_ID): # Don't broadcast to admin self through this
            success_count +=1 # Count as success as admin initiated
            continue 
        try:
            sent_msg = safe_send_message(user_id, f"📢 *Admin Broadcast:*\n\n{broadcast_text}")
            if sent_msg:
                success_count += 1
            else: # safe_send_message returned None, likely bot blocked
                fail_count += 1
        except Exception as e:
            print(f"Broadcast error to user {user_id}: {e}")
            fail_count += 1
        
        if i % 10 == 0 or i == total_users: # Update progress every 10 users or at the end
            if progress_msg_obj:
                try:
                    bot.edit_message_text(
                        f"📢 Broadcasting text to {total_users} users...\n{i}/{total_users} processed.\n✅ Success: {success_count}\n❌ Failed: {fail_count}",
                        chat_id=admin_chat_id,
                        message_id=progress_msg_obj.message_id
                    )
                except Exception as e_edit:
                    print(f"Error updating broadcast progress message: {e_edit}")
        time.sleep(0.2) # Small delay to avoid hitting Telegram rate limits too hard

    final_summary = f"📢 Text broadcast finished!\n\n✅ Sent successfully to: {success_count} users.\n❌ Failed to send to: {fail_count} users."
    safe_send_message(admin_chat_id, final_summary, reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda m: m.text == "📋 Media Broadcast" and is_admin(m.chat.id))
def handle_media_broadcast_prompt(m):
    safe_send_message(m.chat.id, "🖼 Please send the media (photo, video, document) with a caption to broadcast:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(m, process_media_broadcast)

def process_media_broadcast(m):
    admin_chat_id = m.chat.id
    if m.text == "⬅️ Back": # Check if user sent text "Back" instead of media
        safe_send_message(admin_chat_id, "Media broadcast cancelled.", reply_markup=get_broadcast_keyboard())
        return
    
    # Validate that it's a media message
    if not (m.photo or m.video or m.document):
        safe_send_message(admin_chat_id, "❌ No media received. Please send a photo, video, or document. Or press '⬅️ Back'.", reply_markup=get_back_keyboard())
        bot.register_next_step_handler(m, process_media_broadcast) # Re-register
        return

    users_to_broadcast = list(approved_users)
    total_users = len(users_to_broadcast)
    if total_users == 0:
        safe_send_message(admin_chat_id, "📢 No approved users to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_obj = safe_send_message(admin_chat_id, f"📢 Broadcasting media to {total_users} users...\n\n0/{total_users} sent.")
    success_count, fail_count = 0, 0
    caption = m.caption if m.caption else ""

    for i, user_id in enumerate(users_to_broadcast, 1):
        if str(user_id) == str(ADMIN_ID):
            success_count +=1
            continue
        try:
            sent_successfully = False
            if m.photo:
                if bot.send_photo(user_id, m.photo[-1].file_id, caption=caption): sent_successfully = True
            elif m.video:
                if bot.send_video(user_id, m.video.file_id, caption=caption): sent_successfully = True
            elif m.document:
                if bot.send_document(user_id, m.document.file_id, caption=caption): sent_successfully = True
            
            if sent_successfully:
                success_count += 1
            else: # bot.send_xyz returned None or an error occurred handled by its try-except
                fail_count +=1
                if is_bot_blocked(user_id): safe_delete_user(user_id)


        except Exception as e:
            print(f"Media broadcast error to user {user_id}: {e}")
            fail_count += 1
            if is_bot_blocked(user_id): safe_delete_user(user_id) # Attempt cleanup if specific error suggests block
        
        if i % 5 == 0 or i == total_users: # Update progress
             if progress_msg_obj:
                try:
                    bot.edit_message_text(
                        f"📢 Broadcasting media to {total_users} users...\n{i}/{total_users} processed.\n✅ Success: {success_count}\n❌ Failed: {fail_count}",
                        chat_id=admin_chat_id,
                        message_id=progress_msg_obj.message_id
                    )
                except Exception as e_edit:
                    print(f"Error updating media broadcast progress: {e_edit}")
        time.sleep(0.5) # Slower delay for media

    final_summary = f"📢 Media broadcast finished!\n\n✅ Sent successfully to: {success_count} users.\n❌ Failed to send to: {fail_count} users."
    safe_send_message(admin_chat_id, final_summary, reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda m: m.text == "⬅️ Back to Admin" and is_admin(m.chat.id))
def handle_back_to_admin(m):
    safe_send_message(m.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "⬅️ Main Menu")
def handle_back_to_main_menu_from_anywhere(m): # Made more generic
    safe_send_message(m.chat.id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(m.chat.id))


# --- Callback query handlers for approvals ---
@bot.callback_query_handler(func=lambda c: c.data.startswith(('approve_', 'reject_')))
def handle_approval_callback(c):
    admin_chat_id = c.message.chat.id
    if not is_admin(admin_chat_id):
        bot.answer_callback_query(c.id, "Error: Not authorized.")
        return

    try:
        action, uid_str = c.data.split('_')
        uid_to_process = int(uid_str)
    except ValueError:
        bot.answer_callback_query(c.id, "Error: Invalid callback data.")
        bot.edit_message_text("Invalid callback data.", admin_chat_id, c.message.message_id)
        return

    user_profile_info = pending_approvals.get(uid_to_process, user_profiles.get(uid_to_process))
    user_display_name = f"{user_profile_info.get('name', 'User')} (@{user_profile_info.get('username', uid_to_process)})" if user_profile_info else f"User ID {uid_to_process}"


    if action == "approve":
        approved_users.add(uid_to_process)
        if uid_to_process in pending_approvals:
            del pending_approvals[uid_to_process]
        
        # Update user_profiles if it was a pending approval
        if user_profile_info and uid_to_process not in user_profiles:
             user_profiles[uid_to_process] = user_profile_info
        elif user_profile_info : # ensure it's up to date if it was pending
             user_profiles[uid_to_process].update(user_profile_info)


        bot.answer_callback_query(c.id, f"User {uid_to_process} approved.")
        bot.edit_message_text(f"✅ User {user_display_name} approved.", admin_chat_id, c.message.message_id, reply_markup=None)
        #safe_send_message(admin_chat_id, f"✅ User {uid_to_process} approved.") # Redundant with edit_message_text
        safe_send_message(uid_to_process, "✅ Your access request has been approved by the admin! You can now use the bot.", reply_markup=get_main_keyboard(uid_to_process))
    
    elif action == "reject":
        if uid_to_process in pending_approvals:
            del pending_approvals[uid_to_process]
        # We might want to remove them from user_profiles too, or keep for record
        user_profiles.pop(uid_to_process, None) # Remove if they are rejected
        
        bot.answer_callback_query(c.id, f"User {uid_to_process} rejected.")
        bot.edit_message_text(f"❌ User {user_display_name} rejected.", admin_chat_id, c.message.message_id, reply_markup=None)
        #safe_send_message(admin_chat_id, f"❌ User {uid_to_process} rejected.")
        safe_send_message(uid_to_process, "❌ Unfortunately, your access request has been rejected by the admin.")
    else:
        bot.answer_callback_query(c.id, "Unknown action.")


# --- Main mail functions ---
@bot.message_handler(func=lambda m: m.text == "📬 New mail")
def handle_new_mail(m):
    chat_id = m.chat.id
    if is_bot_blocked(chat_id): # Redundant if safe_send_message used, but good for direct calls
        safe_delete_user(chat_id)
        return
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval. Please wait.")
        return

    # If user already has an email, perhaps ask if they want to replace it?
    # For now, just create a new one, overwriting the old.
    if chat_id in user_data:
        safe_send_message(chat_id, "🗑️ Discarding your old temporary email and creating a new one...")
        last_message_ids.pop(chat_id, None) # Clear seen messages for the old email

    email, name_part = generate_email()
    # Mail.tm often requires a somewhat complex password, though for API access it might be less strict.
    # The password "TempPass123!" seems to work for their API token generation.
    password = "TempPass123!" 
    
    status_msg = safe_send_message(chat_id, f"⚙️ Generating new temporary email `{email}`...")

    result = create_account(email, password)

    if result in ["created", "exists"]:
        token = get_token(email, password)
        if token:
            user_data[chat_id] = {"email": email, "password": password, "token": token}
            last_message_ids[chat_id] = set() # Initialize seen IDs for the new email
            if status_msg: bot.delete_message(chat_id, status_msg.message_id) # Delete "Generating..." message
            final_msg = f"✅ *New Temporary Email Created!*\n\n📧 Email: `{email}`\n\nTap the email address to copy it. Emails will be automatically fetched."
            safe_send_message(chat_id, final_msg)
        else:
            if status_msg: bot.delete_message(chat_id, status_msg.message_id)
            safe_send_message(chat_id, f"❌ Failed to log in to `{email}` after creation/check. The mail service might be having issues. Try again.")
    else: # 'error' from create_account
        if status_msg: bot.delete_message(chat_id, status_msg.message_id)
        safe_send_message(chat_id, f"❌ Could not create temporary email `{email}`. The mail service might be unavailable or the domain is problematic. Try again later.")


@bot.message_handler(func=lambda m: m.text == "🔄 Refresh")
def handle_refresh(m):
    chat_id = m.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    if chat_id not in user_data:
        safe_send_message(chat_id, "⚠️ No active temporary email found. Please create one using '📬 New mail' first.")
        return

    email_info = user_data[chat_id]
    current_email = email_info["email"]
    current_password = email_info["password"] # Should be "TempPass123!"
    token = email_info["token"]

    status_msg = safe_send_message(chat_id, f"🔄 Refreshing inbox for `{current_email}`...")

    def fetch_messages_with_given_token(token_to_use, user_email):
        h = {"Authorization": f"Bearer {token_to_use}"}
        try:
            # Fetch newest first, limit to a few for manual refresh display
            return requests.get(f"https://api.mail.tm/messages?sort[createdAt]=desc&page=1", headers=h, timeout=12)
        except requests.exceptions.RequestException as e:
            print(f"Refresh: Connection error for {chat_id} ({user_email}): {e}")
            return None # Indicate connection error

    res = fetch_messages_with_given_token(token, current_email)

    if res is None: # Connection error
        if status_msg: bot.delete_message(chat_id, status_msg.message_id)
        safe_send_message(chat_id, "❌ Connection error while fetching inbox. Please check your network or try again later.")
        return

    if res.status_code != 200:
        if status_msg: bot.edit_message_text(f"⚠️ Token for `{current_email}` might be invalid (Error: {res.status_code}). Attempting to re-authenticate...", chat_id, status_msg.message_id)
        else: status_msg = safe_send_message(chat_id, f"⚠️ Token for `{current_email}` might be invalid (Error: {res.status_code}). Attempting to re-authenticate...")

        new_token = get_token(current_email, current_password)
        if new_token:
            user_data[chat_id]["token"] = new_token # Update the stored token
            token = new_token # Use new token for this attempt
            if status_msg: bot.edit_message_text(f"✅ Re-authenticated for `{current_email}`. Retrying message fetch...", chat_id, status_msg.message_id)
            res = fetch_messages_with_given_token(new_token, current_email)
            
            if res is None:
                if status_msg: bot.delete_message(chat_id, status_msg.message_id)
                safe_send_message(chat_id, "❌ Connection error on retry after re-authentication. Try again later.")
                return
            if res.status_code != 200:
                if status_msg: bot.delete_message(chat_id, status_msg.message_id)
                print(f"Refresh Error after re-auth for {chat_id} ({current_email}): {res.status_code} - {res.text[:200]}")
                safe_send_message(chat_id, f"❌ Still could not fetch inbox for `{current_email}` after re-authentication (Error: {res.status_code}). Your session might be invalid. Try '📬 New mail'.")
                return
        else: # Failed to get new token
            if status_msg: bot.delete_message(chat_id, status_msg.message_id)
            safe_send_message(chat_id, f"❌ Failed to re-authenticate for `{current_email}`. Your email session might be invalid. Please try '📬 New mail'.")
            return
    
    # If we reach here, res is valid and status_code is 200
    if status_msg: bot.delete_message(chat_id, status_msg.message_id)

    messages = res.json().get("hydra:member", [])
    if not isinstance(messages, list): messages = []

    if not messages:
        safe_send_message(chat_id, f"📭 Your inbox for `{current_email}` is currently empty.")
        return

    safe_send_message(chat_id, f"📬 Displaying recent emails for `{current_email}` (newest first):")
    
    displayed_count = 0
    for msg_data in messages[:3]: # Show top 3 from manual refresh
        msg_id = msg_data.get("id")
        if not msg_id: continue

        # Fetch full message detail for display (even if seen by worker, user wants to see it now)
        try:
            detail_headers = {"Authorization": f"Bearer {token}"} # Use current valid token
            detail_res = requests.get(f"https://api.mail.tm/messages/{msg_id}", headers=detail_headers, timeout=10)
            if detail_res.status_code == 200:
                msg_detail = detail_res.json()
                sender = msg_detail.get("from", {}).get("address", "Unknown Sender")
                subject = msg_detail.get("subject", "(No Subject)")
                
                body_text = msg_detail.get("text")
                if not body_text and msg_detail.get("html"):
                    html_body = " ".join(msg_detail.get("html", [])) if isinstance(msg_detail.get("html"), list) else msg_detail.get("html", "")
                    clean_body = re.sub(r'<[^>]+>', '', html_body)
                    body_text = re.sub(r'\s+', ' ', clean_body).strip()
                body = (body_text or "(No Content)").strip()

                intro_text = msg_detail.get('intro', 'Just now')
                if not isinstance(intro_text, str): intro_text = 'Just now'

                otp_match = re.search(r"\b\d{6,8}\b", body)
                otp_text_part = ""
                if otp_match:
                    otp_code = otp_match.group()
                    otp_text_part = f"\n\n🚨 OTP Detected: `{otp_code}` (Click to copy!)"

                msg_to_send = (
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *From:* `{sender}`\n"
                    f"📨 *Subject:* _{subject}_\n"
                    f"🕒 *Received:* {intro_text}\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "💬 *Body Preview:*\n"
                    f"{body[:1000]}{otp_text_part}\n" # Shorter preview for manual refresh list
                    "━━━━━━━━━━━━━━━━━━━━"
                )
                safe_send_message(chat_id, msg_to_send)
                displayed_count +=1
                
                # Add to seen_ids if not already there (worker might have missed it or this is new)
                last_message_ids.setdefault(chat_id, set()).add(msg_id)

            else: # Failed to fetch detail for this message
                safe_send_message(chat_id, f"⚠️ Could not fetch details for a message (ID: {msg_id}, Subject: {msg_data.get('subject', 'N/A')}).")
        except Exception as e_detail_refresh:
            print(f"Refresh: Error processing detail for msg {msg_id} for {chat_id}: {e_detail_refresh}")
            safe_send_message(chat_id, f"⚠️ Error displaying details for a message (ID: {msg_id}).")
    
    if displayed_count == 0 and messages: # If messages list was not empty but failed to display any
        safe_send_message(chat_id, "ℹ️ Found emails, but had trouble displaying details. The auto-fetcher might still deliver them.")


# --- Profile handlers ---
@bot.message_handler(func=lambda m: m.text in ["👨 Male Profile", "👩 Female Profile"])
def handle_generate_profile(m):
    chat_id = m.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    
    gender_str = "male" if m.text == "👨 Male Profile" else "female"
    # Note: generate_profile can be slow due to Faker
    status_msg = safe_send_message(chat_id, f"🎭 Generating {gender_str} profile...")
    
    try:
        gender, name, username, password, phone = generate_profile(gender_str)
        # user_profiles is for Telegram user profile, not these generated ones.
        # If you want to store these generated ones, use a different dict or structure.
        # For now, just display.
        msg_text = profile_message(gender, name, username, password, phone)
        if status_msg: bot.delete_message(chat_id, status_msg.message_id)
        safe_send_message(chat_id, msg_text)
    except Exception as e_gen_prof:
        print(f"Error generating profile: {e_gen_prof}")
        if status_msg: bot.delete_message(chat_id, status_msg.message_id)
        safe_send_message(chat_id, "❌ Error generating profile. Please try again.")


# --- "👤 My Account" ---
@bot.message_handler(func=lambda m: m.text == "👤 My Account")
def handle_my_account(m):
    chat_id = m.chat.id
    tg_user = m.from_user # The Telegram user object

    # Use the stored profile if available, otherwise fetch fresh
    profile = user_profiles.get(chat_id, get_user_info(tg_user))
    
    name = profile.get('name', tg_user.first_name + (f" {tg_user.last_name}" if tg_user.last_name else ""))
    username = profile.get('username', f"@{tg_user.username}" if tg_user.username else "N/A")
    join_date = profile.get('initial_join_date', profile.get('join_date', 'N/A')) # initial_join_date is preferred
    
    status = "Admin" if is_admin(chat_id) else ("Approved" if chat_id in approved_users else "Pending Approval")

    msg = (
        f"👤 *Your Account Information*\n\n"
        f"🗣️ Name: {name}\n"
        f"🆔 Telegram Username: {username}\n"
        f"🔢 Telegram User ID: `{chat_id}`\n"
        f"🗓️ Bot Joined Date: {join_date}\n"
        f"✅ Access Status: {status}\n"
    )
    if chat_id in user_data:
        msg += f"📧 Current Temp Email: `{user_data[chat_id]['email']}`"

    safe_send_message(chat_id, msg)

# --- "🔐 2FA Auth" ---
def get_2fa_platform_keyboard(): # Moved definition before use
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    kb.add("Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple", "Discord", "GitHub", "Other")
    kb.row("⬅️ Main Menu") # Changed from "Back" to "Main Menu" for clarity
    return kb

@bot.message_handler(func=lambda m: m.text == "🔐 2FA Auth")
def handle_2fa_main(m):
    chat_id = m.chat.id
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    safe_send_message(chat_id, "🔐 Choose the platform/service for your 2FA or select 'Other':", reply_markup=get_2fa_platform_keyboard())
    bot.register_next_step_handler(m, handle_2fa_platform_selection)


def handle_2fa_platform_selection(m):
    chat_id = m.chat.id
    platform_choice = m.text.strip()

    if platform_choice == "⬅️ Main Menu":
        safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))
        user_2fa_secrets.pop(chat_id, None) # Clear any partial state
        return
    
    # List of common platforms from the keyboard + "Other"
    valid_platforms = ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple", "Discord", "GitHub", "Other"]
    if platform_choice not in valid_platforms:
        safe_send_message(chat_id, "⚠️ Invalid platform. Please choose from the keyboard or type 'Other'.", reply_markup=get_2fa_platform_keyboard())
        bot.register_next_step_handler(m, handle_2fa_platform_selection) # Re-register
        return

    user_2fa_secrets[chat_id] = {"platform": platform_choice}
    prompt_message = f"🔢 Please enter your Base32 secret key for *{platform_choice}*:"
    if platform_choice == "Other":
        prompt_message = f"🔢 Please enter your Base32 secret key for the 'Other' service:"
    
    safe_send_message(chat_id, prompt_message, reply_markup=get_back_keyboard()) # "Back" here should go back to platform selection or main menu
    bot.register_next_step_handler(m, handle_2fa_secret_input)


def handle_2fa_secret_input(m):
    chat_id = m.chat.id
    secret_input = m.text.strip()

    if secret_input == "⬅️ Back": # This "Back" should ideally go to platform selection or main menu
        safe_send_message(chat_id, "2FA setup cancelled. Returning to 2FA platform selection.", reply_markup=get_2fa_platform_keyboard())
        user_2fa_secrets.pop(chat_id, None) # Clear state
        bot.register_next_step_handler(m, handle_2fa_platform_selection) # Go back to platform selection
        return

    if chat_id not in user_2fa_secrets or "platform" not in user_2fa_secrets[chat_id]:
        # State lost or flow broken
        safe_send_message(chat_id, "⚠️ Something went wrong with 2FA setup. Please start over.", reply_markup=get_main_keyboard(chat_id))
        return

    if not is_valid_base32(secret_input):
        error_msg = (
            "❌ *Invalid Secret Key Format*\n\n"
            "Your secret key must be a valid Base32 string.\n"
            "- It should only contain uppercase letters (A-Z) and digits (2-7).\n"
            "- Spaces or other special characters are not allowed (they will be stripped, but ensure the core key is Base32).\n"
            "- Example: `JBSWY3DPEHPK3PXP`\n\n"
            "Please try entering your secret key again, or press '⬅️ Back' to cancel."
        )
        safe_send_message(chat_id, error_msg, reply_markup=get_back_keyboard())
        bot.register_next_step_handler(m, handle_2fa_secret_input) # Re-register for new input
        return
    
    secret_clean = secret_input.replace(" ", "").replace("-", "").upper()
    platform = user_2fa_secrets[chat_id]["platform"]
    
    try:
        totp = pyotp.TOTP(secret_clean)
        current_code = totp.now()
        
        # Time remaining calculation
        now_ts = time.time()
        time_step = totp.interval
        remaining_seconds = int(time_step - (now_ts % time_step))

        reply_text = (
            f"✅ *2FA Code for {platform}*\n\n"
            f"🔑 Code: `{current_code}`\n\n"
            f"⏳ Valid for approximately: *{remaining_seconds} seconds*.\n\n"
            "This code will update automatically. You can get new codes by re-entering your secret key via the '🔐 2FA Auth' menu."
        )
        safe_send_message(chat_id, reply_text, reply_markup=get_main_keyboard(chat_id))
    except Exception as e_totp:
        print(f"Error generating TOTP for {chat_id} on platform {platform}: {e_totp}")
        safe_send_message(chat_id, f"❌ Error generating 2FA code for {platform}. The secret key might be incorrect despite basic validation. Please verify and try again.", reply_markup=get_main_keyboard(chat_id))
    finally:
        user_2fa_secrets.pop(chat_id, None) # Clear the secret from memory after use


# Fallback handler for any text not caught by other handlers
@bot.message_handler(func=lambda m: True, content_types=['text', 'audio', 'document', 'photo', 'sticker', 'video', 'video_note', 'voice', 'location', 'contact'])
def handle_unknown_messages(m):
    chat_id = m.chat.id
    if not is_authorized(chat_id) and chat_id not in pending_approvals and str(chat_id) != str(ADMIN_ID):
        # New user, not yet started, guide them
        safe_send_message(chat_id, "👋 Welcome! Please use /start to begin interacting with the bot.")
        return

    if is_authorized(chat_id) or is_admin(chat_id):
         # If user is in a specific flow (e.g. waiting for 2FA secret), this fallback shouldn't ideally be hit.
         # The next_step_handlers should catch relevant input.
         # If it's hit, it means an unexpected input during a flow or just random text.
        if chat_id in user_2fa_secrets and "platform" in user_2fa_secrets[chat_id] and "secret" not in user_2fa_secrets[chat_id]:
            # This means they were prompted for a secret key but sent something else.
            # The handle_2fa_secret_input should catch "Back", this is for other random text.
            safe_send_message(chat_id, "⚠️ Unexpected input. Please enter your Base32 secret key or press '⬅️ Back'.", reply_markup=get_back_keyboard())
            bot.register_next_step_handler(m, handle_2fa_secret_input) # Re-register
            return

        safe_send_message(chat_id, "🤔 I'm not sure what you mean. Please use the buttons on the keyboard or available commands.", reply_markup=get_main_keyboard(chat_id))
    else: # Pending approval
        safe_send_message(chat_id, "⏳ Your access request is still pending. Please wait for admin approval.")


# --- Run bot & start threads ---
if __name__ == "__main__":
    print(f"🤖 Bot is starting with Admin ID: {ADMIN_ID}...")
    print(f"Bot started at: {BOT_START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")

    # Start background workers
    threading.Thread(target=auto_refresh_worker, daemon=True).start()
    print("📧 Auto-refresh worker thread started.")
    #threading.Thread(target=cleanup_blocked_users, daemon=True).start() # Optional: can be intensive
    #print("🧹 Blocked user cleanup thread started.")

    # Flask health check for Railway (or similar platforms)
    if os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('RENDER'): # Common PaaS env vars
        app = Flask(__name__)
        @app.route('/')
        def health_check():
            return "Bot is running and healthy!", 200
        
        flask_thread = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False), daemon=True)
        flask_thread.start()
        print(f"🌐 Flask health check endpoint running on port {PORT}.")

    print("🚀 Bot polling started...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)
    except Exception as e_poll:
        print(f"FATAL ERROR during bot polling: {e_poll}")
        # Consider more robust restart or notification logic here for production
    finally:
        print("🛑 Bot polling stopped.")

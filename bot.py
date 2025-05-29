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
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

load_dotenv()
fake = Faker()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise Exception("❌ BOT_TOKEN not set in .env")

# Ensure ADMIN_ID is a string for comparisons
if ADMIN_ID:
    ADMIN_ID = str(ADMIN_ID)
else:
    print("⚠️ WARNING: ADMIN_ID is not set. Admin features will not be fully functional.")


bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown") # Default parse mode

# Data storage
user_data = {}  # Stores mail.tm session info {chat_id: {"email": ..., "token": ...}}
last_message_ids = {} # Stores seen email IDs {chat_id: {msg_id1, msg_id2}}
user_2fa_secrets = {} # Stores user secrets for 2FA {chat_id: {"platform": "Google", "secret": "BASE32SECRET"}}
active_sessions = set() # Stores chat_ids of users who have interacted
pending_approvals = {} # Stores {chat_id: user_info} for users awaiting admin approval
approved_users = set() # Stores chat_ids of approved users
user_profiles = {}  # Stores additional user profile info {chat_id: {"name":..., "username":..., "join_date":...}}

# --- Helper Functions ---

def is_admin(chat_id):
    return str(chat_id) == ADMIN_ID

def safe_delete_user(chat_id):
    if chat_id in user_data:
        del user_data[chat_id]
    if chat_id in last_message_ids:
        del last_message_ids[chat_id]
    # user_2fa_codes was removed as it was unused; user_2fa_secrets holds the necessary data
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
        if e.result_json.get('error_code') == 403 and "bot was blocked" in e.result_json.get('description', ""):
            print(f"Bot blocked by user {chat_id}")
            return True
        # Add check for chat not found (e.g. user deleted account)
        if e.result_json.get('error_code') == 400 and "chat not found" in e.result_json.get('description', ""):
            print(f"Chat not found for user {chat_id}")
            return True
        return False
    except Exception as e:
        print(f"Error in is_bot_blocked for {chat_id}: {e}")
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

def get_user_account_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("📧 My Email", "🆔 My Info")
    keyboard.row("⬅️ Back to Main")
    return keyboard

def get_2fa_platform_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.row("Google", "Facebook", "Instagram")
    keyboard.row("Twitter", "Microsoft", "Apple")
    keyboard.row("⬅️ Back to Main")
    return keyboard

def get_2fa_secret_entry_back_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.row("⬅️ Cancel 2FA Entry") # Specific back button
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
        msg = bot.send_message(chat_id, text, **kwargs)
        active_sessions.add(chat_id)
        return msg
    except telebot.apihelper.ApiTelegramException as e:
        if e.result_json.get('error_code') == 403 and "bot was blocked" in e.result_json.get('description', ""):
            safe_delete_user(chat_id)
        elif e.result_json.get('error_code') == 400 and "chat not found" in e.result_json.get('description', ""):
            safe_delete_user(chat_id)
        else:
            print(f"API Error sending message to {chat_id}: {e}")
        return None
    except Exception as e:
        print(f"General Error sending message to {chat_id}: {str(e)}")
        return None

# Mail.tm functions
def get_domain():
    try:
        res = requests.get("https://api.mail.tm/domains", timeout=10)
        res.raise_for_status()
        domains = res.json().get("hydra:member", [])
        return domains[0]["domain"] if domains and domains[0].get("isActive") else "mail.tm" # Pick active one
    except requests.RequestException as e:
        print(f"Error fetching mail.tm domains: {e}")
        return "mail.tm" # Fallback
    except (KeyError, IndexError) as e:
        print(f"Error parsing mail.tm domain response: {e}")
        return "mail.tm" # Fallback

def generate_email_address(domain): # Renamed to avoid conflict
    name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"{name}@{domain}", name

def create_account(email, password):
    try:
        res = requests.post("https://api.mail.tm/accounts",
                            json={"address": email, "password": password},
                            timeout=10)
        if res.status_code == 201:
            return "created", res.json()
        elif res.status_code == 422: # Unprocessable Entity (likely exists or invalid)
            return "exists_or_invalid", res.json()
        res.raise_for_status() # Raise HTTPError for other bad responses (4xx or 5xx)
        return "error", {"message": f"Unexpected status code {res.status_code}"}
    except requests.RequestException as e:
        print(f"Error creating mail.tm account {email}: {e}")
        return "error", {"message": str(e)}
    except Exception as e:
        print(f"Generic error creating mail.tm account {email}: {e}")
        return "error", {"message": str(e)}


def get_token(email, password):
    time.sleep(1.5) # mail.tm can be slow to register new account for token generation
    try:
        res = requests.post("https://api.mail.tm/token",
                            json={"address": email, "password": password},
                            timeout=10)
        if res.status_code == 200:
            return res.json().get("token")
        # Handle specific error for new accounts not yet ready
        if res.status_code == 401 and "Invalid credentials" in res.text:
            print(f"Token acquisition for {email} failed (401): possibly account not ready or bad creds.")
            return None
        res.raise_for_status()
        return None
    except requests.RequestException as e:
        print(f"Error getting mail.tm token for {email}: {e}")
        return None
    except Exception as e:
        print(f"Generic error getting mail.tm token for {email}: {e}")
        return None


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
        if not cleaned: return False # Empty string is not valid
        # Check if all characters are valid base32 characters
        if not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in cleaned):
            return False
        # pyotp's constructor will also raise an error for invalid length/padding,
        # but we want to catch common char errors first.
        pyotp.TOTP(cleaned).now() # This will try to decode
        return True
    except (binascii.Error, ValueError, Exception):
        return False

def display_2fa_code(chat_id, platform, secret):
    try:
        totp = pyotp.TOTP(secret)
        current_code = totp.now()
        now = datetime.datetime.now()
        seconds_remaining = 30 - (now.second % 30)

        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔄 Refresh Code", callback_data="generate_2fa_code"))
        keyboard.add(InlineKeyboardButton("⬅️ New Secret/Platform", callback_data="2fa_back_to_platform"))

        reply_text = (
            f"Platform: {platform}\n"
            f"<b>2FA CODE</b>\n"
            f"─────────────────────\n"
            f"<code>{current_code}</code> (<i>Expires in {seconds_remaining}s</i>)\n"
            f"─────────────────────\n"
            f"ℹ️ Secret is stored for refresh. Tap code to copy."
        )
        safe_send_message(chat_id, reply_text, parse_mode='HTML', reply_markup=keyboard)
    except Exception as e:
        print(f"Error displaying 2FA code for {chat_id}: {e}")
        safe_send_message(chat_id, "❌ Error generating 2FA code. Please ensure your secret key is correct.", reply_markup=get_2fa_platform_keyboard())
        if chat_id in user_2fa_secrets:
            del user_2fa_secrets[chat_id] # Clean up faulty state

# --- Background Workers ---

def auto_refresh_worker():
    while True:
        try:
            # Create a copy of chat_ids to iterate over, as safe_delete_user might modify user_data
            chat_ids_to_check = list(user_data.keys())
            for chat_id in chat_ids_to_check:
                if not user_data.get(chat_id) or not user_data[chat_id].get("token"): # Check if user still has email session
                    continue

                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id) # This will remove from user_data as well
                    continue

                token = user_data[chat_id]["token"]
                headers = {"Authorization": f"Bearer {token}"}

                try:
                    res = requests.get("https://api.mail.tm/messages", headers=headers, timeout=15) # Increased timeout
                    if res.status_code == 401: # Token expired or invalid
                        print(f"Token expired for user {chat_id}. Clearing email session.")
                        del user_data[chat_id] # Remove email specific data
                        if chat_id in last_message_ids: del last_message_ids[chat_id]
                        safe_send_message(chat_id, "⚠️ Your temporary email session has expired. Please create a new one.")
                        continue
                    res.raise_for_status() # For other HTTP errors

                    messages = res.json().get("hydra:member", [])
                    seen_ids = last_message_ids.setdefault(chat_id, set())

                    new_messages_found = False
                    for msg_summary in messages[:5]: # Check latest 5 messages
                        msg_id = msg_summary["id"]
                        if msg_id in seen_ids:
                            continue
                        new_messages_found = True
                        seen_ids.add(msg_id)

                        try:
                            # Fetch full message details
                            detail_res = requests.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers, timeout=10)
                            if detail_res.status_code == 200:
                                msg_detail = detail_res.json()
                                sender = msg_detail.get("from", {}).get("address", "Unknown Sender")
                                subject = msg_detail.get("subject", "(No Subject)")
                                body_text = msg_detail.get("text") # Prefer text over html for bots
                                if not body_text and msg_detail.get("html"): # Fallback to HTML if text is empty
                                    # Basic HTML to text conversion (very rudimentary)
                                    from bs4 import BeautifulSoup
                                    soup = BeautifulSoup(msg_detail["html"][0], "html.parser")
                                    body_text = soup.get_text(separator='\n')
                                body = body_text.strip() if body_text else "(No Content)"
                                received_at = msg_summary.get('createdAt', 'N/A')
                                if received_at != 'N/A':
                                    try: # Format date
                                        dt_obj = datetime.datetime.fromisoformat(received_at.replace("Z", "+00:00"))
                                        received_at = dt_obj.strftime("%Y-%m-%d %H:%M:%S %Z")
                                    except ValueError:
                                        pass # Keep original if parsing fails

                                formatted_msg = (
                                    f"📬 *New Email Received!*\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"👤 *From:* `{sender}`\n"
                                    f"📨 *Subject:* _{subject}_\n"
                                    f"🕒 *Received:* {received_at}\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"💬 *Body:*\n"
                                    f"{body[:3500]}\n" # Telegram message limit is 4096, leave room for markdown and headers
                                    f"━━━━━━━━━━━━━━━━━━━━"
                                )
                                safe_send_message(chat_id, formatted_msg)
                        except requests.RequestException as req_e:
                            print(f"Error fetching message detail {msg_id} for {chat_id}: {req_e}")
                        except Exception as detail_e:
                            print(f"Error processing message detail {msg_id} for {chat_id}: {detail_e}")
                    # Prune old seen_ids to prevent unbounded growth (keep last 50)
                    if len(seen_ids) > 50:
                        oldest_ids = sorted(list(seen_ids), key=lambda x: msg_summary.get('createdAt', ''))[:-30]
                        for old_id in oldest_ids:
                            if old_id in seen_ids: seen_ids.remove(old_id)


                except requests.RequestException as e:
                    print(f"Mail refresh HTTP error for {chat_id}: {e}")
                    # Consider notifying user or specific handling for 401 (token expiry)
                except Exception as e:
                    print(f"Error in auto_refresh_worker for user {chat_id}: {e}")
        except Exception as e:
            print(f"Overall error in auto_refresh_worker loop: {e}")
        time.sleep(25) # Interval for checking emails

def cleanup_blocked_users():
    while True:
        try:
            # Check all users who have interacted, not just those with active email sessions
            all_known_users = list(approved_users) + list(pending_approvals.keys()) + list(active_sessions)
            sessions_to_check = list(set(all_known_users)) # Unique list

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
    user = message.from_user

    if is_bot_blocked(chat_id): # Should ideally not happen if message is received, but good check
        safe_delete_user(chat_id)
        return

    user_info = get_user_info(user)
    user_profiles[chat_id] = user_info # Store/update profile info
    active_sessions.add(chat_id) # Mark as active

    if is_admin(chat_id):
        approved_users.add(chat_id) # Admin is always approved
        if chat_id in pending_approvals: del pending_approvals[chat_id]
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=get_main_keyboard(chat_id))
        return

    if chat_id in approved_users:
        safe_send_message(chat_id, "👋 Welcome back! You are approved.", reply_markup=get_main_keyboard(chat_id))
    elif chat_id in pending_approvals:
        safe_send_message(chat_id, "⏳ Your access request is still pending admin approval. Please wait.")
    else:
        pending_approvals[chat_id] = user_info
        safe_send_message(chat_id, "👋 Your access request has been sent to the admin. Please wait for approval.")
        if ADMIN_ID and ADMIN_ID != "YOUR_ADMIN_ID_HERE": # Ensure ADMIN_ID is set and not placeholder
            try:
                admin_chat_id = int(ADMIN_ID)
                approval_msg = (
                    f"🆕 *New Approval Request*\n\n"
                    f"🆔 User ID: `{chat_id}`\n"
                    f"👤 Name: `{user_info['name']}`\n"
                    f"📛 Username: @{user_info['username']}\n"
                    f"📅 Requested: `{user_info['join_date']}`"
                )
                bot.send_message(admin_chat_id, approval_msg, reply_markup=get_approval_keyboard(chat_id), parse_mode="Markdown")
            except ValueError:
                print(f"Error: ADMIN_ID ('{ADMIN_ID}') is not a valid integer.")
            except Exception as e:
                print(f"Error sending approval request to admin: {e}")
        else:
            print("Admin ID not set, cannot forward approval request.")


# --- Admin Panel Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel" and is_admin(msg.chat.id))
def admin_panel(message):
    safe_send_message(message.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "👥 Pending Approvals" and is_admin(msg.chat.id))
def show_pending_approvals(message):
    if not pending_approvals:
        safe_send_message(message.chat.id, "✅ No pending approvals.")
        return
    sent_any = False
    for user_id, user_info in list(pending_approvals.items()): # list() for safe iteration if modified
        approval_msg = (
            f"⏳ *Pending Approval*\n\n"
            f"🆔 User ID: `{user_id}`\n"
            f"👤 Name: `{user_info['name']}`\n"
            f"📛 Username: @{user_info['username']}\n"
            f"📅 Requested: `{user_info['join_date']}`"
        )
        safe_send_message(message.chat.id, approval_msg, reply_markup=get_approval_keyboard(user_id))
        sent_any = True
    if not sent_any: # Should not happen if pending_approvals is not empty, but a safeguard
         safe_send_message(message.chat.id, "✅ No pending approvals to display (though list was not empty).")


@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.chat.id))
def show_stats(message):
    # Calculate uptime (assuming bot started when this script ran)
    # For a more accurate uptime, store start_time = datetime.datetime.now() globally
    # For this example, uptime is just current time, implying "since script start"
    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin ID: `{ADMIN_ID}`\n"
        f"👥 Approved Users: `{len(approved_users)}`\n"
        f"👤 Active Users (this session): `{len(active_sessions)}`\n"
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"📧 Active Email Accounts: `{len(user_data)}`\n"
        f"🗓️ Current Time: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
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
    current_page_msg = "👥 *Approved Users (Page 1)*:\n\n"
    count = 0
    page_count = 1

    for user_id in approved_users:
        if user_id == int(ADMIN_ID): # Skip admin in list if desired, or mark as admin
             user_display = f"👑 Admin (`{user_id}`)"
        else:
            user_display = f"🆔 `{user_id}`"

        if user_id in user_profiles:
            user_info = user_profiles[user_id]
            user_display += f" - 👤 {user_info['name']} (@{user_info['username']}) - 📅 Joined: {user_info['join_date']}"
        else:
            user_display += " - (No profile details)"

        current_page_msg += user_display + "\n"
        count += 1

        if count % 10 == 0: # Send in chunks of 10
            users_list_msgs.append(current_page_msg)
            page_count += 1
            current_page_msg = f"👥 *Approved Users (Page {page_count})*:\n\n"

    if current_page_msg.strip() != f"👥 *Approved Users (Page {page_count})*:\n\n".strip(): # Add remaining users
        users_list_msgs.append(current_page_msg)

    if not users_list_msgs:
        safe_send_message(message.chat.id, "❌ No user data available to list (though approved_users is not empty).")
        return

    for msg_chunk in users_list_msgs:
        safe_send_message(message.chat.id, msg_chunk.strip())


@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_user_prompt(message):
    # Using the main admin keyboard's back option is fine here.
    msg = safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=telebot.types.ForceReply(selective=False))
    if msg:
      bot.register_next_step_handler(msg, process_user_removal)

def process_user_removal(message):
    chat_id = message.chat.id # Admin's chat_id
    if message.text.lower() in ["cancel", "/cancel"]: # Allow cancellation
        safe_send_message(chat_id, "Cancelled user removal.", reply_markup=get_user_management_keyboard())
        return
    try:
        user_id_to_remove = int(message.text.strip())
        if user_id_to_remove == int(ADMIN_ID):
            safe_send_message(chat_id, "❌ Cannot remove the admin account!", reply_markup=get_user_management_keyboard())
            return

        removed_from_approved = False
        if user_id_to_remove in approved_users:
            approved_users.remove(user_id_to_remove)
            removed_from_approved = True

        # Also remove from pending if they were somehow there too
        removed_from_pending = False
        if user_id_to_remove in pending_approvals:
            del pending_approvals[user_id_to_remove]
            removed_from_pending = True

        if removed_from_approved or removed_from_pending:
            # Full cleanup for the user
            original_user_name = user_profiles.get(user_id_to_remove, {}).get('name', str(user_id_to_remove))
            safe_delete_user(user_id_to_remove) # This handles all data structures
            safe_send_message(chat_id, f"✅ User {original_user_name} (`{user_id_to_remove}`) has been removed and all their data cleared.", reply_markup=get_user_management_keyboard())
            # Notify user if possible
            safe_send_message(user_id_to_remove, "❌ Your access to this bot has been revoked by an admin. Your data has been cleared.")
        else:
            safe_send_message(chat_id, f"❌ User ID `{user_id_to_remove}` not found in approved or pending users.", reply_markup=get_user_management_keyboard())

    except ValueError:
        safe_send_message(chat_id, "❌ Invalid User ID. Please enter a numeric ID or type 'cancel'.", reply_markup=get_user_management_keyboard())
        # Re-prompt
        msg = safe_send_message(chat_id, "🆔 Enter the User ID to remove (or 'cancel'):", reply_markup=telebot.types.ForceReply(selective=False))
        if msg:
            bot.register_next_step_handler(msg, process_user_removal)
    except Exception as e:
        print(f"Error in process_user_removal: {e}")
        safe_send_message(chat_id, "An error occurred during user removal.", reply_markup=get_user_management_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def broadcast_menu(message):
    safe_send_message(message.chat.id, "📢 Broadcast Message to All Approved Users", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def process_text_broadcast_prompt(message):
    # Using admin keyboard's back option is fine.
    msg = safe_send_message(message.chat.id, "✍️ Enter the broadcast message text (or type /cancel):", reply_markup=telebot.types.ForceReply(selective=False))
    if msg:
        bot.register_next_step_handler(msg, process_text_broadcast)

def process_text_broadcast(message):
    chat_id = message.chat.id # Admin's chat_id
    if message.text == "/cancel":
        safe_send_message(chat_id, "Broadcast cancelled.", reply_markup=get_broadcast_keyboard())
        return

    broadcast_text = message.text
    if not broadcast_text:
        safe_send_message(chat_id, "❌ Broadcast message cannot be empty. Try again or /cancel.", reply_markup=get_broadcast_keyboard())
        return


    # Confirmation step
    confirm_kb = telebot.types.InlineKeyboardMarkup()
    confirm_kb.add(telebot.types.InlineKeyboardButton("✅ Yes, send it!", callback_data="confirm_broadcast_text"))
    confirm_kb.add(telebot.types.InlineKeyboardButton("❌ No, cancel.", callback_data="cancel_broadcast"))
    # Store message for callback
    user_profiles[chat_id]['broadcast_text'] = broadcast_text
    safe_send_message(chat_id, f"Preview:\n\n{broadcast_text}\n\nSend this to all {len(approved_users)} approved users (excluding admin)?", reply_markup=confirm_kb)


@bot.callback_query_handler(func=lambda call: call.data == "confirm_broadcast_text")
def cb_confirm_text_broadcast(call):
    admin_chat_id = call.message.chat.id
    broadcast_text = user_profiles.get(admin_chat_id, {}).get('broadcast_text')

    if not broadcast_text:
        bot.answer_callback_query(call.id, "Error: Broadcast message not found.")
        bot.edit_message_text("Error during broadcast.", admin_chat_id, call.message.message_id, reply_markup=None)
        safe_send_message(admin_chat_id, "Could not retrieve broadcast text. Please try again.", reply_markup=get_broadcast_keyboard())
        return

    bot.edit_message_text(f"📢 Broadcasting text to {len(approved_users)} users...", admin_chat_id, call.message.message_id, reply_markup=None)
    success = 0
    failed = 0
    # Create a snapshot of users to broadcast to
    users_to_broadcast = [uid for uid in approved_users if uid != admin_chat_id] # Exclude admin from broadcast
    total_to_send = len(users_to_broadcast)

    if total_to_send == 0:
        safe_send_message(admin_chat_id, "No other approved users to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_text = f"📢 Broadcasting to {total_to_send} users...\n\n0/{total_to_send} sent"
    progress_message = safe_send_message(admin_chat_id, progress_msg_text)


    for i, user_id_to_send in enumerate(users_to_broadcast, 1):
        try:
            # Add a header to the broadcast message for clarity
            user_msg = f"📢 *Admin Broadcast:*\n\n{broadcast_text}"
            sent_msg = safe_send_message(user_id_to_send, user_msg)
            if sent_msg:
                success += 1
            else: # safe_send_message handles blocked/deleted users internally
                failed +=1
        except Exception as e: # Catch any other unexpected errors
            print(f"Unexpected error broadcasting text to {user_id_to_send}: {e}")
            failed += 1

        if progress_message and (i % 5 == 0 or i == total_to_send): # Update every 5 users or at the end
            try:
                bot.edit_message_text(
                    f"📢 Broadcasting to {total_to_send} users...\n\n{i}/{total_to_send} attempted\n✅ {success} successful\n❌ {failed} failed",
                    chat_id=admin_chat_id,
                    message_id=progress_message.message_id
                )
            except Exception as edit_e:
                print(f"Error updating broadcast progress: {edit_e}") # Continue if edit fails

    final_status_msg = f"📢 Text Broadcast Completed!\n\n✅ Sent to {success} users.\n❌ Failed for {failed} users."
    safe_send_message(admin_chat_id, final_status_msg, reply_markup=get_admin_keyboard())
    if admin_chat_id in user_profiles and 'broadcast_text' in user_profiles[admin_chat_id]:
        del user_profiles[admin_chat_id]['broadcast_text'] # Clean up
    bot.answer_callback_query(call.id, "Broadcast initiated.")


@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def media_broadcast_prompt(message):
    msg = safe_send_message(message.chat.id, "🖼 Send the photo/video/document you want to broadcast (with caption if needed, or type /cancel):", reply_markup=telebot.types.ForceReply(selective=False))
    if msg:
        bot.register_next_step_handler(msg, process_media_broadcast_confirm)

def process_media_broadcast_confirm(message):
    admin_chat_id = message.chat.id
    if message.text == "/cancel": # Check for text cancel first
        safe_send_message(admin_chat_id, "Media broadcast cancelled.", reply_markup=get_broadcast_keyboard())
        return

    media_type = None
    file_id = None
    caption = message.caption if message.caption else ""

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
    else:
        safe_send_message(admin_chat_id, "❌ No media detected. Please send a photo, video, or document. Or type /cancel.", reply_markup=get_broadcast_keyboard())
        # Re-prompt
        msg = safe_send_message(admin_chat_id, "🖼 Send the media again (or /cancel):", reply_markup=telebot.types.ForceReply(selective=False))
        if msg:
          bot.register_next_step_handler(msg, process_media_broadcast_confirm)
        return

    # Store for callback
    user_profiles[admin_chat_id]['broadcast_media'] = {'type': media_type, 'file_id': file_id, 'caption': caption}

    confirm_kb = telebot.types.InlineKeyboardMarkup()
    confirm_kb.add(telebot.types.InlineKeyboardButton("✅ Yes, send it!", callback_data="confirm_broadcast_media"))
    confirm_kb.add(telebot.types.InlineKeyboardButton("❌ No, cancel.", callback_data="cancel_broadcast"))

    preview_text = f"You are about to broadcast this {media_type} "
    if caption:
        preview_text += f"with caption:\n'{caption[:100]}{'...' if len(caption) > 100 else ''}'\n\n"
    else:
        preview_text += "(no caption).\n\n"
    preview_text += f"Send to all {len(approved_users)} approved users (excluding admin)?"
    safe_send_message(admin_chat_id, preview_text, reply_markup=confirm_kb)


@bot.callback_query_handler(func=lambda call: call.data == "confirm_broadcast_media")
def cb_confirm_media_broadcast(call):
    admin_chat_id = call.message.chat.id
    media_info = user_profiles.get(admin_chat_id, {}).get('broadcast_media')

    if not media_info:
        bot.answer_callback_query(call.id, "Error: Broadcast media not found.")
        bot.edit_message_text("Error during media broadcast.", admin_chat_id, call.message.message_id, reply_markup=None)
        safe_send_message(admin_chat_id, "Could not retrieve media for broadcast. Please try again.", reply_markup=get_broadcast_keyboard())
        return

    bot.edit_message_text(f"📢 Broadcasting {media_info['type']} to {len(approved_users)} users...", admin_chat_id, call.message.message_id, reply_markup=None)

    success = 0
    failed = 0
    users_to_broadcast = [uid for uid in approved_users if uid != admin_chat_id]
    total_to_send = len(users_to_broadcast)

    if total_to_send == 0:
        safe_send_message(admin_chat_id, "No other approved users to broadcast media to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_text = f"📢 Broadcasting {media_info['type']} to {total_to_send} users...\n\n0/{total_to_send} sent"
    progress_message = safe_send_message(admin_chat_id, progress_msg_text)


    for i, user_id_to_send in enumerate(users_to_broadcast, 1):
        try:
            caption_with_header = f"📢 *Admin Broadcast:*\n\n{media_info['caption']}" if media_info['caption'] else "📢 *Admin Broadcast:*"
            sent_successfully = False
            if media_info['type'] == "photo":
                bot.send_photo(user_id_to_send, media_info['file_id'], caption=caption_with_header, parse_mode="Markdown")
                sent_successfully = True
            elif media_info['type'] == "video":
                bot.send_video(user_id_to_send, media_info['file_id'], caption=caption_with_header, parse_mode="Markdown")
                sent_successfully = True
            elif media_info['type'] == "document":
                bot.send_document(user_id_to_send, media_info['file_id'], caption=caption_with_header, parse_mode="Markdown")
                sent_successfully = True

            if sent_successfully: # Crude check, as send_photo etc. don't return easily checkable status for actual delivery
                success += 1
            else: # This else might not be hit if ApiTelegramException is raised and caught below
                failed += 1

        except telebot.apihelper.ApiTelegramException as api_ex:
            if "bot was blocked" in str(api_ex) or "user is deactivated" in str(api_ex) or "chat not found" in str(api_ex):
                print(f"User {user_id_to_send} blocked or inactive. Marking as failed.")
                failed += 1
                safe_delete_user(user_id_to_send) # Clean up this user
            else:
                print(f"API error broadcasting media to {user_id_to_send}: {api_ex}")
                failed += 1
        except Exception as e:
            print(f"Unexpected error broadcasting media to {user_id_to_send}: {e}")
            failed += 1

        if progress_message and (i % 5 == 0 or i == total_to_send):
            try:
                bot.edit_message_text(
                    f"📢 Broadcasting {media_info['type']} to {total_to_send} users...\n\n{i}/{total_to_send} attempted\n✅ {success} successful\n❌ {failed} failed",
                    chat_id=admin_chat_id,
                    message_id=progress_message.message_id
                )
            except Exception as edit_e:
                print(f"Error updating media broadcast progress: {edit_e}")


    final_status_msg = f"📢 Media Broadcast ({media_info['type']}) Completed!\n\n✅ Sent to {success} users.\n❌ Failed for {failed} users."
    safe_send_message(admin_chat_id, final_status_msg, reply_markup=get_admin_keyboard())
    if admin_chat_id in user_profiles and 'broadcast_media' in user_profiles[admin_chat_id]:
        del user_profiles[admin_chat_id]['broadcast_media']
    bot.answer_callback_query(call.id, f"{media_info['type'].capitalize()} broadcast initiated.")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_broadcast")
def cb_cancel_broadcast(call):
    admin_chat_id = call.message.chat.id
    bot.edit_message_text("Broadcast cancelled by admin.", admin_chat_id, call.message.message_id, reply_markup=None)
    safe_send_message(admin_chat_id, "Broadcast operation cancelled.", reply_markup=get_broadcast_keyboard())
    # Clean up stored broadcast data
    if admin_chat_id in user_profiles:
        if 'broadcast_text' in user_profiles[admin_chat_id]:
            del user_profiles[admin_chat_id]['broadcast_text']
        if 'broadcast_media' in user_profiles[admin_chat_id]:
            del user_profiles[admin_chat_id]['broadcast_media']
    bot.answer_callback_query(call.id, "Broadcast cancelled.")


@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin" and is_admin(msg.chat.id))
def back_to_admin(message):
    safe_send_message(message.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu") # Generic handler for this button
def general_back_to_main_menu(message):
    chat_id = message.chat.id
    # Clear any pending 2FA state if user explicitly goes to main menu
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]
        print(f"Cleared 2FA context for {chat_id} due to Main Menu navigation.")
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))


@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def handle_approval(call):
    admin_chat_id = call.message.chat.id
    if not is_admin(admin_chat_id): #Should not happen if button only shown to admin
        bot.answer_callback_query(call.id, "Error: Not an admin.")
        return

    try:
        action, user_id_str = call.data.split('_')
        user_id = int(user_id_str)
    except ValueError:
        bot.answer_callback_query(call.id, "Error: Invalid user ID in callback.")
        bot.edit_message_text("Error processing approval: Invalid user ID.", admin_chat_id, call.message.message_id, reply_markup=None)
        return

    user_info = pending_approvals.get(user_id, user_profiles.get(user_id)) # Get info if available

    if action == "approve":
        approved_users.add(user_id)
        if user_id in pending_approvals:
            del pending_approvals[user_id]
        # Ensure user_profile exists for approved user if they re-interact
        if user_id not in user_profiles and user_info:
            user_profiles[user_id] = user_info
        elif user_id not in user_profiles: # Fallback if somehow no info was stored
             user_profiles[user_id] = {"name": "Unknown", "username": "N/A", "join_date": "N/A"}


        safe_send_message(user_id, "✅ Your access request has been approved by the admin! You can now use the bot.", reply_markup=get_main_keyboard(user_id))
        bot.answer_callback_query(call.id, f"User {user_id} approved.")
        bot.edit_message_text(f"✅ User {user_info['name'] if user_info else user_id} (`{user_id}`) approved.", admin_chat_id, call.message.message_id, reply_markup=None)
    elif action == "reject":
        if user_id in pending_approvals:
            del pending_approvals[user_id]
        # Optionally, fully delete rejected user's data if not needed for logs
        # safe_delete_user(user_id) # Uncomment if rejected users should be fully wiped immediately

        safe_send_message(user_id, "❌ Your access request has been rejected by the admin.")
        bot.answer_callback_query(call.id, f"User {user_id} rejected.")
        bot.edit_message_text(f"❌ User {user_info['name'] if user_info else user_id} (`{user_id}`) rejected.", admin_chat_id, call.message.message_id, reply_markup=None)
    else:
        bot.answer_callback_query(call.id, "Unknown action.")
        bot.edit_message_text("Unknown approval action.", admin_chat_id, call.message.message_id, reply_markup=None)


# --- User Account Handlers (Non-Admin) ---
@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
def my_account_info(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or has been revoked.")
        return
    safe_send_message(chat_id, "👤 Account Options:", reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📧 My Email")
def my_email_info(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or has been revoked.")
        return

    if chat_id in user_data and user_data[chat_id].get("email"):
        email = user_data[chat_id]["email"]
        safe_send_message(chat_id, f"📧 Your current temporary email is: `{email}`\nTap to copy.", reply_markup=get_user_account_keyboard())
    else:
        safe_send_message(chat_id, "📬 You don't have an active temporary email. Create one using 'New mail'.", reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "🆔 My Info")
def my_telegram_info(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or has been revoked.")
        return

    user_info = user_profiles.get(chat_id)
    if user_info:
        info_text = (
            f"👤 *Your Telegram Info:*\n\n"
            f"🆔 User ID: `{chat_id}`\n"
            f"🗣️ Name: `{user_info['name']}`\n"
            f"📛 Username: `@{user_info['username']}`\n"
            f"🗓️ Bot Join Date: `{user_info['join_date']}`\n"
            f"✅ Access Status: Approved"
        )
    else: # Should not happen if /start logic is correct
        info_text = f"🆔 Your User ID: `{chat_id}`\n✅ Access Status: Approved\n(Detailed profile info not found)"
    safe_send_message(chat_id, info_text, reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main" and not is_admin(msg.chat.id)) # For non-admins using this button from account screen
def user_back_to_main(message):
    chat_id = message.chat.id
    if chat_id in user_2fa_secrets: # Clear 2FA context if any
        del user_2fa_secrets[chat_id]
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))


# --- Mail handlers ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval. You cannot create an email yet.")
        return

    # If user already has an email, maybe ask if they want to replace it?
    # For now, just create a new one, overwriting old session.
    loading_msg = safe_send_message(chat_id, "⏳ Generating new temporary email, please wait...")

    domain = get_domain()
    email, _ = generate_email_address(domain) # Using renamed function
    # Generate a strong random password for mail.tm account
    temp_password = ''.join(random.choices(string.ascii_letters + string.digits + string.punctuation, k=16))

    status, acc_data = create_account(email, temp_password)

    if status == "created":
        token = get_token(email, temp_password)
        if token:
            user_data[chat_id] = {"email": email, "password": temp_password, "token": token, "id": acc_data.get("id")}
            last_message_ids[chat_id] = set() # Reset seen messages
            msg_text = f"✅ *Temporary Email Created!*\n\n`{email}`\n\nTap to copy. Inbox will be checked automatically."
            if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
            safe_send_message(chat_id, msg_text, reply_markup=get_main_keyboard(chat_id))
        else:
            if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
            safe_send_message(chat_id, "❌ Failed to log in to the new email account. The account might have been created, but token generation failed. Try again or contact admin.", reply_markup=get_main_keyboard(chat_id))
    elif status == "exists_or_invalid":
        # This could be a collision or an issue with mail.tm's validation. Try again.
        if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
        safe_send_message(chat_id, "❌ Email address generation conflict or invalid. Please try 'New mail' again.", reply_markup=get_main_keyboard(chat_id))
    else: # 'error'
        if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
        error_detail = acc_data.get('message', 'Unknown reason.')
        safe_send_message(chat_id, f"❌ Could not create temporary email: {error_detail}. Please try again later.", reply_markup=get_main_keyboard(chat_id))


@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh")
def refresh_mail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return

    if chat_id not in user_data or not user_data[chat_id].get("token"):
        safe_send_message(chat_id, "⚠️ Please create a new email first using '📬 New mail'.")
        return

    loading_msg = safe_send_message(chat_id, "🔄 Checking your inbox for new mail...")
    token = user_data[chat_id]["token"]
    headers = {"Authorization": f"Bearer {token}"}
    any_new_message_displayed = False

    try:
        res = requests.get("https://api.mail.tm/messages", headers=headers, timeout=15)
        if res.status_code == 401: # Token expired
            if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
            safe_send_message(chat_id, "⚠️ Your temporary email session has expired. Please create a new one.")
            del user_data[chat_id] # Clear expired session
            if chat_id in last_message_ids: del last_message_ids[chat_id]
            return
        res.raise_for_status() # For other errors

        messages = res.json().get("hydra:member", [])
        if not messages:
            if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
            safe_send_message(chat_id, "📭 *Your inbox is currently empty.*")
            return

        # We only show new messages on manual refresh or a summary.
        # The auto-refresh worker handles pushing all new messages.
        # For manual refresh, let's show the latest few if they haven't been "seen" by manual refresh.
        # Or, more simply, just show the latest 1-3 messages regardless of auto-push.

        seen_ids_for_manual_refresh = last_message_ids.setdefault(chat_id, set()) # Use the main seen_ids
        new_messages_manually_shown = 0

        for msg_summary in messages[:3]: # Show up to 3 latest messages
            msg_id = msg_summary["id"]
            # If you want manual refresh to ONLY show things not pushed by auto, this logic gets complex.
            # Simplest: manual refresh shows latest, auto-refresh also pushes. User might see duplicates if they time it right.
            # Alternative: manual refresh primarily for checking if auto-refresh is working or if user missed a notification.

            try:
                detail_res = requests.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers, timeout=10)
                detail_res.raise_for_status()
                msg_detail = detail_res.json()
                sender = msg_detail.get("from", {}).get("address", "Unknown Sender")
                subject = msg_detail.get("subject", "(No Subject)")
                body_text = msg_detail.get("text")
                if not body_text and msg_detail.get("html"):
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(msg_detail["html"][0], "html.parser")
                    body_text = soup.get_text(separator='\n')
                body = body_text.strip() if body_text else "(No Content)"
                received_at = msg_summary.get('createdAt', 'N/A')
                if received_at != 'N/A':
                    try:
                        dt_obj = datetime.datetime.fromisoformat(received_at.replace("Z", "+00:00"))
                        received_at = dt_obj.strftime("%Y-%m-%d %H:%M:%S %Z")
                    except ValueError:
                        pass

                formatted_msg = (
                    f"📩 *Email from Inbox:*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *From:* `{sender}`\n"
                    f"📨 *Subject:* _{subject}_\n"
                    f"🕒 *Received:* {received_at}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💬 *Body:*\n"
                    f"{body[:3500]}\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )
                safe_send_message(chat_id, formatted_msg)
                any_new_message_displayed = True
                new_messages_manually_shown +=1
                seen_ids_for_manual_refresh.add(msg_id) # Mark as seen by this refresh

            except requests.RequestException as e_detail:
                print(f"Error fetching detail for msg {msg_id} on refresh: {e_detail}")
                safe_send_message(chat_id, f"⚠️ Error loading one message (ID: {msg_id}).")
            except Exception as e_proc:
                print(f"Error processing message {msg_id} on refresh: {e_proc}")
                safe_send_message(chat_id, f"⚠️ Error processing one message (ID: {msg_id}).")

        if loading_msg: bot.delete_message(chat_id, loading_msg.message_id) # Delete loading message
        if not any_new_message_displayed: # If loop completed but nothing shown (e.g. all were errors)
            safe_send_message(chat_id, "✅ No new messages found in the latest check, or already displayed.")
        elif new_messages_manually_shown > 0 :
             safe_send_message(chat_id, f"✅ Refresh complete. Displayed {new_messages_manually_shown} message(s).")


    except requests.RequestException as e:
        if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
        safe_send_message(chat_id, f"❌ Connection error during refresh: {e}. Try again later.")
    except Exception as e_gen:
        if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
        print(f"Generic error in refresh_mail for {chat_id}: {e_gen}")
        safe_send_message(chat_id, "❌ An unexpected error occurred while refreshing. Please try again.")


# --- Profile handlers ---
@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
def generate_profile_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return

    gender = "male" if message.text == "👨 Male Profile" else "female"
    # This is a compute-intensive task (potentially, if fake library is slow)
    # Consider running in a thread if it causes blocking, though usually fast enough.
    g, name, username, pwd, phone = generate_profile(gender)
    message_text = profile_message(g, name, username, pwd, phone)
    safe_send_message(chat_id, message_text)

# --- 2FA Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def two_fa_auth_start(message): # Renamed to avoid conflict
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    # Clear any previous 2FA state for this user before starting a new one
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]
    safe_send_message(chat_id, "🔐 Choose the platform for 2FA code generation:", reply_markup=get_2fa_platform_keyboard())


@bot.message_handler(func=lambda msg: msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform_selection(message): # Renamed
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)): # Redundant if previous handler caught, but good practice
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return

    platform = message.text
    user_2fa_secrets[chat_id] = {"platform": platform} # Store platform, await secret
    # Use ForceReply to guide user input for the secret
    reply_msg = safe_send_message(chat_id, f"🔢 Enter the 2FA secret key for *{platform}* (Base32 format).\n\nType /cancel2fa to abort.",
                                  reply_markup=telebot.types.ForceReply(selective=True)) # Changed to ForceReply for specific secret input
    if reply_msg:
        # The next message from this user will be handled by the generic text handler,
        # which needs to check if it's a secret key input.
        pass


# This handler will catch the 2FA secret key after platform selection due to ForceReply
# It also handles "⬅️ Cancel 2FA Entry" if that custom keyboard was used.
# And general unhandled text.
@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_all_text_messages(message):
    chat_id = message.chat.id
    text = message.text.strip()

    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)) and text not in ['/start', '/help']: # Allow start/help for new users
        # Check if they are in pending_approvals
        if chat_id in pending_approvals:
             safe_send_message(chat_id, "⏳ Your access request is still pending admin approval. Please wait.")
        else: # Not pending, not approved - likely rejected or data lost. Prompt to /start.
             safe_send_message(chat_id, "⚠️ Your access is not currently approved. Please use /start if you wish to request access.")
        return

    # 1. Handle 2FA Secret Key Input State
    if chat_id in user_2fa_secrets and "platform" in user_2fa_secrets[chat_id] and "secret" not in user_2fa_secrets[chat_id]:
        if text.lower() == "/cancel2fa":
            del user_2fa_secrets[chat_id]
            safe_send_message(chat_id, "2FA secret key entry cancelled. Choose a platform or go back.", reply_markup=get_2fa_platform_keyboard())
            return

        secret_key = text.upper().replace(" ", "").replace("-", "") # Clean common issues
        if is_valid_base32(secret_key):
            user_2fa_secrets[chat_id]["secret"] = secret_key
            platform = user_2fa_secrets[chat_id]["platform"]
            safe_send_message(chat_id, f"✅ Secret key for *{platform}* accepted. Generating code...", reply_markup=telebot.types.ReplyKeyboardRemove()) # Remove ForceReply kbd
            display_2fa_code(chat_id, platform, secret_key)
        else:
            # Re-prompt for secret
            safe_send_message(chat_id, "❌ *Invalid Base32 Secret Key.*\n\nIt should only contain `A-Z` and `2-7`.\nPlease enter a valid key or type /cancel2fa.",
                              reply_markup=telebot.types.ForceReply(selective=True))
        return # End processing for this message

    # 2. Handle other specific text commands or replies if any (e.g. from ForceReply in admin section)
    # (Covered by register_next_step_handler mostly)

    # 3. Fallback for unhandled text - maybe offer help or main menu
    if text not in ["📬 New mail", "🔄 Refresh", "👨 Male Profile", "👩 Female Profile", "🔐 2FA Auth", "👤 My Account",
                    "👑 Admin Panel", "⬅️ Main Menu", # Add admin options if user is admin
                    "Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple", # 2FA platforms
                    "⬅️ Back to Admin", "👥 Pending Approvals", "📊 Stats", "👤 User Management", "📢 Broadcast",
                    "📜 List Users", "❌ Remove User", "📢 Text Broadcast", "📋 Media Broadcast",
                    "📧 My Email", "🆔 My Info"
                    ]: # Avoid replying to known button presses
        if chat_id in approved_users or is_admin(chat_id): # Only for approved users
            safe_send_message(chat_id, f"🤔 I didn't understand '{text}'. Please use the buttons or commands.", reply_markup=get_main_keyboard(chat_id))
        # else: unapproved users are handled at the start of this function.


@bot.callback_query_handler(func=lambda call: call.data == "generate_2fa_code")
def cb_generate_2fa_code_refresh(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id, "Refreshing code...")

    if chat_id not in user_2fa_secrets or "secret" not in user_2fa_secrets[chat_id] or "platform" not in user_2fa_secrets[chat_id]:
        bot.edit_message_text("❌ Error: 2FA secret or platform not found. Please start over.",
                              chat_id, call.message.message_id, reply_markup=None)
        safe_send_message(chat_id, "Please select a platform for 2FA again.", reply_markup=get_2fa_platform_keyboard())
        return

    secret = user_2fa_secrets[chat_id]["secret"]
    platform = user_2fa_secrets[chat_id]["platform"]

    try:
        totp = pyotp.TOTP(secret)
        current_code = totp.now()
        now_time = datetime.datetime.now()
        seconds_remaining = 30 - (now_time.second % 30)

        # Keyboard remains the same (refresh and back to platform)
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔄 Refresh Code", callback_data="generate_2fa_code"))
        keyboard.add(InlineKeyboardButton("⬅️ New Secret/Platform", callback_data="2fa_back_to_platform"))

        reply_text = (
            f"Platform: {platform}\n"
            f"<b>2FA CODE</b>\n"
            f"─────────────────────\n"
            f"<code>{current_code}</code> (<i>Expires in {seconds_remaining}s</i>)\n"
            f"─────────────────────\n"
            f"ℹ️ Secret is stored for refresh. Tap code to copy."
        )
        bot.edit_message_text(reply_text, chat_id, call.message.message_id,
                              parse_mode='HTML', reply_markup=keyboard)
    except Exception as e:
        print(f"Error refreshing 2FA code for {chat_id}: {e}")
        bot.edit_message_text("❌ Error generating new code. Check your secret key and try starting 2FA setup again.",
                              chat_id, call.message.message_id, reply_markup=None)
        if chat_id in user_2fa_secrets: del user_2fa_secrets[chat_id] # Clear corrupted state
        safe_send_message(chat_id, "Please select a platform for 2FA again.", reply_markup=get_2fa_platform_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "2fa_back_to_platform")
def cb_2fa_back_to_platform_selection(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id, "Returning to platform selection.")
    bot.delete_message(chat_id, call.message.message_id) # Delete the code message

    # Clear the specific secret and platform choice, user will re-select
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]

    safe_send_message(chat_id, "🔐 Choose the platform for 2FA code generation:", reply_markup=get_2fa_platform_keyboard())


if __name__ == '__main__':
    print("🤖 Bot is preparing to launch...")
    if not ADMIN_ID or ADMIN_ID == "YOUR_ADMIN_ID_HERE":
        print("🚨 CRITICAL: ADMIN_ID is not set in your .env file or is set to placeholder.")
        print("🚨 The bot may not function correctly, especially admin features and user approvals.")
        # Depending on desired behavior, you might exit here:
        # raise Exception("ADMIN_ID not configured. Bot cannot start.")

    # Start background threads
    threading.Thread(target=auto_refresh_worker, daemon=True, name="AutoRefreshMail").start()
    threading.Thread(target=cleanup_blocked_users, daemon=True, name="CleanupBlockedUsers").start()

    print(f"🎉 Bot is running with ADMIN_ID: {ADMIN_ID}")
    print(f"Py نسخه TeleBot: {telebot.__version__}")
    bot.infinity_polling(timeout=60, long_polling_timeout=30, none_stop=True) # Added timeouts & none_stop

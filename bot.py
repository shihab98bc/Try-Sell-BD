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
from bs4 import BeautifulSoup # For HTML parsing in emails

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
        if e.result_json.get('error_code') == 400 and "chat not found" in e.result_json.get('description', ""):
            print(f"Chat not found for user {chat_id}")
            return True
        return False
    except Exception as e:
        print(f"Error in is_bot_blocked for {chat_id}: {e}")
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
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.row("Google", "Facebook", "Instagram")
    keyboard.row("Twitter", "Microsoft", "Apple")
    keyboard.row("⬅️ Back to Main")
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
        return domains[0]["domain"] if domains and domains[0].get("isActive") else "mail.tm"
    except requests.RequestException as e:
        print(f"Error fetching mail.tm domains: {e}")
        return "mail.tm"
    except (KeyError, IndexError) as e:
        print(f"Error parsing mail.tm domain response: {e}")
        return "mail.tm"

def generate_email_address(domain):
    name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"{name}@{domain}", name

def create_account(email, password):
    try:
        res = requests.post("https://api.mail.tm/accounts",
                            json={"address": email, "password": password},
                            timeout=10)
        if res.status_code == 201:
            return "created", res.json()
        elif res.status_code == 422:
            return "exists_or_invalid", res.json()
        res.raise_for_status()
        return "error", {"message": f"Unexpected status code {res.status_code}"}
    except requests.RequestException as e:
        print(f"Error creating mail.tm account {email}: {e}")
        return "error", {"message": str(e)}
    except Exception as e:
        print(f"Generic error creating mail.tm account {email}: {e}")
        return "error", {"message": str(e)}


def get_token(email, password):
    time.sleep(1.5)
    try:
        res = requests.post("https://api.mail.tm/token",
                            json={"address": email, "password": password},
                            timeout=10)
        if res.status_code == 200:
            return res.json().get("token")
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
    try:
        cleaned = secret.replace(" ", "").replace("-", "").upper()
        if not cleaned: return False
        if not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in cleaned):
            return False
        pyotp.TOTP(cleaned).now()
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
            del user_2fa_secrets[chat_id]

# --- Background Workers ---

def auto_refresh_worker():
    while True:
        try:
            chat_ids_to_check = list(user_data.keys())
            for chat_id in chat_ids_to_check:
                if not user_data.get(chat_id) or not user_data[chat_id].get("token"):
                    continue

                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id)
                    continue

                token = user_data[chat_id]["token"]
                headers = {"Authorization": f"Bearer {token}"}

                try:
                    res = requests.get("https://api.mail.tm/messages", headers=headers, timeout=15)
                    if res.status_code == 401:
                        print(f"Token expired for user {chat_id}. Clearing email session.")
                        del user_data[chat_id]
                        if chat_id in last_message_ids: del last_message_ids[chat_id]
                        safe_send_message(chat_id, "⚠️ Your temporary email session has expired. Please create a new one.")
                        continue
                    res.raise_for_status()

                    messages = res.json().get("hydra:member", [])
                    seen_ids = last_message_ids.setdefault(chat_id, set())

                    for msg_summary in messages[:5]:
                        msg_id = msg_summary["id"]
                        if msg_id in seen_ids:
                            continue
                        seen_ids.add(msg_id)

                        try:
                            detail_res = requests.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers, timeout=10)
                            if detail_res.status_code == 200:
                                msg_detail = detail_res.json()
                                sender = msg_detail.get("from", {}).get("address", "Unknown Sender")
                                subject = msg_detail.get("subject", "(No Subject)")
                                body_text = msg_detail.get("text")
                                if not body_text and msg_detail.get("html"):
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
                                    f"📬 *New Email Received!*\n"
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
                        except requests.RequestException as req_e:
                            print(f"Error fetching message detail {msg_id} for {chat_id}: {req_e}")
                        except Exception as detail_e:
                            print(f"Error processing message detail {msg_id} for {chat_id}: {detail_e}")
                    if len(seen_ids) > 50: # type: ignore
                        # Ensure msg_summary is available for key in sort or use a default
                        oldest_ids = sorted(list(seen_ids), key=lambda id_val: messages[0].get('createdAt', '') if messages else '')[:-30]
                        for old_id in oldest_ids:
                            if old_id in seen_ids: seen_ids.remove(old_id)
                except requests.RequestException as e:
                    print(f"Mail refresh HTTP error for {chat_id}: {e}")
                except Exception as e:
                    print(f"Error in auto_refresh_worker for user {chat_id}: {e}")
        except Exception as e:
            print(f"Overall error in auto_refresh_worker loop: {e}")
        time.sleep(25)

def cleanup_blocked_users():
    while True:
        try:
            all_known_users = list(approved_users) + list(pending_approvals.keys()) + list(active_sessions)
            sessions_to_check = list(set(all_known_users))

            for chat_id in sessions_to_check:
                if is_bot_blocked(chat_id):
                    print(f"Cleanup: User {chat_id} blocked the bot. Removing data.")
                    safe_delete_user(chat_id)
        except Exception as e:
            print(f"Error in cleanup_blocked_users: {e}")
        time.sleep(3600)

# --- Bot Handlers ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    chat_id = message.chat.id
    user = message.from_user

    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return

    user_info = get_user_info(user)
    user_profiles[chat_id] = user_info
    active_sessions.add(chat_id)

    if is_admin(chat_id):
        approved_users.add(chat_id)
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
        if ADMIN_ID and ADMIN_ID != "YOUR_ADMIN_ID_HERE":
            try:
                admin_chat_id_int = int(ADMIN_ID)
                approval_msg_text = (
                    f"🆕 *New Approval Request*\n\n"
                    f"🆔 User ID: `{chat_id}`\n"
                    f"👤 Name: `{user_info['name']}`\n"
                    f"📛 Username: @{user_info['username']}\n"
                    f"📅 Requested: `{user_info['join_date']}`"
                )
                bot.send_message(admin_chat_id_int, approval_msg_text, reply_markup=get_approval_keyboard(chat_id), parse_mode="Markdown")
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
    for user_id, user_info_data in list(pending_approvals.items()):
        approval_msg_text = (
            f"⏳ *Pending Approval*\n\n"
            f"🆔 User ID: `{user_id}`\n"
            f"👤 Name: `{user_info_data['name']}`\n"
            f"📛 Username: @{user_info_data['username']}\n"
            f"📅 Requested: `{user_info_data['join_date']}`"
        )
        safe_send_message(message.chat.id, approval_msg_text, reply_markup=get_approval_keyboard(user_id))
        sent_any = True
    if not sent_any:
         safe_send_message(message.chat.id, "✅ No pending approvals to display (though list was not empty).")


@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.chat.id))
def show_stats(message):
    stats_msg_text = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin ID: `{ADMIN_ID}`\n"
        f"👥 Approved Users: `{len(approved_users)}`\n"
        f"👤 Active Users (this session): `{len(active_sessions)}`\n"
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"📧 Active Email Accounts: `{len(user_data)}`\n"
        f"🗓️ Current Time: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
    )
    safe_send_message(message.chat.id, stats_msg_text)

@bot.message_handler(func=lambda msg: msg.text == "👤 User Management" and is_admin(msg.chat.id))
def user_management(message):
    safe_send_message(message.chat.id, "👤 User Management Panel", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📜 List Users" and is_admin(msg.chat.id))
def list_users(message):
    if not approved_users:
        safe_send_message(message.chat.id, "❌ No approved users yet.")
        return

    users_list_msgs_chunks = []
    current_page_msg_text = "👥 *Approved Users (Page 1)*:\n\n"
    count = 0
    page_count = 1

    for user_id_val in approved_users:
        user_display_text = ""
        # Ensure ADMIN_ID is comparable as int if user_id_val is int
        admin_id_int = 0
        if ADMIN_ID:
            try:
                admin_id_int = int(ADMIN_ID)
            except ValueError:
                print(f"Warning: Could not convert ADMIN_ID '{ADMIN_ID}' to int for comparison.")
        
        if user_id_val == admin_id_int:
             user_display_text = f"👑 Admin (`{user_id_val}`)"
        else:
            user_display_text = f"🆔 `{user_id_val}`"

        if user_id_val in user_profiles:
            user_info_val = user_profiles[user_id_val]
            user_display_text += f" - 👤 {user_info_val['name']} (@{user_info_val['username']}) - 📅 Joined: {user_info_val['join_date']}"
        else:
            user_display_text += " - (No profile details)"

        current_page_msg_text += user_display_text + "\n"
        count += 1

        if count % 10 == 0:
            users_list_msgs_chunks.append(current_page_msg_text)
            page_count += 1
            current_page_msg_text = f"👥 *Approved Users (Page {page_count})*:\n\n"

    if current_page_msg_text.strip() != f"👥 *Approved Users (Page {page_count})*:\n\n".strip():
        users_list_msgs_chunks.append(current_page_msg_text)

    if not users_list_msgs_chunks:
        safe_send_message(message.chat.id, "❌ No user data available to list (though approved_users is not empty).")
        return

    for msg_chunk_val in users_list_msgs_chunks:
        safe_send_message(message.chat.id, msg_chunk_val.strip())


@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_user_prompt(message):
    msg_reply = safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=telebot.types.ForceReply(selective=False))
    if msg_reply:
      bot.register_next_step_handler(msg_reply, process_user_removal)

def process_user_removal(message):
    chat_id_admin = message.chat.id
    if message.text.lower() in ["cancel", "/cancel"]:
        safe_send_message(chat_id_admin, "Cancelled user removal.", reply_markup=get_user_management_keyboard())
        return
    try:
        user_id_to_remove_val = int(message.text.strip())
        admin_id_int = 0
        if ADMIN_ID:
            try:
                admin_id_int = int(ADMIN_ID)
            except ValueError: pass # Handled by earlier print

        if user_id_to_remove_val == admin_id_int:
            safe_send_message(chat_id_admin, "❌ Cannot remove the admin account!", reply_markup=get_user_management_keyboard())
            return

        removed_from_approved_flag = False
        if user_id_to_remove_val in approved_users:
            approved_users.remove(user_id_to_remove_val)
            removed_from_approved_flag = True

        removed_from_pending_flag = False
        if user_id_to_remove_val in pending_approvals:
            del pending_approvals[user_id_to_remove_val]
            removed_from_pending_flag = True

        if removed_from_approved_flag or removed_from_pending_flag:
            original_user_name_val = user_profiles.get(user_id_to_remove_val, {}).get('name', str(user_id_to_remove_val))
            safe_delete_user(user_id_to_remove_val)
            safe_send_message(chat_id_admin, f"✅ User {original_user_name_val} (`{user_id_to_remove_val}`) has been removed and all their data cleared.", reply_markup=get_user_management_keyboard())
            safe_send_message(user_id_to_remove_val, "❌ Your access to this bot has been revoked by an admin. Your data has been cleared.")
        else:
            safe_send_message(chat_id_admin, f"❌ User ID `{user_id_to_remove_val}` not found in approved or pending users.", reply_markup=get_user_management_keyboard())

    except ValueError:
        safe_send_message(chat_id_admin, "❌ Invalid User ID. Please enter a numeric ID or type 'cancel'.", reply_markup=get_user_management_keyboard())
        msg_reply = safe_send_message(chat_id_admin, "🆔 Enter the User ID to remove (or 'cancel'):", reply_markup=telebot.types.ForceReply(selective=False))
        if msg_reply:
            bot.register_next_step_handler(msg_reply, process_user_removal)
    except Exception as e:
        print(f"Error in process_user_removal: {e}")
        safe_send_message(chat_id_admin, "An error occurred during user removal.", reply_markup=get_user_management_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def broadcast_menu(message):
    safe_send_message(message.chat.id, "📢 Broadcast Message to All Approved Users", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def process_text_broadcast_prompt(message):
    msg_reply = safe_send_message(message.chat.id, "✍️ Enter the broadcast message text (or type /cancel):", reply_markup=telebot.types.ForceReply(selective=False))
    if msg_reply:
        bot.register_next_step_handler(msg_reply, process_text_broadcast)

def process_text_broadcast(message):
    chat_id_admin = message.chat.id
    if message.text == "/cancel":
        safe_send_message(chat_id_admin, "Broadcast cancelled.", reply_markup=get_broadcast_keyboard())
        return

    broadcast_text_val = message.text
    if not broadcast_text_val:
        safe_send_message(chat_id_admin, "❌ Broadcast message cannot be empty. Try again or /cancel.", reply_markup=get_broadcast_keyboard())
        return

    confirm_kb_val = telebot.types.InlineKeyboardMarkup()
    confirm_kb_val.add(telebot.types.InlineKeyboardButton("✅ Yes, send it!", callback_data="confirm_broadcast_text"))
    confirm_kb_val.add(telebot.types.InlineKeyboardButton("❌ No, cancel.", callback_data="cancel_broadcast"))
    
    if chat_id_admin not in user_profiles: user_profiles[chat_id_admin] = {} # Ensure admin profile exists
    user_profiles[chat_id_admin]['broadcast_text'] = broadcast_text_val
    safe_send_message(chat_id_admin, f"Preview:\n\n{broadcast_text_val}\n\nSend this to all {len(approved_users)} approved users (excluding admin)?", reply_markup=confirm_kb_val)


@bot.callback_query_handler(func=lambda call: call.data == "confirm_broadcast_text")
def cb_confirm_text_broadcast(call):
    admin_chat_id_val = call.message.chat.id
    broadcast_text_val = user_profiles.get(admin_chat_id_val, {}).get('broadcast_text')

    if not broadcast_text_val:
        bot.answer_callback_query(call.id, "Error: Broadcast message not found.")
        bot.edit_message_text("Error during broadcast.", admin_chat_id_val, call.message.message_id, reply_markup=None)
        safe_send_message(admin_chat_id_val, "Could not retrieve broadcast text. Please try again.", reply_markup=get_broadcast_keyboard())
        return

    bot.edit_message_text(f"📢 Broadcasting text to {len(approved_users)} users...", admin_chat_id_val, call.message.message_id, reply_markup=None)
    success_count = 0
    failed_count = 0
    users_to_broadcast_list = [uid for uid in approved_users if uid != admin_chat_id_val]
    total_to_send_val = len(users_to_broadcast_list)

    if total_to_send_val == 0:
        safe_send_message(admin_chat_id_val, "No other approved users to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_text_val = f"📢 Broadcasting to {total_to_send_val} users...\n\n0/{total_to_send_val} sent"
    progress_message_val = safe_send_message(admin_chat_id_val, progress_msg_text_val)


    for i, user_id_to_send_val in enumerate(users_to_broadcast_list, 1):
        try:
            user_msg_text = f"📢 *Admin Broadcast:*\n\n{broadcast_text_val}"
            sent_msg_val = safe_send_message(user_id_to_send_val, user_msg_text)
            if sent_msg_val:
                success_count += 1
            else:
                failed_count +=1
        except Exception as e:
            print(f"Unexpected error broadcasting text to {user_id_to_send_val}: {e}")
            failed_count += 1

        if progress_message_val and (i % 5 == 0 or i == total_to_send_val):
            try:
                bot.edit_message_text(
                    f"📢 Broadcasting to {total_to_send_val} users...\n\n{i}/{total_to_send_val} attempted\n✅ {success_count} successful\n❌ {failed_count} failed",
                    chat_id=admin_chat_id_val,
                    message_id=progress_message_val.message_id
                )
            except Exception as edit_e:
                print(f"Error updating broadcast progress: {edit_e}")

    final_status_msg_text = f"📢 Text Broadcast Completed!\n\n✅ Sent to {success_count} users.\n❌ Failed for {failed_count} users."
    safe_send_message(admin_chat_id_val, final_status_msg_text, reply_markup=get_admin_keyboard())
    if admin_chat_id_val in user_profiles and 'broadcast_text' in user_profiles[admin_chat_id_val]:
        del user_profiles[admin_chat_id_val]['broadcast_text']
    bot.answer_callback_query(call.id, "Broadcast initiated.")


@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def media_broadcast_prompt(message):
    msg_reply = safe_send_message(message.chat.id, "🖼 Send the photo/video/document you want to broadcast (with caption if needed, or type /cancel):", reply_markup=telebot.types.ForceReply(selective=False))
    if msg_reply:
        bot.register_next_step_handler(msg_reply, process_media_broadcast_confirm)

def process_media_broadcast_confirm(message):
    admin_chat_id_val = message.chat.id
    if message.text == "/cancel":
        safe_send_message(admin_chat_id_val, "Media broadcast cancelled.", reply_markup=get_broadcast_keyboard())
        return

    media_type_val = None
    file_id_val = None
    caption_val = message.caption if message.caption else ""

    if message.photo:
        media_type_val = "photo"
        file_id_val = message.photo[-1].file_id
    elif message.video:
        media_type_val = "video"
        file_id_val = message.video.file_id
    elif message.document:
        media_type_val = "document"
        file_id_val = message.document.file_id
    else:
        safe_send_message(admin_chat_id_val, "❌ No media detected. Please send a photo, video, or document. Or type /cancel.", reply_markup=get_broadcast_keyboard())
        msg_reply = safe_send_message(admin_chat_id_val, "🖼 Send the media again (or /cancel):", reply_markup=telebot.types.ForceReply(selective=False))
        if msg_reply:
          bot.register_next_step_handler(msg_reply, process_media_broadcast_confirm)
        return
    
    if admin_chat_id_val not in user_profiles: user_profiles[admin_chat_id_val] = {}
    user_profiles[admin_chat_id_val]['broadcast_media'] = {'type': media_type_val, 'file_id': file_id_val, 'caption': caption_val}

    confirm_kb_val = telebot.types.InlineKeyboardMarkup()
    confirm_kb_val.add(telebot.types.InlineKeyboardButton("✅ Yes, send it!", callback_data="confirm_broadcast_media"))
    confirm_kb_val.add(telebot.types.InlineKeyboardButton("❌ No, cancel.", callback_data="cancel_broadcast"))

    preview_text_val = f"You are about to broadcast this {media_type_val} "
    if caption_val:
        preview_text_val += f"with caption:\n'{caption_val[:100]}{'...' if len(caption_val) > 100 else ''}'\n\n"
    else:
        preview_text_val += "(no caption).\n\n"
    preview_text_val += f"Send to all {len(approved_users)} approved users (excluding admin)?"
    safe_send_message(admin_chat_id_val, preview_text_val, reply_markup=confirm_kb_val)


@bot.callback_query_handler(func=lambda call: call.data == "confirm_broadcast_media")
def cb_confirm_media_broadcast(call):
    admin_chat_id_val = call.message.chat.id
    media_info_val = user_profiles.get(admin_chat_id_val, {}).get('broadcast_media')

    if not media_info_val:
        bot.answer_callback_query(call.id, "Error: Broadcast media not found.")
        bot.edit_message_text("Error during media broadcast.", admin_chat_id_val, call.message.message_id, reply_markup=None)
        safe_send_message(admin_chat_id_val, "Could not retrieve media for broadcast. Please try again.", reply_markup=get_broadcast_keyboard())
        return

    bot.edit_message_text(f"📢 Broadcasting {media_info_val['type']} to {len(approved_users)} users...", admin_chat_id_val, call.message.message_id, reply_markup=None)

    success_count = 0
    failed_count = 0
    users_to_broadcast_list = [uid for uid in approved_users if uid != admin_chat_id_val]
    total_to_send_val = len(users_to_broadcast_list)

    if total_to_send_val == 0:
        safe_send_message(admin_chat_id_val, "No other approved users to broadcast media to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_text_val = f"📢 Broadcasting {media_info_val['type']} to {total_to_send_val} users...\n\n0/{total_to_send_val} sent"
    progress_message_val = safe_send_message(admin_chat_id_val, progress_msg_text_val)


    for i, user_id_to_send_val in enumerate(users_to_broadcast_list, 1):
        try:
            caption_with_header_val = f"📢 *Admin Broadcast:*\n\n{media_info_val['caption']}" if media_info_val['caption'] else "📢 *Admin Broadcast:*"
            sent_successfully_flag = False
            if media_info_val['type'] == "photo":
                bot.send_photo(user_id_to_send_val, media_info_val['file_id'], caption=caption_with_header_val, parse_mode="Markdown")
                sent_successfully_flag = True
            elif media_info_val['type'] == "video":
                bot.send_video(user_id_to_send_val, media_info_val['file_id'], caption=caption_with_header_val, parse_mode="Markdown")
                sent_successfully_flag = True
            elif media_info_val['type'] == "document":
                bot.send_document(user_id_to_send_val, media_info_val['file_id'], caption=caption_with_header_val, parse_mode="Markdown")
                sent_successfully_flag = True

            if sent_successfully_flag:
                success_count += 1
            else:
                failed_count += 1

        except telebot.apihelper.ApiTelegramException as api_ex:
            if "bot was blocked" in str(api_ex) or "user is deactivated" in str(api_ex) or "chat not found" in str(api_ex):
                print(f"User {user_id_to_send_val} blocked or inactive. Marking as failed.")
                failed_count += 1
                safe_delete_user(user_id_to_send_val)
            else:
                print(f"API error broadcasting media to {user_id_to_send_val}: {api_ex}")
                failed_count += 1
        except Exception as e:
            print(f"Unexpected error broadcasting media to {user_id_to_send_val}: {e}")
            failed_count += 1

        if progress_message_val and (i % 5 == 0 or i == total_to_send_val):
            try:
                bot.edit_message_text(
                    f"📢 Broadcasting {media_info_val['type']} to {total_to_send_val} users...\n\n{i}/{total_to_send_val} attempted\n✅ {success_count} successful\n❌ {failed_count} failed",
                    chat_id=admin_chat_id_val,
                    message_id=progress_message_val.message_id
                )
            except Exception as edit_e:
                print(f"Error updating media broadcast progress: {edit_e}")


    final_status_msg_text = f"📢 Media Broadcast ({media_info_val['type']}) Completed!\n\n✅ Sent to {success_count} users.\n❌ Failed for {failed_count} users."
    safe_send_message(admin_chat_id_val, final_status_msg_text, reply_markup=get_admin_keyboard())
    if admin_chat_id_val in user_profiles and 'broadcast_media' in user_profiles[admin_chat_id_val]:
        del user_profiles[admin_chat_id_val]['broadcast_media']
    bot.answer_callback_query(call.id, f"{media_info_val['type'].capitalize()} broadcast initiated.")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_broadcast")
def cb_cancel_broadcast(call):
    admin_chat_id_val = call.message.chat.id
    bot.edit_message_text("Broadcast cancelled by admin.", admin_chat_id_val, call.message.message_id, reply_markup=None)
    safe_send_message(admin_chat_id_val, "Broadcast operation cancelled.", reply_markup=get_broadcast_keyboard())
    if admin_chat_id_val in user_profiles:
        if 'broadcast_text' in user_profiles[admin_chat_id_val]:
            del user_profiles[admin_chat_id_val]['broadcast_text']
        if 'broadcast_media' in user_profiles[admin_chat_id_val]:
            del user_profiles[admin_chat_id_val]['broadcast_media']
    bot.answer_callback_query(call.id, "Broadcast cancelled.")


@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin" and is_admin(msg.chat.id))
def back_to_admin(message):
    safe_send_message(message.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu")
def general_back_to_main_menu(message):
    chat_id_val = message.chat.id
    if chat_id_val in user_2fa_secrets:
        del user_2fa_secrets[chat_id_val]
        print(f"Cleared 2FA context for {chat_id_val} due to Main Menu navigation.")
    safe_send_message(chat_id_val, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id_val))


@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def handle_approval(call):
    admin_chat_id_val = call.message.chat.id
    if not is_admin(admin_chat_id_val):
        bot.answer_callback_query(call.id, "Error: Not an admin.")
        return

    try:
        action_val, user_id_str_val = call.data.split('_')
        user_id_val = int(user_id_str_val)
    except ValueError:
        bot.answer_callback_query(call.id, "Error: Invalid user ID in callback.")
        bot.edit_message_text("Error processing approval: Invalid user ID.", admin_chat_id_val, call.message.message_id, reply_markup=None)
        return

    user_info_val = pending_approvals.get(user_id_val, user_profiles.get(user_id_val))

    if action_val == "approve":
        approved_users.add(user_id_val)
        if user_id_val in pending_approvals:
            del pending_approvals[user_id_val]
        if user_id_val not in user_profiles and user_info_val:
            user_profiles[user_id_val] = user_info_val
        elif user_id_val not in user_profiles:
             user_profiles[user_id_val] = {"name": "Unknown", "username": "N/A", "join_date": "N/A"}


        safe_send_message(user_id_val, "✅ Your access request has been approved by the admin! You can now use the bot.", reply_markup=get_main_keyboard(user_id_val))
        bot.answer_callback_query(call.id, f"User {user_id_val} approved.")
        bot.edit_message_text(f"✅ User {user_info_val['name'] if user_info_val else user_id_val} (`{user_id_val}`) approved.", admin_chat_id_val, call.message.message_id, reply_markup=None)
    elif action_val == "reject":
        if user_id_val in pending_approvals:
            del pending_approvals[user_id_val]
        safe_send_message(user_id_val, "❌ Your access request has been rejected by the admin.")
        bot.answer_callback_query(call.id, f"User {user_id_val} rejected.")
        bot.edit_message_text(f"❌ User {user_info_val['name'] if user_info_val else user_id_val} (`{user_id_val}`) rejected.", admin_chat_id_val, call.message.message_id, reply_markup=None)
    else:
        bot.answer_callback_query(call.id, "Unknown action.")
        bot.edit_message_text("Unknown approval action.", admin_chat_id_val, call.message.message_id, reply_markup=None)


# --- User Account Handlers (Non-Admin) ---
@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
def my_account_info(message):
    chat_id_val = message.chat.id
    if is_bot_blocked(chat_id_val): safe_delete_user(chat_id_val); return
    if not (chat_id_val in approved_users or is_admin(chat_id_val)):
        safe_send_message(chat_id_val, "⏳ Your access is pending approval or has been revoked.")
        return
    safe_send_message(chat_id_val, "👤 Account Options:", reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📧 My Email")
def my_email_info(message):
    chat_id_val = message.chat.id
    if is_bot_blocked(chat_id_val): safe_delete_user(chat_id_val); return
    if not (chat_id_val in approved_users or is_admin(chat_id_val)):
        safe_send_message(chat_id_val, "⏳ Your access is pending approval or has been revoked.")
        return

    if chat_id_val in user_data and user_data[chat_id_val].get("email"):
        email_val = user_data[chat_id_val]["email"]
        safe_send_message(chat_id_val, f"📧 Your current temporary email is: `{email_val}`\nTap to copy.", reply_markup=get_user_account_keyboard())
    else:
        safe_send_message(chat_id_val, "📬 You don't have an active temporary email. Create one using 'New mail'.", reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "🆔 My Info")
def my_telegram_info(message):
    chat_id_val = message.chat.id
    if is_bot_blocked(chat_id_val): safe_delete_user(chat_id_val); return
    if not (chat_id_val in approved_users or is_admin(chat_id_val)):
        safe_send_message(chat_id_val, "⏳ Your access is pending approval or has been revoked.")
        return

    user_info_val = user_profiles.get(chat_id_val)
    if user_info_val:
        info_text_val = (
            f"👤 *Your Telegram Info:*\n\n"
            f"🆔 User ID: `{chat_id_val}`\n"
            f"🗣️ Name: `{user_info_val['name']}`\n"
            f"📛 Username: `@{user_info_val['username']}`\n"
            f"🗓️ Bot Join Date: `{user_info_val['join_date']}`\n"
            f"✅ Access Status: Approved"
        )
    else:
        info_text_val = f"🆔 Your User ID: `{chat_id_val}`\n✅ Access Status: Approved\n(Detailed profile info not found)"
    safe_send_message(chat_id_val, info_text_val, reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main" and not is_admin(msg.chat.id))
def user_back_to_main(message):
    chat_id_val = message.chat.id
    if chat_id_val in user_2fa_secrets:
        del user_2fa_secrets[chat_id_val]
    safe_send_message(chat_id_val, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id_val))


# --- Mail handlers ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail(message):
    chat_id_val = message.chat.id
    if is_bot_blocked(chat_id_val): safe_delete_user(chat_id_val); return
    if not (chat_id_val in approved_users or is_admin(chat_id_val)):
        safe_send_message(chat_id_val, "⏳ Your access is pending approval. You cannot create an email yet.")
        return

    loading_msg_val = safe_send_message(chat_id_val, "⏳ Generating new temporary email, please wait...")

    domain_val = get_domain()
    email_val, _ = generate_email_address(domain_val)
    temp_password_val = ''.join(random.choices(string.ascii_letters + string.digits + string.punctuation, k=16))

    status_val, acc_data_val = create_account(email_val, temp_password_val)

    if status_val == "created":
        token_val = get_token(email_val, temp_password_val)
        if token_val:
            user_data[chat_id_val] = {"email": email_val, "password": temp_password_val, "token": token_val, "id": acc_data_val.get("id")}
            last_message_ids[chat_id_val] = set()
            msg_text_val = f"✅ *Temporary Email Created!*\n\n`{email_val}`\n\nTap to copy. Inbox will be checked automatically."
            if loading_msg_val: bot.delete_message(chat_id_val, loading_msg_val.message_id)
            safe_send_message(chat_id_val, msg_text_val, reply_markup=get_main_keyboard(chat_id_val))
        else:
            if loading_msg_val: bot.delete_message(chat_id_val, loading_msg_val.message_id)
            safe_send_message(chat_id_val, "❌ Failed to log in to the new email account. The account might have been created, but token generation failed. Try again or contact admin.", reply_markup=get_main_keyboard(chat_id_val))
    elif status_val == "exists_or_invalid":
        if loading_msg_val: bot.delete_message(chat_id_val, loading_msg_val.message_id)
        safe_send_message(chat_id_val, "❌ Email address generation conflict or invalid. Please try 'New mail' again.", reply_markup=get_main_keyboard(chat_id_val))
    else:
        if loading_msg_val: bot.delete_message(chat_id_val, loading_msg_val.message_id)
        error_detail_val = acc_data_val.get('message', 'Unknown reason.')
        safe_send_message(chat_id_val, f"❌ Could not create temporary email: {error_detail_val}. Please try again later.", reply_markup=get_main_keyboard(chat_id_val))


@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh")
def refresh_mail(message):
    chat_id_val = message.chat.id
    if is_bot_blocked(chat_id_val): safe_delete_user(chat_id_val); return
    if not (chat_id_val in approved_users or is_admin(chat_id_val)):
        safe_send_message(chat_id_val, "⏳ Your access is pending approval.")
        return

    if chat_id_val not in user_data or not user_data[chat_id_val].get("token"):
        safe_send_message(chat_id_val, "⚠️ Please create a new email first using '📬 New mail'.")
        return

    loading_msg_val = safe_send_message(chat_id_val, "🔄 Checking your inbox for new mail...")
    token_val = user_data[chat_id_val]["token"]
    headers_val = {"Authorization": f"Bearer {token_val}"}
    any_new_message_displayed_flag = False

    try:
        res_val = requests.get("https://api.mail.tm/messages", headers=headers_val, timeout=15)
        if res_val.status_code == 401:
            if loading_msg_val: bot.delete_message(chat_id_val, loading_msg_val.message_id)
            safe_send_message(chat_id_val, "⚠️ Your temporary email session has expired. Please create a new one.")
            del user_data[chat_id_val]
            if chat_id_val in last_message_ids: del last_message_ids[chat_id_val]
            return
        res_val.raise_for_status()

        messages_val = res_val.json().get("hydra:member", [])
        if not messages_val:
            if loading_msg_val: bot.delete_message(chat_id_val, loading_msg_val.message_id)
            safe_send_message(chat_id_val, "📭 *Your inbox is currently empty.*")
            return

        seen_ids_for_manual_refresh_val = last_message_ids.setdefault(chat_id_val, set())
        new_messages_manually_shown_count = 0

        for msg_summary_val in messages_val[:3]:
            msg_id_val = msg_summary_val["id"]
            try:
                detail_res_val = requests.get(f"https://api.mail.tm/messages/{msg_id_val}", headers=headers_val, timeout=10)
                detail_res_val.raise_for_status()
                msg_detail_val = detail_res_val.json()
                sender_val = msg_detail_val.get("from", {}).get("address", "Unknown Sender")
                subject_val = msg_detail_val.get("subject", "(No Subject)")
                body_text_val = msg_detail_val.get("text")
                if not body_text_val and msg_detail_val.get("html"):
                    soup_val = BeautifulSoup(msg_detail_val["html"][0], "html.parser")
                    body_text_val = soup_val.get_text(separator='\n')
                body_val = body_text_val.strip() if body_text_val else "(No Content)"
                received_at_val = msg_summary_val.get('createdAt', 'N/A')
                if received_at_val != 'N/A':
                    try:
                        dt_obj_val = datetime.datetime.fromisoformat(received_at_val.replace("Z", "+00:00"))
                        received_at_val = dt_obj_val.strftime("%Y-%m-%d %H:%M:%S %Z")
                    except ValueError:
                        pass

                formatted_msg_val = (
                    f"📩 *Email from Inbox:*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *From:* `{sender_val}`\n"
                    f"📨 *Subject:* _{subject_val}_\n"
                    f"🕒 *Received:* {received_at_val}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💬 *Body:*\n"
                    f"{body_val[:3500]}\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )
                safe_send_message(chat_id_val, formatted_msg_val)
                any_new_message_displayed_flag = True
                new_messages_manually_shown_count +=1
                seen_ids_for_manual_refresh_val.add(msg_id_val)

            except requests.RequestException as e_detail:
                print(f"Error fetching detail for msg {msg_id_val} on refresh: {e_detail}")
                safe_send_message(chat_id_val, f"⚠️ Error loading one message (ID: {msg_id_val}).")
            except Exception as e_proc:
                print(f"Error processing message {msg_id_val} on refresh: {e_proc}")
                safe_send_message(chat_id_val, f"⚠️ Error processing one message (ID: {msg_id_val}).")

        if loading_msg_val: bot.delete_message(chat_id_val, loading_msg_val.message_id)
        if not any_new_message_displayed_flag:
            safe_send_message(chat_id_val, "✅ No new messages found in the latest check, or already displayed.")
        elif new_messages_manually_shown_count > 0 :
             safe_send_message(chat_id_val, f"✅ Refresh complete. Displayed {new_messages_manually_shown_count} message(s).")

    except requests.RequestException as e:
        if loading_msg_val: bot.delete_message(chat_id_val, loading_msg_val.message_id)
        safe_send_message(chat_id_val, f"❌ Connection error during refresh: {e}. Try again later.")
    except Exception as e_gen:
        if loading_msg_val: bot.delete_message(chat_id_val, loading_msg_val.message_id)
        print(f"Generic error in refresh_mail for {chat_id_val}: {e_gen}")
        safe_send_message(chat_id_val, "❌ An unexpected error occurred while refreshing. Please try again.")


# --- Profile handlers ---
@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
def generate_profile_handler(message):
    chat_id_val = message.chat.id
    if is_bot_blocked(chat_id_val): safe_delete_user(chat_id_val); return
    if not (chat_id_val in approved_users or is_admin(chat_id_val)):
        safe_send_message(chat_id_val, "⏳ Your access is pending approval.")
        return

    gender_val = "male" if message.text == "👨 Male Profile" else "female"
    g_val, name_val, username_val, pwd_val, phone_val = generate_profile(gender_val)
    message_text_val = profile_message(g_val, name_val, username_val, pwd_val, phone_val)
    safe_send_message(chat_id_val, message_text_val)

# --- 2FA Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def two_fa_auth_start(message):
    chat_id_val = message.chat.id
    if is_bot_blocked(chat_id_val): safe_delete_user(chat_id_val); return
    if not (chat_id_val in approved_users or is_admin(chat_id_val)):
        safe_send_message(chat_id_val, "⏳ Your access is pending approval.")
        return
    if chat_id_val in user_2fa_secrets:
        del user_2fa_secrets[chat_id_val]
    safe_send_message(chat_id_val, "🔐 Choose the platform for 2FA code generation:", reply_markup=get_2fa_platform_keyboard())


@bot.message_handler(func=lambda msg: msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform_selection(message):
    chat_id_val = message.chat.id
    if is_bot_blocked(chat_id_val): safe_delete_user(chat_id_val); return
    if not (chat_id_val in approved_users or is_admin(chat_id_val)):
        safe_send_message(chat_id_val, "⏳ Your access is pending approval.")
        return

    platform_val = message.text
    user_2fa_secrets[chat_id_val] = {"platform": platform_val}
    safe_send_message(chat_id_val, f"🔢 Enter the 2FA secret key for *{platform_val}* (Base32 format).\n\nType /cancel2fa to abort.",
                                  reply_markup=telebot.types.ForceReply(selective=True))


@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_all_text_messages(message):
    chat_id_val = message.chat.id
    text_val = message.text.strip()

    if is_bot_blocked(chat_id_val): safe_delete_user(chat_id_val); return
    if not (chat_id_val in approved_users or is_admin(chat_id_val)) and text_val not in ['/start', '/help']:
        if chat_id_val in pending_approvals:
             safe_send_message(chat_id_val, "⏳ Your access request is still pending admin approval. Please wait.")
        else:
             safe_send_message(chat_id_val, "⚠️ Your access is not currently approved. Please use /start if you wish to request access.")
        return

    if chat_id_val in user_2fa_secrets and "platform" in user_2fa_secrets[chat_id_val] and "secret" not in user_2fa_secrets[chat_id_val]:
        if text_val.lower() == "/cancel2fa":
            del user_2fa_secrets[chat_id_val]
            safe_send_message(chat_id_val, "2FA secret key entry cancelled. Choose a platform or go back.", reply_markup=get_2fa_platform_keyboard())
            return

        secret_key_val = text_val.upper().replace(" ", "").replace("-", "")
        if is_valid_base32(secret_key_val):
            user_2fa_secrets[chat_id_val]["secret"] = secret_key_val
            platform_val = user_2fa_secrets[chat_id_val]["platform"]
            safe_send_message(chat_id_val, f"✅ Secret key for *{platform_val}* accepted. Generating code...", reply_markup=telebot.types.ReplyKeyboardRemove())
            display_2fa_code(chat_id_val, platform_val, secret_key_val)
        else:
            safe_send_message(chat_id_val, "❌ *Invalid Base32 Secret Key.*\n\nIt should only contain `A-Z` and `2-7`.\nPlease enter a valid key or type /cancel2fa.",
                              reply_markup=telebot.types.ForceReply(selective=True))
        return

    known_buttons = [
        "📬 New mail", "🔄 Refresh", "👨 Male Profile", "👩 Female Profile", "🔐 2FA Auth", "👤 My Account",
        "👑 Admin Panel", "⬅️ Main Menu",
        "Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple",
        "⬅️ Back to Admin", "👥 Pending Approvals", "📊 Stats", "👤 User Management", "📢 Broadcast",
        "📜 List Users", "❌ Remove User", "📢 Text Broadcast", "📋 Media Broadcast",
        "📧 My Email", "🆔 My Info"
    ]
    if text_val not in known_buttons and not text_val.startswith('/'): # Avoid replying to commands handled by other handlers
        if chat_id_val in approved_users or is_admin(chat_id_val):
            safe_send_message(chat_id_val, f"🤔 I didn't understand '{text_val}'. Please use the buttons or commands.", reply_markup=get_main_keyboard(chat_id_val))


@bot.callback_query_handler(func=lambda call: call.data == "generate_2fa_code")
def cb_generate_2fa_code_refresh(call):
    chat_id_val = call.message.chat.id
    bot.answer_callback_query(call.id, "Refreshing code...")

    if chat_id_val not in user_2fa_secrets or "secret" not in user_2fa_secrets[chat_id_val] or "platform" not in user_2fa_secrets[chat_id_val]:
        bot.edit_message_text("❌ Error: 2FA secret or platform not found. Please start over.",
                              chat_id_val, call.message.message_id, reply_markup=None)
        safe_send_message(chat_id_val, "Please select a platform for 2FA again.", reply_markup=get_2fa_platform_keyboard())
        return

    secret_val = user_2fa_secrets[chat_id_val]["secret"]
    platform_val = user_2fa_secrets[chat_id_val]["platform"]

    try:
        totp_val = pyotp.TOTP(secret_val)
        current_code_val = totp_val.now()
        now_time_val = datetime.datetime.now()
        seconds_remaining_val = 30 - (now_time_val.second % 30)

        keyboard_val = InlineKeyboardMarkup()
        keyboard_val.add(InlineKeyboardButton("🔄 Refresh Code", callback_data="generate_2fa_code"))
        keyboard_val.add(InlineKeyboardButton("⬅️ New Secret/Platform", callback_data="2fa_back_to_platform"))

        reply_text_val = (
            f"Platform: {platform_val}\n"
            f"<b>2FA CODE</b>\n"
            f"─────────────────────\n"
            f"<code>{current_code_val}</code> (<i>Expires in {seconds_remaining_val}s</i>)\n"
            f"─────────────────────\n"
            f"ℹ️ Secret is stored for refresh. Tap code to copy."
        )
        bot.edit_message_text(reply_text_val, chat_id_val, call.message.message_id,
                              parse_mode='HTML', reply_markup=keyboard_val)
    except Exception as e:
        print(f"Error refreshing 2FA code for {chat_id_val}: {e}")
        bot.edit_message_text("❌ Error generating new code. Check your secret key and try starting 2FA setup again.",
                              chat_id_val, call.message.message_id, reply_markup=None)
        if chat_id_val in user_2fa_secrets: del user_2fa_secrets[chat_id_val]
        safe_send_message(chat_id_val, "Please select a platform for 2FA again.", reply_markup=get_2fa_platform_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "2fa_back_to_platform")
def cb_2fa_back_to_platform_selection(call):
    chat_id_val = call.message.chat.id
    bot.answer_callback_query(call.id, "Returning to platform selection.")
    bot.delete_message(chat_id_val, call.message.message_id)

    if chat_id_val in user_2fa_secrets:
        del user_2fa_secrets[chat_id_val]

    safe_send_message(chat_id_val, "🔐 Choose the platform for 2FA code generation:", reply_markup=get_2fa_platform_keyboard())


if __name__ == '__main__':
    print("🤖 Bot is preparing to launch...")
    if not ADMIN_ID or ADMIN_ID == "YOUR_ADMIN_ID_HERE":
        print("🚨 CRITICAL: ADMIN_ID is not set in your .env file or is set to placeholder.")
        print("🚨 The bot may not function correctly, especially admin features and user approvals.")

    threading.Thread(target=auto_refresh_worker, daemon=True, name="AutoRefreshMail").start()
    threading.Thread(target=cleanup_blocked_users, daemon=True, name="CleanupBlockedUsers").start()

    print(f"🎉 Bot is running with ADMIN_ID: {ADMIN_ID}")
    print(f"PyTeleBot Version: {telebot.__version__}")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)

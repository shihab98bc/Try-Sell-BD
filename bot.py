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

load_dotenv()
fake = Faker()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise Exception("❌ BOT_TOKEN not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Data storage
user_data = {}
last_message_ids = {}
user_2fa_codes = {}
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
        if e.result.status_code == 403 and "bot was blocked" in e.result.text:
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
            
        msg = bot.send_message(chat_id, text, **kwargs)
        active_sessions.add(chat_id)
        return msg
    except telebot.apihelper.ApiTelegramException as e:
        if e.result.status_code == 403 and "bot was blocked" in e.result.text:
            safe_delete_user(chat_id)
        return None
    except Exception as e:
        print(f"Error sending message to {chat_id}: {str(e)}")
        return None

# Mail.tm functions
def get_domain():
    try:
        res = requests.get("https://api.mail.tm/domains", timeout=10)
        domains = res.json().get("hydra:member", [])
        return domains[0]["domain"] if domains else "mail.tm"
    except Exception:
        return "mail.tm"

def generate_email(domain):
    name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"{name}@{domain}", name

def create_account(email, password):
    try:
        res = requests.post("https://api.mail.tm/accounts", 
                          json={"address": email, "password": password},
                          timeout=10)
        if res.status_code == 201:
            return "created"
        elif res.status_code == 422:
            return "exists"
        return "error"
    except Exception:
        return "error"

def get_token(email, password):
    time.sleep(1.5)
    try:
        res = requests.post("https://api.mail.tm/token", 
                          json={"address": email, "password": password},
                          timeout=10)
        if res.status_code == 200:
            return res.json().get("token")
        return None
    except Exception:
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
        # Remove spaces/hyphens and uppercase
        cleaned = secret.replace(" ", "").replace("-", "").upper()
        # pyotp will throw error if invalid
        pyotp.TOTP(cleaned).now()
        return True
    except (binascii.Error, ValueError, Exception):
        return False

# --- Background Workers ---

def auto_refresh_worker():
    while True:
        try:
            for chat_id in list(user_data):
                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id)
                    continue
                    
                token = user_data[chat_id]["token"]
                headers = {"Authorization": f"Bearer {token}"}
                
                try:
                    res = requests.get("https://api.mail.tm/messages", headers=headers, timeout=10)
                    if res.status_code != 200:
                        continue

                    messages = res.json().get("hydra:member", [])
                    seen_ids = last_message_ids.setdefault(chat_id, set())

                    for msg in messages[:3]:
                        msg_id = msg["id"]
                        if msg_id in seen_ids:
                            continue
                        seen_ids.add(msg_id)

                        try:
                            detail_res = requests.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers, timeout=10)
                            if detail_res.status_code == 200:
                                msg_detail = detail_res.json()
                                sender = msg_detail["from"]["address"]
                                subject = msg_detail.get("subject", "(No Subject)")
                                body = msg_detail.get("text", "(No Content)").strip()

                                formatted_msg = (
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"📬 *New Email Received!*\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"👤 *From:* `{sender}`\n"
                                    f"📨 *Subject:* _{subject}_\n"
                                    f"🕒 *Received:* {msg_detail.get('intro', 'Just now')}\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"💬 *Body:*\n"
                                    f"{body[:4000]}\n"
                                    f"━━━━━━━━━━━━━━━━━━━━"
                                )
                                safe_send_message(chat_id, formatted_msg)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception as e:
            print(f"Error in auto_refresh_worker: {e}")
        time.sleep(30)

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
        pending_approvals[chat_id] = user_info
        safe_send_message(chat_id, "👋 Your access request has been sent to admin. Please wait for approval.")
        if ADMIN_ID:
            approval_msg = (
                f"🆕 *New Approval Request*\n\n"
                f"🆔 User ID: `{chat_id}`\n"
                f"👤 Name: `{user_info['name']}`\n"
                f"📛 Username: @{user_info['username']}\n"
                f"📅 Joined: `{user_info['join_date']}`"
            )
            bot.send_message(ADMIN_ID, approval_msg, reply_markup=get_approval_keyboard(chat_id))

# --- Admin Panel Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel" and is_admin(msg.chat.id))
def admin_panel(message):
    safe_send_message(message.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "👥 Pending Approvals" and is_admin(msg.chat.id))
def show_pending_approvals(message):
    if not pending_approvals:
        safe_send_message(message.chat.id, "✅ No pending approvals.")
        return
    for user_id, user_info in pending_approvals.items():
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
    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin: `{ADMIN_ID}`\n"
        f"👥 Total Users: `{len(approved_users)}`\n"
        f"📭 Active Sessions: `{len(active_sessions)}`\n"
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"📧 Active Email Sessions: `{len(user_data)}`\n"
        f"📅 Uptime: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
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
    users_list = []
    for user_id in approved_users:
        if user_id in user_profiles:
            user_info = user_profiles[user_id]
            users_list.append(
                f"🆔 `{user_id}` - 👤 {user_info['name']} (@{user_info['username']}) - 📅 {user_info['join_date']}"
            )
    if not users_list:
        safe_send_message(message.chat.id, "❌ No user data available.")
        return
    # Split into chunks
    chunk_size = 10
    for i in range(0, len(users_list), chunk_size):
        chunk = users_list[i:i + chunk_size]
        response = "👥 *Approved Users*\n\n" + "\n".join(chunk)
        safe_send_message(message.chat.id, response)

@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_user_prompt(message):
    safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(message, process_user_removal)

def process_user_removal(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled user removal.", reply_markup=get_user_management_keyboard())
        return
    try:
        user_id = int(message.text.strip())
        if user_id == int(ADMIN_ID):
            safe_send_message(chat_id, "❌ Cannot remove admin!", reply_markup=get_user_management_keyboard())
            return
        if user_id in approved_users:
            approved_users.remove(user_id)
            safe_delete_user(user_id)
            safe_send_message(chat_id, f"✅ User {user_id} has been removed.", reply_markup=get_user_management_keyboard())
            # Notify user
            try:
                safe_send_message(user_id, "❌ Your access has been revoked by admin.")
            except:
                pass
        else:
            safe_send_message(chat_id, f"❌ User {user_id} not found in approved users.", reply_markup=get_user_management_keyboard())
    except ValueError:
        safe_send_message(chat_id, "❌ Invalid User ID. Please enter a numeric ID.", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def broadcast_menu(message):
    safe_send_message(message.chat.id, "📢 Broadcast Message to All Users", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def process_text_broadcast_prompt(message):
    safe_send_message(message.chat.id, "✍️ Enter the broadcast message text:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(message, process_text_broadcast)

def process_text_broadcast(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled broadcast.", reply_markup=get_broadcast_keyboard())
        return
    broadcast_text = message.text
    success = 0
    failed = 0
    total = len(approved_users)
    progress_msg = safe_send_message(chat_id, f"📢 Broadcasting to {total} users...\n\n0/{total} sent")
    for i, user_id in enumerate(approved_users, 1):
        try:
            if user_id == int(ADMIN_ID):
                continue
            safe_send_message(user_id, f"📢 *Admin Broadcast*\n\n{broadcast_text}")
            success += 1
        except:
            failed += 1
        if i % 5 == 0 or i == total:
            try:
                bot.edit_message_text(
                    f"📢 Broadcasting to {total} users...\n\n{i}/{total} sent\n✅ {success} successful\n❌ {failed} failed",
                    chat_id=chat_id,
                    message_id=progress_msg.message_id
                )
            except:
                pass
    safe_send_message(chat_id, f"📢 Broadcast completed!\n\n✅ {success} successful\n❌ {failed} failed", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def media_broadcast_prompt(message):
    safe_send_message(message.chat.id, "🖼 Send the photo/video/document you want to broadcast (with caption if needed):", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(message, process_media_broadcast)

def process_media_broadcast(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled broadcast.", reply_markup=get_broadcast_keyboard())
        return
    success = 0
    failed = 0
    total = len(approved_users)
    progress_msg = safe_send_message(chat_id, f"📢 Broadcasting media to {total} users...\n\n0/{total} sent")
    for i, user_id in enumerate(approved_users, 1):
        try:
            if user_id == int(ADMIN_ID):
                continue
            if message.photo:
                bot.send_photo(user_id, message.photo[-1].file_id, caption=message.caption)
            elif message.video:
                bot.send_video(user_id, message.video.file_id, caption=message.caption)
            elif message.document:
                bot.send_document(user_id, message.document.file_id, caption=message.caption)
            else:
                failed += 1
                continue
            success += 1
        except:
            failed += 1
        if i % 5 == 0 or i == total:
            try:
                bot.edit_message_text(
                    f"📢 Broadcasting media to {total} users...\n\n{i}/{total} sent\n✅ {success} successful\n❌ {failed} failed",
                    chat_id=chat_id,
                    message_id=progress_msg.message_id
                )
            except:
                pass
    safe_send_message(chat_id, f"📢 Media broadcast completed!\n\n✅ {success} successful\n❌ {failed} failed", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin" and is_admin(msg.chat.id))
def back_to_admin(message):
    safe_send_message(message.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu" and is_admin(msg.chat.id))
def admin_back_to_main(message):
    safe_send_message(message.chat.id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(message.chat.id))

@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def handle_approval(call):
    if not is_admin(call.message.chat.id):
        return
    action, user_id = call.data.split('_')
    user_id = int(user_id)
    if action == "approve":
        approved_users.add(user_id)
        if user_id in pending_approvals:
            del pending_approvals[user_id]
        safe_send_message(user_id, "✅ Your access has been approved!", reply_markup=get_main_keyboard(user_id))
        bot.answer_callback_query(call.id, "User approved")
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
        safe_send_message(call.message.chat.id, f"✅ User {user_id} approved.")
    else:
        if user_id in pending_approvals:
            del pending_approvals[user_id]
        safe_send_message(user_id, "❌ Your access request has been rejected.")
        bot.answer_callback_query(call.id, "User rejected")
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
        safe_send_message(call.message.chat.id, f"❌ User {user_id} rejected.")

# --- Mail handlers ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if chat_id not in approved_users and not is_admin(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    domain = get_domain()
    email, _ = generate_email(domain)
    password = "TempPass123!"
    status = create_account(email, password)
    if status in ["created", "exists"]:
        token = get_token(email, password)
        if token:
            user_data[chat_id] = {"email": email, "password": password, "token": token}
            last_message_ids[chat_id] = set()
            msg_text = f"✅ *Temporary Email Created!*\n\n`{email}`\n\nTap to copy"
            safe_send_message(chat_id, msg_text)
        else:
            safe_send_message(chat_id, "❌ Failed to log in. Try again.")
    else:
        safe_send_message(chat_id, "❌ Could not create temp mail.")

@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh")
def refresh_mail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if chat_id not in approved_users and not is_admin(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    if chat_id not in user_data:
        safe_send_message(chat_id, "⚠️ Please create a new email first.")
        return
    token = user_data[chat_id]["token"]
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = requests.get("https://api.mail.tm/messages", headers=headers, timeout=10)
    except:
        safe_send_message(chat_id, "❌ Connection error. Try again later.")
        return
    if res.status_code != 200:
        safe_send_message(chat_id, "❌ Could not fetch inbox.")
        return
    messages = res.json().get("hydra:member", [])
    if not messages:
        safe_send_message(chat_id, "📭 *Your inbox is empty.*")
        return
    for msg in messages[:3]:
        msg_id = msg["id"]
        try:
            detail_res = requests.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers, timeout=10)
            if detail_res.status_code == 200:
                msg_detail = detail_res.json()
                sender = msg_detail["from"]["address"]
                subject = msg_detail.get("subject", "(No Subject)")
                body = msg_detail.get("text", "(No Content)").strip()
                formatted_msg = (
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📬 *New Email Received!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *From:* `{sender}`\n"
                    f"📨 *Subject:* _{subject}_\n"
                    f"🕒 *Received:* {msg_detail.get('intro', 'Just now')}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💬 *Body:*\n"
                    f"{body[:4000]}\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )
                safe_send_message(chat_id, formatted_msg)
            else:
                safe_send_message(chat_id, "⚠️ Error loading message.")
        except:
            safe_send_message(chat_id, "⚠️ Error loading message details.")

# --- Profile handlers ---
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
    gender, name, username, password, phone = generate_profile(gender)
    message_text = profile_message(gender, name, username, password, phone)
    safe_send_message(chat_id, message_text)

# --- 2FA Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def two_fa_auth(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if chat_id not in approved_users and not is_admin(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    safe_send_message(chat_id, "🔐 Choose the platform for 2FA code:", reply_markup=get_2fa_platform_keyboard())

@bot.message_handler(func=lambda msg: msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_platform_selection(message):
    chat_id = message.chat.id
    platform = message.text
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    user_2fa_secrets[chat_id] = {"platform": platform}
    # Ask for secret key
    safe_send_message(chat_id, f"🔢 Enter the 2FA secret key for {platform}:", reply_markup=get_back_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main")
def back_to_main(message):
    chat_id = message.chat.id
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))

@bot.message_handler(func=lambda msg: True)
def handle_all_text(message):
    chat_id = message.chat.id
    # Handle secret key input for 2FA
    if chat_id in user_2fa_secrets and "platform" in user_2fa_secrets[chat_id]:
        secret = message.text.strip()
        if not is_valid_base32(secret):
            safe_send_message(chat_id, "❌ <b>Invalid Secret Key</b>\n\nYour secret must be a valid Base32 string:\n- Only A-Z and 2-7\n- No lowercase letters\n- No spaces/special chars\n\nPlease try again or /cancel", reply_markup=get_back_keyboard())
            return
        # Store the clean secret
        user_2fa_secrets[chat_id]["secret"] = secret.replace(" ", "").replace("-", "").upper()
        platform = user_2fa_secrets[chat_id]["platform"]
        # Generate current code
        totp = pyotp.TOTP(user_2fa_secrets[chat_id]["secret"])
        current_code = totp.now()
        now = datetime.datetime.now()
        seconds = 30 - (now.second % 30)
        valid_until = now + datetime.timedelta(seconds=seconds)
        # Send code with copy
        reply_text = (
            f"<b>CODE</b>       <b>SECRET KEY</b>\n"
            f"─────────────────────\n"
            f"<code>{current_code}</code>    <i>Valid for {seconds}s</i>\n"
            f"─────────────────────\n"
            f"Copy this code to use\n"
            f"Valid until: {valid_until.strftime('%H:%M:%S')}"
        )
        safe_send_message(chat_id, reply_text, reply_markup=get_main_keyboard(chat_id))
        # Remove secret after display to prevent reuse
        if chat_id in user_2fa_secrets:
            del user_2fa_secrets[chat_id]
        return
    # Handle "🔄 GET CODE" button if user clicks
    # (We will handle via callback_query below)
    pass

@bot.callback_query_handler(func=lambda call: call.data == "generate_code")
def generate_2fa_code_callback(call):
    chat_id = call.message.chat.id
    if chat_id not in user_2fa_secrets or "secret" not in user_2fa_secrets.get(chat_id, {}):
        bot.answer_callback_query(call.id, "No secret set. Please enter your secret.")
        return
    secret = user_2fa_secrets[chat_id]["secret"]
    try:
        totp = pyotp.TOTP(secret)
        current_code = totp.now()
        now = datetime.datetime.now()
        seconds = 30 - (now.second % 30)
        valid_until = now + datetime.timedelta(seconds=seconds)
        reply_text = (
            f"<b>CODE</b>       <b>SECRET KEY</b>\n"
            f"─────────────────────\n"
            f"<code>{current_code}</code>    <i>Valid for {seconds}s</i>\n"
            f"─────────────────────\n"
            f"Copy this code to use\n"
            f"Valid until: {valid_until.strftime('%H:%M:%S')}"
        )
        bot.edit_message_text(
            reply_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh Code", callback_data="generate_code")]])
        )
    except Exception as e:
        bot.answer_callback_query(call.id, "Error generating code. Please check your secret.")

# --- All other handlers (profiles, emails, etc.) are already integrated. ---

print("🤖 Bot is running...")
threading.Thread(target=auto_refresh_worker, daemon=True).start()
threading.Thread(target=cleanup_blocked_users, daemon=True).start()

bot.infinity_polling()

import os
import time
import re
import requests
import threading
import datetime
import random
import string
from faker import Faker
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import pyotp

load_dotenv()

# Initialize Faker
fake = Faker()

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")  # e.g., '123456789'

if not BOT_TOKEN:
    raise Exception("❌ BOT_TOKEN not set in .env")
if not ADMIN_ID:
    raise Exception("❌ ADMIN_ID not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Data storage
user_data = {}             # {chat_id: {"email":..., "password":..., "token":...}}
last_message_ids = {}      # {chat_id: set(msg_ids)}
user_2fa_secrets = {}      # {chat_id: {"platform":..., "secret":...}}
active_sessions = set()
pending_approvals = {}     # {chat_id: user_info}
approved_users = set()     # set of chat_ids approved
user_profiles = {}        # {chat_id: {"name":..., "username":..., "join_date":...}}

# --- Helper Functions ---
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

def is_bot_blocked(chat_id):
    try:
        bot.get_chat(chat_id)
        return False
    except:
        return True

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
        if is_bot_blocked(chat_id):
            safe_delete_user(chat_id)
            return
        return bot.send_message(chat_id, text, **kwargs)
    except:
        safe_delete_user(chat_id)

# --- Mail.tm API functions ---
def get_domain():
    try:
        res = requests.get("https://api.mail.tm/domains", timeout=10)
        domains = res.json().get("hydra:member", [])
        return domains[0]["domain"] if domains else "mail.tm"
    except:
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
        if res.status_code in [201, 422]:
            return "created" if res.status_code == 201 else "exists"
        return "error"
    except:
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
    except:
        return None

# Profile generator
def generate_profile(gender):
    name = fake.name_male() if gender == "male" else fake.name_female()
    username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    password = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8)) + datetime.datetime.now().strftime("%d")
    phone = '1' + ''.join([str(random.randint(200, 999))]) + ''.join([str(random.randint(0, 9)) for _ in range(7)])
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
        pyotp.TOTP(clean_secret).now()
        return True
    except:
        return False

# --- Workers ---
def auto_refresh_worker():
    while True:
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

                            otp_match = re.search(r"\b\d{6,8}\b", body)
                            otp_text = ""
                            if otp_match:
                                otp_code = otp_match.group()
                                otp_text = f"\n\n🚨 OTP Detected: `{otp_code}` (Click to copy!)"

                            msg_text = (
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                "📬 *New Email Received!*\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                f"👤 *From:* `{sender}`\n"
                                f"📨 *Subject:* _{subject}_\n"
                                f"🕒 *Received:* {msg_detail.get('intro', 'Just now')}\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                "💬 *Body:*\n"
                                f"{body[:4000]}{otp_text}\n"
                                "━━━━━━━━━━━━━━━━━━━━"
                            )
                            safe_send_message(chat_id, msg_text)
                        # no else
                    except:
                        pass
            except:
                pass
        time.sleep(30)

def cleanup_blocked_users():
    while True:
        for chat_id in list(active_sessions):
            if is_bot_blocked(chat_id):
                safe_delete_user(chat_id)
        time.sleep(3600)

# --- Handlers ---

@bot.message_handler(commands=['start', 'help'])
def handle_start_help(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    user_info = get_user_info(message.from_user)
    user_profiles[chat_id] = user_info
    if is_admin(chat_id):
        approved_users.add(chat_id)
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=get_main_keyboard(chat_id))
    elif chat_id in approved_users:
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

# --- Main Menu Handling ---
@bot.message_handler(func=lambda m: m.text == "👑 Admin Panel" and is_admin(m.chat.id))
def handle_admin_panel(m):
    safe_send_message(m.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "👥 Pending Approvals" and is_admin(m.chat.id))
def handle_pending_approvals(m):
    if not pending_approvals:
        safe_send_message(m.chat.id, "✅ No pending approvals.")
        return
    for uid, info in pending_approvals.items():
        msg = (
            f"🆕 *Pending Approval*\n\n"
            f"🆔 User ID: `{uid}`\n"
            f"👤 Name: `{info['name']}`\n"
            f"📛 Username: @{info['username']}\n"
            f"📅 Joined: `{info['join_date']}`"
        )
        safe_send_message(m.chat.id, msg, reply_markup=get_approval_keyboard(uid))

@bot.message_handler(func=lambda m: m.text == "📊 Stats" and is_admin(m.chat.id))
def handle_stats(m):
    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin: `{ADMIN_ID}`\n"
        f"👥 Total Users: `{len(approved_users)}`\n"
        f"📭 Active Sessions: `{len(active_sessions)}`\n"
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"📧 Active Email Sessions: `{len(user_data)}`\n"
        f"📅 Uptime: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
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
    full_details = []
    for uid in approved_users:
        info = user_profiles.get(uid, {})
        full_details.append(f"🆔 `{uid}`\n👤 {info.get('name','')}\n@{info.get('username','')}\nJoined: {info.get('join_date','')}\n")
    msg = "👥 *Approved Users:*\n\n" + "\n".join(full_details)
    safe_send_message(m.chat.id, msg)

@bot.message_handler(func=lambda m: m.text == "❌ Remove User" and is_admin(m.chat.id))
def handle_remove_user_prompt(m):
    safe_send_message(m.chat.id, "🆔 Enter User ID to remove:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(m, handle_process_user_removal)

def handle_process_user_removal(m):
    chat_id = m.chat.id
    if m.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled.", reply_markup=get_user_management_keyboard())
        return
    try:
        uid = int(m.text.strip())
        if str(uid) == str(ADMIN_ID):
            safe_send_message(chat_id, "❌ Cannot remove admin!", reply_markup=get_user_management_keyboard())
            return
        if uid in approved_users:
            approved_users.remove(uid)
            safe_delete_user(uid)
            safe_send_message(chat_id, f"✅ User {uid} removed.", reply_markup=get_user_management_keyboard())
            try:
                safe_send_message(uid, "❌ Your access has been revoked by admin.")
            except:
                pass
        else:
            safe_send_message(chat_id, f"❌ User {uid} not found.", reply_markup=get_user_management_keyboard())
    except:
        safe_send_message(chat_id, "❌ Invalid User ID.", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast" and is_admin(m.chat.id))
def handle_broadcast_menu(m):
    safe_send_message(m.chat.id, "📢 Broadcast Options", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda m: m.text == "📢 Text Broadcast" and is_admin(m.chat.id))
def handle_text_broadcast_prompt(m):
    safe_send_message(m.chat.id, "✍️ Enter message to broadcast:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(m, handle_process_text_broadcast)

def handle_process_text_broadcast(m):
    chat_id = m.chat.id
    if m.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled.", reply_markup=get_broadcast_keyboard())
        return
    msg_text = m.text
    total = len(approved_users)
    progress_msg = safe_send_message(chat_id, f"📢 Broadcasting to {total} users...\n\n0/{total} sent")
    success, fail = 0, 0
    for i, uid in enumerate(approved_users, 1):
        try:
            if uid == int(ADMIN_ID):
                continue
            safe_send_message(uid, f"📢 *Admin Broadcast*\n\n{msg_text}")
            success += 1
        except:
            fail += 1
        if i % 5 == 0 or i == total:
            try:
                bot.edit_message_text(
                    f"📢 Sending to {total} users...\n{i}/{total} done\n✅ {success} success\n❌ {fail} failed",
                    chat_id=chat_id,
                    message_id=progress_msg.message_id
                )
            except:
                pass
    safe_send_message(chat_id, f"📢 Broadcast finished!\n\n✅ {success} success\n❌ {fail} failed", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "📋 Media Broadcast" and is_admin(m.chat.id))
def handle_media_broadcast_prompt(m):
    safe_send_message(m.chat.id, "🖼 Send media with caption:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(m, handle_process_media_broadcast)

def handle_process_media_broadcast(m):
    chat_id = m.chat.id
    if m.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled.", reply_markup=get_broadcast_keyboard())
        return
    total = len(approved_users)
    progress_msg = safe_send_message(chat_id, f"📢 Broadcasting media to {total} users...\n\n0/{total} sent")
    success, fail = 0, 0
    for i, uid in enumerate(approved_users, 1):
        try:
            if uid == int(ADMIN_ID):
                continue
            if m.photo:
                bot.send_photo(uid, m.photo[-1].file_id, caption=m.caption)
            elif m.video:
                bot.send_video(uid, m.video.file_id, caption=m.caption)
            elif m.document:
                bot.send_document(uid, m.document.file_id, caption=m.caption)
            else:
                fail += 1
                continue
            success += 1
        except:
            fail += 1
        if i % 5 == 0 or i == total:
            try:
                bot.edit_message_text(
                    f"📢 Sending media to {total} users...\n{i}/{total} done\n✅ {success} success\n❌ {fail} failed",
                    chat_id=chat_id,
                    message_id=progress_msg.message_id
                )
            except:
                pass
    safe_send_message(chat_id, f"📢 Media broadcast finished!\n\n✅ {success} success\n❌ {fail} failed", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "⬅️ Back to Admin" and is_admin(m.chat.id))
def handle_back_to_admin(m):
    safe_send_message(m.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "⬅️ Main Menu")
def handle_back_to_main(m):
    safe_send_message(m.chat.id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(m.chat.id))

# --- Callback Handlers ---
@bot.callback_query_handler(func=lambda c: c.data.startswith(('approve_', 'reject_')))
def handle_approval_callback(c):
    if not is_admin(c.message.chat.id):
        return
    action, uid_str = c.data.split('_')
    uid = int(uid_str)
    if action == "approve":
        approved_users.add(uid)
        if uid in pending_approvals:
            del pending_approvals[uid]
        safe_send_message(uid, "✅ Your access has been approved!", reply_markup=get_main_keyboard(uid))
        bot.answer_callback_query(c.id, "User approved")
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        safe_send_message(c.message.chat.id, f"✅ User {uid} approved.")
    elif action == "reject":
        if uid in pending_approvals:
            del pending_approvals[uid]
        safe_send_message(uid, "❌ Your access request has been rejected.")
        bot.answer_callback_query(c.id, "User rejected")
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        safe_send_message(c.message.chat.id, f"❌ User {uid} rejected.")

# --- Main Mail Functions ---
@bot.message_handler(func=lambda m: m.text == "📬 New mail")
def handle_new_mail(m):
    chat_id = m.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    email, name = generate_email()
    password = "TempPass123!"
    result = create_account(email, password)
    if result in ["created", "exists"]:
        token = get_token(email, password)
        if token:
            user_data[chat_id] = {"email": email, "password": password, "token": token}
            last_message_ids[chat_id] = set()
            msg = f"✅ *Temporary Email Created!*\n\n`{email}`\n\nTap to copy"
            safe_send_message(chat_id, msg)
        else:
            safe_send_message(chat_id, "❌ Failed to login. Try again.")
    else:
        safe_send_message(chat_id, "❌ Could not create temp mail.")

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

                otp_match = re.search(r"\b\d{6,8}\b", body)
                otp_text = ""
                if otp_match:
                    otp_code = otp_match.group()
                    otp_text = f"\n\n🚨 OTP Detected: `{otp_code}` (Click to copy!)"

                msg_text = (
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "📬 *New Email Received!*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *From:* `{sender}`\n"
                    f"📨 *Subject:* _{subject}_\n"
                    f"🕒 *Received:* {msg_detail.get('intro', 'Just now')}\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "💬 *Body:*\n"
                    f"{body[:4000]}{otp_text}\n"
                    "━━━━━━━━━━━━━━━━━━━━"
                )
                safe_send_message(chat_id, msg_text)
        except:
            pass

# --- Profile handlers ---
@bot.message_handler(func=lambda m: m.text in ["👨 Male Profile", "👩 Female Profile"])
def handle_generate_profile(m):
    chat_id = m.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    gender = "male" if m.text == "👨 Male Profile" else "female"
    gender, name, username, password, phone = generate_profile(gender)
    user_profiles[chat_id] = {
        "name": name,
        "username": username,
        "join_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    msg = profile_message(gender, name, username, password, phone)
    safe_send_message(chat_id, msg)

# --- "👤 My Account" ---
@bot.message_handler(func=lambda m: m.text == "👤 My Account")
def handle_my_account(m):
    chat_id = m.chat.id
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    profile = user_profiles.get(chat_id)
    if not profile:
        safe_send_message(chat_id, "No profile info available.")
        return
    msg = (
        f"🧑‍💼 *Your Account Info*\n\n"
        f"👤 Name: {profile.get('name')}\n"
        f"🆔 Username: {profile.get('username')}\n"
        f"📅 Joined: {profile.get('join_date')}"
    )
    safe_send_message(chat_id, msg)

# --- 2FA Handler ---
@bot.message_handler(func=lambda m: m.text == "🔐 2FA Auth")
def handle_2fa_main(m):
    chat_id = m.chat.id
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    safe_send_message(chat_id, "🔐 Choose platform for your 2FA:", reply_markup=get_2fa_platform_keyboard())

@bot.message_handler(func=lambda m: m.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform(m):
    chat_id = m.chat.id
    platform = m.text
    user_2fa_secrets[chat_id] = {"platform": platform}
    safe_send_message(chat_id, f"🔢 Enter your secret key for {platform}:", reply_markup=get_back_keyboard())

@bot.message_handler(func=lambda m: m.text == "⬅️ Back")
def handle_back(m):
    chat_id = m.chat.id
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))

@bot.message_handler(func=lambda m: True)
def handle_2fa_input(m):
    chat_id = m.chat.id
    if chat_id in user_2fa_secrets and "platform" in user_2fa_secrets[chat_id]:
        secret_input = m.text.strip()
        if not is_valid_base32(secret_input):
            safe_send_message(chat_id, "❌ <b>Invalid Secret Key</b>\n\nYour secret must be a valid Base32 string.\n- Only A-Z and 2-7\n- No lowercase\n- No spaces or special chars\n\nTry again or /cancel", reply_markup=get_back_keyboard())
            return
        secret_clean = secret_input.replace(" ", "").replace("-", "").upper()
        user_2fa_secrets[chat_id]["secret"] = secret_clean
        platform = user_2fa_secrets[chat_id]["platform"]
        totp = pyotp.TOTP(secret_clean)
        current_code = totp.now()
        now = datetime.datetime.now()
        seconds = 30 - (now.second % 30)
        reply_text = f"Your current {platform} 2FA code:\n\n`{current_code}`\n\nValid for {seconds} seconds."
        safe_send_message(chat_id, reply_text, reply_markup=get_main_keyboard(chat_id))
        user_2fa_secrets.pop(chat_id, None)

def get_2fa_platform_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("Google", "Facebook", "Instagram")
    kb.row("Twitter", "Microsoft", "Apple")
    kb.row("⬅️ Back")
    return kb

# --- Profile message ---
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

# --- Generate Profile ---
def generate_profile(gender):
    name = fake.name_male() if gender == "male" else fake.name_female()
    username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    password = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8)) + datetime.datetime.now().strftime("%d")
    phone = '1' + ''.join([str(random.randint(200, 999))]) + ''.join([str(random.randint(0, 9)) for _ in range(7)])
    return gender, name, username, password, phone

# --- Email Functions ---
def get_domain():
    try:
        res = requests.get("https://api.mail.tm/domains", timeout=10)
        domains = res.json().get("hydra:member", [])
        return domains[0]["domain"] if domains else "mail.tm"
    except:
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
        if res.status_code in [201, 422]:
            return "created" if res.status_code == 201 else "exists"
        return "error"
    except:
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
    except:
        return None

# --- Main Handlers ---
@bot.message_handler(commands=['start', 'help'])
def handle_start_help(m):
    chat_id = m.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    user_info = get_user_info(m.from_user)
    user_profiles[chat_id] = user_info
    if is_admin(chat_id):
        approved_users.add(chat_id)
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=get_main_keyboard(chat_id))
    elif chat_id in approved_users:
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

# --- Main Menu Handlers ---
@bot.message_handler(func=lambda m: m.text == "👑 Admin Panel" and is_admin(m.chat.id))
def handle_admin_panel(m):
    safe_send_message(m.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "👥 Pending Approvals" and is_admin(m.chat.id))
def handle_pending_approvals(m):
    if not pending_approvals:
        safe_send_message(m.chat.id, "✅ No pending approvals.")
        return
    for uid, info in pending_approvals.items():
        msg = (
            f"🆕 *Pending Approval*\n\n"
            f"🆔 User ID: `{uid}`\n"
            f"👤 Name: `{info['name']}`\n"
            f"📛 Username: @{info['username']}\n"
            f"📅 Joined: `{info['join_date']}`"
        )
        safe_send_message(m.chat.id, msg, reply_markup=get_approval_keyboard(uid))

@bot.message_handler(func=lambda m: m.text == "📊 Stats" and is_admin(m.chat.id))
def handle_stats(m):
    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin: `{ADMIN_ID}`\n"
        f"👥 Total Users: `{len(approved_users)}`\n"
        f"📭 Active Sessions: `{len(active_sessions)}`\n"
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        f"📧 Active Email Sessions: `{len(user_data)}`\n"
        f"📅 Uptime: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
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
    # Compose full details of each approved user
    details_list = []
    for uid in approved_users:
        profile = user_profiles.get(uid, {})
        details = (
            f"🆔 `{uid}`\n"
            f"👤 {profile.get('name','')}\n"
            f"@{profile.get('username','')}\n"
            f"Joined: {profile.get('join_date','')}\n"
        )
        details_list.append(details)
    msg = "👥 *Approved Users Details:*\n\n" + "\n".join(details_list)
    safe_send_message(m.chat.id, msg)

@bot.message_handler(func=lambda m: m.text == "❌ Remove User" and is_admin(m.chat.id))
def handle_remove_user_prompt(m):
    safe_send_message(m.chat.id, "🆔 Enter User ID to remove:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(m, handle_process_user_removal)

def handle_process_user_removal(m):
    chat_id = m.chat.id
    if m.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled.", reply_markup=get_user_management_keyboard())
        return
    try:
        uid = int(m.text.strip())
        if str(uid) == str(ADMIN_ID):
            safe_send_message(chat_id, "❌ Cannot remove admin!", reply_markup=get_user_management_keyboard())
            return
        if uid in approved_users:
            approved_users.remove(uid)
            safe_delete_user(uid)
            safe_send_message(chat_id, f"✅ User {uid} removed.", reply_markup=get_user_management_keyboard())
            try:
                safe_send_message(uid, "❌ Your access has been revoked by admin.")
            except:
                pass
        else:
            safe_send_message(chat_id, f"❌ User {uid} not found.", reply_markup=get_user_management_keyboard())
    except:
        safe_send_message(chat_id, "❌ Invalid User ID.", reply_markup=get_user_management_keyboard())

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast" and is_admin(m.chat.id))
def handle_broadcast_menu(m):
    safe_send_message(m.chat.id, "📢 Broadcast Options", reply_markup=get_broadcast_keyboard())

@bot.message_handler(func=lambda m: m.text == "📢 Text Broadcast" and is_admin(m.chat.id))
def handle_broadcast_prompt(m):
    safe_send_message(m.chat.id, "✍️ Enter message to broadcast:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(m, handle_process_text_broadcast)

def handle_process_text_broadcast(m):
    chat_id = m.chat.id
    if m.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled.", reply_markup=get_broadcast_keyboard())
        return
    msg_text = m.text
    total = len(approved_users)
    progress_msg = safe_send_message(chat_id, f"📢 Broadcasting to {total} users...\n\n0/{total} sent")
    success, fail = 0, 0
    for i, uid in enumerate(approved_users, 1):
        try:
            if uid == int(ADMIN_ID):
                continue
            safe_send_message(uid, f"📢 *Admin Broadcast*\n\n{msg_text}")
            success += 1
        except:
            fail += 1
        if i % 5 == 0 or i == total:
            try:
                bot.edit_message_text(
                    f"📢 Sending to {total} users...\n{i}/{total} done\n✅ {success} success\n❌ {fail} failed",
                    chat_id=chat_id,
                    message_id=progress_msg.message_id
                )
            except:
                pass
    safe_send_message(chat_id, f"📢 Broadcast finished!\n\n✅ {success} success\n❌ {fail} failed", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "📋 Media Broadcast" and is_admin(m.chat.id))
def handle_media_broadcast_prompt(m):
    safe_send_message(m.chat.id, "🖼 Send media with caption:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(m, handle_process_media_broadcast)

def handle_process_media_broadcast(m):
    chat_id = m.chat.id
    if m.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled.", reply_markup=get_broadcast_keyboard())
        return
    total = len(approved_users)
    progress_msg = safe_send_message(chat_id, f"📢 Broadcasting media to {total} users...\n\n0/{total} sent")
    success, fail = 0, 0
    for i, uid in enumerate(approved_users, 1):
        try:
            if uid == int(ADMIN_ID):
                continue
            if m.photo:
                bot.send_photo(uid, m.photo[-1].file_id, caption=m.caption)
            elif m.video:
                bot.send_video(uid, m.video.file_id, caption=m.caption)
            elif m.document:
                bot.send_document(uid, m.document.file_id, caption=m.caption)
            else:
                fail += 1
                continue
            success += 1
        except:
            fail += 1
        if i % 5 == 0 or i == total:
            try:
                bot.edit_message_text(
                    f"📢 Sending media to {total} users...\n{i}/{total} done\n✅ {success} success\n❌ {fail} failed",
                    chat_id=chat_id,
                    message_id=progress_msg.message_id
                )
            except:
                pass
    safe_send_message(chat_id, f"📢 Media broadcast finished!\n\n✅ {success} success\n❌ {fail} failed", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "⬅️ Back to Admin" and is_admin(m.chat.id))
def handle_back_to_admin(m):
    safe_send_message(m.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda m: m.text == "⬅️ Main Menu")
def handle_back_to_main(m):
    safe_send_message(m.chat.id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(m.chat.id))

# --- Callback Query Handlers ---
@bot.callback_query_handler(func=lambda c: c.data.startswith(('approve_', 'reject_')))
def handle_approval_callback(c):
    if not is_admin(c.message.chat.id):
        return
    action, uid_str = c.data.split('_')
    uid = int(uid_str)
    if action == "approve":
        approved_users.add(uid)
        if uid in pending_approvals:
            del pending_approvals[uid]
        safe_send_message(uid, "✅ Your access has been approved!", reply_markup=get_main_keyboard(uid))
        bot.answer_callback_query(c.id, "User approved")
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        safe_send_message(c.message.chat.id, f"✅ User {uid} approved.")
    elif action == "reject":
        if uid in pending_approvals:
            del pending_approvals[uid]
        safe_send_message(uid, "❌ Your access request has been rejected.")
        bot.answer_callback_query(c.id, "User rejected")
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        safe_send_message(c.message.chat.id, f"❌ User {uid} rejected.")

# --- Main Mail Functions ---
@bot.message_handler(func=lambda m: m.text == "📬 New mail")
def handle_new_mail(m):
    chat_id = m.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    email, name = generate_email()
    password = "TempPass123!"
    result = create_account(email, password)
    if result in ["created", "exists"]:
        token = get_token(email, password)
        if token:
            user_data[chat_id] = {"email": email, "password": password, "token": token}
            last_message_ids[chat_id] = set()
            msg = f"✅ *Temporary Email Created!*\n\n`{email}`\n\nTap to copy"
            safe_send_message(chat_id, msg)
        else:
            safe_send_message(chat_id, "❌ Failed to login. Try again.")
    else:
        safe_send_message(chat_id, "❌ Could not create temp mail.")

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

                otp_match = re.search(r"\b\d{6,8}\b", body)
                otp_text = ""
                if otp_match:
                    otp_code = otp_match.group()
                    otp_text = f"\n\n🚨 OTP Detected: `{otp_code}` (Click to copy!)"

                msg_text = (
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "📬 *New Email Received!*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *From:* `{sender}`\n"
                    f"📨 *Subject:* _{subject}_\n"
                    f"🕒 *Received:* {msg_detail.get('intro', 'Just now')}\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "💬 *Body:*\n"
                    f"{body[:4000]}{otp_text}\n"
                    "━━━━━━━━━━━━━━━━━━━━"
                )
                safe_send_message(chat_id, msg_text)
        except:
            pass

# --- Profile Handlers ---
@bot.message_handler(func=lambda m: m.text in ["👨 Male Profile", "👩 Female Profile"])
def handle_generate_profile(m):
    chat_id = m.chat.id
    if is_bot_blocked(chat_id):
        safe_delete_user(chat_id)
        return
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    gender = "male" if m.text == "👨 Male Profile" else "female"
    gender, name, username, password, phone = generate_profile(gender)
    user_profiles[chat_id] = {
        "name": name,
        "username": username,
        "join_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    msg = profile_message(gender, name, username, password, phone)
    safe_send_message(chat_id, msg)

# --- Handle "👤 My Account" ---
@bot.message_handler(func=lambda m: m.text == "👤 My Account")
def handle_my_account(m):
    chat_id = m.chat.id
    if not is_authorized(chat_id):
        safe_send_message(chat_id, "⏳ Your access is pending approval.")
        return
    profile = user_profiles.get(chat_id)
    if not profile:
        safe_send_message(chat_id, "No profile info available.")
        return
    msg = (
        f"🧑‍💼 *Your Account Info*\n\n"
        f"👤 Name: {profile.get('name')}\n"
        f"🆔 Username: {profile.get('username')}\n"
        f"📅 Joined: {profile.get('join_date')}"
    )
    safe_send_message(chat_id, msg)

# --- Run bot & start threads ---
if __name__ == "__main__":
    print("🤖 Bot is running...")
    threading.Thread(target=auto_refresh_worker, daemon=True).start()
    threading.Thread(target=cleanup_blocked_users, daemon=True).start()
    
    # For Railway deployment
    if 'RAILWAY_ENVIRONMENT' in os.environ:
        PORT = int(os.environ.get('PORT', 5000))
        # Simple health check endpoint
        from flask import Flask
        app = Flask(__name__)
        @app.route('/')
        def health_check():
            return "Bot is running"
        threading.Thread(target=app.run, kwargs={'host':'0.0.0.0','port':PORT}).start()
    
    bot.infinity_polling()
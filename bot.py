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
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton # Added import

load_dotenv()
fake = Faker()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
MAILSLURP_API_KEY = os.getenv("MAILSLURP_API_KEY")

if not BOT_TOKEN:
    raise Exception("❌ BOT_TOKEN not set in .env")
if not ADMIN_ID:
    raise Exception("❌ ADMIN_ID not set in .env")
if not MAILSLURP_API_KEY:
    raise Exception("❌ MAILSLURP_API_KEY not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown") # Default parse mode

# Data storage
user_data = {}  # Stores {"email": email_address, "inbox_id": inbox_id}
last_message_ids = {} # Stores seen email IDs from MailSlurp {chat_id: {email_id1, email_id2}}
user_2fa_secrets = {} # Store user secrets and platform for 2FA {chat_id: {"platform": "Google", "secret": "BASE32SECRET"}}
active_sessions = set()
pending_approvals = {}
approved_users = set()
user_profiles = {}  # Stores additional user profile info

# --- Helper Functions ---

def is_admin(chat_id):
    return str(chat_id) == ADMIN_ID

def safe_delete_user(chat_id):
    if chat_id in user_data:
        del user_data[chat_id]
    if chat_id in last_message_ids:
        del last_message_ids[chat_id]
    if chat_id in user_2fa_secrets: # Changed from user_2fa_codes
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
        if e.error_code == 403: # More robust check for 403
             print(f"Bot might be blocked by user {chat_id}: {e}")
             return True # Assume blocked on 403, specific message check can be fragile
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
    keyboard.row("⬅️ Back to Main") # This button now needs to clear 2FA state
    return keyboard

def get_back_keyboard(context="main"): # context can be '2fa_secret_prompt' or 'admin_user_removal' etc.
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    if context == "2fa_secret_prompt":
        keyboard.row("⬅️ Back to Platforms") # More specific back
    else:
        keyboard.row("⬅️ Back")
    return keyboard


def get_broadcast_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("📢 Text Broadcast", "📋 Media Broadcast")
    keyboard.row("⬅️ Back to Admin")
    return keyboard

def safe_send_message(chat_id, text, parse_mode=None, **kwargs): # Allow overriding parse_mode
    try:
        if is_bot_blocked(chat_id):
            safe_delete_user(chat_id)
            return None

        current_parse_mode = parse_mode if parse_mode else bot.parse_mode
        msg = bot.send_message(chat_id, text, parse_mode=current_parse_mode, **kwargs)
        active_sessions.add(chat_id)
        return msg
    except telebot.apihelper.ApiTelegramException as e:
        if e.error_code == 403:
            print(f"Bot blocked by {chat_id} during send. Cleaning up.")
            safe_delete_user(chat_id)
        else:
            print(f"API Error sending message to {chat_id}: {e}")
        return None
    except Exception as e:
        print(f"Generic error sending message to {chat_id}: {str(e)}")
        return None

# --- MailSlurp Functions ---
MAILSLURP_API_URL = "https://api.mailslurp.com"

def create_mailslurp_inbox():
    """Creates a new MailSlurp inbox."""
    try:
        headers = {"x-api-key": MAILSLURP_API_KEY}
        # useDomainPool=true allows MailSlurp to pick a domain
        response = requests.post(f"{MAILSLURP_API_URL}/inboxes?useDomainPool=true", headers=headers, timeout=15)
        response.raise_for_status() # Raise an exception for HTTP errors
        inbox_data = response.json()
        return inbox_data.get("id"), inbox_data.get("emailAddress")
    except requests.exceptions.RequestException as e:
        print(f"MailSlurp inbox creation error: {e}")
        return None, None

def get_mailslurp_emails(inbox_id, unread_only=False, sort_desc=True):
    """Fetches email previews from a MailSlurp inbox."""
    try:
        headers = {"x-api-key": MAILSLURP_API_KEY}
        params = {}
        if unread_only:
            params['unreadOnly'] = 'true'
        if sort_desc:
            params['sort'] = 'DESC'
        
        response = requests.get(f"{MAILSLURP_API_URL}/inboxes/{inbox_id}/emails", headers=headers, params=params, timeout=15)
        response.raise_for_status()
        return response.json() # This is a list of EmailPreview objects
    except requests.exceptions.RequestException as e:
        print(f"MailSlurp fetch emails error for inbox {inbox_id}: {e}")
        return []

def get_mailslurp_email_details(email_id):
    """Fetches full details for a specific email from MailSlurp."""
    try:
        headers = {"x-api-key": MAILSLURP_API_KEY}
        response = requests.get(f"{MAILSLURP_API_URL}/emails/{email_id}", headers=headers, timeout=15)
        response.raise_for_status()
        return response.json() # This is the full EmailDto object
    except requests.exceptions.RequestException as e:
        print(f"MailSlurp fetch email detail error for email {email_id}: {e}")
        return None

# --- Profile generator --- (Copied from original, assumed correct)
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
        # Ensure only valid Base32 characters (A-Z, 2-7) and correct padding
        if not all(c in string.ascii_uppercase + '234567' for c in cleaned.rstrip('=')):
            return False
        pyotp.TOTP(cleaned).now() # This will raise error if invalid format or padding
        return True
    except (binascii.Error, ValueError, Exception) as e:
        print(f"Base32 validation error: {e}")
        return False

def get_2fa_code_message_content(secret_key):
    totp = pyotp.TOTP(secret_key)
    current_code = totp.now()
    now = datetime.datetime.now()
    seconds_remaining = 30 - (now.second % 30)
    
    # Ensure code is padded to 6 digits if it's shorter (some authenticators do this)
    formatted_code = str(current_code).zfill(6)

    reply_text = (
        f"<b>CODE</b>\n"
        f"<code>{formatted_code}</code>\n"
        f"<i>Valid for {seconds_remaining}s</i>\n"
        f"─────────────────────\n"
        f"Tap code to copy. Refresh if needed."
    )
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh Code", callback_data="generate_2fa_code")]])
    return reply_text, reply_markup


# --- Background Workers ---

def auto_refresh_worker():
    while True:
        try:
            active_user_data_keys = list(user_data.keys()) # Iterate over a copy
            for chat_id in active_user_data_keys:
                if chat_id not in user_data: # User might have been deleted
                    continue
                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id)
                    continue

                inbox_id = user_data[chat_id].get("inbox_id")
                if not inbox_id:
                    continue

                email_previews = get_mailslurp_emails(inbox_id, unread_only=True, sort_desc=True)
                if not email_previews:
                    continue

                seen_ids = last_message_ids.setdefault(chat_id, set())
                new_messages_found = False

                for preview in email_previews[:5]: # Process up to 5 newest unread
                    msg_id = preview["id"]
                    if msg_id in seen_ids:
                        continue
                    
                    new_messages_found = True
                    msg_detail = get_mailslurp_email_details(msg_id)
                    if not msg_detail:
                        continue
                    
                    seen_ids.add(msg_id) # Add to seen only after successful fetch

                    sender = msg_detail.get("from") or "N/A"
                    subject = msg_detail.get("subject", "(No Subject)")
                    # Prefer textBody, fallback to body (which is HTML, so keep it short or strip)
                    body_content = msg_detail.get("textBody") or msg_detail.get("body", "(No Content)")
                    
                    received_at_str = msg_detail.get("createdAt")
                    received_display = "Just now"
                    if received_at_str:
                        try:
                            received_dt = datetime.datetime.fromisoformat(received_at_str.replace("Z", "+00:00"))
                            received_display = received_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                        except ValueError:
                            pass # Keep "Just now" if parsing fails

                    formatted_msg = (
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📬 *New Email Received!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 *From:* `{sender}`\n"
                        f"📨 *Subject:* _{subject}_\n"
                        f"🕒 *Received:* {received_display}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💬 *Body:*\n"
                        f"{body_content[:1000]}\n" # Limit body length
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )
                    safe_send_message(chat_id, formatted_msg)
                
                # Optional: Limit the size of seen_ids to prevent memory issues over time
                if len(seen_ids) > 100:
                    oldest_ids = sorted(list(seen_ids))[:-50] # Keep the 50 newest
                    for old_id in oldest_ids:
                        seen_ids.remove(old_id)

        except Exception as e:
            print(f"Error in auto_refresh_worker: {e}")
        time.sleep(45) # Check less frequently than mail.tm due to potential API call difference

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
        time.sleep(3600) # Check every hour

# --- Bot Handlers ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): # Check early
        safe_delete_user(chat_id)
        return

    user_info = get_user_info(message.from_user)
    user_profiles[chat_id] = user_info # Store profile info regardless of approval

    if is_admin(chat_id):
        approved_users.add(chat_id)
        active_sessions.add(chat_id)
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=get_main_keyboard(chat_id))
        return

    if chat_id in approved_users:
        active_sessions.add(chat_id)
        safe_send_message(chat_id, "👋 Welcome back!", reply_markup=get_main_keyboard(chat_id))
    else:
        if chat_id not in pending_approvals: # Send request only if not already pending
            pending_approvals[chat_id] = user_info
            if ADMIN_ID: # Ensure ADMIN_ID is valid before sending
                try:
                    admin_chat_id = int(ADMIN_ID)
                    approval_msg = (
                        f"🆕 *New Approval Request*\n\n"
                        f"🆔 User ID: `{chat_id}`\n"
                        f"👤 Name: `{user_info['name']}`\n"
                        f"📛 Username: @{user_info['username']}\n"
                        f"📅 Joined: `{user_info['join_date']}`"
                    )
                    bot.send_message(admin_chat_id, approval_msg, reply_markup=get_approval_keyboard(chat_id), parse_mode="Markdown")
                    safe_send_message(chat_id, "👋 Your access request has been sent to the admin. Please wait for approval.")
                except Exception as e:
                    print(f"Failed to send approval request to admin: {e}")
                    safe_send_message(chat_id, "👋 Welcome! Could not notify admin currently. Please try /start again later.")
            else:
                 safe_send_message(chat_id, "👋 Welcome! Admin not configured for approvals. Please contact support.")
        else:
            safe_send_message(chat_id, "⏳ Your access request is still pending. Please wait.")


# --- Admin Panel Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel" and is_admin(msg.chat.id))
def admin_panel(message):
    safe_send_message(message.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "👥 Pending Approvals" and is_admin(msg.chat.id))
def show_pending_approvals(message):
    if not pending_approvals:
        safe_send_message(message.chat.id, "✅ No pending approvals.")
        return
    for user_id, user_info in list(pending_approvals.items()): # Iterate over a copy
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
    # uptime_delta = datetime.datetime.now() - bot_start_time # Assuming bot_start_time is defined globally
    # uptime_str = str(uptime_delta).split('.')[0] # Simple uptime
    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin: `{ADMIN_ID}`\n"
        f"👥 Approved Users: `{len(approved_users)}`\n"
        f"📭 Active Email Sessions (MailSlurp): `{len(user_data)}`\n"
        f"⏳ Pending Approvals: `{len(pending_approvals)}`\n"
        # f"🕰 Uptime: `{uptime_str}`\n" # Add if you track bot_start_time
        f"🤖 Active Bot Sessions (rough): `{len(active_sessions)}`"
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
            users_list_msgs.append(f"🆔 `{user_id_approved}` - (Profile info not available)")
            
    if not users_list_msgs:
        safe_send_message(message.chat.id, "❌ No user data available to list.")
        return

    chunk_size = 10 # Messages per chunk
    for i in range(0, len(users_list_msgs), chunk_size):
        chunk = users_list_msgs[i:i + chunk_size]
        response = "👥 *Approved Users (" + str(i+1) + "-" + str(min(i+chunk_size, len(users_list_msgs))) + ")*\n\n" + "\n".join(chunk)
        safe_send_message(message.chat.id, response)

@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_user_prompt(message):
    safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=get_back_keyboard("admin_user_removal"))
    bot.register_next_step_handler(message, process_user_removal)

def process_user_removal(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back":
        safe_send_message(chat_id, "Cancelled user removal.", reply_markup=get_user_management_keyboard())
        return
    try:
        user_id_to_remove = int(message.text.strip())
        if user_id_to_remove == int(ADMIN_ID):
            safe_send_message(chat_id, "❌ Cannot remove admin!", reply_markup=get_user_management_keyboard())
            return
        
        removed_from_approved = False
        if user_id_to_remove in approved_users:
            approved_users.remove(user_id_to_remove)
            removed_from_approved = True

        # Safe delete will also try to remove from approved_users if it's there, but good to do it explicitly.
        safe_delete_user(user_id_to_remove) # This handles all data structures

        if removed_from_approved:
            safe_send_message(chat_id, f"✅ User {user_id_to_remove} has been removed and all their data cleared.", reply_markup=get_user_management_keyboard())
            try:
                safe_send_message(user_id_to_remove, "❌ Your access has been revoked by the admin and your data has been cleared.")
            except Exception as e:
                print(f"Could not notify user {user_id_to_remove} about removal: {e}")
        else:
            safe_send_message(chat_id, f"⚠️ User {user_id_to_remove} was not found in approved list, but attempted to clear any residual data.", reply_markup=get_user_management_keyboard())

    except ValueError:
        safe_send_message(chat_id, "❌ Invalid User ID. Please enter a numeric ID.", reply_markup=get_user_management_keyboard())
        # Re-prompt
        safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=get_back_keyboard("admin_user_removal"))
        bot.register_next_step_handler(message, process_user_removal)


@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def broadcast_menu(message):
    safe_send_message(message.chat.id, "📢 Broadcast Message to All Approved Users", reply_markup=get_broadcast_keyboard())

def _do_broadcast(admin_chat_id, message_to_broadcast, is_media=False, media_info=None):
    success = 0
    failed = 0
    # Create a snapshot of approved users at the start of the broadcast
    users_to_broadcast = list(approved_users)
    total = len(users_to_broadcast)

    if total == 0:
        safe_send_message(admin_chat_id, "📢 No users to broadcast to.", reply_markup=get_admin_keyboard())
        return

    progress_msg_text = f"📢 Broadcasting to {total} users...\n\n0/{total} sent"
    progress_msg = safe_send_message(admin_chat_id, progress_msg_text)
    
    if not progress_msg: # Could not send initial progress to admin
        print("Failed to send broadcast progress message to admin.")
        return

    for i, user_id in enumerate(users_to_broadcast, 1):
        if str(user_id) == str(ADMIN_ID): # Don't broadcast to admin self this way
            # success +=1 # Or skip, admin already knows
            continue
        try:
            if is_media and media_info:
                if media_info['type'] == 'photo':
                    bot.send_photo(user_id, media_info['file_id'], caption=media_info['caption'], parse_mode="Markdown") # Assuming caption might have markdown
                elif media_info['type'] == 'video':
                    bot.send_video(user_id, media_info['file_id'], caption=media_info['caption'], parse_mode="Markdown")
                elif media_info['type'] == 'document':
                    bot.send_document(user_id, media_info['file_id'], caption=media_info['caption'], parse_mode="Markdown")
            else:
                # For text, add a header
                full_broadcast_text = f"📢 *Admin Broadcast*\n\n{message_to_broadcast}"
                safe_send_message(user_id, full_broadcast_text) # Uses default markdown
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 403: # User blocked the bot
                print(f"User {user_id} blocked the bot during broadcast. Removing.")
                safe_delete_user(user_id) # Remove them
            failed += 1
        except Exception as e_gen:
            print(f"Failed to send broadcast to {user_id}: {e_gen}")
            failed += 1
        
        time.sleep(0.1) # Small delay to avoid hitting limits too fast

        if i % 5 == 0 or i == total: # Update progress
            try:
                bot.edit_message_text(
                    f"📢 Broadcasting to {total} users...\n\n{i}/{total} sent\n✅ {success} successful\n❌ {failed} failed",
                    chat_id=admin_chat_id,
                    message_id=progress_msg.message_id
                )
            except Exception as e_edit:
                print(f"Error updating broadcast progress: {e_edit}") # Continue broadcast even if progress update fails

    final_summary = f"📢 Broadcast completed!\n\n✅ {success} successful\n❌ {failed} failed"
    safe_send_message(admin_chat_id, final_summary, reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def process_text_broadcast_prompt(message):
    safe_send_message(message.chat.id, "✍️ Enter the broadcast message text (Markdown supported):", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(message, process_text_broadcast)

def process_text_broadcast(message):
    admin_chat_id = message.chat.id
    if message.text == "⬅️ Back":
        safe_send_message(admin_chat_id, "Cancelled text broadcast.", reply_markup=get_broadcast_keyboard())
        return
    
    broadcast_text = message.text
    _do_broadcast(admin_chat_id, broadcast_text)


@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def media_broadcast_prompt(message):
    safe_send_message(message.chat.id, "🖼 Send the photo/video/document you want to broadcast (you can add a caption, Markdown supported):", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(message, process_media_broadcast)

def process_media_broadcast(message):
    admin_chat_id = message.chat.id
    if message.text == "⬅️ Back": # Check for text "Back" first
        safe_send_message(admin_chat_id, "Cancelled media broadcast.", reply_markup=get_broadcast_keyboard())
        return

    media_info = None
    if message.photo:
        media_info = {'type': 'photo', 'file_id': message.photo[-1].file_id, 'caption': message.caption}
    elif message.video:
        media_info = {'type': 'video', 'file_id': message.video.file_id, 'caption': message.caption}
    elif message.document:
        media_info = {'type': 'document', 'file_id': message.document.file_id, 'caption': message.caption}
    else:
        safe_send_message(admin_chat_id, "❌ No media detected. Please send a photo, video, or document. Or click '⬅️ Back' to cancel.", reply_markup=get_back_keyboard())
        bot.register_next_step_handler(message, process_media_broadcast) # Re-register
        return
        
    _do_broadcast(admin_chat_id, None, is_media=True, media_info=media_info)


@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin" and is_admin(msg.chat.id))
def back_to_admin(message):
    safe_send_message(message.chat.id, "⬅️ Returning to admin panel...", reply_markup=get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu") # Simplified: works for admin and regular user if they see this
def back_to_main_shared(message):
    chat_id = message.chat.id
    # Clear 2FA state if returning to main menu from a 2FA context
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]
        print(f"Cleared 2FA secret for {chat_id} due to 'Main Menu'")
    safe_send_message(chat_id, "⬅️ Returning to main menu...", reply_markup=get_main_keyboard(chat_id))


@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def handle_approval(call):
    admin_chat_id = call.message.chat.id
    if not is_admin(admin_chat_id):
        bot.answer_callback_query(call.id, "⚠️ Action not allowed.")
        return

    try:
        action, user_id_str = call.data.split('_')
        user_id = int(user_id_str)
    except ValueError:
        bot.answer_callback_query(call.id, "Error: Invalid user ID in callback.")
        bot.edit_message_text("Error processing request.", chat_id=admin_chat_id, message_id=call.message.message_id)
        return

    user_info = pending_approvals.get(user_id, user_profiles.get(user_id)) # Get info if still pending or from general profiles
    username_display = f"@{user_info['username']}" if user_info and user_info['username'] != "N/A" else ""
    name_display = user_info['name'] if user_info else str(user_id)

    if action == "approve":
        approved_users.add(user_id)
        active_sessions.add(user_id) # Mark as active upon approval
        if user_id in pending_approvals:
            del pending_approvals[user_id]
        
        bot.answer_callback_query(call.id, f"User {name_display} approved.")
        bot.edit_message_text(f"✅ User {name_display} ({user_id} {username_display}) has been approved.",
                              chat_id=admin_chat_id, message_id=call.message.message_id, reply_markup=None)
        safe_send_message(user_id, "🎉 Your access request has been approved! You can now use the bot.", reply_markup=get_main_keyboard(user_id))
    
    elif action == "reject":
        if user_id in pending_approvals:
            del pending_approvals[user_id]
        # User is not added to approved_users, no further data cleanup needed unless they were somehow partially added
        
        bot.answer_callback_query(call.id, f"User {name_display} rejected.")
        bot.edit_message_text(f"❌ User {name_display} ({user_id} {username_display}) has been rejected.",
                              chat_id=admin_chat_id, message_id=call.message.message_id, reply_markup=None)
        safe_send_message(user_id, "😔 Your access request has been rejected by the admin.")
        # Optionally, fully delete their profile if you don't want to keep data of rejected users
        # if user_id in user_profiles: del user_profiles[user_id]


# --- Mail handlers (Using MailSlurp) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or you are not authorized.")
        return

    progress_msg = safe_send_message(chat_id, "⏳ Creating your temporary email address with MailSlurp...")
    inbox_id, email_address = create_mailslurp_inbox()

    if progress_msg: # Delete "Creating..." message
        try:
            bot.delete_message(chat_id, progress_msg.message_id)
        except Exception:
            pass # Ignore if deletion fails

    if email_address and inbox_id:
        user_data[chat_id] = {"email": email_address, "inbox_id": inbox_id}
        last_message_ids[chat_id] = set() # Initialize seen messages for this new inbox
        msg_text = f"✅ *Temporary Email Created with MailSlurp!*\n\n`{email_address}`\n\nTap to copy. Emails will appear automatically or use '🔄 Refresh'."
        safe_send_message(chat_id, msg_text)
    else:
        safe_send_message(chat_id, "❌ Failed to create temporary email with MailSlurp. The service might be unavailable or API key invalid. Please try again later.")


@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh")
def refresh_mail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or you are not authorized.")
        return

    if chat_id not in user_data or "inbox_id" not in user_data[chat_id]:
        safe_send_message(chat_id, "⚠️ No active email session. Please create one using '📬 New mail'.")
        return

    inbox_id = user_data[chat_id]["inbox_id"]
    safe_send_message(chat_id, "🔄 Checking for new emails in your MailSlurp inbox...")

    email_previews = get_mailslurp_emails(inbox_id, sort_desc=True) # Get all, sorted
    if email_previews is None: # API error
        safe_send_message(chat_id, "❌ Error connecting to MailSlurp. Try again later.")
        return
    
    if not email_previews:
        safe_send_message(chat_id, "📭 *Your MailSlurp inbox is empty or no new messages found.*")
        return

    # In a manual refresh, we might want to show more than just unread, or the latest few.
    # For simplicity, this will be similar to auto-refresh's display logic for new ones.
    # A more sophisticated refresh could list recent emails even if "seen" by auto-refresh.
    
    seen_ids = last_message_ids.setdefault(chat_id, set())
    new_message_count = 0
    
    for preview in email_previews[:5]: # Show up to 5 most recent
        msg_id = preview["id"]
        # if msg_id in seen_ids: # For manual refresh, maybe show it again if recent?
        #    continue           # For now, let's only show "new" ones to this manual refresh action

        msg_detail = get_mailslurp_email_details(msg_id)
        if not msg_detail:
            continue # Skip if details can't be fetched
        
        if msg_id not in seen_ids: # Only count as "new" if not seen before
             new_message_count +=1
        seen_ids.add(msg_id) # Mark as seen by this refresh action

        sender = msg_detail.get("from") or "N/A"
        subject = msg_detail.get("subject", "(No Subject)")
        body_content = msg_detail.get("textBody") or msg_detail.get("body", "(No Content)")
        received_at_str = msg_detail.get("createdAt")
        received_display = "Recently"
        if received_at_str:
            try:
                received_dt = datetime.datetime.fromisoformat(received_at_str.replace("Z", "+00:00"))
                received_display = received_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except ValueError: pass

        formatted_msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📧 *Email Details*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *From:* `{sender}`\n"
            f"📨 *Subject:* _{subject}_\n"
            f"🕒 *Received:* {received_display}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💬 *Body:*\n"
            f"{body_content[:1000]}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        safe_send_message(chat_id, formatted_msg)
    
    if new_message_count == 0:
         safe_send_message(chat_id, "✅ No new unread emails found since last check. Displaying most recent if any.")
    elif new_message_count > 0 and new_message_count < len(email_previews[:5]):
         safe_send_message(chat_id, f"Found {new_message_count} new email(s). Others shown are recent.")


# --- Profile handlers ---
@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
def generate_profile_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or you are not authorized.")
        return
    gender = "male" if message.text == "👨 Male Profile" else "female"
    # This function (generate_profile) is from the original code, ensure it's defined
    _gender, name, username, password, phone = generate_profile(gender) 
    message_text = profile_message(_gender, name, username, password, phone)
    safe_send_message(chat_id, message_text)

# --- Account Info Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
def my_account_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or you are not authorized.")
        return
    safe_send_message(chat_id, "👤 Your Account Options:", reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📧 My Email")
def my_email_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
        # This check is a bit redundant if they got here via approved path, but good for safety
        safe_send_message(chat_id, "⏳ Your access is pending approval or you are not authorized.")
        return
    
    if chat_id in user_data and "email" in user_data[chat_id]:
        email_address = user_data[chat_id]["email"]
        safe_send_message(chat_id, f"✉️ Your current temporary email is:\n`{email_address}`\n\nTap to copy.", reply_markup=get_user_account_keyboard())
    else:
        safe_send_message(chat_id, "ℹ️ You don't have an active temporary email. Use '📬 New mail' to create one.", reply_markup=get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "🆔 My Info")
def my_info_handler(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or you are not authorized.")
        return

    if chat_id in user_profiles:
        user_p = user_profiles[chat_id]
        info_text = (
            f"👤 *Your Information*\n\n"
            f"Telegram ID: `{chat_id}`\n"
            f"Name: `{user_p['name']}`\n"
            f"Username: @{user_p['username']}\n"
            f"Bot Join Date: `{user_p['join_date']}`"
        )
        safe_send_message(chat_id, info_text, reply_markup=get_user_account_keyboard())
    else:
        safe_send_message(chat_id, "ℹ️ Could not retrieve your profile information.", reply_markup=get_user_account_keyboard())

# --- 2FA Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "🔐 2FA Auth")
def two_fa_auth_start(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
        safe_send_message(chat_id, "⏳ Your access is pending approval or you are not authorized.")
        return
    # Clear any previous 2FA state for this user before starting a new one
    if chat_id in user_2fa_secrets:
        del user_2fa_secrets[chat_id]
    safe_send_message(chat_id, "🔐 Choose the platform for 2FA code generation:", reply_markup=get_2fa_platform_keyboard())

@bot.message_handler(func=lambda msg: msg.text in ["Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple"])
def handle_2fa_platform_selection(message):
    chat_id = message.chat.id
    # Basic auth check (redundant if they reached here via keyboard but good practice)
    if chat_id not in approved_users and not is_admin(chat_id): return

    platform = message.text
    user_2fa_secrets[chat_id] = {"platform": platform} # Store platform, wait for secret
    safe_send_message(chat_id, f"🔑 Enter the Base32 2FA secret key for *{platform}*:", reply_markup=get_back_keyboard("2fa_secret_prompt"))
    bot.register_next_step_handler(message, process_2fa_secret_input)

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Platforms")
def back_to_2fa_platforms(message):
    chat_id = message.chat.id
    if chat_id in user_2fa_secrets: # Clear partial state
        del user_2fa_secrets[chat_id]
    safe_send_message(chat_id, "🔁 Choose the platform again or go back to main menu.", reply_markup=get_2fa_platform_keyboard())


def process_2fa_secret_input(message):
    chat_id = message.chat.id
    if message.text == "⬅️ Back to Platforms": # Handled by specific handler now, but good to keep as failsafe
        back_to_2fa_platforms(message)
        return
    
    # Ensure user is in a state expecting a secret
    if chat_id not in user_2fa_secrets or "platform" not in user_2fa_secrets[chat_id]:
        # User might have typed text when not in 2FA secret input mode
        # Or state was cleared. Send to main menu.
        # handle_all_text will pick up other messages. If this is hit, it's an unexpected state.
        safe_send_message(chat_id, "Unexpected input. Returning to main menu.", reply_markup=get_main_keyboard(chat_id))
        return

    secret = message.text.strip()
    if not is_valid_base32(secret):
        safe_send_message(chat_id,
                          "❌ *Invalid Secret Key Format*\n\n"
                          "Your secret must be a valid Base32 string (e.g., `JBSWY3DPEHPK3PXP`)\n"
                          "- Usually uppercase letters (A-Z) and digits (2-7).\n"
                          "- No spaces or special characters (hyphens are sometimes shown but should be removed).\n\n"
                          "Please try again, or click '⬅️ Back to Platforms'.",
                          parse_mode="Markdown", reply_markup=get_back_keyboard("2fa_secret_prompt"))
        bot.register_next_step_handler(message, process_2fa_secret_input) # Re-register
        return

    cleaned_secret = secret.replace(" ", "").replace("-", "").upper()
    user_2fa_secrets[chat_id]["secret"] = cleaned_secret # Store the valid, cleaned secret

    platform = user_2fa_secrets[chat_id]["platform"]
    
    reply_text, reply_markup = get_2fa_code_message_content(cleaned_secret)
    safe_send_message(chat_id, f"✅ *{platform}* 2FA Code:", parse_mode="Markdown") # Send title
    safe_send_message(chat_id, reply_text, parse_mode="HTML", reply_markup=reply_markup) # Send code with inline button
    
    # Important: Send main keyboard AFTER the code message, so the inline button isn't immediately replaced
    safe_send_message(chat_id, "You can refresh the code above or return to the main menu.", reply_markup=get_main_keyboard(chat_id))


@bot.callback_query_handler(func=lambda call: call.data == "generate_2fa_code")
def generate_2fa_code_callback(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id) # Acknowledge callback

    if chat_id not in user_2fa_secrets or "secret" not in user_2fa_secrets.get(chat_id, {}):
        try:
            bot.edit_message_text("⚠️ Error: 2FA secret not found. Please start the 2FA process again.",
                                  chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  reply_markup=None) # Remove button
        except Exception as e:
            print(f"Error editing 2FA message on secret not found: {e}")
        return

    secret = user_2fa_secrets[chat_id]["secret"]
    try:
        reply_text, reply_markup = get_2fa_code_message_content(secret)
        bot.edit_message_text(reply_text,
                              chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              parse_mode='HTML',
                              reply_markup=reply_markup)
    except Exception as e:
        print(f"Error generating/editing 2FA code: {e}")
        try:
            bot.edit_message_text("Error generating code. Check secret or try again.",
                                  chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  reply_markup=None) # Remove button on error
        except Exception as e_edit:
             print(f"Error editing 2FA message on generation error: {e_edit}")


# --- Fallback Handler for unexpected text ---
# This should be the LAST message handler
@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_unknown_text(message):
    chat_id = message.chat.id
    # If user is approved or admin, and types something not a command/button
    if chat_id in approved_users or is_admin(chat_id):
        # Check if it's a reply to a bot message asking for input (handled by register_next_step_handler)
        # This handler is a fallback, so if a next_step_handler was supposed to catch it, it would have.
        # So, if we reach here, it's likely an unrecognised command or random text.
        if not any(handler.check(message) for handler in bot.message_handlers if handler.func != handle_unknown_text): # Avoid loops
            safe_send_message(chat_id,
                              f"😕 I didn't understand '{message.text}'. Please use the buttons or commands.",
                              reply_markup=get_main_keyboard(chat_id))
    else:
        # User is not approved and not admin, and not pending.
        # They might have been rejected and are trying to interact.
        if chat_id not in pending_approvals:
            safe_send_message(chat_id, "Your access is not currently approved. If you previously requested access, please wait or contact the admin.")
        else: # Still pending
             safe_send_message(chat_id, "Your access request is still pending. Please wait.")


if __name__ == '__main__':
    print("🤖 Bot is preparing to run...")
    # bot_start_time = datetime.datetime.now() # For uptime calculation if needed

    # Start background threads
    threading.Thread(target=auto_refresh_worker, daemon=True).start()
    print("Auto-refresh worker started.")
    threading.Thread(target=cleanup_blocked_users, daemon=True).start()
    print("Cleanup_blocked_users worker started.")

    print(f"🎉 Bot connected as {bot.get_me().username} and is now polling for messages...")
    bot.infinity_polling(timeout=60, long_polling_timeout=30) # Added timeouts

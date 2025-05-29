# main_bot.py
import os
import time
import requests
import telebot
import random
import string
import threading
import datetime
from faker import Faker
import pyotp
import binascii
import logging

# Custom modules
import config
import database as db
import keyboards as kb

# Initialize Faker
fake = Faker()

# Configure logging
logging.basicConfig(level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Bot
bot = telebot.TeleBot(config.BOT_TOKEN, parse_mode="Markdown")

# --- State Management (Simplified for this example) ---
# For multi-step operations like adding 2FA secret or broadcast message
user_states = {} # chat_id: {"state": "awaiting_2fa_secret", "platform": "Google"}

# --- Helper Functions ---
def is_admin(chat_id):
    return str(chat_id) == config.ADMIN_ID

def generate_random_password(length=12):
    characters = string.ascii_letters + string.digits + string.punctuation
    return ''.join(random.choice(characters) for i in range(length))

def safe_delete_user_session_data(chat_id):
    """ Only deletes session-like data, not the user record itself unless intended """
    if chat_id in user_states:
        del user_states[chat_id]
    db.delete_email_session(chat_id)
    # db.delete_all_2fa_secrets_for_user(chat_id) # Decide if this is desired on block/rejection

def is_bot_blocked(chat_id):
    try:
        bot.get_chat_member(chat_id, chat_id) # Simple check
        return False
    except telebot.apihelper.ApiTelegramException as e:
        if e.error_code == 403: # Forbidden: bot was blocked by the user or kicked
            logger.warning(f"Bot blocked or kicked by user {chat_id}. Cleaning up.")
            db.remove_user_data(chat_id) # Full cleanup for blocked user
            return True
        logger.error(f"API Exception checking chat {chat_id}: {e}")
        return False # Other errors, might not be blocked
    except Exception as e:
        logger.error(f"Unexpected error checking chat {chat_id}: {e}")
        return False # Assume not blocked for other errors

def get_user_info_from_message(message):
    user = message.from_user
    return {
        "name": user.first_name + (f" {user.last_name}" if user.last_name else ""),
        "username": user.username if user.username else "N/A",
        "join_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def safe_send_message(chat_id, text, **kwargs):
    try:
        # if is_bot_blocked(chat_id): # This check can be slow if done for every message
        #     return None
        return bot.send_message(chat_id, text, **kwargs)
    except telebot.apihelper.ApiTelegramException as e:
        if e.error_code == 403: # Bot was blocked or chat not found
            logger.warning(f"Bot blocked by user {chat_id} or chat not found. Cleaning up.")
            db.remove_user_data(chat_id) # Full cleanup for blocked user
        else:
            logger.error(f"Failed to send message to {chat_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"General error sending message to {chat_id}: {e}")
        return None

# --- Mail.tm functions ---
def get_mailtm_domain():
    try:
        res = requests.get("https://api.mail.tm/domains?page=1", timeout=10)
        res.raise_for_status()
        domains = res.json().get("hydra:member", [])
        return domains[0]["domain"] if domains and domains[0]["isActive"] else "mail.tm" # Check if active
    except requests.RequestException as e:
        logger.error(f"Mail.tm get_domain error: {e}")
        return "mail.tm" # Fallback

def generate_mailtm_email_and_pass(domain):
    name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    password = generate_random_password(15) # Generate a random password
    return f"{name}@{domain}", password

def create_mailtm_account(email, password):
    try:
        res = requests.post("https://api.mail.tm/accounts",
                            json={"address": email, "password": password},
                            timeout=10)
        if res.status_code == 201: return "created"
        if res.status_code == 400: # Bad request (e.g. password too short, but we control it)
            logger.error(f"Mail.tm account creation bad request for {email}: {res.text}")
            return "error_bad_request"
        if res.status_code == 422: return "exists" # Unprocessable entity (address already used)
        logger.error(f"Mail.tm account creation error {res.status_code} for {email}: {res.text}")
        return "error"
    except requests.RequestException as e:
        logger.error(f"Mail.tm create_account request exception: {e}")
        return "error"

def get_mailtm_token(email, password):
    time.sleep(1.5) # mail.tm can be slow
    try:
        res = requests.post("https://api.mail.tm/token",
                            json={"address": email, "password": password},
                            timeout=10)
        res.raise_for_status()
        return res.json().get("token")
    except requests.RequestException as e:
        logger.error(f"Mail.tm get_token error for {email}: {e}")
        return None

# --- Profile generator --- (These are fine, no major changes needed for now)
def generate_username():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))

def generate_profile_password():
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
    password = generate_profile_password()
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

# --- 2FA Functions ---
def is_valid_base32(secret):
    try:
        cleaned = secret.replace(" ", "").replace("-", "").upper()
        if not cleaned or not all(c in pyotp.totp.DEFAULT_VALID_CHARS for c in cleaned):
            return False
        pyotp.TOTP(cleaned).now() # Test if it can be initialized
        return True
    except Exception: # Catches binascii.Error, ValueError, etc.
        return False

def generate_2fa_display(secret_key, platform_name, secret_id_for_refresh=None):
    try:
        totp = pyotp.TOTP(secret_key)
        current_code = totp.now()
        now = datetime.datetime.now()
        seconds_remaining = 30 - (now.second % 30)
        # valid_until = now + datetime.timedelta(seconds=seconds_remaining)

        message_text = (
            f"Platform: *{platform_name}*\n"
            f"Code: ` {current_code} `\n"
            f"_Valid for {seconds_remaining} seconds._\n\n"
            f"Tap code to copy."
        )
        reply_markup = kb.get_2fa_code_refresh_keyboard(secret_id_for_refresh) if secret_id_for_refresh else None
        return message_text, reply_markup
    except Exception as e:
        logger.error(f"Error generating 2FA display for {platform_name}: {e}")
        return "❌ Error generating 2FA code. The secret key might be invalid.", None

# --- Background Workers ---
def auto_refresh_mail_worker():
    logger.info("Auto-refresh mail worker started.")
    while True:
        try:
            active_email_users = db.get_active_email_sessions_count() # Just for logging, not iterating yet
            if active_email_users == 0:
                time.sleep(60) # Sleep longer if no active sessions
                continue

            # This needs to fetch all users with active email sessions
            # For now, let's assume we have a way to get chat_ids of users with active mail
            # This part needs redesign if you want specific user refreshes.
            # A simpler approach is not to auto-push but let users refresh.
            # If you stick to auto-refresh, you need to query `email_sessions` table.
            # For now, this worker is less effective without iterating through sessions.

            # Example: Iterate through users who have an email session
            conn = db.get_db_connection()
            sessions = conn.execute("SELECT chat_id, token FROM email_sessions").fetchall()
            conn.close()

            for session in sessions:
                chat_id = session['chat_id']
                token = session['token']

                if not db.is_user_approved(chat_id) and not is_admin(chat_id):
                    db.delete_email_session(chat_id) # Clean up if user got unapproved
                    continue
                if is_bot_blocked(chat_id): # Check if blocked before API call
                    continue

                headers = {"Authorization": f"Bearer {token}"}
                try:
                    res = requests.get("https://api.mail.tm/messages", headers=headers, timeout=15)
                    if res.status_code == 401: # Token expired or invalid
                        logger.warning(f"Mail.tm token expired for user {chat_id}. Deleting session.")
                        db.delete_email_session(chat_id)
                        safe_send_message(chat_id, "⚠️ Your temporary email session has expired. Please create a new one.")
                        continue
                    res.raise_for_status() # For other HTTP errors

                    messages = res.json().get("hydra:member", [])
                    if not messages: continue

                    # Simple way to avoid re-sending: check last 1-2 messages by ID if needed
                    # Or rely on user manually refreshing for now. Auto-pushing can be spammy.
                    # For this example, let's assume manual refresh is primary.
                    # If you want to implement intelligent auto-push, you'll need to store last seen message IDs.

                except requests.RequestException as e:
                    logger.error(f"Mail.tm API error during auto-refresh for {chat_id}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error in mail refresh for {chat_id}: {e}")
            time.sleep(120) # Check every 2 minutes
        except Exception as e:
            logger.error(f"Critical error in auto_refresh_mail_worker: {e}")
            time.sleep(300) # Longer sleep on critical failure


# No need for cleanup_blocked_users separately, integrated into is_bot_blocked and safe_send_message

# --- Bot Handlers ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    chat_id = message.chat.id
    user_info = get_user_info_from_message(message)

    if is_bot_blocked(chat_id): return # Already handled by is_bot_blocked

    existing_user = db.get_user_profile(chat_id)

    if is_admin(chat_id):
        if not existing_user or not existing_user['is_admin']:
            db.add_user_profile(chat_id, user_info['name'], user_info['username'], user_info['join_date'], is_admin_user=True)
        safe_send_message(chat_id, "👋 Welcome Admin!", reply_markup=kb.get_main_keyboard(chat_id))
        return

    if existing_user and existing_user['is_approved']:
        safe_send_message(chat_id, "👋 Welcome back!", reply_markup=kb.get_main_keyboard(chat_id))
    elif existing_user and not existing_user['is_approved']: # User exists but not approved (e.g. rejected before)
        # Check if already in pending_approvals table
        conn = db.get_db_connection()
        is_pending = conn.execute("SELECT 1 FROM pending_approvals WHERE chat_id = ?", (chat_id,)).fetchone()
        conn.close()
        if is_pending:
            safe_send_message(chat_id, "⏳ Your access request is still pending approval.")
        else: # Not pending, treat as new request
            db.add_pending_approval(chat_id, user_info['name'], user_info['username'], user_info['join_date'])
            safe_send_message(chat_id, "👋 Your access request has been re-sent to the admin. Please wait for approval.")
            notify_admin_of_request(chat_id, user_info)
    else: # New user
        db.add_pending_approval(chat_id, user_info['name'], user_info['username'], user_info['join_date'])
        safe_send_message(chat_id, "👋 Your access request has been sent to the admin. Please wait for approval.")
        notify_admin_of_request(chat_id, user_info)

def notify_admin_of_request(user_id, user_info):
    if config.ADMIN_ID:
        approval_msg = (
            f"🆕 *New Approval Request*\n\n"
            f"🆔 User ID: `{user_id}`\n"
            f"👤 Name: `{user_info['name']}`\n"
            f"📛 Username: @{user_info['username']}\n"
            f"📅 Requested: `{user_info['join_date']}`"
        )
        safe_send_message(config.ADMIN_ID_INT, approval_msg, reply_markup=kb.get_approval_keyboard(user_id))


# --- Middleware for access control ---
def access_check(handler_func):
    def wrapper(message_or_call):
        chat_id = message_or_call.chat.id if hasattr(message_or_call, 'chat') else message_or_call.message.chat.id
        
        if is_bot_blocked(chat_id): return

        if not db.is_user_approved(chat_id) and not is_admin(chat_id):
            user_profile = db.get_user_profile(chat_id)
            conn = db.get_db_connection()
            is_pending = conn.execute("SELECT 1 FROM pending_approvals WHERE chat_id = ?", (chat_id,)).fetchone()
            conn.close()

            if user_profile and not user_profile['is_approved'] and not is_pending:
                 # User exists, not approved, and not in pending (e.g. rejected)
                safe_send_message(chat_id, "❌ Your access has not been approved. Contact admin for details or try /start to re-request.")
            elif is_pending:
                safe_send_message(chat_id, "⏳ Your access is pending approval. Please wait.")
            else: # Should ideally not happen if /start logic is correct
                safe_send_message(chat_id, "⚠️ Please use /start to request access.")
            return
        
        # Clear any lingering state if user starts a new command
        if isinstance(message_or_call, telebot.types.Message) and message_or_call.text and not message_or_call.text.startswith('/'): # or not a command
            if user_states.get(chat_id) and message_or_call.text not in ["⬅️ Back to Main", "❌ Cancel"]: # Don't clear if it's part of a flow
                 pass # Let specific handlers manage state clearing or navigation
        
        return handler_func(message_or_call)
    return wrapper

# --- Admin Panel Handlers (all wrapped with access_check and admin check) ---
def admin_only(handler_func):
    def wrapper(message):
        if not is_admin(message.chat.id):
            safe_send_message(message.chat.id, "❌ You are not authorized for this action.")
            return
        if is_bot_blocked(message.chat.id): return
        return handler_func(message)
    return wrapper

@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel")
@admin_only
@access_check
def admin_panel_handler(message):
    safe_send_message(message.chat.id, "👑 Admin Panel", reply_markup=kb.get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "👥 User Approvals")
@admin_only
@access_check
def show_pending_approvals_handler(message):
    pending = db.get_pending_approvals()
    if not pending:
        safe_send_message(message.chat.id, "✅ No pending approvals.")
        return
    safe_send_message(message.chat.id, f"⏳ Found {len(pending)} pending approvals:")
    for p_user in pending:
        user_info = {
            'name': p_user['name'],
            'username': p_user['username'],
            'join_date': p_user['request_date'] # This is request_date
        }
        notify_admin_of_request(p_user['chat_id'], user_info) # Re-uses the notification format

@bot.message_handler(func=lambda msg: msg.text == "📊 Stats")
@admin_only
@access_check
def show_stats_handler(message):
    total_approved = db.get_approved_users_count()
    # active_sessions needs to be defined. If it means users who interacted recently, that's complex.
    # For now, let's use active email sessions as a proxy or remove it.
    active_email_sessions = db.get_active_email_sessions_count()
    pending_approvals_count = len(db.get_pending_approvals())

    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👑 Admin: `{config.ADMIN_ID}`\n"
        f"👥 Approved Users: `{total_approved}`\n"
        # f"📭 Active Sessions: `{len(active_sessions)}`\n" # Define 'active_sessions'
        f"⏳ Pending Approvals: `{pending_approvals_count}`\n"
        f"📧 Active Email Accounts: `{active_email_sessions}`\n"
        f"🕒 Current Time: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
    )
    safe_send_message(message.chat.id, stats_msg)

@bot.message_handler(func=lambda msg: msg.text == "👤 User Management")
@admin_only
@access_check
def user_management_handler(message):
    safe_send_message(message.chat.id, "👤 User Management Panel", reply_markup=kb.get_user_management_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📜 List Users")
@admin_only
@access_check
def list_users_handler(message):
    approved_user_profiles = db.get_all_user_profiles()
    if not approved_user_profiles:
        safe_send_message(message.chat.id, "❌ No approved users yet.")
        return

    users_list_str = []
    for user_profile in approved_user_profiles:
        admin_tag = " (Admin)" if user_profile['is_admin'] else ""
        users_list_str.append(
            f"🆔 `{user_profile['chat_id']}` - 👤 {user_profile['name']} (@{user_profile['username']}){admin_tag} - 📅 {user_profile['join_date']}"
        )

    if not users_list_str:
        safe_send_message(message.chat.id, "❌ No user data available for approved users.")
        return

    response_header = f"👥 *Approved Users ({len(users_list_str)})*:\n\n"
    # Split into chunks if too long for one message
    current_message = response_header
    for user_entry in users_list_str:
        if len(current_message) + len(user_entry) + 2 > 4096: # Telegram limit + newline
            safe_send_message(message.chat.id, current_message)
            current_message = ""
        current_message += user_entry + "\n"
    if current_message: # Send any remaining part
        safe_send_message(message.chat.id, current_message)


@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User")
@admin_only
@access_check
def remove_user_prompt_handler(message):
    user_states[message.chat.id] = {"state": "awaiting_user_removal_id"}
    safe_send_message(message.chat.id, "🆔 Enter the User ID to remove:", reply_markup=kb.get_cancel_keyboard())
    # bot.register_next_step_handler(message, process_user_removal_input) # Replaced by state machine logic

# Callback query handlers for approvals
@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
@admin_only # Ensures only admin can click these buttons
# No @access_check needed here as it's an admin action on admin's own message
def handle_approval_callback(call):
    chat_id_admin = call.message.chat.id
    action, user_id_to_act_on_str = call.data.split('_')
    user_id_to_act_on = int(user_id_to_act_on_str)

    user_profile_to_act = db.get_user_profile(user_id_to_act_on)
    if not user_profile_to_act:
        bot.answer_callback_query(call.id, "User not found in database.")
        bot.edit_message_text("User not found.", chat_id_admin, call.message.message_id, reply_markup=None)
        return

    if action == "approve":
        if db.approve_user(user_id_to_act_on, chat_id_admin):
            safe_send_message(user_id_to_act_on, "✅ Your access has been approved by the admin!", reply_markup=kb.get_main_keyboard(user_id_to_act_on))
            bot.answer_callback_query(call.id, "User approved")
            new_text = call.message.text + f"\n\n✅ Approved user {user_id_to_act_on}."
            bot.edit_message_text(new_text, chat_id_admin, call.message.message_id, reply_markup=None, parse_mode="Markdown")
        else:
            bot.answer_callback_query(call.id, "Failed to approve user.")
            bot.edit_message_text(call.message.text + "\n\n⚠️ Failed to approve.", chat_id_admin, call.message.message_id, reply_markup=None)
    elif action == "reject":
        if db.reject_user(user_id_to_act_on, chat_id_admin):
            safe_send_message(user_id_to_act_on, "❌ Your access request has been rejected by the admin.")
            # db.remove_user_data(user_id_to_act_on) # Optional: fully delete rejected user data
            bot.answer_callback_query(call.id, "User rejected")
            new_text = call.message.text + f"\n\n❌ Rejected user {user_id_to_act_on}."
            bot.edit_message_text(new_text, chat_id_admin, call.message.message_id, reply_markup=None, parse_mode="Markdown")
        else:
            bot.answer_callback_query(call.id, "Failed to reject user.")
            bot.edit_message_text(call.message.text + "\n\n⚠️ Failed to reject.", chat_id_admin, call.message.message_id, reply_markup=None)

# --- Broadcast Handlers --- (Admin only, access checked)
@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast")
@admin_only
@access_check
def broadcast_menu_handler(message):
    safe_send_message(message.chat.id, "📢 Broadcast Message to All Approved Users", reply_markup=kb.get_broadcast_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast")
@admin_only
@access_check
def text_broadcast_prompt_handler(message):
    user_states[message.chat.id] = {"state": "awaiting_text_broadcast"}
    safe_send_message(message.chat.id, "✍️ Enter the broadcast message text (or '❌ Cancel'):", reply_markup=kb.get_cancel_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "🖼️ Media Broadcast")
@admin_only
@access_check
def media_broadcast_prompt_handler(message):
    user_states[message.chat.id] = {"state": "awaiting_media_broadcast"}
    safe_send_message(message.chat.id, "🖼 Send the photo/video/document with caption (or send '❌ Cancel'):", reply_markup=kb.get_cancel_keyboard())


# --- Main Menu Navigation Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "⬅️ Main Menu")
@admin_only # This specific "Main Menu" is from Admin panel
@access_check
def admin_back_to_main_handler(message):
    user_states.pop(message.chat.id, None) # Clear any admin state
    safe_send_message(message.chat.id, "⬅️ Returning to main menu...", reply_markup=kb.get_main_keyboard(message.chat.id))

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Admin")
@admin_only
@access_check
def back_to_admin_panel_handler(message):
    user_states.pop(message.chat.id, None) # Clear any specific admin sub-state
    safe_send_message(message.chat.id, "⬅️ Returning to admin panel...", reply_markup=kb.get_admin_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back to Main") # General users
@access_check
def user_back_to_main_handler(message):
    user_states.pop(message.chat.id, None) # Clear any user state
    safe_send_message(message.chat.id, "⬅️ Returning to main menu...", reply_markup=kb.get_main_keyboard(message.chat.id))


# --- Mail Handlers (Access Checked) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
@access_check
def new_mail_handler(message):
    chat_id = message.chat.id
    loading_msg = safe_send_message(chat_id, "⏳ Generating temporary email...")
    
    domain = get_mailtm_domain()
    email, password = generate_mailtm_email_and_pass(domain)
    status = create_mailtm_account(email, password)

    if status in ["created", "exists"]: # "exists" can happen if random email collides, or if mail.tm reuses. Try to login.
        token = get_mailtm_token(email, password)
        if token:
            db.store_email_session(chat_id, email, password, token) # Store mail.tm password too now
            msg_text = f"✅ *Temporary Email Created!*\n\nEmail: `{email}`\nPass: `{password}`\n\nTap to copy. Messages will appear upon refresh."
            if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
            safe_send_message(chat_id, msg_text)
        else:
            if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
            safe_send_message(chat_id, "❌ Failed to log in to the temporary email. Try again.")
    elif status == "error_bad_request":
        if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
        safe_send_message(chat_id, "❌ Mail.tm reported an issue with the request (e.g. bad domain). Please try again later.")
    else: # error
        if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
        safe_send_message(chat_id, "❌ Could not create temporary email. The mail service might be down or busy. Try again later.")


@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh Inbox")
@access_check
def refresh_mail_handler(message):
    chat_id = message.chat.id
    session = db.get_email_session(chat_id)

    if not session:
        safe_send_message(chat_id, "⚠️ Please create a new email first using '📬 New mail'.")
        return

    loading_msg = safe_send_message(chat_id, "🔄 Fetching emails...")
    token = session['token']
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        res = requests.get("https://api.mail.tm/messages", headers=headers, timeout=15)
        if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)

        if res.status_code == 401: # Token expired
            db.delete_email_session(chat_id)
            safe_send_message(chat_id, "⚠️ Your mail session expired. Please create a new one with '📬 New mail'.")
            return
        res.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

        messages = res.json().get("hydra:member", [])
        if not messages:
            safe_send_message(chat_id, "📭 *Your inbox is empty.*")
            return

        safe_send_message(chat_id, f"📬 *Found {len(messages)} message(s). Showing up to 3 newest:*")
        for msg_summary in messages[:3]: # Show top 3
            msg_id = msg_summary["id"]
            # Fetch full message details
            detail_res = requests.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers, timeout=10)
            if detail_res.status_code == 200:
                msg_detail = detail_res.json()
                sender = msg_detail.get("from", {}).get("address", "N/A")
                subject = msg_detail.get("subject", "(No Subject)")
                intro = msg_detail.get("intro", "(No preview)") # Intro is often enough
                # body = msg_detail.get("text") or msg_detail.get("html") # Prefer text, fallback to HTML
                # For simplicity, using intro. Fetching full body for all can be slow.
                
                formatted_msg = (
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *From:* `{sender}`\n"
                    f"📨 *Subject:* _{subject}_\n"
                    f"📄 *Preview:* {intro}\n"
                    # f"🕒 *Received:* {msg_summary.get('createdAt', 'N/A')}\n" # From summary
                    f"━━━━━━━━━━━━━━━━━━━━"
                )
                safe_send_message(chat_id, formatted_msg)
            else:
                safe_send_message(chat_id, f"⚠️ Error loading details for a message (ID: {msg_id}).")
    except requests.Timeout:
        if loading_msg and not loading_msg._is_coroutine: bot.delete_message(chat_id, loading_msg.message_id)
        safe_send_message(chat_id, "❌ Connection to mail service timed out. Try again later.")
    except requests.RequestException as e:
        if loading_msg and not loading_msg._is_coroutine: bot.delete_message(chat_id, loading_msg.message_id)
        logger.error(f"Mail refresh API error for {chat_id}: {e}")
        safe_send_message(chat_id, f"❌ Error fetching inbox: {e}. Session might be invalid.")
    except Exception as e:
        if loading_msg and not loading_msg._is_coroutine: bot.delete_message(chat_id, loading_msg.message_id)
        logger.error(f"Unexpected error during mail refresh for {chat_id}: {e}")
        safe_send_message(chat_id, "❌ An unexpected error occurred while fetching emails.")


# --- Profile Generation Handlers (Access Checked) ---
@bot.message_handler(func=lambda msg: msg.text in ["👨 Male Profile", "👩 Female Profile"])
@access_check
def generate_profile_handler(message):
    chat_id = message.chat.id
    gender = "male" if message.text == "👨 Male Profile" else "female"
    _gender, name, username, password, phone = generate_profile(gender)
    response_text = profile_message(_gender, name, username, password, phone)
    safe_send_message(chat_id, response_text)

# --- Account Info Handlers ---
@bot.message_handler(func=lambda msg: msg.text == "👤 My Account")
@access_check
def my_account_handler(message):
    safe_send_message(message.chat.id, "Account Information:", reply_markup=kb.get_user_account_keyboard())

@bot.message_handler(func=lambda msg: msg.text == "📧 My Email Info")
@access_check
def my_email_info_handler(message):
    chat_id = message.chat.id
    session = db.get_email_session(chat_id)
    if session:
        text = (f"📧 *Your Current Temporary Email:*\n"
                f"Email: `{session['email']}`\n"
                f"Password: `{session['password']}` (for mail.tm website if needed)\n\n"
                f"Use '🔄 Refresh Inbox' to check for new emails.\n"
                f"Use '📬 New mail' to generate a new address (this one will be replaced).")
    else:
        text = "ℹ️ You don't have an active temporary email. Use '📬 New mail' to create one."
    safe_send_message(chat_id, text)

@bot.message_handler(func=lambda msg: msg.text == "ℹ️ My Profile Info")
@access_check
def my_profile_info_handler(message):
    chat_id = message.chat.id
    user_profile = db.get_user_profile(chat_id)
    if user_profile:
        status = "Approved" if user_profile['is_approved'] else "Pending Approval"
        if is_admin(chat_id): status = "Admin"

        text = (f"👤 *Your Profile Information:*\n"
                f"ID: `{chat_id}`\n"
                f"Name: `{user_profile['name']}`\n"
                f"Username: `@{user_profile['username']}`\n"
                f"Joined Bot: `{user_profile['join_date']}`\n"
                f"Status: `{status}`")
    else:
        text = "⚠️ Could not retrieve your profile information. Try /start ."
    safe_send_message(chat_id, text)

# --- 2FA Handlers (Access Checked) ---
@bot.message_handler(func=lambda msg: msg.text == "🔐 My 2FA Codes")
@access_check
def my_2fa_codes_handler(message):
    chat_id = message.chat.id
    user_secrets_keyboard = kb.get_user_2fa_secrets_keyboard(chat_id)
    
    text = "🔐 *Your Saved 2FA Secrets:*\n\n"
    if user_secrets_keyboard:
        text += "Click a platform below to get the current code."
        safe_send_message(chat_id, text, reply_markup=user_secrets_keyboard)
    else:
        text += "You haven't added any 2FA secrets yet."
        safe_send_message(chat_id, text)
    
    # Also show the main 2FA menu for adding/deleting
    safe_send_message(chat_id, "Manage your 2FA secrets:", reply_markup=kb.get_2fa_main_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "➕ Add New 2FA")
@access_check
def add_new_2fa_handler(message):
    chat_id = message.chat.id
    safe_send_message(chat_id, "✨ Select the platform for the new 2FA secret:",
                      reply_markup=kb.get_2fa_platform_selection_keyboard("2fa_add_platform_"))

@bot.message_handler(func=lambda msg: msg.text == "🗑️ Delete 2FA Secret")
@access_check
def delete_2fa_secret_handler(message):
    chat_id = message.chat.id
    delete_keyboard = kb.get_delete_2fa_secrets_keyboard(chat_id)
    if delete_keyboard:
        safe_send_message(chat_id, "🗑️ Select a 2FA secret to delete:", reply_markup=delete_keyboard)
    else:
        safe_send_message(chat_id, "ℹ️ You have no 2FA secrets saved to delete.", reply_markup=kb.get_2fa_main_keyboard())


# --- Callback Query Handlers for 2FA ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("2fa_add_platform_"))
@access_check # access_check needs to handle call objects too
def cq_2fa_add_platform_selected(call):
    chat_id = call.message.chat.id
    platform = call.data.split("2fa_add_platform_")[1]

    bot.answer_callback_query(call.id)
    bot.edit_message_text(f"Adding 2FA for: *{platform}*", chat_id, call.message.message_id, reply_markup=None)

    if platform == "cancel":
        safe_send_message(chat_id, "Cancelled adding 2FA.", reply_markup=kb.get_main_keyboard(chat_id))
        user_states.pop(chat_id, None)
        return

    user_states[chat_id] = {"state": "awaiting_2fa_secret", "platform": platform}
    safe_send_message(chat_id, f"🔑 Enter the Base32 secret key for *{platform}* (or '❌ Cancel'):",
                      reply_markup=kb.get_cancel_keyboard())


@bot.callback_query_handler(func=lambda call: call.data.startswith("2fa_getcode_"))
@access_check
def cq_2fa_get_code(call):
    chat_id = call.message.chat.id
    secret_id = int(call.data.split("2fa_getcode_")[1])
    bot.answer_callback_query(call.id, "Generating code...")

    secret_info = db.get_2fa_secret_by_id(secret_id)
    if secret_info and secret_info['chat_id'] == chat_id:
        message_text, reply_markup = generate_2fa_display(secret_info['secret'], secret_info['platform_name'], secret_id)
        # Edit the message that contained the buttons, or send a new one
        bot.edit_message_text(message_text, chat_id, call.message.message_id, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        bot.edit_message_text("❌ Error: Could not find this 2FA secret or it does not belong to you.", chat_id, call.message.message_id, reply_markup=None)

@bot.callback_query_handler(func=lambda call: call.data.startswith("2fa_refreshcode_"))
@access_check
def cq_2fa_refresh_code(call):
    chat_id = call.message.chat.id
    secret_id = int(call.data.split("2fa_refreshcode_")[1])
    
    secret_info = db.get_2fa_secret_by_id(secret_id)
    if secret_info and secret_info['chat_id'] == chat_id:
        message_text, reply_markup = generate_2fa_display(secret_info['secret'], secret_info['platform_name'], secret_id)
        try:
            bot.edit_message_text(message_text, chat_id, call.message.message_id, reply_markup=reply_markup, parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Code refreshed!")
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e).lower():
                bot.answer_callback_query(call.id, "Code is the same (not enough time passed).")
            else:
                logger.error(f"Error refreshing 2FA code message: {e}")
                bot.answer_callback_query(call.id, "Error updating display.")
    else:
        bot.answer_callback_query(call.id, "Error: Secret not found.")
        bot.edit_message_text("❌ Error: Could not find this 2FA secret.", chat_id, call.message.message_id, reply_markup=None)


@bot.callback_query_handler(func=lambda call: call.data.startswith("2fa_delete_"))
@access_check
def cq_2fa_delete_secret(call):
    chat_id = call.message.chat.id
    
    if call.data == "2fa_delete_cancel":
        bot.answer_callback_query(call.id, "Deletion cancelled.")
        bot.edit_message_text("Deletion cancelled.", chat_id, call.message.message_id, reply_markup=None)
        # safe_send_message(chat_id, "Manage your 2FA secrets:", reply_markup=kb.get_2fa_main_keyboard())
        return

    secret_id_to_delete = int(call.data.split("2fa_delete_")[1])
    
    # Verify ownership before deleting (db function should do this too)
    secret_info = db.get_2fa_secret_by_id(secret_id_to_delete) # Fetches platform_name for message
    if not secret_info or secret_info['chat_id'] != chat_id:
        bot.answer_callback_query(call.id, "Error: Secret not found or not yours.")
        bot.edit_message_text("❌ Invalid selection for deletion.", chat_id, call.message.message_id, reply_markup=None)
        return

    if db.delete_2fa_secret(secret_id_to_delete, chat_id):
        bot.answer_callback_query(call.id, f"{secret_info['platform_name']} secret deleted!")
        bot.edit_message_text(f"✅ Successfully deleted 2FA secret for *{secret_info['platform_name']}*.",
                              chat_id, call.message.message_id, reply_markup=None, parse_mode="Markdown")
        # Optionally, resend the delete keyboard if there are other secrets
        new_delete_keyboard = kb.get_delete_2fa_secrets_keyboard(chat_id)
        if new_delete_keyboard:
            safe_send_message(chat_id, "Your remaining 2FA secrets:", reply_markup=new_delete_keyboard)
        else:
            safe_send_message(chat_id, "All 2FA secrets have been deleted.", reply_markup=kb.get_2fa_main_keyboard())
    else:
        bot.answer_callback_query(call.id, "Failed to delete secret.")
        bot.edit_message_text("❌ Failed to delete the secret. Please try again.",
                              chat_id, call.message.message_id, reply_markup=None)


# --- General Text Handler for States (Order Matters - Place this near the end) ---
@bot.message_handler(func=lambda message: True, content_types=['text', 'photo', 'video', 'document'])
@access_check # Ensure user is approved before processing states
def handle_stateful_text_and_media(message):
    chat_id = message.chat.id
    current_state_info = user_states.get(chat_id)

    if message.text == "❌ Cancel":
        if current_state_info: # If there was a state
            action = current_state_info.get("state", "operation").replace("awaiting_", "").replace("_", " ")
            safe_send_message(chat_id, f"Cancelled {action}.", reply_markup=kb.get_main_keyboard(chat_id))
        else: # Generic cancel
            safe_send_message(chat_id, "Operation cancelled.", reply_markup=kb.get_main_keyboard(chat_id))
        user_states.pop(chat_id, None)
        return

    if not current_state_info:
        # If no specific state, and not a command button, send a generic help or ignore
        # For now, let's assume it's an unhandled message if it's not a known button/command
        # safe_send_message(chat_id, "🤔 I'm not sure what you mean. Try a command from the menu.", reply_markup=kb.get_main_keyboard(chat_id))
        return

    state = current_state_info["state"]

    # --- State: awaiting_2fa_secret ---
    if state == "awaiting_2fa_secret":
        secret_key = message.text.strip()
        platform = current_state_info["platform"]
        if not is_valid_base32(secret_key):
            safe_send_message(chat_id, "❌ *Invalid Secret Key Format.*\n\nYour secret must be a valid Base32 string (usually uppercase A-Z and digits 2-7). Please try again or '❌ Cancel'.", reply_markup=kb.get_cancel_keyboard())
            return # Keep user in this state

        cleaned_secret = secret_key.replace(" ", "").replace("-", "").upper()
        if db.add_2fa_secret(chat_id, platform, cleaned_secret):
            safe_send_message(chat_id, f"✅ 2FA secret for *{platform}* saved successfully!", reply_markup=kb.get_main_keyboard(chat_id))
            
            # Display the code immediately after adding
            message_text, reply_markup = generate_2fa_display(cleaned_secret, platform) # Don't pass ID here, it's just for display
            if message_text: safe_send_message(chat_id, message_text, reply_markup=None) # No refresh button on immediate display

        else:
            safe_send_message(chat_id, f"❌ Failed to save 2FA secret for *{platform}*. It might already exist or there was a database error.", reply_markup=kb.get_main_keyboard(chat_id))
        user_states.pop(chat_id, None) # Clear state

    # --- State: awaiting_user_removal_id ---
    elif state == "awaiting_user_removal_id":
        if not is_admin(chat_id): return # Should be caught by admin_only on prompt
        try:
            user_id_to_remove = int(message.text.strip())
            if user_id_to_remove == config.ADMIN_ID_INT:
                safe_send_message(chat_id, "❌ Cannot remove the admin account!", reply_markup=kb.get_user_management_keyboard())
            elif db.get_user_profile(user_id_to_remove): # Check if user exists
                db.remove_user_data(user_id_to_remove) # This removes from all tables
                safe_send_message(chat_id, f"✅ User {user_id_to_remove} and all their data has been removed.", reply_markup=kb.get_user_management_keyboard())
                try: # Notify user if possible
                    safe_send_message(user_id_to_remove, "❌ Your access and data have been revoked by an admin.")
                except Exception: pass # User might have blocked the bot
            else:
                safe_send_message(chat_id, f"❌ User {user_id_to_remove} not found in the database.", reply_markup=kb.get_user_management_keyboard())
        except ValueError:
            safe_send_message(chat_id, "❌ Invalid User ID. Please enter a numeric ID or '❌ Cancel'.", reply_markup=kb.get_cancel_keyboard())
            return # Keep state
        user_states.pop(chat_id, None) # Clear state

    # --- State: awaiting_text_broadcast ---
    elif state == "awaiting_text_broadcast":
        if not is_admin(chat_id): return
        broadcast_text = message.text
        user_ids_to_broadcast = db.get_all_approved_users_ids()
        
        if not user_ids_to_broadcast:
            safe_send_message(chat_id, "No approved users to broadcast to.", reply_markup=kb.get_admin_keyboard())
            user_states.pop(chat_id, None)
            return

        total_users = len(user_ids_to_broadcast)
        progress_msg_text = f"📢 Broadcasting text to {total_users} users...\n\nSent: 0/{total_users}"
        progress_message = safe_send_message(chat_id, progress_msg_text)
        
        success_count, fail_count = 0, 0
        for i, user_id_bc in enumerate(user_ids_to_broadcast):
            if user_id_bc == chat_id: continue # Don't send to self (admin)
            try:
                # Adding a header to the broadcast message
                full_broadcast_msg = f"🔔 *Admin Broadcast:*\n\n{broadcast_text}"
                if safe_send_message(user_id_bc, full_broadcast_msg):
                    success_count += 1
                else: # safe_send_message returned None (likely blocked)
                    fail_count += 1
            except Exception as e:
                logger.error(f"Broadcast error to {user_id_bc}: {e}")
                fail_count += 1
            
            if (i + 1) % 10 == 0 or (i + 1) == total_users: # Update every 10 users or at the end
                if progress_message:
                    try:
                        bot.edit_message_text(
                            f"📢 Broadcasting text...\n\nSent: {i+1}/{total_users}\n✅ Successful: {success_count}\n❌ Failed: {fail_count}",
                            chat_id, progress_message.message_id
                        )
                    except Exception: pass # Ignore edit errors (e.g., message not modified)
            time.sleep(0.1) # Small delay to avoid hitting rate limits too hard

        final_status = f"📢 Text broadcast completed!\n\nTotal attempted: {total_users-1 if chat_id in user_ids_to_broadcast else total_users}\n✅ Successful: {success_count}\n❌ Failed: {fail_count}"
        if progress_message: bot.edit_message_text(final_status, chat_id, progress_message.message_id)
        else: safe_send_message(chat_id, final_status)
        safe_send_message(chat_id, "Broadcast finished.", reply_markup=kb.get_admin_keyboard())
        user_states.pop(chat_id, None)

    # --- State: awaiting_media_broadcast ---
    elif state == "awaiting_media_broadcast":
        if not is_admin(chat_id): return
        
        # Check content type
        content_type = message.content_type
        file_id = None
        caption = message.caption or "" # Add "Admin Broadcast:" to caption
        caption_to_send = f"🔔 *Admin Broadcast:*\n\n{caption}".strip()


        if content_type == 'photo':
            file_id = message.photo[-1].file_id
            send_method = bot.send_photo
        elif content_type == 'video':
            file_id = message.video.file_id
            send_method = bot.send_video
        elif content_type == 'document':
            file_id = message.document.file_id
            send_method = bot.send_document
        else:
            safe_send_message(chat_id, "Unsupported media type for broadcast. Please send a photo, video, or document, or '❌ Cancel'.", reply_markup=kb.get_cancel_keyboard())
            return # Keep state

        user_ids_to_broadcast = db.get_all_approved_users_ids()
        if not user_ids_to_broadcast:
            safe_send_message(chat_id, "No approved users to broadcast to.", reply_markup=kb.get_admin_keyboard())
            user_states.pop(chat_id, None)
            return

        total_users = len(user_ids_to_broadcast)
        progress_msg_text = f"📢 Broadcasting media to {total_users} users...\n\nSent: 0/{total_users}"
        progress_message = safe_send_message(chat_id, progress_msg_text)

        success_count, fail_count = 0, 0
        for i, user_id_bc in enumerate(user_ids_to_broadcast):
            if user_id_bc == chat_id: continue # Don't send to self
            try:
                send_method(user_id_bc, file_id, caption=caption_to_send, parse_mode="Markdown")
                success_count += 1
            except telebot.apihelper.ApiTelegramException as e:
                if e.error_code == 403 : # Bot blocked
                     db.remove_user_data(user_id_bc) # remove blocked user
                logger.error(f"Media broadcast error to {user_id_bc}: {e}")
                fail_count += 1
            except Exception as e:
                logger.error(f"Unexpected media broadcast error to {user_id_bc}: {e}")
                fail_count += 1

            if (i + 1) % 5 == 0 or (i + 1) == total_users: # Update every 5 users for media
                if progress_message:
                    try:
                        bot.edit_message_text(
                            f"📢 Broadcasting media...\n\nSent: {i+1}/{total_users}\n✅ Successful: {success_count}\n❌ Failed: {fail_count}",
                            chat_id, progress_message.message_id
                        )
                    except Exception: pass
            time.sleep(0.2) # Slightly longer delay for media

        final_status = f"📢 Media broadcast completed!\n\nTotal attempted: {total_users-1 if chat_id in user_ids_to_broadcast else total_users}\n✅ Successful: {success_count}\n❌ Failed: {fail_count}"
        if progress_message: bot.edit_message_text(final_status, chat_id, progress_message.message_id)
        else: safe_send_message(chat_id, final_status)
        safe_send_message(chat_id, "Media broadcast finished.", reply_markup=kb.get_admin_keyboard())
        user_states.pop(chat_id, None)

    else:
        # Fallback for unhandled states or messages when a state is active but logic is missing
        if current_state_info: # If user is in a state but sent an unhandled message
            safe_send_message(chat_id, "Please follow the instructions or use '❌ Cancel'.", reply_markup=kb.get_cancel_keyboard())
        # else: (already handled by initial check in this function)


if __name__ == '__main__':
    logger.info("Bot starting...")
    db.init_db() # Ensure DB is initialized

    # Start background tasks
    # mail_refresh_thread = threading.Thread(target=auto_refresh_mail_worker, daemon=True)
    # mail_refresh_thread.start()
    # logger.info("Mail refresh worker thread started.")
    # The mail refresh worker needs careful thought about rate limits and usefulness.
    # For now, manual refresh is more robust. Consider removing auto-refresh or making it opt-in.

    logger.info(f"Admin ID: {config.ADMIN_ID}")
    logger.info("Bot is now polling for messages.")
    bot.infinity_polling(logger_level=logging.INFO, timeout=20, long_polling_timeout=30) # Added timeouts

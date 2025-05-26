import os
import time
import requests
import telebot # type: ignore
import random
import string
import threading
import datetime
from faker import Faker # type: ignore
from dotenv import load_dotenv # type: ignore
import pyotp # type: ignore
import binascii
import hashlib
import re

print(f"[{datetime.datetime.now()}] Script starting up...")

try:
    load_dotenv()
    print(f"[{datetime.datetime.now()}] .env file loaded (if present).")
except Exception as e_dotenv:
    print(f"[{datetime.datetime.now()}] Warning: Could not load .env file: {e_dotenv}")


fake = Faker()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    print(f"[{datetime.datetime.now()}] CRITICAL ERROR: BOT_TOKEN not set. Exiting.")
    raise Exception("❌ BOT_TOKEN not set.")
if not ADMIN_ID:
    print(f"[{datetime.datetime.now()}] WARNING: ADMIN_ID not set. Admin features might not work as expected.")

print(f"[{datetime.datetime.now()}] BOT_TOKEN loaded: ...{BOT_TOKEN[-6:] if BOT_TOKEN and len(BOT_TOKEN) > 5 else 'TOKEN_INVALID_OR_SHORT'}")
print(f"[{datetime.datetime.now()}] ADMIN_ID loaded: {ADMIN_ID if ADMIN_ID else 'NOT SET'}")

try:
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
    print(f"[{datetime.datetime.now()}] TeleBot instance created.")
except Exception as e_telebot:
    print(f"[{datetime.datetime.now()}] CRITICAL ERROR: Failed to create TeleBot instance: {e_telebot}. Exiting.")
    raise

# --- API Configuration for temp-mail.org style API and Retry Settings ---
TEMP_MAIL_ORG_API_BASE_URL = "https://api.temp-mail.org/request" # Common base for this type
DEFAULT_FALLBACK_DOMAIN = "kumailone.com" # A common domain often seen with these services
MAX_RETRIES = 3
RETRY_DELAY = 3 
REQUESTS_TIMEOUT = 15

HTTP_HEADERS = { 
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36',
    'Accept': 'application/json', 
}

# Data storage
user_data = {} # For temp-mail.org style: {"email": "user@tempdomain.com"}
last_message_ids = {} 
active_sessions = set()
pending_approvals = {}
approved_users = set()
user_profiles = {}
user_2fa_secrets = {} 

# --- Helper Functions ---
def is_admin(chat_id): 
    if not ADMIN_ID: return False
    return str(chat_id) == str(ADMIN_ID)

def safe_delete_user(chat_id):
    try:
        user_data.pop(chat_id, None)
        last_message_ids.pop(chat_id, None)
        user_2fa_secrets.pop(chat_id, None)
        active_sessions.discard(chat_id)
        pending_approvals.pop(chat_id, None)
        approved_users.discard(chat_id)
        user_profiles.pop(chat_id, None)
    except Exception as e:
        print(f"[{datetime.datetime.now()}] Error in safe_delete_user for {chat_id}: {e}")

def is_bot_blocked(chat_id):
    try: 
        bot.get_chat(chat_id)
        return False
    except telebot.apihelper.ApiTelegramException as e:
        if hasattr(e, 'result_json') and e.result_json and isinstance(e.result_json, dict) and \
           e.result_json.get("error_code") == 403 and "bot was blocked" in e.result_json.get("description", ""):
            return True
        elif hasattr(e, 'result') and hasattr(e.result, 'status_code') and e.result.status_code == 403 and \
             hasattr(e.result, 'text') and "bot was blocked" in e.result.text:
            return True
        return False
    except Exception as e_block_check:
        print(f"[{datetime.datetime.now()}] Error checking if bot is blocked for {chat_id}: {e_block_check}")
        return False

def get_user_info(user):
    return {"name": user.first_name + (f" {user.last_name}" if user.last_name else ""),
            "username": user.username if user.username else "N/A",
            "join_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

# --- Keyboards ---
def get_main_keyboard(chat_id):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(telebot.types.KeyboardButton("📬 New mail"), telebot.types.KeyboardButton("🔄 Refresh")) 
    kb.add(telebot.types.KeyboardButton("👨 Male Profile"), telebot.types.KeyboardButton("👩 Female Profile"))
    kb.add(telebot.types.KeyboardButton("🔐 2FA Auth"), telebot.types.KeyboardButton("👤 My Account"))
    if is_admin(chat_id): kb.add(telebot.types.KeyboardButton("👑 Admin Panel"))
    return kb
def get_admin_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("👥 Pending Approvals", "📊 Stats", "👤 User Management", "📢 Broadcast", "⬅️ Main Menu")
    return kb
def get_user_management_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📜 List Users", "❌ Remove User", "⬅️ Back to Admin")
    return kb
def get_approval_keyboard(user_id):
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
           telebot.types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}"))
    return kb
def get_user_account_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📧 My Email", "🆔 My Info", "⬅️ Back to Main") 
    return kb
def get_2fa_platform_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    kb.add("Google", "Facebook", "Instagram", "Twitter", "Microsoft", "Apple", "⬅️ Back to Main")
    return kb
def get_back_keyboard(target="main"): 
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    if target=="admin_user_management": kb.row("⬅️ Back to User Management")
    elif target=="admin_broadcast": kb.row("⬅️ Back to Broadcast Menu")
    elif target=="2fa_secret_entry": kb.row("⬅️ Back to 2FA Platforms")
    elif target=="generic_back": kb.row("⬅️ Back") 
    else: kb.row("⬅️ Back to Main") 
    return kb
def get_broadcast_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📢 Text Broadcast", "📋 Media Broadcast", "⬅️ Back to Admin")
    return kb

# --- Safe Messaging ---
def safe_send_message(chat_id, text, **kwargs):
    try:
        if is_bot_blocked(chat_id): safe_delete_user(chat_id); return None
        msg = bot.send_message(chat_id, text, **kwargs)
        active_sessions.add(chat_id); return msg
    except telebot.apihelper.ApiTelegramException as e:
        if hasattr(e, 'result_json') and e.result_json and isinstance(e.result_json, dict) and \
           e.result_json.get("error_code")==403 and "bot was blocked" in e.result_json.get("description",""):
            safe_delete_user(chat_id)
        elif hasattr(e, 'result_json'): print(f"[{datetime.datetime.now()}] Msg Err to {chat_id}: API {e.result_json}")
        else: print(f"[{datetime.datetime.now()}] Msg Err to {chat_id}: API {str(e)}")
        return None
    except Exception as e: print(f"[{datetime.datetime.now()}] Generic Msg Err to {chat_id}: {type(e).__name__} - {e}"); return None

# --- temp-mail.org Style API Functions ---
def get_temp_mail_org_domains():
    url = f"{TEMP_MAIL_ORG_API_BASE_URL}/domains/format/json/"
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(url, headers=HTTP_HEADERS, timeout=REQUESTS_TIMEOUT)
            res.raise_for_status(); data = res.json()
            if data and isinstance(data, list) and data: # Expects a list of strings
                # Domains from API often start with '.', remove it
                return [d.lstrip('.') for d in data if isinstance(d, str) and d.startswith('.')]
            return None
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1: time.sleep(RETRY_DELAY * (attempt + 1))
            else: print(f"[{datetime.datetime.now()}] Net err temp-mail.org domains: {e}"); return None
        except ValueError: print(f"[{datetime.datetime.now()}] JSON err temp-mail.org domains"); return None
    return None

def generate_temp_mail_org_address():
    name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    domains = get_temp_mail_org_domains()
    domain_to_use = DEFAULT_FALLBACK_DOMAIN
    if domains and isinstance(domains, list) and domains:
        domain_to_use = random.choice(domains)
    
    email_address = f"{name}@{domain_to_use}"
    # This API style doesn't usually require account creation.
    # The existence of the email is implicit for fetching.
    return "SUCCESS", {"email": email_address}


def fetch_temp_mail_org_messages(email_address):
    if not email_address or '@' not in email_address:
        return "API_ERROR", "Invalid email address format."
    
    email_hash = hashlib.md5(email_address.encode('utf-8')).hexdigest()
    url = f"{TEMP_MAIL_ORG_API_BASE_URL}/mail/id/{email_hash}/format/json/"
    
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(url, headers=HTTP_HEADERS, timeout=REQUESTS_TIMEOUT)
            res.raise_for_status(); data = res.json()
            if isinstance(data, list): # Success usually returns a list of message objects
                return "EMPTY" if not data else "SUCCESS", data
            elif isinstance(data, dict) and "error" in data: # API specific error
                if data["error"] == "no_mail": return "EMPTY", []
                return "API_ERROR", data["error"]
            return "API_ERROR", "Unexp resp temp-mail.org msg list."
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in [500,502,503,504,403] and attempt<MAX_RETRIES-1: time.sleep(RETRY_DELAY*(attempt+1)); continue # Added 403
            return "API_ERROR", f"temp-mail.org HTTP {e.response.status_code} for list."
        except requests.exceptions.RequestException as e:
            if attempt<MAX_RETRIES-1: time.sleep(RETRY_DELAY*(attempt+1))
            else: return "NETWORK_ERROR", f"Net err temp-mail.org list after {MAX_RETRIES} attempts."
        except ValueError: return "JSON_ERROR", "Invalid JSON temp-mail.org msg list."
    return "API_ERROR", "Failed temp-mail.org msg list after retries."

# --- Profile Generator ---
def generate_username_profile(): return ''.join(random.choices(string.ascii_lowercase+string.digits,k=10))
def generate_password_profile(): return ''.join(random.choices(string.ascii_letters+string.digits,k=8)) + datetime.datetime.now().strftime("%d%m")
def generate_us_phone(): return f"1{random.randint(200,999)}{''.join([str(random.randint(0,9)) for _ in range(7)])}"
def generate_profile(gender):
    name = fake.name_male() if gender=="male" else fake.name_female()
    return gender, name, generate_username_profile(), generate_password_profile(), generate_us_phone()
def profile_message(g,n,u,p,ph):
    return (f"🔐*Generated Profile*\n\n{'👨' if g=='male' else '👩'}*Gender:* {g.capitalize()}\n"
            f"🧑‍💼*Name:* `{n}`\n🆔*Username:* `{u}`\n🔑*Password:* `{p}`\n📞*Phone:* `{ph}`\n\n✅Tap to copy")

# --- 2FA ---
def is_valid_base32(s):
    try: c=s.replace(" ","").replace("-","").upper(); assert not any(x not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for x in c) and c; pyotp.TOTP(c+("="*(-len(c)%8))).now(); return True
    except: return False

# --- Email Formatting & Background Workers ---
def format_temp_mail_org_message(msg_detail): 
    sender = msg_detail.get('mail_from', 'N/A') # Key might be 'from' or 'mail_from'
    subject = msg_detail.get('mail_subject', msg_detail.get('subject', '(No Subject)'))
    body_content = msg_detail.get('mail_text', '') 
    if not body_content: body_content = msg_detail.get('mail_html', '') # Fallback to HTML
    if not body_content: body_content = msg_detail.get('html', '') # Another common HTML key
    
    if body_content and ("<" in body_content and ">" in body_content): # Basic check if it's HTML
        body_content = re.sub(r'<style.*?</style>','',body_content,flags=re.DOTALL|re.IGNORECASE)
        body_content = re.sub(r'<script.*?</script>','',body_content,flags=re.DOTALL|re.IGNORECASE)
        body_content = re.sub(r'<br\s*/?>','\n',body_content,flags=re.IGNORECASE)
        body_content = re.sub(r'</p>','\n</p>',body_content,flags=re.IGNORECASE) 
        body_content = re.sub(r'<[^>]+>','',body_content)
        body_content = body_content.replace('&nbsp;',' ').replace('&amp;','&').replace('&lt;','<').replace('&gt;','>')
        body_content = '\n'.join([ln.strip() for ln in body_content.splitlines() if ln.strip()])
    body_content = body_content.strip() if body_content else "(No Content)"
    
    ts_str = msg_detail.get('mail_timestamp', msg_detail.get('date', ''))
    recv_time = "Just now"
    if ts_str:
        try: recv_time = datetime.datetime.fromtimestamp(int(ts_str)).strftime('%Y-%m-%d %H:%M:%S UTC')
        except: # If timestamp is already formatted date string
            recv_time = str(ts_str)

    return (f"━━━━━━━━━\n📬*New Email!*\n━━━━━━━━━\n👤*From:* `{sender}`\n📨*Subject:* _{subject}_\n🕒*Recv:* {recv_time}\n"
            f"━━━━━━━━━\n💬*Body:*\n{body_content[:3500]}\n━━━━━━━━━")

def auto_refresh_worker():
    print(f"[{datetime.datetime.now()}] Auto-refresh worker started.")
    while True:
        try:
            for chat_id in list(user_data.keys()):
                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id); continue
                
                email_info = user_data.get(chat_id)
                if not email_info or "email" not in email_info: continue
                email_address = email_info["email"]
                
                list_status, messages = fetch_temp_mail_org_messages(email_address)

                if list_status not in ["SUCCESS","EMPTY"]: 
                    print(f"[{datetime.datetime.now()}] Auto-refresh: Err temp-mail.org list {chat_id}: {list_status}-{messages}"); continue
                if list_status == "EMPTY" or not messages: continue

                seen_ids = last_message_ids.setdefault(chat_id, set())
                try: messages.sort(key=lambda m: int(m.get('mail_timestamp', 0)), reverse=True) # Sort by timestamp
                except: pass 

                for msg_detail in messages[:5]: 
                    # Unique ID for temp-mail.org messages is often 'mail_id' or part of '_id'
                    msg_id = msg_detail.get('mail_id', msg_detail.get('_id', str(msg_detail)))
                    if isinstance(msg_id, dict): msg_id = msg_id.get('$id', str(msg_detail)) # Handle Mongo-like IDs if present

                    if not msg_id or str(msg_id) in seen_ids: continue # Ensure ID is string for set
                    
                    # With temp-mail.org, the list often contains enough detail, no second fetch needed
                    if safe_send_message(chat_id, format_temp_mail_org_message(msg_detail)): 
                        seen_ids.add(str(msg_id))
                    time.sleep(0.5)
                
                if len(seen_ids)>150: oldest=random.sample(list(seen_ids), len(seen_ids)-75) if len(seen_ids)>75 else []; [seen_ids.discard(oid) for oid in oldest]
        except Exception as e: print(f"[{datetime.datetime.now()}] Error in auto_refresh_worker: {type(e).__name__} - {e}")
        time.sleep(40) # Refresh interval

def cleanup_blocked_users():
    print(f"[{datetime.datetime.now()}] Cleanup_blocked_users worker started.")
    while True:
        try:
            for chat_id in list(active_sessions):
                if is_bot_blocked(chat_id): print(f"[{datetime.datetime.now()}] Cleaning blocked: {chat_id}"); safe_delete_user(chat_id)
        except Exception as e: print(f"[{datetime.datetime.now()}] Err cleanup: {type(e).__name__} - {e}")
        time.sleep(3600) 

# --- Bot Handlers ---
@bot.message_handler(commands=['start','help'])
def send_welcome(m):
    cid=m.chat.id; 
    if is_bot_blocked(cid): safe_delete_user(cid); return
    info=get_user_info(m.from_user); user_profiles[cid]=info
    if is_admin(cid): approved_users.add(cid); safe_send_message(cid,"👋 Admin!",reply_markup=get_main_keyboard(cid)); return
    if cid in approved_users: safe_send_message(cid,"👋 Welcome Back!",reply_markup=get_main_keyboard(cid))
    else:
        if cid not in pending_approvals: pending_approvals[cid]=info; safe_send_message(cid,"👋 Access request sent. Wait for admin approval.")
        else: safe_send_message(cid,"⏳ Access request pending admin approval.")
        if ADMIN_ID:
            try: adm_cid=int(ADMIN_ID); msg_text=(f"🆕*Approval Req*\nID:`{cid}`\nN:`{info['name']}`\nU:`@{info['username']}`\nJ:`{info['join_date']}`")
            except ValueError: print(f"[{datetime.datetime.now()}] ADMIN_ID '{ADMIN_ID}' invalid."); return
            safe_send_message(adm_cid,msg_text,reply_markup=get_approval_keyboard(cid))

@bot.message_handler(func=lambda msg: msg.text == "👑 Admin Panel" and is_admin(msg.chat.id))
def admin_panel(message): safe_send_message(message.chat.id, "👑 Admin Panel", reply_markup=get_admin_keyboard())
@bot.message_handler(func=lambda msg: msg.text == "👥 Pending Approvals" and is_admin(msg.chat.id))
def show_pending_approvals(message):
    if not pending_approvals: safe_send_message(message.chat.id, "✅ No pending approvals."); return
    count = 0
    for user_id, info in list(pending_approvals.items()): 
        count +=1; name, uname, joined = info.get('name',str(user_id)), info.get('username','N/A'), info.get('join_date','N/A')
        text = (f"*Pending {count}*\nID:`{user_id}`\nName:`{name}`\nUser:@{uname}\nJoined:`{joined}`")
        safe_send_message(message.chat.id, text, reply_markup=get_approval_keyboard(user_id)); time.sleep(0.1)
    if count == 0: safe_send_message(message.chat.id, "✅ No pending approvals after iterating.")
@bot.message_handler(func=lambda msg: msg.text == "📊 Stats" and is_admin(msg.chat.id))
def show_stats(message):
    start_time = user_profiles.get("bot_start_time"); up, s_str="N/A","N/A"
    if not start_time: user_profiles["bot_start_time"]=datetime.datetime.now(); start_time = user_profiles["bot_start_time"]
    if start_time:
        s_str=start_time.strftime('%y-%m-%d %H:%M'); delta=datetime.datetime.now()-start_time; d,r=delta.days,delta.seconds; h,r=divmod(r,3600);mn,_=divmod(r,60); up=f"{d}d {h}h {mn}m"
    safe_send_message(message.chat.id,f"📊*Stats*\n👑Adm:`{ADMIN_ID}`\n👥Appr:`{len(approved_users)}`\n👤ActSess:`{len(active_sessions)}`\n⏳Pend:`{len(pending_approvals)}`\n📧EmailsAct:`{len(user_data)}`\n🚀Start:`{s_str}`\n⏱Up:`{up}`")
@bot.message_handler(func=lambda msg: msg.text == "👤 User Management" and is_admin(msg.chat.id))
def user_mgmt(message): safe_send_message(message.chat.id,"👤User Mgmt",reply_markup=get_user_management_keyboard())
@bot.message_handler(func=lambda msg: msg.text == "📜 List Users" and is_admin(msg.chat.id))
def list_users(message):
    if not approved_users: safe_send_message(message.chat.id,"❌No users."); return
    user_list_str = "👥 *Approved Users:*\n"; count = 0
    for uid in list(approved_users):
        if count >= 50: user_list_str += f"...and {len(approved_users)-count} more.\n"; break
        p_info = user_profiles.get(uid, {}); user_list_str += f"- `{uid}`: {p_info.get('name', '?')} (@{p_info.get('username','?')})\n"; count += 1
    if count == 0 : user_list_str += "_None_"
    safe_send_message(message.chat.id, user_list_str)
@bot.message_handler(func=lambda msg: msg.text == "❌ Remove User" and is_admin(msg.chat.id))
def remove_prompt(message): safe_send_message(message.chat.id,"🆔Enter User ID:",reply_markup=get_back_keyboard("admin_user_management")); bot.register_next_step_handler(message,proc_removal)
def proc_removal(m):
    cid=m.chat.id; kbd=get_user_management_keyboard()
    if m.text=="⬅️ Back to User Management": safe_send_message(cid,"Cancelled.",reply_markup=kbd); return
    try: uid_to_remove=int(m.text.strip())
    except ValueError: safe_send_message(cid,"❌Invalid ID.",reply_markup=kbd); return
    if ADMIN_ID and uid_to_remove == int(ADMIN_ID): safe_send_message(cid, "❌ Cannot remove admin!", reply_markup=kbd); return
    was_appr,was_p=uid_to_remove in approved_users,uid_to_remove in pending_approvals; n=user_profiles.get(uid_to_remove,{}).get('name',str(uid_to_remove))
    if was_appr or was_p: safe_delete_user(uid_to_remove); safe_send_message(cid,f"✅User `{n}`({uid_to_remove}) removed.",reply_markup=kbd); safe_send_message(uid_to_remove,"❌Access revoked.") if not is_bot_blocked(uid_to_remove) else None
    else: safe_send_message(cid,f"❌User {uid_to_remove} not found.",reply_markup=kbd)
@bot.message_handler(func=lambda msg: msg.text == "📢 Broadcast" and is_admin(msg.chat.id))
def broadcast_menu(m): safe_send_message(m.chat.id,"📢Choose:",reply_markup=get_broadcast_keyboard())
@bot.message_handler(func=lambda msg: msg.text == "📢 Text Broadcast" and is_admin(msg.chat.id))
def text_bc_prompt(m): safe_send_message(m.chat.id,"✍️Enter msg (/cancel):",reply_markup=get_back_keyboard("admin_broadcast")); bot.register_next_step_handler(m,proc_text_bc)
def proc_text_bc(m):
    cid=m.chat.id; kbd=get_broadcast_keyboard()
    if m.text in ["⬅️ Back to Broadcast Menu","/cancel"]: safe_send_message(cid,"Cancelled.",reply_markup=kbd); return
    if not m.text: safe_send_message(cid,"Empty. Cancelled.",reply_markup=kbd); return
    users_to_send = [u for u in approved_users if ADMIN_ID and u != int(ADMIN_ID)] if ADMIN_ID else list(approved_users)
    s,f,t=0,0,len(users_to_send); adm_kbd=get_admin_keyboard()
    if t==0: safe_send_message(cid,"No users to broadcast to (excl admin).",reply_markup=adm_kbd); return
    pt=lambda i,sc,fl:f"📢Brdcst\nSnt:{i}/{t}\n✅OK:{sc}❌Fail:{fl}"; pm=safe_send_message(cid,pt(0,0,0))
    if not pm: safe_send_message(cid,"Err starting broadcast.",reply_markup=adm_kbd); return
    for i,uid in enumerate(users_to_send):
        if safe_send_message(uid,f"📢*Admin Broadcast:*\n\n{m.text}"): s+=1
        else: f+=1
        if (i+1)%10==0 or (i+1)==t: 
            try: 
                if pm: bot.edit_message_text(pt(i+1,s,f),cid,pm.message_id)
            except Exception as e_edit: pm=None; print(f"[{datetime.datetime.now()}] Err updating broadcast prog: {e_edit}")
        time.sleep(0.2)
    safe_send_message(cid,f"📢Done!\n✅OK:{s}❌Fail:{f}",reply_markup=adm_kbd)
@bot.message_handler(func=lambda msg: msg.text == "📋 Media Broadcast" and is_admin(msg.chat.id))
def media_bc_prompt(m): safe_send_message(m.chat.id,"🖼Send media&caption (/cancel):",reply_markup=get_back_keyboard("admin_broadcast")); bot.register_next_step_handler(m,proc_media_bc)
def proc_media_bc(m):
    cid=m.chat.id; kbd=get_broadcast_keyboard()
    if m.text in ["⬅️ Back to Broadcast Menu","/cancel"]: safe_send_message(cid,"Cancelled.",reply_markup=kbd); return
    if not (m.photo or m.video or m.document): safe_send_message(cid,"No media. Cancelled.",reply_markup=kbd); return
    users_to_send = [u for u in approved_users if ADMIN_ID and u != int(ADMIN_ID)] if ADMIN_ID else list(approved_users)
    s,f,t=0,0,len(users_to_send); adm_kbd=get_admin_keyboard()
    if t==0: safe_send_message(cid,"No users to broadcast to (excl admin).",reply_markup=adm_kbd); return
    pt=lambda i,sc,fl:f"📢Media Brdcst\nSnt:{i}/{t}\n✅OK:{sc}❌Fail:{fl}"; pm=safe_send_message(cid,pt(0,0,0))
    if not pm: safe_send_message(cid,"Err starting media broadcast.",reply_markup=adm_kbd); return
    cap=f"📢*Admin Media Broadcast:*\n\n{m.caption or ''}".strip()
    for i,uid in enumerate(users_to_send):
        try:
            sent=False
            if m.photo: bot.send_photo(uid,m.photo[-1].file_id,caption=cap,parse_mode="Markdown");sent=True
            elif m.video: bot.send_video(uid,m.video.file_id,caption=cap,parse_mode="Markdown");sent=True
            elif m.document: bot.send_document(uid,m.document.file_id,caption=cap,parse_mode="Markdown");sent=True
            if sent: s+=1
            else: f+=1
        except Exception as e_media_send: f+=1; print(f"[{datetime.datetime.now()}] Error sending media to {uid}: {e_media_send}")
        if (i+1)%5==0 or (i+1)==t: 
            try: 
                if pm: bot.edit_message_text(pt(i+1,s,f),cid,pm.message_id)
            except Exception as e_edit_media: pm=None; print(f"[{datetime.datetime.now()}] Err updating media broadcast prog: {e_edit_media}")
        time.sleep(0.3)
    safe_send_message(cid,f"📢Media Done!\n✅OK:{s}❌Fail:{f}",reply_markup=adm_kbd)

@bot.message_handler(func=lambda m: m.text=="⬅️ Back to Admin" and is_admin(m.chat.id))
def back_to_admin(m): safe_send_message(m.chat.id,"⬅️To admin",reply_markup=get_admin_keyboard())
@bot.message_handler(func=lambda m: m.text=="⬅️ Main Menu" and is_admin(m.chat.id))
def admin_back_main(m): safe_send_message(m.chat.id,"⬅️To main",reply_markup=get_main_keyboard(m.chat.id))

@bot.callback_query_handler(func=lambda c: c.data.startswith(('approve_','reject_')))
def handle_approval(c):
    if not is_admin(c.message.chat.id): bot.answer_callback_query(c.id,"❌Not allowed."); return
    try: act,uid_s=c.data.split('_'); uid=int(uid_s)
    except: bot.answer_callback_query(c.id,"Err."); bot.edit_message_text("Err.",c.message.chat.id,c.message.message_id); return
    info=pending_approvals.get(uid, user_profiles.get(uid)); n=info.get('name',str(uid)) if info else str(uid)
    if act=="approve":
        if uid in pending_approvals or uid not in approved_users: 
            approved_users.add(uid)
            if info:
                if uid not in user_profiles: user_profiles[uid] = info 
                else: user_profiles[uid].update(info)
            pending_approvals.pop(uid,None)
            safe_send_message(uid,"✅Access approved!",reply_markup=get_main_keyboard(uid))
            bot.answer_callback_query(c.id,f"User {n} approved.")
            bot.edit_message_text(f"✅User `{n}`({uid}) approved.",c.message.chat.id,c.message.message_id,reply_markup=None)
        else: bot.answer_callback_query(c.id,"Already processed."); bot.edit_message_text(f"⚠️User `{n}`({uid}) already processed.",c.message.chat.id,c.message.message_id,reply_markup=None)
    elif act=="reject": 
        safe_delete_user(uid); safe_send_message(uid,"❌Access rejected.")
        bot.answer_callback_query(c.id,f"User {n} rejected.")
        bot.edit_message_text(f"❌User `{n}`({uid}) rejected.",c.message.chat.id,c.message.message_id,reply_markup=None)

# --- Mail Handlers (temp-mail.org style) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail_temp_mail_org(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    
    user_data.pop(chat_id, None); last_message_ids.pop(chat_id, None)
    gen_msg = safe_send_message(chat_id, "⏳ Generating new email (temp-mail.org style)...")

    status, email_data = generate_temp_mail_org_address() # This returns {"email": ...}

    if status == "SUCCESS" and email_data and "email" in email_data :
        user_data[chat_id] = email_data # Store {"email": "user@domain.com"}
        last_message_ids[chat_id] = set() 
        msg_txt = f"✅ *New Email (temp-mail.org style):*\n`{email_data['email']}`\n\nTap to copy. Use 'Refresh' button."
        if gen_msg: bot.edit_message_text(msg_txt, chat_id, gen_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, msg_txt, parse_mode="Markdown")
    else:
        error_txt = f"❌ Failed to generate email: {email_data}.\nThis often means a network problem from the bot's location trying to reach the email service. Please check your server's internet connection, firewall, DNS, or try again much later."
        if gen_msg: bot.edit_message_text(error_txt, chat_id, gen_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, error_txt, parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh") 
def refresh_mail_temp_mail_org(message): 
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    
    email_info = user_data.get(chat_id)
    if not email_info or "email" not in email_info: 
        safe_send_message(chat_id, "⚠️ No active email. Use '📬 New mail'."); return

    email_address = email_info["email"]
    refresh_msg = safe_send_message(chat_id, f"🔄 Checking inbox for `{email_address}` (temp-mail.org style)...")
    
    def post_status_message(text_content):
        if refresh_msg:
            try: bot.edit_message_text(text_content, chat_id, refresh_msg.message_id, parse_mode="Markdown")
            except: safe_send_message(chat_id, text_content, parse_mode="Markdown")
        else: safe_send_message(chat_id, text_content, parse_mode="Markdown")

    list_status, messages = fetch_temp_mail_org_messages(email_address)

    if list_status == "EMPTY":
        post_status_message(f"📭 Inbox for `{email_address}` is empty.")
        return
    elif list_status != "SUCCESS":
        post_status_message(f"⚠️ Error fetching emails for `{email_address}`: {messages}\nTemp-mail.org style service might be unavailable. Try '📬 New mail' or later.")
        return
    
    if refresh_msg: 
        try: bot.delete_message(chat_id, refresh_msg.message_id)
        except: pass 
    
    seen_ids, new_count = last_message_ids.setdefault(chat_id, set()), 0
    try: messages.sort(key=lambda m: int(m.get('mail_timestamp', 0)), reverse=True)
    except: pass

    for msg_detail in messages[:10]: 
        msg_id_str = str(msg_detail.get('mail_id', msg_detail.get('_id', str(msg_detail))))
        if isinstance(msg_detail.get('_id'), dict): msg_id_str = str(msg_detail['_id'].get('$id', str(msg_detail)))

        if not msg_id_str or msg_id_str in seen_ids: continue
        
        new_count +=1
        if safe_send_message(chat_id, format_temp_mail_org_message(msg_detail)): 
            seen_ids.add(msg_id_str)
        time.sleep(0.5)
            
    if new_count == 0: safe_send_message(chat_id, f"✅ No *new* messages in `{email_address}` since last check.")
    else: safe_send_message(chat_id, f"✨ Found {new_count} new message(s) for `{email_address}`.")

# --- Profile & Account Handlers ---
@bot.message_handler(func=lambda m:m.text in ["👨 Male Profile","👩 Female Profile"])
def gen_profile_h(m):
    cid=m.chat.id; 
    if is_bot_blocked(cid): safe_delete_user(cid); return
    if not (cid in approved_users or is_admin(cid)): safe_send_message(cid,"⏳Access pending."); return
    gen="male" if m.text=="👨 Male Profile" else "female"; g,n,u,p,ph=generate_profile(gen); safe_send_message(cid,profile_message(g,n,u,p,ph))
@bot.message_handler(func=lambda m:m.text=="👤 My Account")
def my_acc_info(m):
    cid=m.chat.id; 
    if is_bot_blocked(cid): return
    if not (cid in approved_users or is_admin(cid)): safe_send_message(cid,"⏳Access pending."); return
    safe_send_message(cid,"👤Account Options:",reply_markup=get_user_account_keyboard())
@bot.message_handler(func=lambda m:m.text=="📧 My Email")
def show_my_email(m):
    cid=m.chat.id;
    if is_bot_blocked(cid): return
    if not (cid in approved_users or is_admin(cid)): safe_send_message(cid,"⏳Access pending."); return
    email=user_data.get(cid,{}).get('email') 
    if email: safe_send_message(cid,f"✉️Current Email:\n`{email}`\nTap to copy.")
    else: safe_send_message(cid,"ℹ️No active email. Use '📬 New mail'.",reply_markup=get_main_keyboard(cid))
@bot.message_handler(func=lambda m:m.text=="🆔 My Info")
def show_my_info(m):
    cid=m.chat.id; 
    if is_bot_blocked(cid): return
    if not (cid in approved_users or is_admin(cid)): safe_send_message(cid,"⏳Access pending."); return
    info=user_profiles.get(cid)
    if info: safe_send_message(cid,f"👤*Info:*\nN:`{info.get('name','?')}`\nU:`@{info.get('username','?')}`\nJ:`{info.get('join_date','?')}`\nID:`{cid}`")
    else: safe_send_message(cid,"Info not found. Try /start.")

# --- 2FA --- 
STATE_WAITING_FOR_2FA_SECRET = "waiting_for_2fa_secret" 
user_states = {} 
@bot.message_handler(func=lambda m:m.text=="🔐 2FA Auth")
def two_fa_start(m):
    cid=m.chat.id; 
    if is_bot_blocked(cid): safe_delete_user(cid); return
    if not (cid in approved_users or is_admin(cid)): safe_send_message(cid,"⏳Access pending."); return
    user_states[cid]={"state":"2fa_platform_select"}; safe_send_message(cid,"🔐Choose platform:",reply_markup=get_2fa_platform_keyboard())
@bot.message_handler(func=lambda m:user_states.get(m.chat.id,{}).get("state")=="2fa_platform_select" and m.text in ["Google","Facebook","Instagram","Twitter","Microsoft","Apple"])
def handle_2fa_plat(m):
    cid,plat=m.chat.id,m.text; s_info=user_2fa_secrets.get(cid,{}).get(plat)
    if s_info and "secret" in s_info:
        try: totp=pyotp.TOTP(s_info["secret"]);c,s=totp.now(),30-(datetime.datetime.now().second%30); safe_send_message(cid,f"🔐*{plat} Code:*\n➡️`{c}`⬅️\n⏳Valid ~*{s}s*.",reply_markup=get_main_keyboard(cid)); time.sleep(0.5); safe_send_message(cid,f"To set new key for {plat}, enter now. Else '⬅️ Back'.",reply_markup=get_back_keyboard("2fa_secret_entry")); user_states[cid]={"state":STATE_WAITING_FOR_2FA_SECRET,"platform":plat}
        except Exception as e: safe_send_message(cid,f"Err {plat} secret:{e}.Re-add.",reply_markup=get_2fa_platform_keyboard()); user_states[cid]={"state":STATE_WAITING_FOR_2FA_SECRET,"platform":plat}; user_2fa_secrets.get(cid,{}).pop(plat,None)
    else: user_states[cid]={"state":STATE_WAITING_FOR_2FA_SECRET,"platform":plat}; safe_send_message(cid,f"🔢Enter Base32 secret for *{plat}*:\nOr '⬅️ Back'.",reply_markup=get_back_keyboard("2fa_secret_entry"))
@bot.message_handler(func=lambda m:m.text=="⬅️ Back to Main")
def back_main_h(m):user_states.pop(m.chat.id,None);safe_send_message(m.chat.id,"⬅️To main",reply_markup=get_main_keyboard(m.chat.id))
@bot.message_handler(func=lambda m:m.text=="⬅️ Back to 2FA Platforms")
def back_2fa_plat(m):user_states[m.chat.id]={"state":"2fa_platform_select"};safe_send_message(m.chat.id,"⬅️Choose platform:",reply_markup=get_2fa_platform_keyboard())
@bot.message_handler(func=lambda m:user_states.get(m.chat.id,{}).get("state")==STATE_WAITING_FOR_2FA_SECRET)
def handle_2fa_secret_in(m):
    cid,s_in=m.chat.id,m.text.strip();plat=user_states.get(cid,{}).get("platform")
    if not plat:safe_send_message(cid,"Error: Platform not set.Start again.",reply_markup=get_main_keyboard(cid));user_states.pop(cid,None);return
    if s_in == "⬅️ Back": user_states.pop(cid, None); safe_send_message(cid, "2FA secret input cancelled.", reply_markup=get_2fa_platform_keyboard()); return
    if not is_valid_base32(s_in):safe_send_message(cid,"❌*Invalid Secret*(A-Z,2-7).\nTry again,'⬅️ Back'.",reply_markup=get_back_keyboard("2fa_secret_entry"));return
    cl,p=s_in.replace(" ","").replace("-","").upper(),"";p="="*(-len(cl)%8);final_s=cl+p
    if cid not in user_2fa_secrets:user_2fa_secrets[cid]={}
    user_2fa_secrets[cid][plat]={"secret":final_s,"added":datetime.datetime.now().isoformat()};user_states.pop(cid,None)
    try:totp,now=pyotp.TOTP(final_s),datetime.datetime.now();c,s=totp.now(),30-(now.second%30);safe_send_message(cid,f"✅*2FA Secret for {plat} Saved!*\n🔑Code:`{c}`\n⏳Valid ~*{s}s*.",reply_markup=get_main_keyboard(cid))
    except Exception as e:user_2fa_secrets.get(cid,{}).pop(plat,None);safe_send_message(cid,f"❌Err with secret for {plat}:{e}.Not saved.",reply_markup=get_2fa_platform_keyboard());user_states[cid]={"state":"2fa_platform_select"}

# --- Fallback Handler ---
@bot.message_handler(func=lambda m:True,content_types=['text'])
def echo_all(m):
    cid=m.chat.id; 
    if is_bot_blocked(cid): safe_delete_user(cid); return
    if not (cid in approved_users or is_admin(cid)): (safe_send_message(cid,"⏳Access pending.") if cid in pending_approvals else send_welcome(m)); return
    st_info=user_states.get(cid,{});st=st_info.get("state")
    backs=["⬅️ Back to 2FA Platforms","⬅️ Back to Main","⬅️ Back to User Management","⬅️ Back to Broadcast Menu","⬅️ Back to Admin", "⬅️ Back"] 
    if st==STATE_WAITING_FOR_2FA_SECRET and m.text not in backs: 
        safe_send_message(cid,f"Waiting for 2FA secret for {st_info.get('platform','platform')} or use 'Back'.",reply_markup=get_back_keyboard("2fa_secret_entry")); return 
    if m.text == "⬅️ Back": 
        user_states.pop(cid,None) 
        safe_send_message(cid,"⬅️ Operation cancelled or going back...", reply_markup=get_main_keyboard(cid)) 
        return
    safe_send_message(cid,f"🤔Unknown:'{m.text}'.Use buttons.",reply_markup=get_main_keyboard(cid))

# --- Main Loop ---
if __name__ == '__main__':
    print(f"[{datetime.datetime.now()}] Main: Initializing bot...")
    user_profiles["bot_start_time"] = datetime.datetime.now() 
    print(f"[{datetime.datetime.now()}] Main: Starting background threads...")
    try:
        threading.Thread(target=auto_refresh_worker, daemon=True, name="AutoRefreshThread").start()
        threading.Thread(target=cleanup_blocked_users, daemon=True, name="CleanupThread").start()
        print(f"[{datetime.datetime.now()}] Main: Background threads initiated.")
    except Exception as e_thread_start:
        print(f"[{datetime.datetime.now()}] CRITICAL ERROR: Failed to start background threads: {e_thread_start}")
    
    print(f"[{datetime.datetime.now()}] Main: Starting polling for bot token: ...{BOT_TOKEN[-6:] if BOT_TOKEN and len(BOT_TOKEN)>5 else 'TOKEN_INVALID_OR_SHORT'}")
    
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30, logger_level=None, none_stop=True)
            print(f"[{datetime.datetime.now()}] Warning: infinity_polling loop has exited. Restarting...")
        except requests.exceptions.ReadTimeout as e_rt:
            print(f"[{datetime.datetime.now()}] Polling ReadTimeout: {e_rt}. Retrying in 15s...")
            time.sleep(15)
        except requests.exceptions.ConnectionError as e_ce:
            print(f"[{datetime.datetime.now()}] Polling ConnectionError: {e_ce}. Retrying in 30s...")
            time.sleep(30)
        except telebot.apihelper.ApiTelegramException as e_api_tg:
            print(f"[{datetime.datetime.now()}] Telegram API Exception in polling: {e_api_tg}. Retrying in 60s...")
            time.sleep(60)
        except Exception as main_loop_e:
            print(f"[{datetime.datetime.now()}] CRITICAL ERROR in main polling loop: {type(main_loop_e).__name__} - {main_loop_e}")
            import traceback
            traceback.print_exc() 
            print(f"[{datetime.datetime.now()}] Retrying polling in 60 seconds...")
            time.sleep(60)
        else: 
            print(f"[{datetime.datetime.now()}] Polling loop exited cleanly (unexpected). Restarting in 10s...")
            time.sleep(10)

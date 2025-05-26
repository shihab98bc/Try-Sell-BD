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

# --- API Configuration for GuerrillaMail and Retry Settings ---
GUERRILLAMAIL_API_URL = "https://api.guerrillamail.com/ajax.php"
MAX_RETRIES = 3
RETRY_DELAY = 3  # seconds, base delay for retries
REQUESTS_TIMEOUT = 15 # General timeout for requests

# Standard User-Agent
HTTP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36'
}

# Data storage
user_data = {} # For GuerrillaMail: {"email_addr": ..., "sid_token": ..., "alias": ..., "email_timestamp": ..., "current_seq_id": 0}
last_message_ids = {} # Stores set of integer mail_id from GuerrillaMail that have been fully processed
active_sessions = set()
pending_approvals = {}
approved_users = set()
user_profiles = {}
user_2fa_secrets = {}

# --- Helper Functions ---
def is_admin(chat_id): return str(chat_id) == ADMIN_ID

def safe_delete_user(chat_id):
    user_data.pop(chat_id, None)
    last_message_ids.pop(chat_id, None)
    user_2fa_secrets.pop(chat_id, None)
    active_sessions.discard(chat_id)
    pending_approvals.pop(chat_id, None)
    approved_users.discard(chat_id)
    user_profiles.pop(chat_id, None)

def is_bot_blocked(chat_id):
    try: bot.get_chat(chat_id); return False
    except telebot.apihelper.ApiTelegramException as e:
        return hasattr(e, 'result_json') and e.result_json.get("error_code") == 403 and \
               "bot was blocked" in e.result_json.get("description", "")
    except Exception: return False

def get_user_info(user):
    return {"name": user.first_name + (f" {user.last_name}" if user.last_name else ""),
            "username": user.username if user.username else "N/A",
            "join_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

# --- Keyboards (No changes needed here) ---
def get_main_keyboard(chat_id):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📬 New mail", "🔄 Refresh Mail", "👨 Male Profile", "👩 Female Profile", "🔐 2FA Auth", "👤 My Account")
    if is_admin(chat_id): kb.add("👑 Admin Panel")
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
    kb.add("📧 My Current Email", "🆔 My Info", "⬅️ Back to Main")
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
        if hasattr(e, 'result_json') and e.result_json.get("error_code")==403 and "bot was blocked" in e.result_json.get("description",""):
            safe_delete_user(chat_id)
        elif hasattr(e, 'result_json'): print(f"Msg Err to {chat_id}: API {e.result_json}")
        else: print(f"Msg Err to {chat_id}: API {str(e)}")
        return None
    except Exception as e: print(f"Generic Msg Err to {chat_id}: {str(e)}"); return None

# --- GuerrillaMail API Functions ---
def generate_guerrillamail_address():
    params = {'f': 'get_email_address', 'lang': 'en'}
    # print(f"DEBUG GM: Requesting new email. URL: {GUERRILLAMAIL_API_URL} PARAMS: {params}")
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(GUERRILLAMAIL_API_URL, params=params, headers=HTTP_HEADERS, timeout=REQUESTS_TIMEOUT)
            res.raise_for_status()
            data = res.json()
            if data and data.get("email_addr") and data.get("sid_token"):
                # print(f"DEBUG GM: Email generated: {data['email_addr']}")
                return "SUCCESS", {
                    "email_addr": data["email_addr"],
                    "sid_token": data["sid_token"],
                    "alias": data.get("alias", data["email_addr"].split('@')[0]),
                    "email_timestamp": data.get("email_timestamp", time.time()),
                    "current_seq_id": 0 # Start with 0 for check_email
                }
            # print(f"DEBUG GM: Failed to parse email from response: {data}")
            err_msg = data.get("error", "Invalid response from email generation service (GuerrillaMail).") if isinstance(data, dict) else "Invalid response."
            return "API_ERROR", err_msg
        except requests.exceptions.HTTPError as e:
            # print(f"DEBUG GM: HTTP error gen email (attempt {attempt+1}): {e.response.status_code} - {e.response.text[:100]}")
            if e.response.status_code in [500, 502, 503, 504] and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1)); continue
            return "API_ERROR", f"GuerrillaMail service returned HTTP {e.response.status_code}."
        except requests.exceptions.RequestException as e:
            # print(f"DEBUG GM: Network error gen email (attempt {attempt+1}): {type(e).__name__} - {e}")
            if attempt < MAX_RETRIES - 1: time.sleep(RETRY_DELAY * (attempt + 1))
            else: return "NETWORK_ERROR", (f"Network error generating email from GuerrillaMail after {MAX_RETRIES} attempts. "
                                           f"Check internet/firewall. Service at {GUERRILLAMAIL_API_URL} might be unreachable.")
        except ValueError: # print(f"DEBUG GM: Invalid JSON gen email."); 
            return "JSON_ERROR", "Invalid JSON response from email generation service (GuerrillaMail)."
        except Exception as e: # print(f"DEBUG GM: Unexpected error gen email: {type(e).__name__} - {e}"); 
            return "API_ERROR", f"Unexpected error generating email (GuerrillaMail): {str(e)}"
    return "API_ERROR", "Failed to generate email from GuerrillaMail after multiple attempts."

def check_guerrillamail_new_emails(sid_token, current_seq_id):
    params = {'f': 'check_email', 'seq': str(current_seq_id), 'sid_token': sid_token}
    # print(f"DEBUG GM: Checking new emails. PARAMS: {params}")
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(GUERRILLAMAIL_API_URL, params=params, headers=HTTP_HEADERS, timeout=REQUESTS_TIMEOUT)
            res.raise_for_status()
            data = res.json()
            if "error" in data: # Specific error from GuerrillaMail
                # print(f"DEBUG GM: API error checking mail: {data['error']}")
                if "sid_token_expired" in data["error"] or "sid_token_invalid" in data["error"]:
                    return "SESSION_EXPIRED", data['error']
                return "API_ERROR", data['error']
            if "list" in data and isinstance(data["list"], list):
                new_seq_id = data.get("seq", current_seq_id) # API returns new highest seq
                # print(f"DEBUG GM: Emails received: {len(data['list'])}, new_seq: {new_seq_id}")
                return "EMPTY" if not data["list"] else "SUCCESS", {"emails": data["list"], "new_seq_id": new_seq_id}
            # print(f"DEBUG GM: Unexpected response checking mail: {data}")
            return "API_ERROR", "Unexpected response format for email list (GuerrillaMail)."
        except requests.exceptions.HTTPError as e:
            # print(f"DEBUG GM: HTTP error checking mail (attempt {attempt+1}): {e.response.status_code} - {e.response.text[:100]}")
            if e.response.status_code in [500, 502, 503, 504] and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1)); continue
            return "API_ERROR", f"GuerrillaMail service returned HTTP {e.response.status_code} for email list."
        except requests.exceptions.RequestException as e:
            # print(f"DEBUG GM: Network error checking mail (attempt {attempt+1}): {type(e).__name__} - {e}")
            if attempt < MAX_RETRIES - 1: time.sleep(RETRY_DELAY * (attempt + 1))
            else: return "NETWORK_ERROR", f"Network error fetching email list from GuerrillaMail after {MAX_RETRIES} attempts."
        except ValueError: # print(f"DEBUG GM: Invalid JSON checking mail."); 
            return "JSON_ERROR", "Invalid JSON response for email list (GuerrillaMail)."
        except Exception as e: # print(f"DEBUG GM: Unexpected error checking mail: {type(e).__name__} - {e}"); 
            return "API_ERROR", f"Unexpected error fetching email list (GuerrillaMail): {str(e)}"
    return "API_ERROR", "Failed to fetch email list from GuerrillaMail after multiple attempts."

def fetch_guerrillamail_email_detail(email_id, sid_token):
    params = {'f': 'fetch_email', 'email_id': str(email_id), 'sid_token': sid_token}
    # print(f"DEBUG GM: Fetching email detail. PARAMS: {params}")
    for attempt in range(MAX_RETRIES):
        try:
            res = requests.get(GUERRILLAMAIL_API_URL, params=params, headers=HTTP_HEADERS, timeout=REQUESTS_TIMEOUT)
            res.raise_for_status()
            data = res.json()
            if "error" in data:
                # print(f"DEBUG GM: API error fetching detail: {data['error']}")
                if "sid_token_expired" in data["error"] or "sid_token_invalid" in data["error"]:
                    return "SESSION_EXPIRED", data['error']
                return "API_ERROR", data['error']
            if isinstance(data, dict) and 'mail_id' in data:
                # print(f"DEBUG GM: Email detail fetched for ID {data['mail_id']}")
                return "SUCCESS", data
            # print(f"DEBUG GM: Unexpected response fetching detail: {data}")
            return "API_ERROR", "Unexpected response format for email detail (GuerrillaMail)."
        except requests.exceptions.HTTPError as e:
            # print(f"DEBUG GM: HTTP error fetching detail (attempt {attempt+1}): {e.response.status_code} - {e.response.text[:100]}")
            if e.response.status_code in [500, 502, 503, 504] and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1)); continue
            return "API_ERROR", f"GuerrillaMail service returned HTTP {e.response.status_code} for email detail."
        except requests.exceptions.RequestException as e:
            # print(f"DEBUG GM: Network error fetching detail (attempt {attempt+1}): {type(e).__name__} - {e}")
            if attempt < MAX_RETRIES - 1: time.sleep(RETRY_DELAY * (attempt + 1))
            else: return "NETWORK_ERROR", f"Network error fetching email detail from GuerrillaMail after {MAX_RETRIES} attempts."
        except ValueError: # print(f"DEBUG GM: Invalid JSON fetching detail."); 
            return "JSON_ERROR", "Invalid JSON response for email detail (GuerrillaMail)."
        except Exception as e: # print(f"DEBUG GM: Unexpected error fetching detail: {type(e).__name__} - {e}"); 
            return "API_ERROR", f"Unexpected error fetching email detail (GuerrillaMail): {str(e)}"
    return "API_ERROR", "Failed to fetch email detail from GuerrillaMail after multiple attempts."

# --- Profile Generator ---
def generate_username(): return ''.join(random.choices(string.ascii_lowercase+string.digits,k=10))
def generate_password(): return ''.join(random.choices(string.ascii_lowercase+string.digits,k=8)) + datetime.datetime.now().strftime("%d")
def generate_us_phone(): return f"1{random.randint(200,999)}{''.join([str(random.randint(0,9)) for _ in range(7)])}"
def generate_profile(gender):
    name = fake.name_male() if gender=="male" else fake.name_female()
    return gender, name, generate_username(), generate_password(), generate_us_phone()
def profile_message(g,n,u,p,ph):
    return (f"🔐*Generated Profile*\n\n{'👨' if g=='male' else '👩'}*Gender:* {g.capitalize()}\n"
            f"🧑‍💼*Name:* `{n}`\n🆔*Username:* `{u}`\n🔑*Password:* `{p}`\n📞*Phone:* `{ph}`\n\n✅Tap to copy")

# --- 2FA ---
def is_valid_base32(s):
    try: c=s.replace(" ","").replace("-","").upper(); assert not any(x not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for x in c) and c; pyotp.TOTP(c+("="*(-len(c)%8))).now(); return True
    except: return False

# --- Email Formatting & Background Workers ---
def format_guerrillamail_message(msg_detail):
    sender = msg_detail.get('mail_from', 'N/A')
    subject = msg_detail.get('mail_subject', '(No Subject)')
    body_content = msg_detail.get('mail_body', '') # mail_body is HTML
    if body_content: # Basic HTML stripping
        body_content = re.sub(r'<style[^>]*?>.*?</style>','',body_content,flags=re.DOTALL|re.IGNORECASE)
        body_content = re.sub(r'<script[^>]*?>.*?</script>','',body_content,flags=re.DOTALL|re.IGNORECASE)
        body_content = re.sub(r'<br\s*/?>','\n',body_content,flags=re.IGNORECASE)
        body_content = re.sub(r'</p>','\n</p>',body_content,flags=re.IGNORECASE) 
        body_content = re.sub(r'<[^>]+>','',body_content)
        body_content = body_content.replace('&nbsp;',' ').replace('&amp;','&').replace('&lt;','<').replace('&gt;','>')
        body_content = '\n'.join([ln.strip() for ln in body_content.splitlines() if ln.strip()])
    body_content = body_content.strip() if body_content else "(No Content)"
    ts = msg_detail.get('mail_timestamp', time.time())
    recv_time = datetime.datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M:%S UTC') if isinstance(ts, (int, float, str)) and str(ts).isdigit() else "Unknown"

    return (f"━━━━━━━━━━━━━━━━━━━━\n📬*New Email!*\n━━━━━━━━━━━━━━━━━━━━\n"
            f"👤*From:* `{sender}`\n📨*Subject:* _{subject}_\n🕒*Received:* {recv_time}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n💬*Body:*\n{body_content[:3500]}\n━━━━━━━━━━━━━━━━━━━━")

def auto_refresh_worker():
    while True:
        try:
            for chat_id in list(user_data.keys()):
                if is_bot_blocked(chat_id) or (chat_id not in approved_users and not is_admin(chat_id)):
                    safe_delete_user(chat_id); continue
                
                session_info = user_data.get(chat_id)
                if not session_info or "sid_token" not in session_info: continue

                sid_token = session_info["sid_token"]
                current_seq_id = session_info.get("current_seq_id", 0)
                
                list_status, mail_data = check_guerrillamail_new_emails(sid_token, current_seq_id)

                if list_status == "SESSION_EXPIRED":
                    print(f"Auto-refresh: Session expired for {chat_id}. Clearing email data.")
                    user_data.pop(chat_id, None); last_message_ids.pop(chat_id, None)
                    safe_send_message(chat_id, "⏳ Your email session with GuerrillaMail has expired. Please use '📬 New mail' to get a new one.")
                    continue
                if list_status not in ["SUCCESS", "EMPTY"]:
                    print(f"Auto-refresh: Err chk mail {chat_id}: {list_status}-{mail_data}"); continue
                if list_status == "EMPTY" or not mail_data or not mail_data.get("emails"): continue

                new_emails = mail_data["emails"]
                new_highest_seq_id = mail_data.get("new_seq_id", current_seq_id)
                
                seen_ids = last_message_ids.setdefault(chat_id, set())
                new_emails.sort(key=lambda m: m.get('mail_id', 0)) # Process oldest new first

                for msg_summary in new_emails:
                    msg_id = msg_summary.get('mail_id')
                    if not msg_id or int(msg_id) in seen_ids: continue # Guerrilla mail_id is int
                    
                    detail_status, detail_data = fetch_guerrillamail_email_detail(msg_id, sid_token)
                    if detail_status == "SUCCESS":
                        if safe_send_message(chat_id, format_guerrillamail_message(detail_data)):
                            seen_ids.add(int(msg_id))
                        time.sleep(0.7)
                    elif detail_status == "SESSION_EXPIRED":
                        print(f"Auto-refresh: Session expired for {chat_id} while fetching detail. Clearing data."); 
                        user_data.pop(chat_id,None); last_message_ids.pop(chat_id,None)
                        safe_send_message(chat_id, "⏳ Email session expired. Use '📬 New mail'."); break # Break from processing this user's emails
                    else: print(f"Auto-refresh: Err detail msg {msg_id} ({chat_id}): {detail_status}-{detail_data}")
                
                user_data[chat_id]["current_seq_id"] = new_highest_seq_id # Update seq_id
                if len(seen_ids) > 150: # Prune seen_ids
                    sorted_seen = sorted(list(seen_ids)); oldest = sorted_seen[:-75]
                    for old_id in oldest: seen_ids.discard(old_id)
        except Exception as e: print(f"Error in auto_refresh_worker: {type(e).__name__} - {e}")
        time.sleep(45) # Check interval

def cleanup_blocked_users():
    while True:
        try:
            for chat_id in list(active_sessions):
                if is_bot_blocked(chat_id): print(f"Cleaning blocked: {chat_id}"); safe_delete_user(chat_id)
        except Exception as e: print(f"Err cleanup: {e}")
        time.sleep(3600) 

# --- Bot Handlers (Welcome, Admin, Mail, Profile, Account, 2FA - largely unchanged except mail) ---
@bot.message_handler(commands=['start','help'])
def send_welcome(m):
    cid=m.chat.id; safe_delete_user(cid) if is_bot_blocked(cid) else None; info=get_user_info(m.from_user); user_profiles[cid]=info
    if is_admin(cid): approved_users.add(cid); safe_send_message(cid,"👋Admin!",reply_markup=get_main_keyboard(cid)); return
    if cid in approved_users: safe_send_message(cid,"👋Back!",reply_markup=get_main_keyboard(cid))
    else:
        if cid not in pending_approvals: pending_approvals[cid]=info; safe_send_message(cid,"👋Access request sent.")
        else: safe_send_message(cid,"⏳Request pending.")
        if ADMIN_ID:
            try: adm_cid=int(ADMIN_ID); msg=(f"🆕*Approval Req*\nID:`{cid}`\nN:`{info['name']}`\nU:`@{info['username']}`\nJ:`{info['join_date']}`")
            except: print("ADMIN_ID invalid"); return
            safe_send_message(adm_cid,msg,reply_markup=get_approval_keyboard(cid))

@bot.message_handler(func=lambda m: m.text=="👑 Admin Panel" and is_admin(m.chat.id))
def admin_panel(m): safe_send_message(m.chat.id,"👑Admin Panel",reply_markup=get_admin_keyboard())
@bot.message_handler(func=lambda m: m.text=="👥 Pending Approvals" and is_admin(m.chat.id))
def show_pending(m):
    if not pending_approvals: safe_send_message(m.chat.id,"✅No pending."); return
    c=0; text=""
    for uid,info in list(pending_approvals.items()):
        c+=1; n,un,j=info.get('name',str(uid)),info.get('username','N/A'),info.get('join_date','N/A')
        item=f"*Req {c}*\nID:`{uid}`\nN:`{n}`\nU:@{un}\nJ:`{j}`"
        safe_send_message(m.chat.id,item,reply_markup=get_approval_keyboard(uid)); time.sleep(0.1)
    if c==0: safe_send_message(m.chat.id,"✅No pending iter.")
@bot.message_handler(func=lambda m: m.text=="📊 Stats" and is_admin(m.chat.id))
def show_stats(m):
    st=user_profiles.get("bot_start_time"); up, s_str="N/A","N/A"
    if not st: user_profiles["bot_start_time"]=datetime.datetime.now(); st=user_profiles["bot_start_time"]
    if st: s_str=st.strftime('%y-%m-%d %H:%M'); dlt=datetime.datetime.now()-st; d,r=dlt.days,dlt.seconds; h,r=divmod(r,3600);mn,_=divmod(r,60); up=f"{d}d {h}h {mn}m"
    safe_send_message(m.chat.id,f"📊*Stats*\n👑Adm:`{ADMIN_ID}`\n👥Appr:`{len(approved_users)}`\n👤ActSess:`{len(active_sessions)}`\n⏳Pend:`{len(pending_approvals)}`\n📧EmailsAct:`{len(user_data)}`\n🚀Start:`{s_str}`\n⏱Up:`{up}`")
@bot.message_handler(func=lambda m: m.text=="👤 User Management" and is_admin(m.chat.id))
def user_mgmt(m): safe_send_message(m.chat.id,"👤User Mgmt",reply_markup=get_user_management_keyboard())
@bot.message_handler(func=lambda m: m.text=="📜 List Users" and is_admin(m.chat.id))
def list_users(m):
    if not approved_users: safe_send_message(m.chat.id,"❌No users."); return
    parts,cur=[], "👥*Users*\n\n"
    for uid in approved_users: p=user_profiles.get(uid,{}); info=f"🆔`{uid}`-👤{p.get('name','?')}(@{p.get('username','?')})-📅{p.get('join_date','?')}\n";parts.append(cur) if len(cur)+len(info)>4k else (cur:="👥*(cont.)*\n\n"+info); cur+=info
    if cur.strip() not in ["👥*Users*\n\n".strip(),"👥*(cont.)*\n\n".strip()]: parts.append(cur)
    if not parts: safe_send_message(m.chat.id,"❌No data.")
    else: 
        for p_msg in parts: safe_send_message(m.chat.id,p_msg); time.sleep(0.2)
@bot.message_handler(func=lambda m: m.text=="❌ Remove User" and is_admin(m.chat.id))
def remove_prompt(m): safe_send_message(m.chat.id,"🆔Enter User ID:",reply_markup=get_back_keyboard("admin_user_management")); bot.register_next_step_handler(m,proc_removal)
def proc_removal(m):
    cid=m.chat.id; kbd=get_user_management_keyboard()
    if m.text=="⬅️ Back to User Management": safe_send_message(cid,"Cancelled.",reply_markup=kbd); return
    try: uid=int(m.text.strip()); assert uid!=int(ADMIN_ID)
    except: safe_send_message(cid,"❌Invalid ID/Can't remove admin.",reply_markup=kbd); return
    was_a,was_p=uid in approved_users,uid in pending_approvals; n=user_profiles.get(uid,{}).get('name',str(uid))
    if was_a or was_p: safe_delete_user(uid); safe_send_message(cid,f"✅User `{n}`({uid}) removed.",reply_markup=kbd); safe_send_message(uid,"❌Access revoked.") if not is_bot_blocked(uid) else None
    else: safe_send_message(cid,f"❌User {uid} not found.",reply_markup=kbd)
@bot.message_handler(func=lambda m: m.text=="📢 Broadcast" and is_admin(m.chat.id))
def broadcast_menu(m): safe_send_message(m.chat.id,"📢Choose:",reply_markup=get_broadcast_keyboard())
@bot.message_handler(func=lambda m: m.text=="📢 Text Broadcast" and is_admin(m.chat.id))
def text_bc_prompt(m): safe_send_message(m.chat.id,"✍️Enter msg (/cancel):",reply_markup=get_back_keyboard("admin_broadcast")); bot.register_next_step_handler(m,proc_text_bc)
def proc_text_bc(m):
    cid=m.chat.id; kbd=get_broadcast_keyboard()
    if m.text in ["⬅️ Back to Broadcast Menu","/cancel"]: safe_send_message(cid,"Cancelled.",reply_markup=kbd); return
    if not m.text: safe_send_message(cid,"Empty. Cancelled.",reply_markup=kbd); return
    usrs,s,f,t=list(approved_users),0,0,len(list(approved_users)); adm_kbd=get_admin_keyboard()
    if t==0: safe_send_message(cid,"No users.",reply_markup=adm_kbd); return
    pt=lambda i,sc,fl:f"📢Brdcst\nSnt:{i}/{t}\n✅OK:{sc}❌Fail:{fl}"; pm=safe_send_message(cid,pt(0,0,0))
    if not pm: safe_send_message(cid,"Err start.",reply_markup=adm_kbd); return
    for i,uid in enumerate(usrs): (s:=s+1) if safe_send_message(uid,f"📢*Admin Brdcst:*\n\n{m.text}") else (f:=f+1); time.sleep(0.2)
    if (i+1)%10==0 or (i+1)==t: 
        try: bot.edit_message_text(pt(i+1,s,f),cid,pm.message_id) if pm else None
        except: pm=None
    safe_send_message(cid,f"📢Done!\n✅OK:{s}❌Fail:{f}",reply_markup=adm_kbd)
@bot.message_handler(func=lambda m: m.text=="📋 Media Broadcast" and is_admin(m.chat.id))
def media_bc_prompt(m): safe_send_message(m.chat.id,"🖼Send media&caption (/cancel):",reply_markup=get_back_keyboard("admin_broadcast")); bot.register_next_step_handler(m,proc_media_bc)
def proc_media_bc(m):
    cid=m.chat.id; kbd=get_broadcast_keyboard()
    if m.text in ["⬅️ Back to Broadcast Menu","/cancel"]: safe_send_message(cid,"Cancelled.",reply_markup=kbd); return
    if not (m.photo or m.video or m.document): safe_send_message(cid,"No media. Cancelled.",reply_markup=kbd); return
    usrs,s,f,t=list(approved_users),0,0,len(list(approved_users)); adm_kbd=get_admin_keyboard()
    if t==0: safe_send_message(cid,"No users.",reply_markup=adm_kbd); return
    pt=lambda i,sc,fl:f"📢Media Brdcst\nSnt:{i}/{t}\n✅OK:{sc}❌Fail:{fl}"; pm=safe_send_message(cid,pt(0,0,0))
    if not pm: safe_send_message(cid,"Err start.",reply_markup=adm_kbd); return
    cap=f"📢*Admin Media Brdcst:*\n\n{m.caption or ''}".strip()
    for i,uid in enumerate(usrs):
        try:
            sent=False
            if m.photo: bot.send_photo(uid,m.photo[-1].file_id,caption=cap,parse_mode="Markdown");sent=True
            elif m.video: bot.send_video(uid,m.video.file_id,caption=cap,parse_mode="Markdown");sent=True
            elif m.document: bot.send_document(uid,m.document.file_id,caption=cap,parse_mode="Markdown");sent=True
            (s:=s+1) if sent else (f:=f+1)
        except: f:=f+1
        if (i+1)%5==0 or (i+1)==t: 
            try: bot.edit_message_text(pt(i+1,s,f),cid,pm.message_id) if pm else None
            except: pm=None
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
    info=pending_approvals.get(uid,user_profiles.get(uid)); n=info.get('name',str(uid)) if info else str(uid)
    if act=="approve":
        if uid in pending_approvals or uid not in approved_users: approved_users.add(uid); (user_profiles[uid]:=info) if uid not in user_profiles and info else None; pending_approvals.pop(uid,None); safe_send_message(uid,"✅Access appr!",reply_markup=get_main_keyboard(uid)); bot.answer_callback_query(c.id,f"User {n} appr."); bot.edit_message_text(f"✅Usr `{n}`({uid}) appr.",c.message.chat.id,c.message.message_id,reply_markup=None)
        else: bot.answer_callback_query(c.id,"Processed."); bot.edit_message_text(f"⚠️Usr `{n}`({uid}) processed.",c.message.chat.id,c.message.message_id,reply_markup=None)
    elif act=="reject": safe_delete_user(uid); safe_send_message(uid,"❌Access rej."); bot.answer_callback_query(c.id,f"User {n} rej."); bot.edit_message_text(f"❌Usr `{n}`({uid}) rej.",c.message.chat.id,c.message.message_id,reply_markup=None)

# --- Mail Handlers (GuerrillaMail) ---
@bot.message_handler(func=lambda msg: msg.text == "📬 New mail")
def new_mail_guerrillamail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    user_data.pop(chat_id, None); last_message_ids.pop(chat_id, None)
    gen_msg = safe_send_message(chat_id, "⏳ Generating new email (GuerrillaMail)...")
    status, email_data = generate_guerrillamail_address()

    if status == "SUCCESS" and email_data:
        user_data[chat_id] = email_data # Stores full dict from generate_guerrillamail_address
        last_message_ids[chat_id] = set() 
        msg_txt = f"✅ *New Email (GuerrillaMail):*\n`{email_data['email_addr']}`\n\nTap to copy. Use 'Refresh Mail'."
        if gen_msg: bot.edit_message_text(msg_txt, chat_id, gen_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, msg_txt)
    else:
        error_txt = f"❌ Failed to generate email: {email_data}.\nThis often indicates a network problem from the bot's location or the email service is unavailable. Check your server's internet connection, firewall, DNS, or try again much later."
        if gen_msg: bot.edit_message_text(error_txt, chat_id, gen_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, error_txt)

@bot.message_handler(func=lambda msg: msg.text == "🔄 Refresh Mail")
def refresh_mail_guerrillamail(message):
    chat_id = message.chat.id
    if is_bot_blocked(chat_id): safe_delete_user(chat_id); return
    if not (chat_id in approved_users or is_admin(chat_id)): safe_send_message(chat_id, "⏳ Access pending."); return
    
    session_info = user_data.get(chat_id)
    if not session_info or "sid_token" not in session_info:
        safe_send_message(chat_id, "⚠️ No active GuerrillaMail session. Use '📬 New mail'."); return

    email_addr = session_info["email_addr"]
    sid_token = session_info["sid_token"]
    current_seq_id = session_info.get("current_seq_id", 0)

    refresh_msg = safe_send_message(chat_id, f"🔄 Checking inbox for `{email_addr}` (GuerrillaMail)...")
    
    list_status, mail_data = check_guerrillamail_new_emails(sid_token, current_seq_id)

    if list_status == "SESSION_EXPIRED":
        user_data.pop(chat_id, None); last_message_ids.pop(chat_id, None)
        err_txt = f"⏳ Email session for `{email_addr}` expired. Please use '📬 New mail' for a new one."
        if refresh_msg: bot.edit_message_text(err_txt, chat_id, refresh_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, err_txt); return
    elif list_status == "EMPTY":
        txt = f"📭 Inbox for `{email_addr}` is empty or no new messages."
        if refresh_msg: bot.edit_message_text(txt, chat_id, refresh_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, txt)
        return
    elif list_status != "SUCCESS":
        err_txt = f"⚠️ Error fetching emails for `{email_addr}`: {mail_data}\nGuerrillaMail service might be unavailable. Try later or '📬 New mail'."
        if refresh_msg: bot.edit_message_text(err_txt, chat_id, refresh_msg.message_id, parse_mode="Markdown")
        else: safe_send_message(chat_id, err_txt)
        return
    
    if refresh_msg: 
        try: bot.delete_message(chat_id, refresh_msg.message_id)
        except: pass 

    new_emails = mail_data.get("emails", [])
    new_highest_seq_id = mail_data.get("new_seq_id", current_seq_id)
    
    seen_ids = last_message_ids.setdefault(chat_id, set())
    new_messages_count = 0
    
    new_emails.sort(key=lambda m: m.get('mail_id', 0)) # Process oldest new first

    for msg_summary in new_emails: 
        msg_id = msg_summary.get('mail_id')
        if not msg_id or int(msg_id) in seen_ids: continue
        
        detail_status, detail_data = fetch_guerrillamail_email_detail(msg_id, sid_token)
        if detail_status == "SUCCESS":
            new_messages_count +=1
            if safe_send_message(chat_id, format_guerrillamail_message(detail_data)):
                seen_ids.add(int(msg_id))
            time.sleep(0.5)
        elif detail_status == "SESSION_EXPIRED":
            user_data.pop(chat_id,None); last_message_ids.pop(chat_id,None)
            safe_send_message(chat_id, "⏳ Email session expired while fetching details. Use '📬 New mail'."); break
        else: safe_send_message(chat_id, f"⚠️ Error fetching detail for msg ID {msg_id}: {detail_data}")
    
    user_data[chat_id]["current_seq_id"] = new_highest_seq_id # Update for next poll
    if new_messages_count == 0: safe_send_message(chat_id, f"✅ No *new* messages in `{email_addr}` since last check.")
    else: safe_send_message(chat_id, f"✨ Found {new_messages_count} new message(s) for `{email_addr}`.")

# --- Profile & Account ---
@bot.message_handler(func=lambda m:m.text in ["👨 Male Profile","👩 Female Profile"])
def gen_profile_h(m):
    cid=m.chat.id; safe_delete_user(cid) if is_bot_blocked(cid) else None
    if not (cid in approved_users or is_admin(cid)): safe_send_message(cid,"⏳Access pending."); return
    gen="male" if m.text=="👨 Male Profile" else "female"; g,n,u,p,ph=generate_profile(gen); safe_send_message(cid,profile_message(g,n,u,p,ph))
@bot.message_handler(func=lambda m:m.text=="👤 My Account")
def my_acc_info(m):
    cid=m.chat.id; safe_delete_user(cid) if is_bot_blocked(cid) else None
    if not (cid in approved_users or is_admin(cid)): safe_send_message(cid,"⏳Access pending."); return
    safe_send_message(cid,"👤Account Options:",reply_markup=get_user_account_keyboard())
@bot.message_handler(func=lambda m:m.text=="📧 My Current Email")
def show_my_email(m):
    cid=m.chat.id; safe_delete_user(cid) if is_bot_blocked(cid) else None
    if not (cid in approved_users or is_admin(cid)): safe_send_message(cid,"⏳Access pending."); return
    email=user_data.get(cid,{}).get('email_addr')
    if email: safe_send_message(cid,f"✉️Current GuerrillaMail:\n`{email}`\nTap to copy.")
    else: safe_send_message(cid,"ℹ️No active email. Use '📬 New mail'.",reply_markup=get_main_keyboard(cid))
@bot.message_handler(func=lambda m:m.text=="🆔 My Info")
def show_my_info(m):
    cid=m.chat.id; safe_delete_user(cid) if is_bot_blocked(cid) else None
    if not (cid in approved_users or is_admin(cid)): safe_send_message(cid,"⏳Access pending."); return
    info=user_profiles.get(cid)
    if info: safe_send_message(cid,f"👤*Info:*\nN:`{info.get('name','?')}`\nU:`@{info.get('username','?')}`\nJ:`{info.get('join_date','?')}`\nID:`{cid}`")
    else: safe_send_message(cid,"Info not found. Try /start.")

# --- 2FA ---
STATE_WAITING_FOR_2FA_SECRET = "waiting_for_2fa_secret" 
user_states = {} 
@bot.message_handler(func=lambda m:m.text=="🔐 2FA Auth")
def two_fa_start(m):
    cid=m.chat.id; safe_delete_user(cid) if is_bot_blocked(cid) else None
    if not (cid in approved_users or is_admin(cid)): safe_send_message(cid,"⏳Access pending."); return
    user_states[cid]={"state":"2fa_platform_select"}; safe_send_message(cid,"🔐Choose platform for 2FA:",reply_markup=get_2fa_platform_keyboard())
@bot.message_handler(func=lambda m:user_states.get(m.chat.id,{}).get("state")=="2fa_platform_select" and m.text in ["Google","Facebook","Instagram","Twitter","Microsoft","Apple"])
def handle_2fa_plat(m):
    cid,plat=m.chat.id,m.text; s_info=user_2fa_secrets.get(cid,{}).get(plat)
    if s_info and "secret" in s_info:
        try: totp=pyotp.TOTP(s_info["secret"]);c,s=totp.now(),30-(datetime.datetime.now().second%30); safe_send_message(cid,f"🔐*{plat} 2FA Code:*\n➡️`{c}`⬅️\n⏳Valid ~*{s}s*.\n\nTo update, enter new key.",reply_markup=get_main_keyboard(cid)); time.sleep(0.5); safe_send_message(cid,f"To set new key for {plat}, enter now. Else '⬅️ Back'.",reply_markup=get_back_keyboard("2fa_secret_entry")); user_states[cid]={"state":STATE_WAITING_FOR_2FA_SECRET,"platform":plat}
        except Exception as e: safe_send_message(cid,f"Err with {plat} secret:{e}.Re-add.",reply_markup=get_2fa_platform_keyboard()); user_states[cid]={"state":STATE_WAITING_FOR_2FA_SECRET,"platform":plat}; user_2fa_secrets.get(cid,{}).pop(plat,None)
    else: user_states[cid]={"state":STATE_WAITING_FOR_2FA_SECRET,"platform":plat}; safe_send_message(cid,f"🔢Enter Base32 2FA secret for *{plat}*:\n(e.g.,`KEY123`)\nOr '⬅️ Back'.",reply_markup=get_back_keyboard("2fa_secret_entry"))
@bot.message_handler(func=lambda m:m.text=="⬅️ Back to Main")
def back_main_h(m):user_states.pop(m.chat.id,None);safe_send_message(m.chat.id,"⬅️To main",reply_markup=get_main_keyboard(m.chat.id))
@bot.message_handler(func=lambda m:m.text=="⬅️ Back to 2FA Platforms")
def back_2fa_plat(m):user_states[m.chat.id]={"state":"2fa_platform_select"};safe_send_message(m.chat.id,"⬅️Choose platform:",reply_markup=get_2fa_platform_keyboard())
@bot.message_handler(func=lambda m:user_states.get(m.chat.id,{}).get("state")==STATE_WAITING_FOR_2FA_SECRET)
def handle_2fa_secret_in(m):
    cid,s_in=m.chat.id,m.text.strip();plat=user_states.get(cid,{}).get("platform")
    if not plat:safe_send_message(cid,"Err:Platform not set.Start 2FA again.",reply_markup=get_main_keyboard(cid));user_states.pop(cid,None);return
    if not is_valid_base32(s_in):safe_send_message(cid,"❌*Invalid Secret*(A-Z,2-7).\nTry again,'⬅️ Back'.",reply_markup=get_back_keyboard("2fa_secret_entry"));return
    cl,p=s_in.replace(" ","").replace("-","").upper(),"";p="="*(-len(cl)%8);final_s=cl+p
    if cid not in user_2fa_secrets:user_2fa_secrets[cid]={}
    user_2fa_secrets[cid][plat]={"secret":final_s,"added":datetime.datetime.now().isoformat()};user_states.pop(cid,None)
    try:totp,now=pyotp.TOTP(final_s),datetime.datetime.now();c,s=totp.now(),30-(now.second%30);safe_send_message(cid,f"✅*2FA Secret for {plat} Saved!*\n🔑Code:`{c}`\n⏳Valid ~*{s}s*.",reply_markup=get_main_keyboard(cid))
    except Exception as e:user_2fa_secrets.get(cid,{}).pop(plat,None);safe_send_message(cid,f"❌Err with secret for {plat}:{e}.Not saved.",reply_markup=get_2fa_platform_keyboard());user_states[cid]={"state":"2fa_platform_select"}

# --- Fallback Handler ---
@bot.message_handler(func=lambda m:True,content_types=['text'])
def echo_all(m):
    cid=m.chat.id; safe_delete_user(cid) if is_bot_blocked(cid) else None
    if not (cid in approved_users or is_admin(cid)): (safe_send_message(cid,"⏳Access pending.") if cid in pending_approvals else send_welcome(m)); return
    st_info=user_states.get(cid,{});st=st_info.get("state")
    backs=["⬅️ Back to 2FA Platforms","⬅️ Back to Main","⬅️ Back to User Management","⬅️ Back to Broadcast Menu","⬅️ Back to Admin"]
    if st==STATE_WAITING_FOR_2FA_SECRET and m.text not in backs: safe_send_message(cid,f"Waiting for 2FA secret for {st_info.get('platform','platform')} or 'Back'.",reply_markup=get_back_keyboard("2fa_secret_entry")); return
    safe_send_message(cid,f"🤔Unknown:'{m.text}'.Use buttons.",reply_markup=get_main_keyboard(cid))

# --- Main Loop ---
if __name__ == '__main__':
    print(f"[{datetime.datetime.now()}] Initializing bot...")
    user_profiles["bot_start_time"] = datetime.datetime.now() 
    print(f"[{datetime.datetime.now()}] Starting background threads...")
    threading.Thread(target=auto_refresh_worker, daemon=True).start()
    threading.Thread(target=cleanup_blocked_users, daemon=True).start()
    print(f"[{datetime.datetime.now()}] Starting polling for bot token: ...{BOT_TOKEN[-6:] if BOT_TOKEN else 'NONE'}")
    while True:
        try: bot.infinity_polling(timeout=60, long_polling_timeout=30, logger_level=None) 
        except requests.exceptions.ReadTimeout as e_rt: print(f"[{datetime.datetime.now()}] Poll ReadTimeout:{e_rt}.Retry 15s..."); time.sleep(15)
        except requests.exceptions.ConnectionError as e_ce: print(f"[{datetime.datetime.now()}] Poll ConnectErr:{e_ce}.Retry 30s..."); time.sleep(30)
        except Exception as loop_e: print(f"[{datetime.datetime.now()}] CRITICAL Poll Err:{type(loop_e).__name__}-{loop_e}.Retry 60s..."); time.sleep(60)
        else: print(f"[{datetime.datetime.now()}] Poll loop exit cleanly(unexpected).Restart 10s..."); time.sleep(10)

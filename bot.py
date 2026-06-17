import os
import json
import datetime
import urllib.request
import urllib.parse
import ssl
import re
import requests
import sqlite3
import threading
import time
from bs4 import BeautifulSoup
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# Load env variables
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
    print("WARNING: TELEGRAM_BOT_TOKEN is not configured in .env file.")
    BOT_TOKEN = None

# Initialize bot
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None

# Safe callback query answering to prevent expired query exceptions
def safe_answer_callback(call_id, text=None):
    if not bot:
        return
    try:
        bot.answer_callback_query(call_id, text=text)
    except Exception as e:
        print(f"Warning: Callback query answer expired/invalid: {e}")

# Safe message deletion to prevent ApiTelegramException if message cannot be deleted
def safe_delete_message(chat_id, message_id):
    if not bot:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception as e:
        print(f"Warning: Could not delete message {message_id} in chat {chat_id}: {e}")

# No OCR engine needed anymore since CPC API allows direct fetch

# Disable SSL warnings for requests
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# Global session cache
USER_SESSIONS = {}

# Timezone and DB globals
VIETNAM_TZ = datetime.timezone(datetime.timedelta(hours=7))
DAILY_NOTIFY_TIME = os.getenv("DAILY_NOTIFY_TIME", "07:00")
DB_PATH = os.getenv("SUBSCRIPTIONS_DB", "subscriptions.sqlite3")
DB_LOCK = threading.RLock()
_DB_INITIALIZED = False
NOTIFICATION_SEND_LOCK = threading.Lock()

try:
    ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
except (ValueError, TypeError):
    ADMIN_TELEGRAM_ID = None

# TIME HELPERS
def now_vietnam():
    return datetime.datetime.now(VIETNAM_TZ)

def utc_now_text():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

# DATABASE FUNCTIONS
def init_db():
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return

    with DB_LOCK:
        if _DB_INITIALIZED:
            return
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    company_code TEXT,
                    company_name TEXT,
                    bureau_code TEXT NOT NULL,
                    bureau_name TEXT,
                    ward_key TEXT NOT NULL,
                    ward_name TEXT NOT NULL,
                    notify_time TEXT NOT NULL DEFAULT '07:00',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_sent_date TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_id, bureau_code, ward_key)
                )
            """)
            ensure_table_column(conn, "daily_subscriptions", "company_code", "company_code TEXT")
            ensure_table_column(conn, "daily_subscriptions", "company_name", "company_name TEXT")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feedbacks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unread',
                    admin_reply TEXT,
                    created_at TEXT NOT NULL,
                    read_at TEXT,
                    replied_at TEXT
                )
            """)
            conn.commit()
        _DB_INITIALIZED = True

def get_db_connection():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_user_profile(from_user):
    username = getattr(from_user, 'username', None)
    first_name = getattr(from_user, 'first_name', None)
    return username, first_name

def ensure_table_column(conn, table_name, column_name, column_sql):
    existing_columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in existing_columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")

def save_daily_subscription(call, company_code, company_name, bureau_code, bureau_name, ward_key, ward_name):
    username, first_name = get_user_profile(call.from_user)
    now_text = utc_now_text()
    current_vn = now_vietnam()
    today_text = current_vn.date().isoformat()

    with DB_LOCK:
        with get_db_connection() as conn:
            existing = conn.execute("""
                SELECT id, last_sent_date
                FROM daily_subscriptions
                WHERE chat_id = ? AND bureau_code = ? AND ward_key = ?
            """, (call.message.chat.id, bureau_code, ward_key)).fetchone()
            conn.execute("""
                INSERT INTO daily_subscriptions (
                    telegram_user_id, chat_id, username, first_name,
                    company_code, company_name,
                    bureau_code, bureau_name, ward_key, ward_name,
                    notify_time, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(chat_id, bureau_code, ward_key) DO UPDATE SET
                    telegram_user_id = excluded.telegram_user_id,
                    username = excluded.username,
                    first_name = excluded.first_name,
                    company_code = excluded.company_code,
                    company_name = excluded.company_name,
                    bureau_name = excluded.bureau_name,
                    ward_name = excluded.ward_name,
                    notify_time = excluded.notify_time,
                    enabled = 1,
                    updated_at = excluded.updated_at
            """, (
                call.from_user.id,
                call.message.chat.id,
                username,
                first_name,
                company_code,
                company_name,
                bureau_code,
                bureau_name,
                ward_key,
                ward_name,
                DAILY_NOTIFY_TIME,
                now_text,
                now_text
            ))
            conn.commit()

            row = conn.execute("""
                SELECT id, last_sent_date
                FROM daily_subscriptions
                WHERE chat_id = ? AND bureau_code = ? AND ward_key = ?
            """, (call.message.chat.id, bureau_code, ward_key)).fetchone()

    last_sent_date = row['last_sent_date'] if row else (existing['last_sent_date'] if existing else None)
    return {
        'id': row['id'] if row else None,
        'should_send_today': current_vn.strftime("%H:%M") >= DAILY_NOTIFY_TIME and last_sent_date != today_text
    }

def get_active_subscriptions(chat_id):
    with get_db_connection() as conn:
        return conn.execute("""
            SELECT *
            FROM daily_subscriptions
            WHERE chat_id = ? AND enabled = 1
            ORDER BY bureau_name, ward_name
        """, (chat_id,)).fetchall()

def get_due_subscriptions(current_time, today_text):
    with get_db_connection() as conn:
        return conn.execute("""
            SELECT *
            FROM daily_subscriptions
            WHERE enabled = 1
              AND notify_time <= ?
              AND (last_sent_date IS NULL OR last_sent_date != ?)
            ORDER BY bureau_code, chat_id
        """, (current_time, today_text)).fetchall()

def mark_subscription_sent(subscription_id, today_text):
    with DB_LOCK:
        with get_db_connection() as conn:
            conn.execute("""
                UPDATE daily_subscriptions
                SET last_sent_date = ?, updated_at = ?
                WHERE id = ?
            """, (today_text, utc_now_text(), subscription_id))
            conn.commit()

def disable_subscription(subscription_id, chat_id):
    with DB_LOCK:
        with get_db_connection() as conn:
            cur = conn.execute("""
                UPDATE daily_subscriptions
                SET enabled = 0, updated_at = ?
                WHERE id = ? AND chat_id = ?
            """, (utc_now_text(), subscription_id, chat_id))
            conn.commit()
            return cur.rowcount > 0

def disable_all_subscriptions(chat_id):
    with DB_LOCK:
        with get_db_connection() as conn:
            cur = conn.execute("""
                UPDATE daily_subscriptions
                SET enabled = 0, updated_at = ?
                WHERE chat_id = ? AND enabled = 1
            """, (utc_now_text(), chat_id))
            conn.commit()
            return cur.rowcount

def is_admin_user(user_id):
    return ADMIN_TELEGRAM_ID is not None and user_id == ADMIN_TELEGRAM_ID

# FEEDBACK FUNCTIONS
def format_sender_label(row):
    username = row['username'] if row['username'] else None
    first_name = row['first_name'] if row['first_name'] else None
    parts = []
    if username:
        parts.append(f"@{username}")
    if first_name:
        parts.append(first_name)
    return " / ".join(parts) if parts else f"User {row['telegram_user_id']}"

def save_feedback(message, feedback_text):
    username, first_name = get_user_profile(message.from_user)
    now_text = utc_now_text()

    with DB_LOCK:
        with get_db_connection() as conn:
            cur = conn.execute("""
                INSERT INTO feedbacks (
                    telegram_user_id, chat_id, username, first_name,
                    message, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, 'unread', ?)
            """, (
                message.from_user.id,
                message.chat.id,
                username,
                first_name,
                feedback_text,
                now_text
            ))
            conn.commit()
            feedback_id = cur.lastrowid

    return get_feedback(feedback_id)

def count_user_feedbacks_today(telegram_user_id):
    start_of_day = now_vietnam().replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_of_day.astimezone(datetime.timezone.utc).isoformat()
    with get_db_connection() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS count
            FROM feedbacks
            WHERE telegram_user_id = ? AND created_at >= ?
        """, (telegram_user_id, start_utc)).fetchone()
        return row['count'] if row else 0

def get_feedback(feedback_id):
    with get_db_connection() as conn:
        return conn.execute("""
            SELECT *
            FROM feedbacks
            WHERE id = ?
        """, (feedback_id,)).fetchone()

def list_feedbacks(status=None, limit=10):
    with get_db_connection() as conn:
        if status:
            return conn.execute("""
                SELECT *
                FROM feedbacks
                WHERE status = ?
                ORDER BY id DESC
                LIMIT ?
            """, (status, limit)).fetchall()
        return conn.execute("""
            SELECT *
            FROM feedbacks
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

def mark_feedback_read(feedback_id):
    with DB_LOCK:
        with get_db_connection() as conn:
            cur = conn.execute("""
                UPDATE feedbacks
                SET status = CASE WHEN status = 'unread' THEN 'read' ELSE status END,
                    read_at = COALESCE(read_at, ?)
                WHERE id = ?
            """, (utc_now_text(), feedback_id))
            conn.commit()
            return cur.rowcount > 0

def save_feedback_reply(feedback_id, reply_text):
    now_text = utc_now_text()
    with DB_LOCK:
        with get_db_connection() as conn:
            cur = conn.execute("""
                UPDATE feedbacks
                SET status = 'replied',
                    admin_reply = ?,
                    replied_at = ?,
                    read_at = COALESCE(read_at, ?)
                WHERE id = ?
            """, (reply_text, now_text, now_text, feedback_id))
            conn.commit()
            return cur.rowcount > 0

def format_feedback_detail(feedback):
    if not feedback:
        return "Không tìm thấy góp ý."

    return (
        f"💬 Góp ý #{feedback['id']}\n\n"
        f"Từ: {format_sender_label(feedback)}\n"
        f"User ID: {feedback['telegram_user_id']}\n"
        f"Chat ID: {feedback['chat_id']}\n"
        f"Trạng thái: {feedback['status']}\n"
        f"Thời gian: {feedback['created_at']}\n\n"
        f"Nội dung:\n{feedback['message']}"
    )

def build_admin_feedback_markup(feedback_id):
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("✅ Đã đọc", callback_data=f"fb_read_{feedback_id}"),
        InlineKeyboardButton("↩️ Trả lời", callback_data=f"fb_reply_{feedback_id}"),
        InlineKeyboardButton("📋 Góp ý chưa đọc", callback_data="fb_list_unread"),
        InlineKeyboardButton("📜 Tất cả góp ý", callback_data="fb_list_all")
    )
    return markup

def build_feedback_list_markup(feedbacks):
    markup = InlineKeyboardMarkup(row_width=1)
    for feedback in feedbacks:
        label = f"#{feedback['id']} {format_sender_label(feedback)}"
        if len(label) > 60:
            label = label[:57] + "..."
        markup.add(InlineKeyboardButton(label, callback_data=f"fb_view_{feedback['id']}"))
    markup.add(InlineKeyboardButton("📋 Góp ý chưa đọc", callback_data="fb_list_unread"))
    markup.add(InlineKeyboardButton("📜 Tất cả góp ý", callback_data="fb_list_all"))
    markup.add(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
    return markup

def notify_admin_new_feedback(feedback):
    if not ADMIN_TELEGRAM_ID:
        print(f"Feedback #{feedback['id']} saved, but ADMIN_TELEGRAM_ID is not configured.")
        return

    try:
        bot.send_message(
            ADMIN_TELEGRAM_ID,
            "💬 Góp ý mới\n\n" + format_feedback_detail(feedback),
            reply_markup=build_admin_feedback_markup(feedback['id'])
        )
    except Exception as e:
        print(f"Error sending feedback #{feedback['id']} to admin: {e}")

# UI MARKUP BUILDERS
def build_notification_actions_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("📋 Danh sách đang nhận", callback_data="notify_list"),
        InlineKeyboardButton("🔕 Tắt thông báo", callback_data="notify_disable_menu"),
        InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main")
    )
    return markup

def build_notification_menu_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("➕ Đăng ký thông báo", callback_data="notify_subscribe"),
        InlineKeyboardButton("📋 Danh sách đang nhận", callback_data="notify_list"),
        InlineKeyboardButton("🔕 Tắt thông báo", callback_data="notify_disable_menu"),
        InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main")
    )
    return markup

def format_subscription_list(subscriptions):
    if not subscriptions:
        return "ℹ️ Bạn chưa đăng ký nhận thông báo hằng ngày."

    lines = ["📋 **Danh sách thông báo hằng ngày đang nhận:**", ""]
    for idx, sub in enumerate(subscriptions, 1):
        bureau_name = sub['bureau_name'] or sub['bureau_code']
        lines.append(f"{idx}. **{sub['ward_name']}** - {bureau_name}")
        lines.append(f"   ⏰ Giờ gửi: {sub['notify_time']} hằng ngày")
    return "\n".join(lines)

def build_disable_subscriptions_markup(subscriptions):
    markup = InlineKeyboardMarkup(row_width=1)
    for sub in subscriptions:
        bureau_name = sub['bureau_name'] or sub['bureau_code']
        label = f"🔕 {sub['ward_name']} - {bureau_name}"
        if len(label) > 60:
            label = label[:57] + "..."
        markup.add(InlineKeyboardButton(label, callback_data=f"disable_sub_{sub['id']}"))

    if subscriptions:
        markup.add(InlineKeyboardButton("🔕 Tắt tất cả thông báo", callback_data="disable_all_subs"))
    markup.add(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
    return markup

# ORIGINAL HELPERS EXTRACTED
def remove_accents(input_str):
    s1 = u'ÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝàáâãèéêìíòóôõùúýĂăĐđĨĩŨũƠơƯưẠạẢảẤấẦầẨẩẪẫẬậẮắẰằẲẳẴẵẬậẸẹẺẻẼẽẾếỀềỂểỄễỆệỈỉỊịỌọỎỏỐốỒồỔổỖỗỘộỚớỜờỞởỠỡỢợỤụỦủỨứỪừỬửỮữỰựỲỳỴỵỶỷỸỹ'
    s0 = u'AAAAEEEIIOOOOUUYaaaaeeeioooouuyAaDdIiUuOoUuAaAaAaAaAaAaAaAaAaAaAaAaEeEeEeEeEeEeEeEeIiIiOoOoOoOoOoOoOoOoOoOoOoOoUuUuUuUuUuUuUuYyYyYyYy'
    s = ''
    for c in input_str:
        if c in s1:
            s += s0[s1.index(c)]
        else:
            s += c
    return s.lower()

# Helper function to extract commune name from location string
def extract_commune(location):
    # Match patterns like "thuộc xã An Phú", "thuộc phường Mỹ Long", "thuộc thị trấn Chợ Mới"
    match = re.search(r'(?:thuộc\s+)?(xã|phường|thị\s+trấn)\s+([^,\-\(\.]+)', location, re.IGNORECASE)
    if match:
        prefix = match.group(1).strip().capitalize()
        name = match.group(2).strip()
        name = " ".join(name.split())
        name = re.split(r'\s+[-–(]\s*', name)[0].strip()
        name = " ".join([w.capitalize() for w in name.split()])
        return f"{prefix} {name}"
    
    # Fallback to general prefix match
    match_fallback = re.search(r'\b(Xã|Phường|Thị\s+trấn)\s+([^,\-\(\.]+)', location, re.IGNORECASE)
    if match_fallback:
        prefix = match_fallback.group(1).strip().capitalize()
        name = match_fallback.group(2).strip()
        name = " ".join(name.split())
        name = re.split(r'\s+[-–(]\s*', name)[0].strip()
        name = " ".join([w.capitalize() for w in name.split()])
        return f"{prefix} {name}"
        
    return "Khác"

# Load SPC companies list
def load_spc_companies():
    companies_file = "companies.json"
    if not os.path.exists(companies_file):
        try:
            import setup_helper
            setup_helper.get_companies()
        except Exception as e:
            print(f"Error executing setup_helper: {e}")
            
    if os.path.exists(companies_file):
        with open(companies_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

companies_dict_spc = load_spc_companies()

# Load NPC static data
companies_dict_npc = {
    "PA02": "PC Phú Thọ",
    "PA03": "PC Quảng Ninh",
    "PA04": "PC Thái Nguyên",
    "PA07": "PC Thanh Hóa",
    "PA11": "PC Lạng Sơn",
    "PA12": "PC Tuyên Quang",
    "PA13": "PC Nghệ An",
    "PA14": "PC Cao Bằng",
    "PA15": "PC Sơn La",
    "PA16": "PC Hà Tĩnh",
    "PA18": "PC Lào Cai",
    "PA19": "PC Điện Biên",
    "PA22": "PC Bắc Ninh",
    "PA23": "PC Hưng Yên",
    "PA29": "PC Lai Châu",
    "PH": "PC Hải Phòng",
    "PN": "PC Ninh Bình"
}

def load_npc_bureaus():
    npc_bureaus_file = "npc_bureaus.json"
    if os.path.exists(npc_bureaus_file):
        try:
            with open(npc_bureaus_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading npc_bureaus.json: {e}")
    return {}

npc_bureaus_dict = load_npc_bureaus()

# Fetch local power bureaus for SPC
def fetch_spc_bureaus(parent_code):
    url = f"https://www.cskh.evnspc.vn/TraCuu/GetDanhMucDienLuc?pMA_DVICTREN={parent_code}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            html = response.read().decode('utf-8')
            soup = BeautifulSoup(html, 'html.parser')
            options = soup.find_all('option')
            
            bureaus = {}
            for opt in options:
                val = opt.get('value')
                text = opt.text.strip()
                if val and val.strip() and val != "0" and "chọn" not in text.lower():
                    bureaus[val.strip()] = text
            return bureaus
    except Exception as e:
        print(f"Error fetching SPC bureaus: {e}")
        return {}

# Fetch raw outage schedule for SPC
def fetch_raw_outages_spc(bureau_code):
    today = datetime.datetime.now()
    tu_ngay = today.strftime("%d-%m-%Y")
    den_ngay = (today + datetime.timedelta(days=7)).strftime("%d-%m-%Y")
    
    url = "https://www.cskh.evnspc.vn/TraCuu/GetThongTinLichNgungGiamCungCapDien"
    params = {
        'madvi': bureau_code,
        'tuNgay': tu_ngay,
        'denNgay': den_ngay,
        'ChucNang': 'MaDonVi'
    }
    
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    req = urllib.request.Request(full_url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            html = response.read().decode('utf-8')
            return parse_spc_html_raw(html)
    except Exception as e:
        print(f"Error fetching raw SPC outages: {e}")
        return []

def parse_spc_html_raw(html):
    soup = BeautifulSoup(html, 'html.parser')
    outages = []
    
    # 1. List-based layout (entry)
    entries = soup.find_all(class_='entry')
    for entry in entries:
        where_el = entry.find(class_='where')
        time_el = entry.find(class_='time')
        cause_el = entry.find(class_='cause')
        
        if where_el:
            time_text = ""
            if time_el:
                time_text = time_el.text.replace('THỜI GIAN:', '').strip()
                time_in_where = where_el.find(class_='time')
                if time_in_where:
                    time_in_where.decompose()
            
            location = where_el.text.replace('KHU VỰC:', '').strip()
            reason = "Bảo trì định kỳ"
            if cause_el:
                reason = cause_el.text.replace('LÝ DO NGỪNG CUNG CẤP ĐIỆN:', '').replace('LÝ DO:', '').strip()
            
            location = " ".join(location.split())
            time_text = " ".join(time_text.split())
            reason = " ".join(reason.split())
            
            outages.append({
                'location': location,
                'time': time_text,
                'reason': reason
            })
            
    # 2. Table-based layout
    if not outages:
        table = soup.find('table')
        if table:
            headers = []
            thead = table.find('thead')
            if thead:
                headers = [remove_accents(th.text.strip()) for th in thead.find_all('th')]
            else:
                first_tr = table.find('tr')
                if first_tr:
                    headers = [remove_accents(td.text.strip()) for td in first_tr.find_all(['td', 'th'])]
            
            col_map = {'location': -1, 'start_time': -1, 'end_time': -1, 'reason': -1, 'time': -1}
            for idx, h in enumerate(headers):
                if any(kw in h for kw in ['khu vuc', 'dia diem', 'khach hang', 'pham vi']):
                    col_map['location'] = idx
                elif any(kw in h for kw in ['bat dau', 'tu ngay', 'tu thoi diem', 'tu gio']):
                    col_map['start_time'] = idx
                elif any(kw in h for kw in ['ket thuc', 'den ngay', 'den thoi diem', 'den gio']):
                    col_map['end_time'] = idx
                elif any(kw in h for kw in ['ly do', 'nguyen nhan', 'noi dung']):
                    col_map['reason'] = idx
                elif 'thoi gian' in h:
                    col_map['time'] = idx

            tbody = table.find('tbody')
            rows = tbody.find_all('tr') if tbody else table.find_all('tr')[1:]
            
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 2:
                    continue
                num_cells = len(cells)
                
                loc_idx = col_map['location'] if col_map['location'] != -1 else (1 if num_cells > 1 else 0)
                location = cells[loc_idx].text.strip() if loc_idx < num_cells else "N/A"
                
                time_str = ""
                if col_map['time'] != -1 and col_map['time'] < num_cells:
                    time_str = cells[col_map['time']].text.strip()
                else:
                    start_idx = col_map['start_time'] if col_map['start_time'] != -1 else (2 if num_cells > 2 else -1)
                    end_idx = col_map['end_time'] if col_map['end_time'] != -1 else (3 if num_cells > 3 else -1)
                    
                    start = cells[start_idx].text.strip() if (start_idx != -1 and start_idx < num_cells) else ""
                    end = cells[end_idx].text.strip() if (end_idx != -1 and end_idx < num_cells) else ""
                    
                    if start and end:
                        time_str = f"Từ {start} - Đến {end}"
                    elif start:
                        time_str = f"Từ {start}"
                    else:
                        time_str = "Chưa rõ thời gian"
                        
                reason_idx = col_map['reason'] if col_map['reason'] != -1 else (4 if num_cells > 4 else (num_cells - 1 if num_cells > 1 else -1))
                reason = cells[reason_idx].text.strip() if (reason_idx != -1 and reason_idx < num_cells) else "Bảo trì định kỳ"
                
                location = " ".join(location.split())
                time_str = " ".join(time_str.split())
                reason = " ".join(reason.split())
                
                if location and location != "Không có dữ liệu":
                    outages.append({
                        'location': location,
                        'time': time_str,
                        'reason': reason
                    })
    return outages

# Customer lookup removed

# Format list of outages into message chunks
def format_outage_messages(outages, tu_ngay, den_ngay, title_suffix=""):
    header_msg = f"📅 **LỊCH CÚP ĐIỆN DỰ KIẾN {title_suffix} (Từ {tu_ngay} đến {den_ngay})**\n"
    header_msg += f"Tổng số thông báo: {len(outages)}\n"
    header_msg += "=================================\n\n"
    
    result_messages = []
    current_msg = header_msg
    
    for idx, item in enumerate(outages, 1):
        chunk = f"⚡ **{idx}. Địa điểm:** {item['location']}\n"
        chunk += f"⏰ **Thời gian:** {item['time']}\n"
        chunk += f"📝 **Lý do:** {item['reason']}\n"
        chunk += "---------------------------------\n\n"
        
        if len(current_msg) + len(chunk) > 4000:
            result_messages.append(current_msg)
            current_msg = chunk
        else:
            current_msg += chunk
            
    result_messages.append(current_msg)
    return result_messages


# EVN NPC (NORTHERN REGION) API CALLS
def fetch_npc_outages(bureau_code):
    today = datetime.datetime.now()
    tu_ngay = today.strftime("%d/%m/%Y")
    den_ngay = (today + datetime.timedelta(days=15)).strftime("%d/%m/%Y")
    
    url = "https://cskh.npc.com.vn/ThongTinKhachHang/LichNgungGiamCungCapDienSPC"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
        'X-Requested-With': 'XMLHttpRequest'
    }
    params = {
        'madvi': bureau_code,
        'tuNgay': tu_ngay,
        'denNgay': den_ngay
    }
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as response:
            html = response.read().decode('utf-8')
            return parse_npc_html_raw(html)
    except Exception as e:
        print(f"Error fetching NPC outages for {bureau_code}: {e}")
        return []

def parse_npc_html_raw(html):
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table')
    if not table:
        return []
        
    tbody = table.find('tbody')
    rows = tbody.find_all('tr') if tbody else table.find_all('tr')[1:]
    
    outages = []
    for row in rows:
        cells = row.find_all('td')
        if len(cells) < 5:
            continue
            
        text_cells = [c.text.strip() for c in cells]
        if len(text_cells) >= 5:
            location = text_cells[4]
            start_raw = text_cells[2]
            end_raw = text_cells[3]
            
            def format_time_str(val):
                try:
                    dt = datetime.datetime.strptime(val[:19], "%Y-%m-%d %H:%M:%S")
                    return dt.strftime("%H:%M %d/%m/%Y")
                except Exception:
                    return val
                    
            start_fmt = format_time_str(start_raw)
            end_fmt = format_time_str(end_raw)
            time_str = f"Từ {start_fmt} - Đến {end_fmt}"
            
            reason = "Bảo trì định kỳ"
            if len(text_cells) >= 6 and text_cells[5]:
                reason = text_cells[5]
                
            if location and location != "Không có dữ liệu":
                outages.append({
                    'location': " ".join(location.split()),
                    'time': " ".join(time_str.split()),
                    'reason': " ".join(reason.split())
                })
    return outages


# EVN CPC (CENTRAL REGION) API CALLS

def fetch_cpc_companies():
    return {
        "PC02": "Quảng Trị & Quảng Bình",
        "PC03": "Thừa Thiên Huế",
        "PP": "Đà Nẵng & Quảng Nam",
        "PC06": "Quảng Ngãi & Kon Tum",
        "PC10": "Gia Lai & Bình Định",
        "PC12": "Đắk Lắk & Phú Yên",
        "PQ": "Khánh Hòa",
        "PB18": "Ninh Thuận"
    }

def fetch_cpc_bureaus(parent_code):
    cpc_bureaus_map = {
        "PC02": {
            "PC02AA": "Điện lực Đông Hà",
            "PC02DD": "Điện lực Vĩnh Linh",
            "PC02CC": "Điện lực Gio Linh",
            "PC02KK": "Điện lực Hải Lăng",
            "PC02BB": "Điện lực Thành Cổ",
            "PC02HH": "Điện lực Triệu Phong",
            "PC02FF": "Điện lực Khe Sanh",
            "PC02GG": "Điện lực Cam Lộ",
            "PC02LL": "Điện lực ĐaKrông",
            "PC02MM": "Điện lực Cồn Cỏ",
            "PC01AA": "Điện lực Đồng Hới",
            "PC01DD": "Điện lực Bố Trạch",
            "PC01BB": "Điện lực Quảng Trạch",
            "PC01EE": "Điện lực Tuyên Hóa",
            "PC01CC": "Điện lực Quảng Ninh",
            "PC01FF": "Điện lực Lệ Thủy",
            "PC01MM": "Điện lực Minh Hóa"
        },
        "PC03": {
            "PC03BB": "Điện lực Bắc Sông Hương",
            "PC03AA": "Điện lực Nam Sông Hương",
            "PC03PP": "Điện lực Hương Thủy",
            "PC03CC": "Điện lực Phong Điền",
            "PC03TT": "Điện lực Hương Trà",
            "PC03DD": "Điện lực Phú Vang",
            "PC03HH": "Điện lực Quảng Điền",
            "PC03EE": "Điện lực A Lưới",
            "PC03FF": "Điện lực Nam Đông",
            "PC03GG": "Điện lực Phú Lộc"
        },
        "PP": {
            "PP0100": "Điện lực Hải Châu",
            "PP0300": "Điện lực Liên Chiểu",
            "PP0500": "Điện lực Sơn Trà",
            "PP0700": "Điện lực Cẩm Lệ",
            "PP0900": "Điện lực Thanh Khê",
            "PP0800": "Điện lực Hòa Vang",
            "PC05GG": "Điện lực Đại Lộc",
            "PC05CC": "Điện lực Hội An",
            "PC05DD": "Điện lực Duy Xuyên",
            "PC05FF": "Điện lực Thăng Bình",
            "PC05HH": "Điện lực Hiệp Đức",
            "PC05AA": "Điện lực Tam Kỳ",
            "PC05BB": "Điện lực Núi Thành",
            "PC05EE": "Điện lực Tiên Phước",
            "PC05MM": "Điện lực Quế Sơn",
            "PC05NN": "Điện lực Trà My",
            "PC05II": "Điện lực Điện Bàn",
            "PC05PP": "Điện lực Nam Giang",
            "PC05KK": "Điện lực Đông Giang"
        },
        "PC06": {
            "PC06AA": "Điện lực TP Quảng Ngãi",
            "PC06BB": "Điện lực Bình Sơn",
            "PC06TT": "Điện lực Trà Bồng",
            "PC06SS": "Điện lực Sơn Tịnh",
            "PC06HH": "Điện lực Sơn Hà",
            "PC06EE": "Điện lực Tư Nghĩa",
            "PC06NN": "Điện lực Nghĩa Hành",
            "PC06MM": "Điện lực Mộ Đức",
            "PC06DD": "Điện lực Đức Phổ",
            "PC06CC": "Điện lực Ba Tơ",
            "PC06LL": "Điện lực Lý Sơn",
            "PC11AA": "Điện lực Thành phố Kon Tum",
            "PC11DD": "Điện lực Đăk Hà",
            "PC11EE": "Điện lực Sa Thầy",
            "PC11CC": "Điện lực Đăk Tô",
            "PC11FF": "Điện lực Ngọc Hồi",
            "PC11GG": "Điện lực Đăk Glei",
            "PC11BB": "Điện lực Kon Rẫy",
            "PC11II": "Điện lực Kon PLong",
            "PC11KK": "Điện lực Tu Mơ Rông"
        },
        "PC10": {
            "PC10BB": "Điện lực An Khê",
            "PC10CC": "Điện lực Ayun Pa",
            "PC10II": "Điện lực Chư Păh",
            "PC10EE": "Điện lực Chư Sê",
            "PC10PP": "Điện lực Chư Pưh",
            "PC10NN": "Điện lực Chư Prông",
            "PC10HH": "Điện lực Đăk Đoa",
            "PC10GG": "Điện lực Đức cơ",
            "PC10FF": "Điện lực Kbang",
            "PC10LL": "Điện lực Kông Chro",
            "PC10DD": "Điện lực Krông Pa",
            "PC10MM": "Điện lực Mang Yang",
            "PC10OO": "Điện lực Phú Thiện",
            "PC10KK": "Điện lực Ia Grai",
            "PC10AA": "Điện lực Pleiku",
            "PC07AA": "Điện lực Quy Nhơn",
            "PC07FF": "Điện lực Phú Tài",
            "PC07HH": "Điện lực Tuy Phước",
            "PC07BB": "Điện lực An Nhơn",
            "PC07GG": "Điện lực Phù Cát",
            "PC07EE": "Điện lực Phù Mỹ",
            "PC07CC": "Điện lực Bồng Sơn",
            "PC07DD": "Điện lực Phú Phong",
            "PC07II": "Điện lực Hoài Ân"
        },
        "PC12": {
            "PC12CC": "Điện lực Nam Buôn Ma Thuột",
            "PC12DD": "Điện lực Buôn Hồ",
            "PC12GG": "Điện lực Cư M'gar",
            "PC12AA": "Điện lực Bắc Buôn Ma Thuột",
            "PC12JJ": "Điện lực Krông Năng",
            "PC12II": "Điện lực Ea H'leo",
            "PC12BB": "Điện lực Krông Pắc",
            "PC12KK": "Điện lực Ea Kar",
            "PC12NN": "Điện lực Krông Bông",
            "PC12PP": "Điện lực Lắk",
            "PC12LL": "Điện lực Krông Ana",
            "PC12EE": "Điện lực Buôn Đôn",
            "PC12MM": "Điện lực Ea Súp",
            "PC12HH": "Điện lực Cư Kuin",
            "PC08AA": "Điện lực Tuy Hòa",
            "PC08CC": "Điện lực Đông Hòa",
            "PC08II": "Điện lực Tây Hòa",
            "PC08EE": "Điện lực Tuy An",
            "PC08BB": "Điện lực Sông Cầu",
            "PC08HH": "Điện lực Phú Hòa",
            "PC08FF": "Điện lực Sông Hinh",
            "PC08DD": "Điện lực Sơn Hòa",
            "PC08GG": "Điện lực Đồng Xuân"
        },
        "PQ": {
            "PQ0200": "Điện lực Trung tâm Nha Trang",
            "PQ0300": "Điện lực Cam Ranh-Khánh Sơn",
            "PQ0400": "Điện lực Ninh Hòa",
            "PQ0500": "Điện lực Diên Khánh-Khánh Vĩnh",
            "PQ0600": "Điện lực Vạn Ninh",
            "PQ0900": "Điện lực Vĩnh Hải",
            "PQ1000": "Điện lực Vĩnh Nguyên",
            "PQ1100": "Điện lực Cam Lâm"
        },
        "PB18": {
            "PB1801": "Điện lực Phan Rang",
            "PB1802": "Điện lực Ninh Hải",
            "PB1804": "Điện lực Ninh Phước",
            "PB1805": "Điện lực Thuận Bắc",
            "PB1806": "Điện lực Thuận Nam",
            "PB1803": "Điện lực Ninh Sơn",
            "PB1807": "Điện lực Trường Sa"
        }
    }
    return cpc_bureaus_map.get(parent_code, {})

# Helper to determine the parent CPC company code based on bureau prefix
def get_cpc_org_code(bureau_code):
    prefix_map = {
        "PC01": "PC02",
        "PC02": "PC02",
        "PC03": "PC03",
        "PP": "PP",
        "PC05": "PP",
        "PC06": "PC06",
        "PC11": "PC06",
        "PC10": "PC10",
        "PC07": "PC10",
        "PC12": "PC12",
        "PC08": "PC12",
        "PQ": "PQ",
        "PB18": "PB18"
    }
    for prefix, parent in prefix_map.items():
        if bureau_code.startswith(prefix):
            return parent
    return None

# Direct outage fetch for CPC without CAPTCHA validation
def fetch_cpc_outages_direct(org_code, bureau_code):
    url_outages = "https://cskh-api.cpc.vn/api/remote/outages/area"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0',
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://cskh.cpc.vn',
        'Referer': 'https://cskh.cpc.vn/',
        'version': '1.0'
    }
    try:
        today = datetime.datetime.now()
        from_date = today.strftime("%Y-%m-%d 00:00:00")
        to_date = (today + datetime.timedelta(days=7)).strftime("%Y-%m-%d 23:59:59")
        
        params = {
            'orgCode': org_code,
            'subOrgCode': bureau_code,
            'fromDate': from_date,
            'toDate': to_date,
            'page': 1,
            'limit': 100,
            'status': 'Approved'
        }
        
        # Note: verify=False is used because EVN servers frequently have SSL certificate configuration issues.
        r = requests.get(url_outages, headers=headers, params=params, verify=False, timeout=15)
        if r.status_code == 200:
            outages_data = r.json()
            items = outages_data.get('items', [])
            if not items and isinstance(outages_data, list):
                items = outages_data
            return items
        else:
            print(f"[EVN CPC] Direct fetch failed with status code {r.status_code}: {r.text}")
            return None
    except Exception as e:
        print(f"[EVN CPC] Exception fetching outages: {e}")
        return None



# WELCOME AND MENU HANDLERS
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.clear_step_handler_by_chat_id(chat_id=message.chat.id)
    
    welcome_text = (
        "👋 Chào mừng bạn đến với **Bot Tra cứu Lịch cúp điện EVN**!\n\n"
        "Vui lòng chọn khu vực hoặc hình thức tra cứu:"
    )
    markup = InlineKeyboardMarkup(row_width=1)
    btn_spc = InlineKeyboardButton("⚡ EVN miền Nam (SPC)", callback_data="region_SPC")
    btn_cpc = InlineKeyboardButton("⚡ EVN miền Trung (CPC)", callback_data="region_CPC")
    btn_npc = InlineKeyboardButton("⚡ EVN miền Bắc (NPC)", callback_data="region_NPC")
    btn_notify = InlineKeyboardButton("🔔 Nhận thông báo hằng ngày", callback_data="menu_notify")
    btn_guide = InlineKeyboardButton("📖 Hướng dẫn sử dụng", callback_data="menu_guide")
    btn_feedback = InlineKeyboardButton("💬 Góp ý cải thiện", callback_data="menu_feedback")
    
    markup.add(btn_spc, btn_cpc, btn_npc, btn_notify, btn_guide, btn_feedback)
    
    if is_admin_user(message.from_user.id):
        btn_admin_fb = InlineKeyboardButton("📋 Quản lý Góp ý (Admin)", callback_data="fb_list_unread")
        markup.add(btn_admin_fb)
        
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'back_main')
def handle_back_main(call):
    safe_answer_callback(call.id)
    welcome_text = (
        "👋 Chào mừng bạn đến với **Bot Tra cứu Lịch cúp điện EVN**!\n\n"
        "Vui lòng chọn khu vực hoặc hình thức tra cứu:"
    )
    markup = InlineKeyboardMarkup(row_width=1)
    btn_spc = InlineKeyboardButton("⚡ EVN miền Nam (SPC)", callback_data="region_SPC")
    btn_cpc = InlineKeyboardButton("⚡ EVN miền Trung (CPC)", callback_data="region_CPC")
    btn_npc = InlineKeyboardButton("⚡ EVN miền Bắc (NPC)", callback_data="region_NPC")
    btn_notify = InlineKeyboardButton("🔔 Nhận thông báo hằng ngày", callback_data="menu_notify")
    btn_guide = InlineKeyboardButton("📖 Hướng dẫn sử dụng", callback_data="menu_guide")
    btn_feedback = InlineKeyboardButton("💬 Góp ý cải thiện", callback_data="menu_feedback")
    
    markup.add(btn_spc, btn_cpc, btn_npc, btn_notify, btn_guide, btn_feedback)
    
    if is_admin_user(call.from_user.id):
        btn_admin_fb = InlineKeyboardButton("📋 Quản lý Góp ý (Admin)", callback_data="fb_list_unread")
        markup.add(btn_admin_fb)
        
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=welcome_text,
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == 'menu_guide')
def handle_menu_guide(call):
    safe_answer_callback(call.id)
    text = (
        "📖 **HƯỚNG DẪN SỬ DỤNG BOT LỊCH CÚP ĐIỆN**\n\n"
        "**1. Tra cứu thủ công:**\n"
        "👉 Nhấn chọn miền của bạn trên Menu chính (`⚡ EVN miền Nam (SPC)`, `⚡ EVN miền Trung (CPC)`, `⚡ EVN miền Bắc (NPC)`).\n"
        "👉 Chọn tỉnh thành và đơn vị Điện lực trực thuộc.\n"
        "👉 Bot sẽ hiển thị danh sách xã/phường có lịch cúp điện:\n"
        "   - Nhấn chọn xã/phường cụ thể để lọc lịch cúp điện.\n"
        "   - Chọn `🌟 Tất cả khu vực` để xem toàn bộ lịch cúp điện của huyện.\n"
        "   - Chọn `🔍 Tìm theo từ khóa` để tự nhập tên đường, thôn, xóm hoặc trạm biến áp cần tìm kiếm.\n\n"
        "**2. Nhận thông báo tự động hằng ngày:**\n"
        "👉 Nhấn chọn `🔔 Nhận thông báo hằng ngày` -> Chọn `➕ Đăng ký thông báo`.\n"
        "👉 Chọn miền, tỉnh thành và đơn vị Điện lực của bạn.\n"
        "👉 Lựa chọn hình thức nhận tin nhắn:\n"
        "   - **Đăng ký toàn bộ huyện:** Nhận tất cả thông báo cúp điện của huyện đó.\n"
        "   - **Đăng ký theo xã/phường (SPC & NPC):** Chỉ nhận khi xã/phường đó có lịch cúp điện.\n"
        "   - **Đăng ký theo từ khóa tự chọn (Khuyên dùng):** Bạn tự nhập tên đường, thôn, xóm hoặc trạm biến áp gần nhà (Ví dụ: `Hà Ra`, `Thôn 3`, `MBA T.65A`). Bot sẽ so khớp và chỉ gửi thông báo khi lịch cúp điện chứa từ khóa này.\n\n"
        "**3. Quản lý thông báo đang nhận:**\n"
        "👉 Nhấn `🔔 Nhận thông báo hằng ngày` -> Chọn `📋 Danh sách đang nhận` để kiểm tra các đăng ký hiện tại.\n"
        "👉 Chọn `🔕 Tắt thông báo` nếu muốn hủy nhận tin nhắn của một khu vực hoặc tắt tất cả."
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=markup
    )

# ORIGINAL REGIONAL MANUAL HANDLERS
@bot.callback_query_handler(func=lambda call: call.data == 'region_SPC')
def handle_region_spc(call):
    safe_answer_callback(call.id)
    welcome_text = (
        "🏢 **EVN miền Nam (SPC)**\n\n"
        "Hãy chọn **Công ty Điện lực (Tỉnh/Thành phố)** phía dưới để bắt đầu:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(companies_dict_spc.items()):
        display_name = name.replace("Công ty Điện lực ", "").replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"spc_comp_{code}"))
        
    back_btn = InlineKeyboardButton("⬅️ Quay lại Menu chính", callback_data="back_main")
    markup.add(*buttons)
    markup.row(back_btn)
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=welcome_text,
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == 'region_CPC')
def handle_region_cpc(call):
    safe_answer_callback(call.id, text="Đang tải danh sách tỉnh thành...")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🔄 Đang tải danh sách các tỉnh thành miền Trung..."
    )
    
    cpc_companies = fetch_cpc_companies()
    
    if not cpc_companies:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Quay lại Menu chính", callback_data="back_main"))
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="❌ Không thể tải danh sách tỉnh thành EVN miền Trung lúc này.",
            reply_markup=markup
        )
        return
        
    welcome_text = (
        "🏢 **EVN miền Trung (CPC)**\n\n"
        "Hãy chọn **Công ty Điện lực (Tỉnh/Thành phố)** phía dưới để bắt đầu:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(cpc_companies.items()):
        display_name = name.replace("Công ty Điện lực ", "").replace("Điện lực ", "").replace("Công ty CP Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"cpc_comp_{code}"))
        
    back_btn = InlineKeyboardButton("⬅️ Quay lại Menu chính", callback_data="back_main")
    markup.add(*buttons)
    markup.row(back_btn)
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=welcome_text,
        parse_mode="Markdown",
        reply_markup=markup
    )

# COMPANY SELECTORS

@bot.callback_query_handler(func=lambda call: call.data.startswith('spc_comp_'))
def handle_spc_company(call):
    company_code = call.data.split('_')[2]
    company_name = companies_dict_spc.get(company_code, "Công ty Điện lực")
    
    safe_answer_callback(call.id, text=f"Đang tải danh sách Điện lực của {company_name}...")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔄 Đang tải các đơn vị Điện lực thuộc **{company_name}**..."
    )
    
    bureaus = fetch_spc_bureaus(company_code)
    
    if not bureaus:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Chọn lại tỉnh thành", callback_data="region_SPC"))
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"❌ Không tìm thấy đơn vị Điện lực nào trực thuộc **{company_name}**.",
            reply_markup=markup
        )
        return
        
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(bureaus.items(), key=lambda item: item[1]):
        display_name = name.replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"spc_bur_{code}"))
        
    back_button = InlineKeyboardButton("⬅️ Chọn tỉnh thành khác", callback_data="region_SPC")
    markup.add(*buttons)
    markup.row(back_button)
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"📍 Bạn đã chọn: **{company_name}**.\n\nHãy chọn **Điện lực huyện/thành phố** cần tra cứu:",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('cpc_comp_'))
def handle_cpc_company(call):
    company_code = call.data.split('_')[2]
    
    safe_answer_callback(call.id, text="Đang tải danh sách Điện lực...")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🔄 Đang tải các đơn vị Điện lực huyện..."
    )
    
    bureaus = fetch_cpc_bureaus(company_code)
    
    if not bureaus:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Chọn lại tỉnh thành", callback_data="region_CPC"))
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="❌ Không tìm thấy đơn vị Điện lực nào thuộc Công ty này.",
            reply_markup=markup
        )
        return
        
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(bureaus.items(), key=lambda item: item[1]):
        display_name = name.replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"cpc_bur_{code}"))
        
    back_button = InlineKeyboardButton("⬅️ Chọn tỉnh thành khác", callback_data="region_CPC")
    markup.add(*buttons)
    markup.row(back_button)
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Hãy chọn **Điện lực huyện/thành phố** cần tra cứu:",
        parse_mode="Markdown",
        reply_markup=markup
    )

# BUREAU SELECTORS (SPC: SHOW COMMUNES, CPC: AUTO CAPTCHA & Outages)

@bot.callback_query_handler(func=lambda call: call.data.startswith('spc_bur_'))
def handle_spc_bureau(call):
    bureau_code = call.data.split('_')[2]
    
    safe_answer_callback(call.id, text="Đang tải lịch cúp điện...")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🔄 Đang tải và phân tích lịch cúp điện từ hệ thống EVN SPC. Vui lòng chờ..."
    )
    
    outages = fetch_raw_outages_spc(bureau_code)
    
    if not outages:
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("🔎 Tìm kiếm tiếp", callback_data="region_SPC"),
            InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"spc_bur_{bureau_code}")
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="ℹ️ Hiện tại không có lịch cúp điện dự kiến cho Điện lực này trong 7 ngày tới.",
            reply_markup=markup
        )
        return
        
    # Group by commune
    communes = {}
    for item in outages:
        comm = extract_commune(item['location'])
        if comm not in communes:
            communes[comm] = []
        communes[comm].append(item)
        
    # Store session
    USER_SESSIONS[call.message.chat.id] = {
        'region': 'SPC',
        'bureau_code': bureau_code,
        'outages': outages,
        'communes': communes
    }
    
    # Show commune selection buttons
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for comm_name, items in sorted(communes.items()):
        buttons.append(InlineKeyboardButton(f"{comm_name} ({len(items)})", callback_data=f"spc_comm_{comm_name}"))
        
    markup.add(*buttons)
    markup.row(
        InlineKeyboardButton("🌟 Tất cả khu vực", callback_data="spc_comm_all"),
        InlineKeyboardButton("🔍 Tìm theo từ khóa", callback_data="manual_kw_init")
    )
    markup.row(InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data="region_SPC"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🏢 **Tìm thấy lịch cúp điện tại {len(outages)} khu vực**.\n\nVui lòng chọn xã/phường dưới đây để lọc tin nhắn hoặc chọn 'Tất cả khu vực':",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('spc_comm_'))
def handle_spc_commune_select(call):
    commune_name = call.data.replace('spc_comm_', '')
    chat_id = call.message.chat.id
    
    safe_answer_callback(call.id)
    safe_delete_message(chat_id, call.message.message_id)
    
    session_data = USER_SESSIONS.get(chat_id)
    if not session_data or session_data.get('region') != 'SPC':
        bot.send_message(chat_id, "❌ Phiên làm việc hết hạn. Vui lòng gửi /start để thực hiện lại.")
        return
        
    outages = session_data['outages']
    communes = session_data['communes']
    bureau_code = session_data['bureau_code']
    
    today = datetime.datetime.now()
    tu_ngay = today.strftime("%d-%m-%Y")
    den_ngay = (today + datetime.timedelta(days=7)).strftime("%d-%m-%Y")
    
    if commune_name == 'all':
        title = ""
        items_to_format = outages
    else:
        title = f" [{commune_name.upper()}]"
        items_to_format = communes.get(commune_name, [])
        
    formatted_messages = format_outage_messages(items_to_format, tu_ngay, den_ngay, title)
    
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔎 Tìm kiếm tiếp", callback_data="region_SPC"),
        InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"spc_ref_reload_{bureau_code}_{commune_name}")
    )
    
    for msg_chunk in formatted_messages[:-1]:
        bot.send_message(chat_id, msg_chunk, parse_mode="Markdown")
    bot.send_message(chat_id, formatted_messages[-1], parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('spc_ref_reload_'))
def handle_spc_ref_reload(call):
    parts = call.data.split('_')
    bureau_code = parts[3]
    commune_name = parts[4]
    chat_id = call.message.chat.id
    
    safe_answer_callback(call.id, text="Đang làm mới lịch cúp điện...")
    safe_delete_message(chat_id, call.message.message_id)
    
    loading_msg = bot.send_message(chat_id, "🔄 Đang tải lại lịch cúp điện mới nhất...")
    
    outages = fetch_raw_outages_spc(bureau_code)
    safe_delete_message(chat_id, loading_msg.message_id)
    
    if not outages:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔎 Tìm kiếm tiếp", callback_data="region_SPC"))
        bot.send_message(chat_id, "ℹ️ Hiện tại không có lịch cúp điện dự kiến.", reply_markup=markup)
        return
        
    # Re-group
    communes = {}
    for item in outages:
        comm = extract_commune(item['location'])
        if comm not in communes:
            communes[comm] = []
        communes[comm].append(item)
        
    USER_SESSIONS[chat_id] = {
        'region': 'SPC',
        'bureau_code': bureau_code,
        'outages': outages,
        'communes': communes
    }
    
    today = datetime.datetime.now()
    tu_ngay = today.strftime("%d-%m-%Y")
    den_ngay = (today + datetime.timedelta(days=7)).strftime("%d-%m-%Y")
    
    if commune_name == 'all':
        title = ""
        items_to_format = outages
    else:
        title = f" [{commune_name.upper()}]"
        items_to_format = communes.get(commune_name, [])
        
    formatted_messages = format_outage_messages(items_to_format, tu_ngay, den_ngay, title)
    
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔎 Tìm kiếm tiếp", callback_data="region_SPC"),
        InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"spc_ref_reload_{bureau_code}_{commune_name}")
    )
    
    for msg_chunk in formatted_messages[:-1]:
        bot.send_message(chat_id, msg_chunk, parse_mode="Markdown")
    bot.send_message(chat_id, formatted_messages[-1], parse_mode="Markdown", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('cpc_bur_'))
def handle_cpc_bureau_select(call):
    bureau_code = call.data.split('_')[2]
    chat_id = call.message.chat.id
    
    safe_answer_callback(call.id)
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text="🔄 Đang tải lịch cúp điện từ EVN miền Trung..."
    )
    
    # Determine org_code
    org_code = get_cpc_org_code(bureau_code) or "PC02"
            
    items = fetch_cpc_outages_direct(org_code, bureau_code)
    
    if items is None:
        # Failed to fetch outages
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("🔎 Thử lại", callback_data=f"cpc_bur_{bureau_code}"),
            InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data="region_CPC")
        )
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text="❌ Hệ thống EVN miền Trung hiện tại đang bận hoặc không phản hồi. Vui lòng bấm thử lại:",
            reply_markup=markup
        )
        return
        
    today = datetime.datetime.now()
    if not items:
        # Success check but no records
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("🔎 Tìm kiếm tiếp", callback_data="region_CPC"),
            InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"cpc_bur_{bureau_code}")
        )
        bot.edit_message_text(
            chat_id=chat_id, 
            message_id=call.message.message_id,
            text=f"ℹ️ Không có thông tin lịch cúp điện từ ngày **{today.strftime('%d-%m-%Y')}** đến **{(today + datetime.timedelta(days=7)).strftime('%d-%m-%Y')}**.",
            parse_mode="Markdown",
            reply_markup=markup
        )
        return
        
    # Format and send CPC records
    outages = []
    for it in items:
        outages.append({
            'location': it.get('stationName', 'N/A'),
            'time': f"Từ {it.get('fromDateStr', 'N/A')} - Đến {it.get('toDateStr', 'N/A')}",
            'reason': it.get('reason', 'N/A')
        })
        
    bureau_name = bureau_code
    companies = fetch_cpc_companies()
    bureaus = fetch_cpc_bureaus(org_code)
    if bureau_code in bureaus:
        bureau_name = bureaus[bureau_code]
        
    USER_SESSIONS[chat_id] = {
        'region': 'CPC',
        'bureau_code': bureau_code,
        'bureau_name': bureau_name,
        'outages': outages
    }
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("🌟 Xem tất cả", callback_data="cpc_view_all"),
        InlineKeyboardButton("🔍 Tìm theo từ khóa", callback_data="manual_kw_init")
    )
    markup.row(InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data="region_CPC"))
    
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=f"🏢 **Lịch cúp điện {bureau_name}**\n\nTìm thấy {len(outages)} lịch cúp điện dự kiến.\n\nVui lòng chọn hình thức xem:",
        reply_markup=markup
    )


# Customer code callback handlers removed

# NPC REGIONAL MANUAL HANDLERS
@bot.callback_query_handler(func=lambda call: call.data == 'region_NPC')
def handle_region_npc(call):
    safe_answer_callback(call.id)
    welcome_text = (
        "🏢 **EVN miền Bắc (NPC)**\n\n"
        "Hãy chọn **Công ty Điện lực (Tỉnh/Thành phố)** phía dưới để bắt đầu:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(companies_dict_npc.items()):
        display_name = name.replace("Công ty Điện lực ", "").replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"npc_comp_{code}"))
        
    back_btn = InlineKeyboardButton("⬅️ Quay lại Menu chính", callback_data="back_main")
    markup.add(*buttons)
    markup.row(back_btn)
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=welcome_text,
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('npc_comp_'))
def handle_npc_company_select(call):
    company_code = call.data.split('_')[2]
    company_name = companies_dict_npc.get(company_code, "Công ty Điện lực")
    
    safe_answer_callback(call.id)
    
    bureaus = npc_bureaus_dict.get(company_code, {})
    if not bureaus:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Chọn lại tỉnh thành", callback_data="region_NPC"))
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"❌ Không tìm thấy đơn vị Điện lực trực thuộc **{company_name}**.",
            reply_markup=markup
        )
        return
        
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(bureaus.items(), key=lambda item: item[1]):
        display_name = name.replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"npc_bur_{code}"))
    markup.add(*buttons)
    markup.row(InlineKeyboardButton("⬅️ Chọn tỉnh thành khác", callback_data="region_NPC"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"📍 Bạn đang chọn: **{company_name}**.\n\nHãy chọn **Điện lực trực thuộc** để tra cứu:",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('npc_bur_'))
def handle_npc_bureau_select(call):
    bureau_code = call.data.split('_')[2]
    chat_id = call.message.chat.id
    
    safe_answer_callback(call.id)
    
    bureau_name = bureau_code
    company_name = "EVN miền Bắc"
    company_code = ""
    for c_code, burs in npc_bureaus_dict.items():
        if bureau_code in burs:
            bureau_name = burs[bureau_code]
            company_code = c_code
            company_name = companies_dict_npc.get(c_code, "Công ty Điện lực")
            break
            
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=f"🔄 Đang tải lịch cúp điện cho **{bureau_name}**..."
    )
    
    outages = fetch_npc_outages(bureau_code)
    safe_delete_message(chat_id, call.message.message_id)
    
    if outages is None:
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("🔎 Thử lại", callback_data=f"npc_bur_{bureau_code}"),
            InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data=f"npc_comp_{company_code}" if company_code else "region_NPC")
        )
        bot.send_message(
            chat_id,
            "❌ Hệ thống EVN miền Bắc hiện tại đang bận hoặc không phản hồi. Vui lòng bấm thử lại:",
            reply_markup=markup
        )
        return
        
    if not outages:
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("🔎 Tìm kiếm tiếp", callback_data="region_NPC"),
            InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"npc_bur_{bureau_code}")
        )
        today = datetime.datetime.now()
        bot.send_message(
            chat_id,
            f"ℹ️ Không có thông tin lịch cúp điện từ ngày **{today.strftime('%d-%m-%Y')}** đến **{(today + datetime.timedelta(days=15)).strftime('%d-%m-%Y')}**.",
            parse_mode="Markdown",
            reply_markup=markup
        )
        return
        
    communes = {}
    for item in outages:
        comm = extract_commune(item['location'])
        if comm not in communes:
            communes[comm] = []
        communes[comm].append(item)
        
    sorted_communes = sorted(list(communes.keys()))
    
    USER_SESSIONS[chat_id] = {
        'region': 'NPC',
        'bureau_code': bureau_code,
        'bureau_name': bureau_name,
        'company_code': company_code,
        'company_name': company_name,
        'outages': outages,
        'communes': communes
    }
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for c_name in sorted_communes:
        label = f"📍 {c_name}"
        if len(label) > 30:
            label = label[:27] + "..."
        buttons.append(InlineKeyboardButton(label, callback_data=f"npc_ward_{c_name}"))
    markup.add(*buttons)
    
    markup.row(
        InlineKeyboardButton("🌟 Tất cả khu vực", callback_data="npc_ward_all"),
        InlineKeyboardButton("🔍 Tìm theo từ khóa", callback_data="manual_kw_init")
    )
    markup.row(InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data=f"npc_comp_{company_code}" if company_code else "region_NPC"))
    
    today = datetime.datetime.now()
    welcome_text = (
        f"📅 **LỊCH CÚP ĐIỆN {bureau_name.upper()}**\n"
        f"Từ ngày {today.strftime('%d-%m-%Y')} đến {(today + datetime.timedelta(days=15)).strftime('%d-%m-%Y')}\n"
        f"Tìm thấy {len(outages)} lịch cúp điện.\n\n"
        f"Vui lòng chọn xã/phường dưới đây để lọc tin nhắn hoặc chọn 'Tất cả khu vực':"
    )
    bot.send_message(chat_id, welcome_text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('npc_ward_'))
def handle_npc_ward_select(call):
    commune_name = call.data.replace('npc_ward_', '')
    chat_id = call.message.chat.id
    
    safe_answer_callback(call.id)
    safe_delete_message(chat_id, call.message.message_id)
    
    session_data = USER_SESSIONS.get(chat_id)
    if not session_data or session_data.get('region') != 'NPC':
        bot.send_message(chat_id, "❌ Phiên làm việc hết hạn. Vui lòng gửi /start để thực hiện lại.")
        return
        
    outages = session_data['outages']
    communes = session_data['communes']
    bureau_code = session_data['bureau_code']
    
    today = datetime.datetime.now()
    tu_ngay = today.strftime("%d-%m-%Y")
    den_ngay = (today + datetime.timedelta(days=15)).strftime("%d-%m-%Y")
    
    if commune_name == 'all':
        title = ""
        items_to_format = outages
    else:
        title = f" [{commune_name.upper()}]"
        items_to_format = communes.get(commune_name, [])
        
    formatted_messages = format_outage_messages(items_to_format, tu_ngay, den_ngay, title)
    
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔎 Tìm kiếm tiếp", callback_data="region_NPC"),
        InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"npc_ref_reload_{bureau_code}_{commune_name}")
    )
    
    for msg_chunk in formatted_messages[:-1]:
        bot.send_message(chat_id, msg_chunk, parse_mode="Markdown")
    bot.send_message(chat_id, formatted_messages[-1], parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('npc_ref_reload_'))
def handle_npc_ref_reload(call):
    parts = call.data.split('_')
    bureau_code = parts[3]
    commune_name = parts[4]
    chat_id = call.message.chat.id
    
    safe_answer_callback(call.id, text="Đang làm mới lịch cúp điện...")
    safe_delete_message(chat_id, call.message.message_id)
    
    loading_msg = bot.send_message(chat_id, "🔄 Đang tải lại lịch cúp điện mới nhất...")
    
    outages = fetch_npc_outages(bureau_code)
    safe_delete_message(chat_id, loading_msg.message_id)
    
    bureau_name = bureau_code
    company_name = "EVN miền Bắc"
    company_code = ""
    for c_code, burs in npc_bureaus_dict.items():
        if bureau_code in burs:
            bureau_name = burs[bureau_code]
            company_code = c_code
            company_name = companies_dict_npc.get(c_code, "Công ty Điện lực")
            break
            
    if not outages:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔎 Tìm kiếm tiếp", callback_data="region_NPC"))
        bot.send_message(chat_id, "ℹ️ Hiện tại không có lịch cúp điện dự kiến.", reply_markup=markup)
        return
        
    communes = {}
    for item in outages:
        comm = extract_commune(item['location'])
        if comm not in communes:
            communes[comm] = []
        communes[comm].append(item)
        
    USER_SESSIONS[chat_id] = {
        'region': 'NPC',
        'bureau_code': bureau_code,
        'bureau_name': bureau_name,
        'company_code': company_code,
        'company_name': company_name,
        'outages': outages,
        'communes': communes
    }
    
    today = datetime.datetime.now()
    tu_ngay = today.strftime("%d-%m-%Y")
    den_ngay = (today + datetime.timedelta(days=15)).strftime("%d-%m-%Y")
    
    if commune_name == 'all':
        title = ""
        items_to_format = outages
    else:
        title = f" [{commune_name.upper()}]"
        items_to_format = communes.get(commune_name, [])
        
    formatted_messages = format_outage_messages(items_to_format, tu_ngay, den_ngay, title)
    
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔎 Tìm kiếm tiếp", callback_data="region_NPC"),
        InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"npc_ref_reload_{bureau_code}_{commune_name}")
    )
    
    for msg_chunk in formatted_messages[:-1]:
        bot.send_message(chat_id, msg_chunk, parse_mode="Markdown")
    bot.send_message(chat_id, formatted_messages[-1], parse_mode="Markdown", reply_markup=markup)

# NPC SUBSCRIPTION FLOW
@bot.callback_query_handler(func=lambda call: call.data == 'sub_region_NPC')
def handle_sub_region_npc(call):
    safe_answer_callback(call.id)
    text = (
        "🔔 **Đăng ký thông báo - EVN miền Bắc (NPC)**\n\n"
        "Hãy chọn **Công ty Điện lực (Tỉnh/Thành phố)**:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(companies_dict_npc.items()):
        display_name = name.replace("Công ty Điện lực ", "").replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"sub_npc_comp_{code}"))
    markup.add(*buttons)
    markup.row(InlineKeyboardButton("⬅️ Quay lại", callback_data="notify_subscribe"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_npc_comp_'))
def handle_sub_npc_company(call):
    company_code = call.data.split('_')[3]
    company_name = companies_dict_npc.get(company_code, "Công ty Điện lực")
    
    safe_answer_callback(call.id, text="Đang tải danh sách Điện lực...")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔄 Đang tải các đơn vị Điện lực thuộc **{company_name}**..."
    )
    
    bureaus = npc_bureaus_dict.get(company_code, {})
    if not bureaus:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Chọn lại tỉnh thành", callback_data="sub_region_NPC"))
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"❌ Không tìm thấy đơn vị Điện lực nào trực thuộc **{company_name}**.",
            reply_markup=markup
        )
        return
        
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(bureaus.items(), key=lambda item: item[1]):
        display_name = name.replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"sub_npc_bur_{company_code}_{code}"))
    markup.add(*buttons)
    markup.row(InlineKeyboardButton("⬅️ Chọn tỉnh thành khác", callback_data="sub_region_NPC"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"📍 Bạn đã chọn: **{company_name}**.\n\nHãy chọn **Điện lực trực thuộc** để nhận thông báo:",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_npc_bur_'))
def handle_sub_npc_bureau(call):
    parts = call.data.split('_')
    company_code = parts[3]
    bureau_code = parts[4]
    company_name = companies_dict_npc.get(company_code, "Công ty Điện lực")
    
    safe_answer_callback(call.id, text="Đang tải lịch và phân nhóm...")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🔄 Đang tải lịch cúp điện và phân nhóm theo **xã/phường/khu vực**..."
    )
    
    outages = fetch_npc_outages(bureau_code)
    bureau_name = bureau_code
    bureaus = npc_bureaus_dict.get(company_code, {})
    if bureau_code in bureaus:
        bureau_name = bureaus[bureau_code]
        
    if not outages:
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data=f"sub_npc_comp_{company_code}"),
            InlineKeyboardButton("🔄 Thử lại", callback_data=f"sub_npc_bur_{company_code}_{bureau_code}")
        )
        USER_SESSIONS[call.message.chat.id] = {
            'sub_region': 'NPC',
            'sub_company_code': company_code,
            'sub_company_name': company_name,
            'sub_bureau_code': bureau_code,
            'sub_bureau_name': bureau_name,
            'sub_communes': {},
            'sub_commune_list': []
        }
        markup.row(
            InlineKeyboardButton("🌟 Đăng ký nhận toàn bộ huyện (Tất cả)", callback_data="sub_npc_comm_all"),
            InlineKeyboardButton("🔍 Đăng ký theo từ khóa tự chọn", callback_data="sub_kw_init")
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="ℹ️ Hiện tại không có lịch cúp điện dự kiến cho Điện lực này trong 15 ngày tới, nhưng bạn vẫn có thể đăng ký nhận tin nhắn hằng ngày cho toàn bộ huyện hoặc đăng ký theo từ khóa.",
            reply_markup=markup
        )
        return
        
    communes = {}
    for item in outages:
        comm = extract_commune(item['location'])
        if comm not in communes:
            communes[comm] = []
        communes[comm].append(item)
        
    sorted_commune_list = sorted(communes.keys())
    USER_SESSIONS[call.message.chat.id] = {
        'sub_region': 'NPC',
        'sub_company_code': company_code,
        'sub_company_name': company_name,
        'sub_bureau_code': bureau_code,
        'sub_bureau_name': bureau_name,
        'sub_communes': communes,
        'sub_commune_list': sorted_commune_list
    }
    
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for idx, name in enumerate(sorted_commune_list):
        buttons.append(InlineKeyboardButton(f"{name} ({len(communes[name])})", callback_data=f"sub_npc_comm_{idx}"))
    markup.add(*buttons)
    markup.row(InlineKeyboardButton("🌟 Đăng ký toàn bộ huyện (Tất cả)", callback_data="sub_npc_comm_all"))
    markup.row(
        InlineKeyboardButton("🔍 Đăng ký theo từ khóa tự chọn", callback_data="sub_kw_init"),
        InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data=f"sub_npc_comp_{company_code}")
    )
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔔 **Đăng ký thông báo - {bureau_name}**\n\nChọn **Xã/Phường/Thị trấn** cụ thể để nhận thông báo:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_npc_comm_'))
def handle_sub_npc_commune_select(call):
    chat_id = call.message.chat.id
    safe_answer_callback(call.id)
    
    session_data = USER_SESSIONS.get(chat_id)
    if not session_data or session_data.get('sub_region') != 'NPC':
        bot.send_message(chat_id, "❌ Phiên làm việc hết hạn. Vui lòng thử lại.")
        return
        
    comm_idx_str = call.data.replace('sub_npc_comm_', '')
    if comm_idx_str == 'all':
        ward_key = 'all'
        ward_name = 'Tất cả'
    else:
        try:
            idx = int(comm_idx_str)
            ward_key = session_data['sub_commune_list'][idx]
            ward_name = ward_key
        except Exception:
            bot.send_message(chat_id, "❌ Lựa chọn không hợp lệ.")
            return
            
    save_daily_subscription(
        call,
        session_data['sub_company_code'],
        session_data['sub_company_name'],
        session_data['sub_bureau_code'],
        session_data['sub_bureau_name'],
        ward_key,
        ward_name
    )
    
    text = (
        "✅ **Đã đăng ký thông báo hằng ngày thành công!**\n\n"
        f"Khu vực: **{ward_name}**\n"
        f"Đơn vị: **{session_data['sub_bureau_name']}**\n"
        f"Giờ gửi: **{DAILY_NOTIFY_TIME}** mỗi ngày\n\n"
        "Bot sẽ tự động cập nhật lịch cúp điện mới nhất và thông báo cho bạn hằng ngày."
    )
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=build_notification_actions_markup()
    )


# SUBSCRIPTION HANDLERS

# REGIONAL SUBSCRIPTION HANDLERS
# EVN miền Nam (SPC) SUBSCRIPTION FLOW
@bot.callback_query_handler(func=lambda call: call.data == 'menu_notify')
def handle_menu_notify(call):
    safe_answer_callback(call.id)
    text = (
        "🔔 **Thông báo lịch cúp điện hằng ngày**\n\n"
        f"Bot sẽ gửi lịch lúc **{DAILY_NOTIFY_TIME}** mỗi ngày theo xã/phường/khu vực bạn đăng ký."
    )
    markup = build_notification_menu_markup()
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == 'notify_subscribe')
def handle_notify_subscribe(call):
    safe_answer_callback(call.id)
    text = (
        "🔔 **Đăng ký thông báo hằng ngày**\n\n"
        "Vui lòng chọn khu vực bạn muốn đăng ký:"
    )
    markup = InlineKeyboardMarkup(row_width=1)
    btn_spc = InlineKeyboardButton("⚡ EVN miền Nam (SPC)", callback_data="sub_region_SPC")
    btn_cpc = InlineKeyboardButton("⚡ EVN miền Trung (CPC)", callback_data="sub_region_CPC")
    btn_npc = InlineKeyboardButton("⚡ EVN miền Bắc (NPC)", callback_data="sub_region_NPC")
    btn_back = InlineKeyboardButton("⬅️ Quản lý thông báo", callback_data="menu_notify")
    markup.add(btn_spc, btn_cpc, btn_npc, btn_back)
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == 'sub_region_SPC')
def handle_sub_region_spc(call):
    safe_answer_callback(call.id)
    text = (
        "🔔 **Đăng ký thông báo - EVN miền Nam (SPC)**\n\n"
        "Hãy chọn **Công ty Điện lực (Tỉnh/Thành phố)**:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(companies_dict_spc.items()):
        display_name = name.replace("Công ty Điện lực ", "").replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"sub_spc_comp_{code}"))
    markup.add(*buttons)
    markup.row(InlineKeyboardButton("⬅️ Quay lại", callback_data="notify_subscribe"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_spc_comp_'))
def handle_sub_spc_company(call):
    company_code = call.data.split('_')[3]
    company_name = companies_dict_spc.get(company_code, "Công ty Điện lực")
    
    safe_answer_callback(call.id, text="Đang tải danh sách Điện lực...")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔄 Đang tải các đơn vị Điện lực thuộc **{company_name}**..."
    )
    
    bureaus = fetch_spc_bureaus(company_code)
    if not bureaus:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Chọn lại tỉnh thành", callback_data="sub_region_SPC"))
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"❌ Không tìm thấy đơn vị Điện lực nào trực thuộc **{company_name}**.",
            reply_markup=markup
        )
        return
        
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(bureaus.items(), key=lambda item: item[1]):
        display_name = name.replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"sub_spc_bur_{company_code}_{code}"))
    markup.add(*buttons)
    markup.row(InlineKeyboardButton("⬅️ Chọn tỉnh thành khác", callback_data="sub_region_SPC"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"📍 Bạn đã chọn: **{company_name}**.\n\nHãy chọn **Điện lực huyện/thành phố** để nhận thông báo:",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_spc_bur_'))
def handle_sub_spc_bureau(call):
    parts = call.data.split('_')
    company_code = parts[3]
    bureau_code = parts[4]
    company_name = companies_dict_spc.get(company_code, "Công ty Điện lực")
    
    safe_answer_callback(call.id, text="Đang tải lịch và phân nhóm...")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🔄 Đang tải lịch cúp điện và phân nhóm theo **xã/phường/khu vực**..."
    )
    
    outages = fetch_raw_outages_spc(bureau_code)
    bureau_name = bureau_code
    # Try to find bureau name
    bureaus = fetch_spc_bureaus(company_code)
    if bureau_code in bureaus:
        bureau_name = bureaus[bureau_code]
        
    if not outages:
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data=f"sub_spc_comp_{company_code}"),
            InlineKeyboardButton("🔄 Thử lại", callback_data=f"sub_spc_bur_{company_code}_{bureau_code}")
        )
        USER_SESSIONS[call.message.chat.id] = {
            'sub_region': 'SPC',
            'sub_company_code': company_code,
            'sub_company_name': company_name,
            'sub_bureau_code': bureau_code,
            'sub_bureau_name': bureau_name,
            'sub_communes': {},
            'sub_commune_list': []
        }
        markup.row(
            InlineKeyboardButton("🌟 Đăng ký nhận toàn bộ huyện (Tất cả)", callback_data="sub_spc_comm_all"),
            InlineKeyboardButton("🔍 Đăng ký theo từ khóa tự chọn", callback_data="sub_kw_init")
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="ℹ️ Hiện tại không có lịch cúp điện dự kiến cho Điện lực này trong 7 ngày tới, nhưng bạn vẫn có thể đăng ký nhận tin nhắn hằng ngày cho toàn bộ huyện hoặc đăng ký theo từ khóa.",
            reply_markup=markup
        )
        return
        
    communes = {}
    for item in outages:
        comm = extract_commune(item['location'])
        if comm not in communes:
            communes[comm] = []
        communes[comm].append(item)
        
    sorted_commune_list = sorted(communes.keys())
    USER_SESSIONS[call.message.chat.id] = {
        'sub_region': 'SPC',
        'sub_company_code': company_code,
        'sub_company_name': company_name,
        'sub_bureau_code': bureau_code,
        'sub_bureau_name': bureau_name,
        'sub_communes': communes,
        'sub_commune_list': sorted_commune_list
    }
    
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for idx, name in enumerate(sorted_commune_list):
        buttons.append(InlineKeyboardButton(f"{name} ({len(communes[name])})", callback_data=f"sub_spc_comm_{idx}"))
    markup.add(*buttons)
    markup.row(InlineKeyboardButton("🌟 Đăng ký toàn bộ huyện (Tất cả)", callback_data="sub_spc_comm_all"))
    markup.row(
        InlineKeyboardButton("🔍 Đăng ký theo từ khóa tự chọn", callback_data="sub_kw_init"),
        InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data=f"sub_spc_comp_{company_code}")
    )
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔔 **Đăng ký thông báo - {bureau_name}**\n\nChọn **Xã/Phường/Thị trấn** cụ thể để nhận thông báo:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_spc_comm_'))
def handle_sub_spc_commune_select(call):
    chat_id = call.message.chat.id
    safe_answer_callback(call.id)
    
    session_data = USER_SESSIONS.get(chat_id)
    if not session_data or session_data.get('sub_region') != 'SPC':
        bot.send_message(chat_id, "❌ Phiên làm việc hết hạn. Vui lòng thử lại.")
        return
        
    comm_idx_str = call.data.replace('sub_spc_comm_', '')
    if comm_idx_str == 'all':
        ward_key = 'all'
        ward_name = 'Tất cả'
    else:
        try:
            idx = int(comm_idx_str)
            ward_key = session_data['sub_commune_list'][idx]
            ward_name = ward_key
        except Exception:
            bot.send_message(chat_id, "❌ Lựa chọn không hợp lệ.")
            return
            
    subscription_state = save_daily_subscription(
        call,
        session_data['sub_company_code'],
        session_data['sub_company_name'],
        session_data['sub_bureau_code'],
        session_data['sub_bureau_name'],
        ward_key,
        ward_name
    )
    
    text = (
        "✅ **Đã đăng ký thông báo hằng ngày thành công!**\n\n"
        f"Khu vực: **{ward_name}**\n"
        f"Đơn vị: **{session_data['sub_bureau_name']}**\n"
        f"Giờ gửi: **{DAILY_NOTIFY_TIME}** mỗi ngày\n\n"
        "Bot sẽ tự động cập nhật lịch cúp điện mới nhất và thông báo cho bạn hằng ngày."
    )
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=build_notification_actions_markup()
    )

# CPC SUBSCRIPTION FLOW
@bot.callback_query_handler(func=lambda call: call.data == 'sub_region_CPC')
def handle_sub_region_cpc(call):
    safe_answer_callback(call.id, text="Đang tải danh sách tỉnh thành...")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🔄 Đang tải các tỉnh thành miền Trung..."
    )
    
    companies = fetch_cpc_companies()
    if not companies:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Quay lại", callback_data="notify_subscribe"))
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="❌ Không thể tải danh sách tỉnh thành EVN miền Trung lúc này.",
            reply_markup=markup
        )
        return
        
    text = (
        "🔔 **Đăng ký thông báo - EVN miền Trung (CPC)**\n\n"
        "Hãy chọn **Công ty Điện lực (Tỉnh/Thành phố)**:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(companies.items()):
        display_name = name.replace("Công ty Điện lực ", "").replace("Điện lực ", "").replace("Công ty CP Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"sub_cpc_comp_{code}"))
    markup.add(*buttons)
    markup.row(InlineKeyboardButton("⬅️ Quay lại", callback_data="notify_subscribe"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_cpc_comp_'))
def handle_sub_cpc_company(call):
    company_code = call.data.split('_')[3]
    safe_answer_callback(call.id, text="Đang tải danh sách Điện lực...")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🔄 Đang tải các đơn vị Điện lực huyện..."
    )
    
    # We fetch company name from API
    companies = fetch_cpc_companies()
    company_name = companies.get(company_code, "Công ty Điện lực miền Trung")
    
    bureaus = fetch_cpc_bureaus(company_code)
    if not bureaus:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Chọn lại tỉnh thành", callback_data="sub_region_CPC"))
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="❌ Không tìm thấy đơn vị Điện lực nào thuộc Công ty này.",
            reply_markup=markup
        )
        return
        
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(bureaus.items(), key=lambda item: item[1]):
        display_name = name.replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"sub_cpc_bur_{company_code}_{code}"))
    markup.add(*buttons)
    markup.row(InlineKeyboardButton("⬅️ Chọn tỉnh thành khác", callback_data="sub_region_CPC"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"📍 Bạn đã chọn: **{company_name}**.\n\nHãy chọn **Điện lực huyện/thành phố** để đăng ký nhận thông báo:",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_cpc_bur_'))
def handle_sub_cpc_bureau(call):
    parts = call.data.split('_')
    company_code = parts[3]
    bureau_code = parts[4]
    
    safe_answer_callback(call.id)
    
    companies = fetch_cpc_companies()
    company_name = companies.get(company_code, "Công ty Điện lực")
    
    bureaus = fetch_cpc_bureaus(company_code)
    bureau_name = bureaus.get(bureau_code, bureau_code)
    
    USER_SESSIONS[call.message.chat.id] = {
        'sub_region': 'CPC',
        'sub_company_code': company_code,
        'sub_company_name': company_name,
        'sub_bureau_code': bureau_code,
        'sub_bureau_name': bureau_name,
        'sub_communes': {},
        'sub_commune_list': []
    }
    
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("🌟 Đăng ký toàn bộ huyện (Tất cả)", callback_data="sub_cpc_comm_all"),
        InlineKeyboardButton("🔍 Đăng ký theo từ khóa tự chọn", callback_data="sub_kw_init"),
        InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data=f"sub_cpc_comp_{company_code}")
    )
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔔 **Đăng ký thông báo - {bureau_name}**\n\nBạn muốn nhận thông báo cho toàn bộ huyện hay lọc theo từ khóa tự chọn?",
        reply_markup=markup
    )

# UTILITY SUBSCRIPTION MANAGEMENT
@bot.callback_query_handler(func=lambda call: call.data == 'notify_list')
def handle_notify_list(call):
    safe_answer_callback(call.id)
    subscriptions = get_active_subscriptions(call.message.chat.id)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=format_subscription_list(subscriptions),
        parse_mode="Markdown",
        reply_markup=build_notification_actions_markup()
    )

@bot.callback_query_handler(func=lambda call: call.data == 'notify_disable_menu')
def handle_notify_disable_menu(call):
    safe_answer_callback(call.id)
    subscriptions = get_active_subscriptions(call.message.chat.id)
    text = (
        "🔕 **Tắt thông báo hằng ngày**\n\n"
        "Chọn khu vực muốn tắt:"
        if subscriptions
        else "ℹ️ Bạn chưa có thông báo hằng ngày nào đang bật."
    )
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=build_disable_subscriptions_markup(subscriptions)
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('disable_sub_'))
def handle_disable_sub(call):
    try:
        sub_id = int(call.data.split('_')[2])
    except ValueError:
        safe_answer_callback(call.id, text="Yêu cầu không hợp lệ.")
        return
        
    disabled = disable_subscription(sub_id, call.message.chat.id)
    safe_answer_callback(call.id, text="Đã tắt thông báo." if disabled else "Thất bại.")
    subscriptions = get_active_subscriptions(call.message.chat.id)
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="✅ Đã tắt nhận thông báo khu vực này.\n\n" + format_subscription_list(subscriptions),
        parse_mode="Markdown",
        reply_markup=build_notification_actions_markup()
    )

@bot.callback_query_handler(func=lambda call: call.data == 'disable_all_subs')
def handle_disable_all_subs(call):
    count = disable_all_subscriptions(call.message.chat.id)
    safe_answer_callback(call.id, text=f"Đã tắt {count} thông báo.")
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Đã tắt toàn bộ **{count}** thông báo hằng ngày của bạn.",
        reply_markup=build_notification_actions_markup()
    )

# FEEDBACK HANDLERS
@bot.callback_query_handler(func=lambda call: call.data == 'menu_feedback')
def handle_menu_feedback(call):
    safe_answer_callback(call.id)
    bot.clear_step_handler_by_chat_id(chat_id=call.message.chat.id)
    safe_delete_message(call.message.chat.id, call.message.message_id)

    msg = bot.send_message(
        call.message.chat.id,
        "💬 Vui lòng nhập góp ý cải thiện bot.\n\n"
        "Nội dung tối đa 2000 ký tự. Gửi /start để hủy."
    )
    bot.register_next_step_handler(msg, process_feedback_step)

def process_feedback_step(message):
    text = message.text.strip() if message.text else ""

    if text.startswith('/'):
        bot.clear_step_handler_by_chat_id(chat_id=message.chat.id)
        if text.startswith('/start') or text.startswith('/help'):
            send_welcome(message)
        return

    if len(text) < 5:
        msg = bot.reply_to(
            message,
            "❌ Nội dung góp ý quá ngắn. Vui lòng nhập rõ hơn hoặc gửi /start để hủy."
        )
        bot.register_next_step_handler(msg, process_feedback_step)
        return

    if len(text) > 2000:
        msg = bot.reply_to(
            message,
            "❌ Nội dung góp ý quá dài. Vui lòng rút gọn còn tối đa 2000 ký tự."
        )
        bot.register_next_step_handler(msg, process_feedback_step)
        return

    if count_user_feedbacks_today(message.from_user.id) >= 5:
        bot.reply_to(
            message,
            "⚠️ Bạn đã gửi 5 góp ý hôm nay. Vui lòng thử lại vào ngày mai."
        )
        return

    feedback = save_feedback(message, text)
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("💬 Gửi góp ý khác", callback_data="menu_feedback"),
        InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main")
    )
    bot.send_message(
        message.chat.id,
        "✅ Đã ghi nhận góp ý của bạn. Cảm ơn bạn đã giúp cải thiện bot.",
        reply_markup=markup
    )
    notify_admin_new_feedback(feedback)

def require_admin_callback(call):
    if is_admin_user(call.from_user.id):
        return True
    safe_answer_callback(call.id, text="Bạn không có quyền dùng chức năng này.")
    return False

@bot.callback_query_handler(func=lambda call: call.data in ['fb_list_unread', 'fb_list_all'])
def handle_feedback_list(call):
    if not require_admin_callback(call):
        return

    safe_answer_callback(call.id)
    unread_only = call.data == 'fb_list_unread'
    feedbacks = list_feedbacks(status='unread' if unread_only else None, limit=10)
    title = "📋 Góp ý chưa đọc" if unread_only else "📜 Tất cả góp ý gần đây"
    text = title
    if not feedbacks:
        text += "\n\nKhông có góp ý nào."
    else:
        text += "\n\nChọn một góp ý để xem chi tiết:"

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=build_feedback_list_markup(feedbacks)
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('fb_view_'))
def handle_feedback_view(call):
    if not require_admin_callback(call):
        return

    try:
        feedback_id = int(call.data.split('_')[2])
    except ValueError:
        safe_answer_callback(call.id, text="Góp ý không hợp lệ.")
        return

    feedback = get_feedback(feedback_id)
    safe_answer_callback(call.id)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=format_feedback_detail(feedback),
        reply_markup=build_admin_feedback_markup(feedback_id)
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('fb_read_'))
def handle_feedback_read(call):
    if not require_admin_callback(call):
        return

    try:
        feedback_id = int(call.data.split('_')[2])
    except ValueError:
        safe_answer_callback(call.id, text="Góp ý không hợp lệ.")
        return

    marked = mark_feedback_read(feedback_id)
    feedback = get_feedback(feedback_id)
    safe_answer_callback(call.id, text="Đã đánh dấu đã đọc." if marked else "Không tìm thấy góp ý.")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=format_feedback_detail(feedback),
        reply_markup=build_admin_feedback_markup(feedback_id)
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('fb_reply_'))
def handle_feedback_reply(call):
    if not require_admin_callback(call):
        return

    try:
        feedback_id = int(call.data.split('_')[2])
    except ValueError:
        safe_answer_callback(call.id, text="Góp ý không hợp lệ.")
        return

    feedback = get_feedback(feedback_id)
    if not feedback:
        safe_answer_callback(call.id, text="Không tìm thấy góp ý.")
        return

    safe_answer_callback(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        f"↩️ Nhập nội dung phản hồi cho góp ý #{feedback_id}.\n\nGửi /start để hủy."
    )
    bot.register_next_step_handler(msg, lambda message: process_admin_feedback_reply_step(message, feedback_id))

def process_admin_feedback_reply_step(message, feedback_id):
    if not is_admin_user(message.from_user.id):
        return

    text = message.text.strip() if message.text else ""
    if text.startswith('/'):
        bot.clear_step_handler_by_chat_id(chat_id=message.chat.id)
        if text.startswith('/start') or text.startswith('/help'):
            send_welcome(message)
        return

    if len(text) < 2:
        msg = bot.reply_to(message, "❌ Nội dung phản hồi quá ngắn. Vui lòng nhập lại hoặc gửi /start để hủy.")
        bot.register_next_step_handler(msg, lambda next_message: process_admin_feedback_reply_step(next_message, feedback_id))
        return

    if len(text) > 2000:
        msg = bot.reply_to(message, "❌ Nội dung phản hồi quá dài. Vui lòng rút gọn còn tối đa 2000 ký tự.")
        bot.register_next_step_handler(msg, lambda next_message: process_admin_feedback_reply_step(next_message, feedback_id))
        return

    feedback = get_feedback(feedback_id)
    if not feedback:
        bot.reply_to(message, "❌ Không tìm thấy góp ý.")
        return

    try:
        user_markup = InlineKeyboardMarkup(row_width=1)
        user_markup.add(
            InlineKeyboardButton("💬 Góp ý cải thiện", callback_data="menu_feedback"),
            InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main")
        )
        bot.send_message(
            feedback['chat_id'],
            f"📩 Phản hồi từ admin:\n\n{text}",
            reply_markup=user_markup
        )
    except Exception as e:
        bot.reply_to(message, f"❌ Không gửi được phản hồi cho người dùng: {e}")
        return

    save_feedback_reply(feedback_id, text)
    bot.reply_to(message, f"✅ Đã gửi phản hồi cho góp ý #{feedback_id}.")

# NOTIFICATION SCHEDULER
def send_schedule_result(chat_id, schedule_data, markup):
    if isinstance(schedule_data, list):
        for msg_chunk in schedule_data[:-1]:
            bot.send_message(chat_id, msg_chunk, parse_mode="Markdown")
        bot.send_message(chat_id, schedule_data[-1], parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(chat_id, schedule_data, parse_mode="Markdown", reply_markup=markup)

def add_daily_notification_header(schedule_data):
    header = "🔔 **THÔNG BÁO LỊCH CÚP ĐIỆN HẰNG NGÀY**\n\n"
    if isinstance(schedule_data, list):
        messages = schedule_data[:]
        messages[0] = header + messages[0]
        return messages
    return header + schedule_data

def run_due_daily_notifications():
    if not bot:
        return
    if not NOTIFICATION_SEND_LOCK.acquire(blocking=False):
        return

    try:
        now = now_vietnam()
        today_text = now.date().isoformat()
        due_subscriptions = get_due_subscriptions(now.strftime("%H:%M"), today_text)
        if not due_subscriptions:
            return

        schedule_cache = {}
        for sub in due_subscriptions:
            bureau_code = sub['bureau_code']
            company_code = sub['company_code']
            cache_key = (company_code, bureau_code)
            
            # Determine region SPC vs CPC vs NPC
            org_code = get_cpc_org_code(bureau_code)
            is_cpc = org_code is not None
            is_npc = company_code in companies_dict_npc or company_code.startswith("PA") or company_code.startswith("PH") or company_code.startswith("PN")
            
            if cache_key not in schedule_cache:
                if is_cpc:
                    # CPC direct fetch
                    items = fetch_cpc_outages_direct(org_code, bureau_code)
                    if items is None:
                        schedule_cache[cache_key] = "Không thể kết nối hoặc hệ thống EVNCPC bận."
                    else:
                        outages = []
                        for it in items:
                            outages.append({
                                'location': it.get('stationName', 'N/A'),
                                'time': f"Từ {it.get('fromDateStr', 'N/A')} - Đến {it.get('toDateStr', 'N/A')}",
                                'reason': it.get('reason', 'N/A')
                            })
                        schedule_cache[cache_key] = outages
                elif is_npc:
                    # NPC fetch
                    schedule_cache[cache_key] = fetch_npc_outages(bureau_code)
                else:
                    # SPC fetch
                    schedule_cache[cache_key] = fetch_raw_outages_spc(bureau_code)

            try:
                source_data = schedule_cache[cache_key]
                if isinstance(source_data, str):
                    schedule_data = (
                        f"⚠️ Không thể lấy lịch cúp điện hôm nay từ hệ thống EVN.\n\n"
                        f"Chi tiết: {source_data}"
                    )
                else:
                    # filter by ward or keyword if needed
                    if sub['ward_key'].startswith('key:'):
                        kw = remove_accents(sub['ward_key'].replace('key:', ''))
                        outages = [item for item in source_data if (kw in remove_accents(item['location']) or kw in remove_accents(item['reason']))]
                    elif not is_cpc and sub['ward_key'] != 'all':
                        outages = [item for item in source_data if extract_commune(item['location']) == sub['ward_key']]
                    else:
                        outages = source_data
                    
                    tu_ngay = now.strftime("%d-%m-%Y")
                    days_ahead = 15 if is_npc else 7
                    den_ngay = (now + datetime.timedelta(days=days_ahead)).strftime("%d-%m-%Y")
                    
                    ward_suffix = f" [{sub['ward_name'].upper()}]" if sub['ward_name'] != 'Tất cả' else ""
                    schedule_data = format_outage_messages(
                        outages,
                        tu_ngay,
                        den_ngay,
                        title_suffix=ward_suffix
                    )

                schedule_data = add_daily_notification_header(schedule_data)
                send_schedule_result(
                    sub['chat_id'],
                    schedule_data,
                    build_notification_actions_markup()
                )
            except Exception as e:
                print(f"Error sending daily notification to chat {sub['chat_id']}: {e}")
            finally:
                mark_subscription_sent(sub['id'], today_text)
    finally:
        NOTIFICATION_SEND_LOCK.release()

@bot.callback_query_handler(func=lambda call: call.data == 'sub_cpc_comm_all')
def handle_sub_cpc_comm_all(call):
    chat_id = call.message.chat.id
    safe_answer_callback(call.id)
    
    session_data = USER_SESSIONS.get(chat_id)
    if not session_data or session_data.get('sub_region') != 'CPC':
        bot.send_message(chat_id, "❌ Phiên làm việc hết hạn. Vui lòng thử lại.")
        return
        
    save_daily_subscription(
        call,
        session_data['sub_company_code'],
        session_data['sub_company_name'],
        session_data['sub_bureau_code'],
        session_data['sub_bureau_name'],
        "all",
        "Tất cả"
    )
    
    text = (
        "✅ **Đã đăng ký thông báo hằng ngày thành công!**\n\n"
        f"Khu vực: **Tất cả các xã/huyện**\n"
        f"Đơn vị: **{session_data['sub_bureau_name']}**\n"
        f"Giờ gửi: **{DAILY_NOTIFY_TIME}** mỗi ngày\n\n"
        "Bot sẽ tự động gửi lịch cúp điện mới nhất hằng ngày cho bạn."
    )
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=build_notification_actions_markup()
    )

@bot.callback_query_handler(func=lambda call: call.data == 'sub_kw_init')
def handle_sub_kw_init(call):
    chat_id = call.message.chat.id
    safe_answer_callback(call.id)
    
    session_data = USER_SESSIONS.get(chat_id)
    if not session_data:
        bot.send_message(chat_id, "❌ Phiên làm việc hết hạn. Vui lòng thử lại.")
        return
        
    bot.clear_step_handler_by_chat_id(chat_id=chat_id)
    safe_delete_message(chat_id, call.message.message_id)
    
    msg = bot.send_message(
        chat_id,
        f"🔍 Bạn đang đăng ký thông báo cho **{session_data['sub_bureau_name']}**.\n\n"
        "Vui lòng nhập từ khóa bạn muốn nhận thông báo (ví dụ: tên đường, thôn, xóm hoặc tên trạm biến áp, v.v.):\n"
        "Ví dụ: `Hà Ra` hoặc `Nguyễn Thái Học` hoặc `Chung cư B`\n\n"
        "Nhập /start để hủy bỏ đăng ký.",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_sub_keyword_step)

def process_sub_keyword_step(message):
    chat_id = message.chat.id
    text = message.text.strip() if message.text else ""
    
    if text.startswith('/'):
        bot.clear_step_handler_by_chat_id(chat_id=chat_id)
        if text.startswith('/start') or text.startswith('/help'):
            send_welcome(message)
        return
        
    if len(text) < 2:
        msg = bot.reply_to(
            message,
            "❌ Từ khóa quá ngắn. Vui lòng nhập từ khóa có ít nhất 2 ký tự hoặc nhập /start để hủy."
        )
        bot.register_next_step_handler(msg, process_sub_keyword_step)
        return
        
    if len(text) > 50:
        msg = bot.reply_to(
            message,
            "❌ Từ khóa quá dài (tối đa 50 ký tự). Vui lòng nhập lại hoặc nhập /start để hủy."
        )
        bot.register_next_step_handler(msg, process_sub_keyword_step)
        return
        
    session_data = USER_SESSIONS.get(chat_id)
    if not session_data:
        bot.send_message(chat_id, "❌ Phiên làm việc hết hạn. Vui lòng gửi /start để thực hiện lại.")
        return
        
    ward_key = f"key:{text}"
    ward_name = f"Từ khóa: {text}"
    
    class MockCall:
        def __init__(self, msg_obj):
            self.message = msg_obj
            self.from_user = msg_obj.from_user
            
    mock_call = MockCall(message)
    
    save_daily_subscription(
        mock_call,
        session_data['sub_company_code'],
        session_data['sub_company_name'],
        session_data['sub_bureau_code'],
        session_data['sub_bureau_name'],
        ward_key,
        ward_name
    )
    
    resp_text = (
        "✅ **Đã đăng ký thông báo hằng ngày thành công!**\n\n"
        f"Đăng ký theo từ khóa: **{text}**\n"
        f"Đơn vị: **{session_data['sub_bureau_name']}**\n"
        f"Giờ gửi: **{DAILY_NOTIFY_TIME}** mỗi ngày\n\n"
        f"Bot sẽ quét lịch cúp điện của **{session_data['sub_bureau_name']}** hằng ngày, nếu địa điểm cúp điện chứa từ khóa **\"{text}\"** thì bot sẽ gửi thông báo cho bạn."
    )
    bot.send_message(
        chat_id,
        resp_text,
        parse_mode="Markdown",
        reply_markup=build_notification_actions_markup()
    )

@bot.callback_query_handler(func=lambda call: call.data == 'cpc_view_all')
def handle_cpc_view_all(call):
    chat_id = call.message.chat.id
    safe_answer_callback(call.id)
    safe_delete_message(chat_id, call.message.message_id)
    
    session_data = USER_SESSIONS.get(chat_id)
    if not session_data or session_data.get('region') != 'CPC':
        bot.send_message(chat_id, "❌ Phiên làm việc hết hạn. Vui lòng gửi /start để thực hiện lại.")
        return
        
    outages = session_data['outages']
    bureau_code = session_data['bureau_code']
    today = datetime.datetime.now()
    
    formatted_messages = format_outage_messages(outages, today.strftime('%d-%m-%Y'), (today + datetime.timedelta(days=7)).strftime('%d-%m-%Y'))
    
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔎 Tìm kiếm tiếp", callback_data="region_CPC"),
        InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"cpc_bur_{bureau_code}")
    )
    
    for msg_chunk in formatted_messages[:-1]:
        bot.send_message(chat_id, msg_chunk, parse_mode="Markdown")
    bot.send_message(chat_id, formatted_messages[-1], parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'manual_kw_init')
def handle_manual_kw_init(call):
    chat_id = call.message.chat.id
    safe_answer_callback(call.id)
    
    session_data = USER_SESSIONS.get(chat_id)
    if not session_data:
        bot.send_message(chat_id, "❌ Phiên làm việc hết hạn. Vui lòng gửi /start để thực hiện lại.")
        return
        
    bot.clear_step_handler_by_chat_id(chat_id=chat_id)
    safe_delete_message(chat_id, call.message.message_id)
    
    msg = bot.send_message(
        chat_id,
        f"🔍 Tra cứu lịch cúp điện theo từ khóa tại **{session_data.get('bureau_name', 'Đơn vị đã chọn')}**.\n\n"
        "Vui lòng nhập từ khóa bạn muốn tìm kiếm (ví dụ: tên đường, thôn, xóm, trạm biến áp, v.v.):\n"
        "Ví dụ: `Mỹ Long` hoặc `Thôn 3` hoặc `Hà Ra`\n\n"
        "Nhập /start để hủy bỏ tra cứu.",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_manual_keyword_step)

def process_manual_keyword_step(message):
    chat_id = message.chat.id
    text = message.text.strip() if message.text else ""
    
    if text.startswith('/'):
        bot.clear_step_handler_by_chat_id(chat_id=chat_id)
        if text.startswith('/start') or text.startswith('/help'):
            send_welcome(message)
        return
        
    if len(text) < 2:
        msg = bot.reply_to(
            message,
            "❌ Từ khóa quá ngắn. Vui lòng nhập từ khóa có ít nhất 2 ký tự hoặc nhập /start để hủy."
        )
        bot.register_next_step_handler(msg, process_manual_keyword_step)
        return
        
    session_data = USER_SESSIONS.get(chat_id)
    if not session_data:
        bot.send_message(chat_id, "❌ Phiên làm việc hết hạn. Vui lòng gửi /start để thực hiện lại.")
        return
        
    outages = session_data['outages']
    region = session_data['region']
    bureau_code = session_data['bureau_code']
    
    clean_keyword = remove_accents(text)
    filtered_outages = []
    for item in outages:
        clean_location = remove_accents(item['location'])
        clean_reason = remove_accents(item['reason'])
        if clean_keyword in clean_location or clean_keyword in clean_reason:
            filtered_outages.append(item)
            
    today = datetime.datetime.now()
    days = 15 if region == 'NPC' else 7
    tu_ngay = today.strftime("%d-%m-%Y")
    den_ngay = (today + datetime.timedelta(days=days)).strftime("%d-%m-%Y")
    
    if not filtered_outages:
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("🔍 Tìm từ khóa khác", callback_data="manual_kw_init"),
            InlineKeyboardButton("⬅️ Quay lại danh sách", callback_data=f"spc_bur_{bureau_code}" if region == 'SPC' else (f"npc_bur_{bureau_code}" if region == 'NPC' else f"cpc_bur_{bureau_code}"))
        )
        bot.send_message(
            chat_id,
            f"ℹ️ Không tìm thấy lịch cúp điện nào khớp với từ khóa **\"{text}\"** tại khu vực này trong những ngày tới.",
            parse_mode="Markdown",
            reply_markup=markup
        )
        return
        
    title = f" [TỪ KHÓA: {text.upper()}]"
    formatted_messages = format_outage_messages(filtered_outages, tu_ngay, den_ngay, title)
    
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔎 Tra cứu tiếp", callback_data=f"region_{region}"),
        InlineKeyboardButton("🔍 Tìm từ khóa khác", callback_data="manual_kw_init")
    )
    
    for msg_chunk in formatted_messages[:-1]:
        bot.send_message(chat_id, msg_chunk, parse_mode="Markdown")
    bot.send_message(chat_id, formatted_messages[-1], parse_mode="Markdown", reply_markup=markup)

def daily_notification_loop():
    while True:
        try:
            run_due_daily_notifications()
        except Exception as e:
            print(f"Error in daily notification scheduler: {e}")
        time.sleep(60)

def start_daily_notification_scheduler():
    worker = threading.Thread(target=daily_notification_loop, daemon=True)
    worker.start()
    print(f"Daily notification scheduler started. Notify time: {DAILY_NOTIFY_TIME}")

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Error: Please specify TELEGRAM_BOT_TOKEN in the .env file and run again.")
    else:
        print("Bot is starting polling...")
        init_db()
        start_daily_notification_scheduler()
        bot.infinity_polling()

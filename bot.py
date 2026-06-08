import os
import json
import datetime
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import urllib.request
import urllib.parse
import ssl
from bs4 import BeautifulSoup
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
    VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    VIETNAM_TZ = datetime.timezone(datetime.timedelta(hours=7))

# Load env variables
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID_RAW = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
ADMIN_TELEGRAM_ID = int(ADMIN_TELEGRAM_ID_RAW) if ADMIN_TELEGRAM_ID_RAW.isdigit() else None

if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
    print("WARNING: TELEGRAM_BOT_TOKEN is not configured in .env file.")
    BOT_TOKEN = None
if ADMIN_TELEGRAM_ID_RAW and ADMIN_TELEGRAM_ID is None:
    print("WARNING: ADMIN_TELEGRAM_ID must be a numeric Telegram user ID.")

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

# Helper function to remove Vietnamese accents for loose matching
def remove_accents(input_str):
    normalized = unicodedata.normalize('NFD', input_str)
    without_marks = ''.join(
        c for c in normalized
        if unicodedata.category(c) != 'Mn'
    )
    return without_marks.replace('Đ', 'D').replace('đ', 'd').lower()

# Load companies list
def load_companies():
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

companies_dict = load_companies()

WARD_SELECTION_CACHE = {}
WARD_CACHE_TTL = datetime.timedelta(minutes=30)
BUREAU_NAME_CACHE = {}
BUREAU_COMPANY_CACHE = {}
AREA_ALIASES_FILE = os.getenv("AREA_ALIASES_FILE", "area_aliases.json")
AREA_ALIAS_CACHE = {
    'mtime': None,
    'data': {}
}
DB_PATH = os.getenv("SUBSCRIPTIONS_DB", "subscriptions.sqlite3")
DAILY_NOTIFY_TIME = os.getenv("DAILY_NOTIFY_TIME", "07:00")
DB_LOCK = threading.RLock()
NOTIFICATION_SEND_LOCK = threading.Lock()
_DB_INITIALIZED = False

def now_vietnam():
    return datetime.datetime.now(VIETNAM_TZ)

def utc_now_text():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

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

def is_admin_user(user_id):
    return ADMIN_TELEGRAM_ID is not None and user_id == ADMIN_TELEGRAM_ID

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

# Fetch local power bureaus for a parent company
def fetch_bureaus(parent_code):
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
                # Exclude placeholder options
                if val and val.strip() and val != "0" and "chọn" not in text.lower():
                    bureaus[val.strip()] = text
            return bureaus
    except Exception as e:
        print(f"Error fetching bureaus: {e}")
        return {}

# Fetch and parse outage schedule for both bureau code and customer code
def fetch_outage_data(code, is_customer=False, company_code=None):
    today = datetime.datetime.now()
    tu_ngay = today.strftime("%d-%m-%Y")
    # Query for the next 7 days
    den_ngay = (today + datetime.timedelta(days=7)).strftime("%d-%m-%Y")
    
    url = "https://www.cskh.evnspc.vn/TraCuu/GetThongTinLichNgungGiamCungCapDien"
    if is_customer:
        params = {
            'maKH': code,
            'tuNgay': tu_ngay,
            'denNgay': den_ngay,
            'ChucNang': 'MaKhachHang'
        }
    else:
        params = {
            'madvi': code,
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
            bureau_code = None if is_customer else code
            return parse_outage_html(html, tu_ngay, den_ngay, company_code=company_code, bureau_code=bureau_code)
    except Exception as e:
        return f"❌ Đã xảy ra lỗi khi lấy dữ liệu từ EVN SPC: {e}"

def fetch_outage_schedule(code, is_customer=False, ward_key=None, ward_name=None, company_code=None):
    schedule_data = fetch_outage_data(code, is_customer=is_customer, company_code=company_code)
    if isinstance(schedule_data, str):
        return schedule_data

    outages = schedule_data['outages']
    if ward_key:
        outages = filter_outages_by_ward(outages, ward_key)

    return format_outage_messages(
        outages,
        schedule_data['tu_ngay'],
        schedule_data['den_ngay'],
        ward_name=ward_name,
        total_source=len(schedule_data['outages']) if ward_key else None
    )

def parse_outage_html(html, tu_ngay, den_ngay, company_code=None, bureau_code=None):
    soup = BeautifulSoup(html, 'html.parser')
    
    outages = []
    
    # 1. Try list-based parsing (structured as div class="entry")
    entries = soup.find_all(class_='entry')
    for entry in entries:
        where_el = entry.find(class_='where')
        time_el = entry.find(class_='time')
        cause_el = entry.find(class_='cause')
        
        if where_el:
            # Extract time text first before decomposing
            time_text = ""
            if time_el:
                time_text = time_el.text.replace('THỜI GIAN:', '').strip()
                # Decompose time_el from where_el so we get clean location
                time_in_where = where_el.find(class_='time')
                if time_in_where:
                    time_in_where.decompose()
            
            location = where_el.text.replace('KHU VỰC:', '').strip()
            
            reason = "Bảo trì định kỳ"
            if cause_el:
                reason = cause_el.text.replace('LÝ DO NGỪNG CUNG CẤP ĐIỆN:', '').replace('LÝ DO:', '').strip()
            
            # Clean double spaces/newlines
            location = " ".join(location.split())
            time_text = " ".join(time_text.split())
            reason = " ".join(reason.split())
            
            outages.append({
                'location': location,
                'time': time_text,
                'reason': reason
            })
            
    # 2. Fallback to table-based parsing if no list entries were found
    if not outages:
        table = soup.find('table')
        if table:
            # Parse table rows
            headers = []
            thead = table.find('thead')
            if thead:
                headers = [remove_accents(th.text.strip()) for th in thead.find_all('th')]
            else:
                first_tr = table.find('tr')
                if first_tr:
                    headers = [remove_accents(td.text.strip()) for td in first_tr.find_all(['td', 'th'])]
            
            col_map = {
                'location': -1,
                'start_time': -1,
                'end_time': -1,
                'reason': -1,
                'time': -1
            }
            
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
                
                # Location
                loc_idx = col_map['location'] if col_map['location'] != -1 else (1 if num_cells > 1 else 0)
                location = cells[loc_idx].text.strip() if loc_idx < num_cells else "N/A"
                
                # Time
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
                        
                # Reason
                reason_idx = col_map['reason'] if col_map['reason'] != -1 else (4 if num_cells > 4 else (num_cells - 1 if num_cells > 1 else -1))
                reason = cells[reason_idx].text.strip() if (reason_idx != -1 and reason_idx < num_cells) else "Bảo trì định kỳ"
                
                # Clean double spaces/newlines
                location = " ".join(location.split())
                time_str = " ".join(time_str.split())
                reason = " ".join(reason.split())
                
                if location and location != "Không có dữ liệu":
                    outages.append({
                        'location': location,
                        'time': time_str,
                        'reason': reason
                    })
                    
    if not outages:
        body_text = soup.get_text()
        if "không có" in body_text.lower() or "không tìm thấy" in body_text.lower() or "không có lịch" in body_text.lower():
            return f"ℹ️ Không có thông tin lịch cúp điện từ ngày **{tu_ngay}** đến **{den_ngay}**."
        return "⚠️ Không có dữ liệu lịch cúp điện hoặc cấu trúc trang web đã thay đổi."

    annotate_outage_wards(outages, company_code=company_code, bureau_code=bureau_code)
    return {
        'tu_ngay': tu_ngay,
        'den_ngay': den_ngay,
        'outages': outages
    }

def normalize_text_key(text):
    text = remove_accents(text)
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return " ".join(text.split())

def load_area_aliases():
    if not os.path.exists(AREA_ALIASES_FILE):
        return {}

    try:
        mtime = os.path.getmtime(AREA_ALIASES_FILE)
        if AREA_ALIAS_CACHE['mtime'] == mtime:
            return AREA_ALIAS_CACHE['data']

        with open(AREA_ALIASES_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        normalized_data = {}
        for company_code, bureau_map in raw_data.items():
            if company_code.startswith("_") or not isinstance(bureau_map, dict):
                continue

            normalized_data[company_code] = {}
            for bureau_code, aliases in bureau_map.items():
                if bureau_code.startswith("_") or not isinstance(aliases, dict):
                    continue

                normalized_data[company_code][bureau_code] = {
                    normalize_text_key(alias): unicodedata.normalize('NFC', display)
                    for alias, display in aliases.items()
                    if alias and display
                }

        AREA_ALIAS_CACHE['mtime'] = mtime
        AREA_ALIAS_CACHE['data'] = normalized_data
        return normalized_data
    except Exception as e:
        print(f"Error loading area aliases from {AREA_ALIASES_FILE}: {e}")
        return {}

def resolve_area_alias(location, company_code=None, bureau_code=None):
    if not company_code or not bureau_code:
        return None

    aliases = load_area_aliases().get(company_code, {}).get(bureau_code, {})
    if not aliases:
        return None

    area_key = normalize_text_key(extract_fallback_area(location))
    for alias_key, display in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if area_key == alias_key or area_key.endswith(f" {alias_key}"):
            return {
                'key': normalize_text_key(display),
                'display': display
            }

    return None

def canonical_ward_prefix(prefix):
    prefix_key = normalize_text_key(prefix)
    if prefix_key in ["xa", "x"]:
        return "Xã"
    if prefix_key in ["phuong", "p"]:
        return "Phường"
    if prefix_key in ["thi tran", "tt"]:
        return "Thị trấn"
    return " ".join(prefix.split()).capitalize()

WARD_REGEX = re.compile(
    r'(xã|xa|x\.|phường|phuong|p\.|thị\s+trấn|thi\s+tran|tt\.)\s+(.+?)(?=(?:\s+(?:thuộc|và|va)\s+|\s*[-,.;:]|$))',
    re.IGNORECASE
)

def extract_wards(location):
    wards = []
    seen_keys = set()

    for match in WARD_REGEX.finditer(location):
        prefix = canonical_ward_prefix(match.group(1))
        name = unicodedata.normalize('NFC', " ".join(match.group(2).strip(" -–,.;:").split()))
        if not name:
            continue

        display = unicodedata.normalize('NFC', f"{prefix} {name}")
        key = normalize_text_key(display)
        if key and key not in seen_keys:
            wards.append({
                'key': key,
                'display': display
            })
            seen_keys.add(key)

    return wards

def extract_fallback_area(location):
    area = " ".join((location or "").split())
    area = re.sub(r'^\s*khu\s+vực\s*:?\s*', '', area, flags=re.IGNORECASE)
    area = re.sub(r'^\s*khu\s+vuc\s*:?\s*', '', area, flags=re.IGNORECASE)
    area = area.strip(" -–,.;:")
    if not area:
        area = "Khu vực chưa rõ"
    return area[:1].upper() + area[1:]

def annotate_outage_wards(outages, company_code=None, bureau_code=None):
    for item in outages:
        wards = extract_wards(item.get('location', ''))
        if not wards:
            alias = resolve_area_alias(item.get('location', ''), company_code=company_code, bureau_code=bureau_code)
            if alias:
                wards = [alias]
            else:
                area = extract_fallback_area(item.get('location', ''))
                wards = [{
                    'key': f"area:{normalize_text_key(area)}",
                    'display': area
                }]
        item['wards'] = wards

def build_ward_options(outages):
    ward_map = {}

    for item in outages:
        wards = item.get('wards', [])

        for ward in wards:
            key = ward['key']
            if key not in ward_map:
                ward_map[key] = {
                    'key': key,
                    'display': ward['display'],
                    'count': 0
                }
            ward_map[key]['count'] += 1

    ward_options = sorted(ward_map.values(), key=lambda item: normalize_text_key(item['display']))
    return ward_options

def filter_outages_by_ward(outages, ward_key):
    return [
        item for item in outages
        if any(ward['key'] == ward_key for ward in item.get('wards', []))
    ]

def cleanup_ward_cache():
    now = datetime.datetime.now()
    expired_tokens = [
        token for token, entry in WARD_SELECTION_CACHE.items()
        if now - entry['created_at'] > WARD_CACHE_TTL
    ]
    for token in expired_tokens:
        WARD_SELECTION_CACHE.pop(token, None)

def create_ward_cache_entry(bureau_code, schedule_data, bureau_name=None, company_code=None, company_name=None):
    cleanup_ward_cache()
    token = secrets.token_hex(4)
    while token in WARD_SELECTION_CACHE:
        token = secrets.token_hex(4)

    wards = build_ward_options(schedule_data['outages'])
    WARD_SELECTION_CACHE[token] = {
        'created_at': datetime.datetime.now(),
        'bureau_code': bureau_code,
        'company_code': company_code,
        'company_name': company_name,
        'bureau_name': bureau_name,
        'tu_ngay': schedule_data['tu_ngay'],
        'den_ngay': schedule_data['den_ngay'],
        'outages': schedule_data['outages'],
        'wards': wards
    }
    return token, wards

def get_ward_cache_entry(token, bureau_code=None):
    cleanup_ward_cache()
    entry = WARD_SELECTION_CACHE.get(token)
    if not entry:
        return None
    if bureau_code and entry['bureau_code'] != bureau_code:
        return None
    return entry

def format_outage_messages(outages, tu_ngay, den_ngay, ward_name=None, total_source=None):
    if not outages:
        if ward_name:
            return f"ℹ️ Không có thông tin lịch cúp điện cho **{ward_name}** từ ngày **{tu_ngay}** đến **{den_ngay}**."
        return f"ℹ️ Không có thông tin lịch cúp điện từ ngày **{tu_ngay}** đến **{den_ngay}**."

    header_msg = f"📅 **LỊCH CÚP ĐIỆN DỰ KIẾN (Từ {tu_ngay} đến {den_ngay})**\n"
    if ward_name:
        header_msg += f"Khu vực: **{ward_name}**\n"
        header_msg += f"Số thông báo trong khu vực: {len(outages)}"
        if total_source is not None:
            header_msg += f" / {total_source}"
        header_msg += "\n"
    else:
        header_msg += f"Tổng số thông báo: {len(outages)}\n"
    header_msg += "=================================\n\n"
    
    result_messages = []
    current_msg = header_msg
    
    for idx, item in enumerate(outages, 1):
        chunk = f"⚡ **{idx}. Địa điểm:** {item['location']}\n"
        chunk += f"⏰ **Thời gian:** {item['time']}\n"
        chunk += f"📝 **Lý do:** {item['reason']}\n"
        chunk += "---------------------------------\n\n"
        
        # Check message length limit (4096)
        if len(current_msg) + len(chunk) > 4000:
            result_messages.append(current_msg)
            current_msg = chunk
        else:
            current_msg += chunk
            
    result_messages.append(current_msg)
    return result_messages


# TELEGRAM BOT HANDLERS

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    # Clear next step handlers if any to avoid state confusion on restart
    bot.clear_step_handler_by_chat_id(chat_id=message.chat.id)
    
    welcome_text = (
        "👋 Chào mừng bạn đến với **Bot Tra cứu Lịch cúp điện EVN SPC**!\n\n"
        "Vui lòng chọn hình thức tra cứu:"
    )
    markup = InlineKeyboardMarkup(row_width=1)
    btn_customer = InlineKeyboardButton("🔍 Tìm kiếm theo Mã khách hàng", callback_data="menu_customer")
    btn_unit = InlineKeyboardButton("🏢 Tìm kiếm theo Đơn vị quản lý", callback_data="menu_unit")
    btn_notify = InlineKeyboardButton("🔔 Nhận thông báo hằng ngày", callback_data="menu_notify")
    btn_feedback = InlineKeyboardButton("💬 Góp ý cải thiện", callback_data="menu_feedback")
    markup.add(btn_customer, btn_unit, btn_notify, btn_feedback)
    if is_admin_user(message.from_user.id):
        markup.add(InlineKeyboardButton("📋 Góp ý chưa đọc", callback_data="fb_list_unread"))
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'back_main')
def handle_back_main(call):
    safe_answer_callback(call.id)
    welcome_text = (
        "👋 Chào mừng bạn đến với **Bot Tra cứu Lịch cúp điện EVN SPC**!\n\n"
        "Vui lòng chọn hình thức tra cứu:"
    )
    markup = InlineKeyboardMarkup(row_width=1)
    btn_customer = InlineKeyboardButton("🔍 Tìm kiếm theo Mã khách hàng", callback_data="menu_customer")
    btn_unit = InlineKeyboardButton("🏢 Tìm kiếm theo Đơn vị quản lý", callback_data="menu_unit")
    btn_notify = InlineKeyboardButton("🔔 Nhận thông báo hằng ngày", callback_data="menu_notify")
    btn_feedback = InlineKeyboardButton("💬 Góp ý cải thiện", callback_data="menu_feedback")
    markup.add(btn_customer, btn_unit, btn_notify, btn_feedback)
    if is_admin_user(call.from_user.id):
        markup.add(InlineKeyboardButton("📋 Góp ý chưa đọc", callback_data="fb_list_unread"))
    
    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        welcome_text,
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == 'menu_customer')
def handle_menu_customer(call):
    safe_answer_callback(call.id)
    bot.delete_message(call.message.chat.id, call.message.message_id)
    
    msg = bot.send_message(
        call.message.chat.id,
        "⌨️ Vui lòng nhập **Mã khách hàng** của bạn (Mã phải bắt đầu bằng **PB, PK hoặc PC** và có **đúng 13 ký tự**).\n\n"
        "👉 Mã khách hàng tham khảo: `PB22030392316`\n\n"
        "*(Hoặc gửi /start để quay lại menu chính)*",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_customer_code_step)

def process_customer_code_step(message):
    text = message.text.strip() if message.text else ""
    
    # If user sends a command, abort wait step and execute command
    if text.startswith('/'):
        bot.clear_step_handler_by_chat_id(chat_id=message.chat.id)
        if text.startswith('/start') or text.startswith('/help'):
            send_welcome(message)
        return
        
    code = text.upper()
    is_valid = len(code) == 13 and (code.startswith("PB") or code.startswith("PK") or code.startswith("PC"))
    
    if not is_valid:
        msg = bot.reply_to(
            message,
            "❌ Mã khách hàng không đúng định dạng!\n"
            "⚠️ Quy định: Mã khách hàng/đơn vị phải bắt đầu bằng **PB, PK hoặc PC** và có **đúng 13 ký tự**.\n\n"
            "Vui lòng nhập lại (hoặc gửi /start để quay lại):",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, process_customer_code_step)
        return
        
    loading_msg = bot.send_message(
        message.chat.id,
        f"🔄 Đang tra cứu lịch cúp điện cho mã khách hàng **{code}**..."
    )
    
    schedule_data = fetch_outage_schedule(code, is_customer=True)
    bot.delete_message(message.chat.id, loading_msg.message_id)
    
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"),
        InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"refresh_cust_{code}")
    )
    
    if isinstance(schedule_data, list):
        for msg_chunk in schedule_data[:-1]:
            bot.send_message(message.chat.id, msg_chunk, parse_mode="Markdown")
        bot.send_message(message.chat.id, schedule_data[-1], parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(message.chat.id, schedule_data, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('refresh_cust_'))
def handle_refresh_customer(call):
    code = call.data.split('_')[2]
    safe_answer_callback(call.id, text=f"Đang làm mới dữ liệu cho {code}...")
    
    bot.delete_message(call.message.chat.id, call.message.message_id)
    
    loading_msg = bot.send_message(
        call.message.chat.id,
        f"🔄 Đang làm mới lịch cúp điện cho mã khách hàng **{code}**..."
    )
    
    schedule_data = fetch_outage_schedule(code, is_customer=True)
    bot.delete_message(call.message.chat.id, loading_msg.message_id)
    
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"),
        InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"refresh_cust_{code}")
    )
    
    if isinstance(schedule_data, list):
        for msg_chunk in schedule_data[:-1]:
            bot.send_message(call.message.chat.id, msg_chunk, parse_mode="Markdown")
        bot.send_message(call.message.chat.id, schedule_data[-1], parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(call.message.chat.id, schedule_data, parse_mode="Markdown", reply_markup=markup)

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

    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        text,
        parse_mode=None,
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
    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        format_feedback_detail(feedback),
        parse_mode=None,
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
    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        format_feedback_detail(feedback),
        parse_mode=None,
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

@bot.callback_query_handler(func=lambda call: call.data == 'menu_notify')
def handle_menu_notify(call):
    safe_answer_callback(call.id)
    show_notification_menu(call.message, is_edit=True)

def show_notification_menu(message, is_edit=False):
    text = (
        "🔔 **Thông báo lịch cúp điện hằng ngày**\n\n"
        f"Bot sẽ gửi lịch lúc **{DAILY_NOTIFY_TIME}** mỗi ngày theo xã/phường/khu vực bạn đăng ký."
    )
    markup = build_notification_menu_markup()

    if is_edit:
        edit_or_send_message(message.chat.id, message.message_id, text, reply_markup=markup)
    else:
        bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'notify_subscribe')
def handle_notify_subscribe(call):
    safe_answer_callback(call.id)
    show_subscription_companies_menu(call.message, is_edit=True)

def show_subscription_companies_menu(message, is_edit=False):
    text = (
        "🔔 **Đăng ký thông báo hằng ngày**\n\n"
        "Hãy chọn **Công ty Điện lực (Tỉnh/Thành phố)**:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(companies_dict.items()):
        display_name = name.replace("Công ty Điện lực ", "").replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"sub_comp_{code}"))

    markup.add(*buttons)
    markup.row(InlineKeyboardButton("⬅️ Quản lý thông báo", callback_data="menu_notify"))
    markup.row(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))

    if is_edit:
        edit_or_send_message(message.chat.id, message.message_id, text, reply_markup=markup)
    else:
        bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_comp_'))
def handle_subscription_company_select(call):
    company_code = call.data.split('_')[2]
    company_name = companies_dict.get(company_code, "Công ty Điện lực")

    safe_answer_callback(call.id, text=f"Đang tải danh sách Điện lực của {company_name}...")
    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        f"🔄 Đang tải các đơn vị Điện lực thuộc **{company_name}**..."
    )

    bureaus = fetch_bureaus(company_code)
    BUREAU_NAME_CACHE.update(bureaus)
    BUREAU_COMPANY_CACHE.update({code: company_code for code in bureaus})
    if not bureaus:
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("⬅️ Chọn lại tỉnh thành", callback_data="notify_subscribe"))
        markup.row(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
        edit_or_send_message(
            call.message.chat.id,
            call.message.message_id,
            f"❌ Không tìm thấy đơn vị Điện lực nào trực thuộc **{company_name}**.",
            reply_markup=markup
        )
        return

    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(bureaus.items(), key=lambda item: item[1]):
        display_name = name.replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"sub_bur_{company_code}_{code}"))

    markup.add(*buttons)
    markup.row(InlineKeyboardButton("⬅️ Chọn tỉnh thành khác", callback_data="notify_subscribe"))
    markup.row(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))

    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        f"📍 Bạn đã chọn: **{company_name}**.\n\nHãy chọn **Điện lực huyện/thành phố** để nhận thông báo:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_bur_'))
def handle_subscription_bureau_select(call):
    parts = call.data.split('_')
    if len(parts) >= 4:
        company_code = None if parts[2].lower() == "none" else parts[2]
        bureau_code = parts[3]
    else:
        bureau_code = parts[2]
        company_code = BUREAU_COMPANY_CACHE.get(bureau_code)
    company_name = companies_dict.get(company_code, "") if company_code else ""
    bureau_name = BUREAU_NAME_CACHE.get(bureau_code, bureau_code)

    safe_answer_callback(call.id, text="Đang tải lịch và phân nhóm khu vực...")
    show_subscription_ward_selection(
        call.message.chat.id,
        company_code,
        company_name,
        bureau_code,
        bureau_name,
        message_id=call.message.message_id
    )

def build_subscription_ward_selection_markup(company_code, bureau_code, token, wards):
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    company_part = company_code or "none"

    for idx, ward in enumerate(wards):
        label = f"{ward['display']} ({ward['count']})"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append(InlineKeyboardButton(label, callback_data=f"sub_ward_{company_part}_{bureau_code}_{token}_{idx}"))

    markup.add(*buttons)
    markup.row(InlineKeyboardButton("🔄 Tải lại danh sách", callback_data=f"sub_bur_{company_part}_{bureau_code}"))
    markup.row(InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data="notify_subscribe"))
    markup.row(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
    return markup

def show_subscription_ward_selection(chat_id, company_code, company_name, bureau_code, bureau_name, message_id=None):
    loading_message = edit_or_send_message(
        chat_id,
        message_id,
        "🔄 Đang tải lịch cúp điện và phân nhóm theo **xã/phường/khu vực**..."
    )
    target_message_id = getattr(loading_message, 'message_id', message_id)
    company_part = company_code or "none"

    schedule_data = fetch_outage_data(bureau_code, is_customer=False, company_code=company_code)
    if isinstance(schedule_data, str):
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data="notify_subscribe"),
            InlineKeyboardButton("🔄 Tải lại", callback_data=f"sub_bur_{company_part}_{bureau_code}")
        )
        markup.row(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
        edit_or_send_message(chat_id, target_message_id, schedule_data, reply_markup=markup)
        return

    token, wards = create_ward_cache_entry(
        bureau_code,
        schedule_data,
        bureau_name=bureau_name,
        company_code=company_code,
        company_name=company_name
    )
    markup = build_subscription_ward_selection_markup(company_code, bureau_code, token, wards)
    text = (
        f"🔔 **Đăng ký thông báo hằng ngày**\n"
        f"Đơn vị: **{bureau_name}**\n"
        f"Lịch hiện có: {len(schedule_data['outages'])} thông báo\n\n"
        "Chọn **xã/phường/thị trấn/khu vực** để nhận thông báo hằng ngày:"
    )
    edit_or_send_message(chat_id, target_message_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('sub_ward_'))
def handle_subscription_ward_select(call):
    parts = call.data.split('_')
    if len(parts) < 5:
        safe_answer_callback(call.id, text="Lựa chọn không hợp lệ.")
        return

    if len(parts) >= 6:
        company_code = None if parts[2].lower() == "none" else parts[2]
        bureau_code = parts[3]
        token = parts[4]
        ward_index_text = parts[5]
    else:
        company_code = None
        bureau_code = parts[2]
        token = parts[3]
        ward_index_text = parts[4]

    try:
        ward_index = int(ward_index_text)
    except ValueError:
        safe_answer_callback(call.id, text="Lựa chọn không hợp lệ.")
        return

    entry = get_ward_cache_entry(token, bureau_code=bureau_code)
    if not entry or ward_index >= len(entry['wards']):
        safe_answer_callback(call.id, text="Dữ liệu đã hết hạn, đang tải lại...")
        company_code = company_code or BUREAU_COMPANY_CACHE.get(bureau_code)
        company_name = companies_dict.get(company_code, "") if company_code else ""
        bureau_name = BUREAU_NAME_CACHE.get(bureau_code, bureau_code)
        show_subscription_ward_selection(call.message.chat.id, company_code, company_name, bureau_code, bureau_name, message_id=call.message.message_id)
        return

    ward = entry['wards'][ward_index]
    company_code = entry.get('company_code') or company_code
    company_name = entry.get('company_name') or (companies_dict.get(company_code, "") if company_code else "")
    bureau_name = entry.get('bureau_name') or BUREAU_NAME_CACHE.get(bureau_code, bureau_code)
    subscription_state = save_daily_subscription(
        call,
        company_code,
        company_name,
        bureau_code,
        bureau_name,
        ward['key'],
        ward['display']
    )

    safe_answer_callback(call.id, text="Đã bật thông báo hằng ngày.")
    text = (
        "✅ **Đã bật thông báo hằng ngày**\n\n"
        f"Khu vực: **{ward['display']}**\n"
        f"Đơn vị: **{bureau_name}**\n"
        f"Giờ gửi: **{DAILY_NOTIFY_TIME}** mỗi ngày\n\n"
        "Bot đã tự lưu Telegram chat ID của cuộc trò chuyện này để gửi tin."
    )
    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        text,
        reply_markup=build_notification_actions_markup()
    )

    if subscription_state['should_send_today']:
        send_immediate_daily_notification(
            call.message.chat.id,
            subscription_state['id'],
            entry,
            ward
        )

@bot.callback_query_handler(func=lambda call: call.data == 'notify_list')
def handle_notify_list(call):
    safe_answer_callback(call.id)
    subscriptions = get_active_subscriptions(call.message.chat.id)
    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        format_subscription_list(subscriptions),
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
    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        text,
        reply_markup=build_disable_subscriptions_markup(subscriptions)
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('disable_sub_'))
def handle_disable_subscription(call):
    try:
        subscription_id = int(call.data.split('_')[2])
    except ValueError:
        safe_answer_callback(call.id, text="Lựa chọn không hợp lệ.")
        return

    disabled = disable_subscription(subscription_id, call.message.chat.id)
    safe_answer_callback(call.id, text="Đã tắt thông báo." if disabled else "Không tìm thấy thông báo.")
    subscriptions = get_active_subscriptions(call.message.chat.id)
    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        "✅ Đã tắt thông báo đã chọn.\n\n" + format_subscription_list(subscriptions),
        reply_markup=build_notification_actions_markup()
    )

@bot.callback_query_handler(func=lambda call: call.data == 'disable_all_subs')
def handle_disable_all_subscriptions(call):
    disabled_count = disable_all_subscriptions(call.message.chat.id)
    safe_answer_callback(call.id, text=f"Đã tắt {disabled_count} thông báo.")
    edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        f"✅ Đã tắt **{disabled_count}** thông báo hằng ngày.",
        reply_markup=build_notification_actions_markup()
    )

@bot.callback_query_handler(func=lambda call: call.data == 'menu_unit')
def handle_menu_unit(call):
    safe_answer_callback(call.id)
    show_companies_menu(call.message, is_edit=True)

def show_companies_menu(message, is_edit=False):
    welcome_text = (
        "🏢 **Tra cứu theo Đơn vị quản lý**\n\n"
        "Hãy chọn **Công ty Điện lực (Tỉnh/Thành phố)** phía dưới để bắt đầu:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for code, name in sorted(companies_dict.items()):
        display_name = name.replace("Công ty Điện lực ", "").replace("Điện lực ", "")
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"comp_{code}"))
        
    back_btn = InlineKeyboardButton("⬅️ Quay lại Menu chính", callback_data="back_main")
    markup.add(*buttons)
    markup.row(back_btn)
    
    if is_edit:
        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=welcome_text,
            parse_mode="Markdown",
            reply_markup=markup
        )
    else:
        bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('comp_'))
def handle_company_select(call):
    company_code = call.data.split('_')[1]
    company_name = companies_dict.get(company_code, "Công ty Điện lực")
    
    safe_answer_callback(call.id, text=f"Đang tải danh sách Điện lực của {company_name}...")
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔄 Đang tải các đơn vị Điện lực thuộc **{company_name}**..."
    )
    
    bureaus = fetch_bureaus(company_code)
    BUREAU_NAME_CACHE.update(bureaus)
    
    if not bureaus:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Chọn lại tỉnh thành", callback_data="back_companies"))
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
        buttons.append(InlineKeyboardButton(display_name, callback_data=f"bur_{company_code}_{code}"))
        
    back_button = InlineKeyboardButton("⬅️ Chọn tỉnh thành khác", callback_data="back_companies")
    
    markup.add(*buttons)
    markup.row(back_button)
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"📍 Bạn đã chọn: **{company_name}**.\n\nHãy chọn **Điện lực huyện/thành phố** cần tra cứu:",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('bur_'))
def handle_bureau_select(call):
    parts = call.data.split('_')
    if len(parts) >= 3:
        company_code = parts[1]
        bureau_code = parts[2]
    else:
        bureau_code = parts[1]
        company_code = BUREAU_COMPANY_CACHE.get(bureau_code)
    
    safe_answer_callback(call.id, text="Đang tải lịch và phân nhóm khu vực...")
    show_ward_selection(call.message.chat.id, bureau_code, message_id=call.message.message_id, company_code=company_code)

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

def build_cached_area_schedule(entry, area):
    outages = filter_outages_by_ward(entry['outages'], area['key'])
    return format_outage_messages(
        outages,
        entry['tu_ngay'],
        entry['den_ngay'],
        ward_name=area['display'],
        total_source=len(entry['outages'])
    )

def send_immediate_daily_notification(chat_id, subscription_id, entry, area):
    schedule_data = add_daily_notification_header(build_cached_area_schedule(entry, area))
    send_schedule_result(chat_id, schedule_data, build_notification_actions_markup())
    if subscription_id:
        mark_subscription_sent(subscription_id, now_vietnam().date().isoformat())

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
            company_code = sub['company_code'] if 'company_code' in sub.keys() else None
            cache_key = (company_code, bureau_code)
            if cache_key not in schedule_cache:
                schedule_cache[cache_key] = fetch_outage_data(bureau_code, is_customer=False, company_code=company_code)

            try:
                source_data = schedule_cache[cache_key]
                if isinstance(source_data, str):
                    schedule_data = (
                        "⚠️ Không thể lấy lịch cúp điện hôm nay từ EVN SPC.\n\n"
                        f"{source_data}"
                    )
                else:
                    outages = filter_outages_by_ward(source_data['outages'], sub['ward_key'])
                    schedule_data = format_outage_messages(
                        outages,
                        source_data['tu_ngay'],
                        source_data['den_ngay'],
                        ward_name=sub['ward_name'],
                        total_source=len(source_data['outages'])
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

def safe_delete_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception as e:
        print(f"Warning: Could not delete message: {e}")

def edit_or_send_message(chat_id, message_id, text, parse_mode="Markdown", reply_markup=None):
    if message_id:
        try:
            return bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        except Exception as e:
            print(f"Warning: Could not edit message, sending a new one: {e}")

    return bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)

def build_ward_selection_markup(bureau_code, token, wards, company_code=None):
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    company_part = company_code or "none"

    for idx, ward in enumerate(wards):
        label = f"{ward['display']} ({ward['count']})"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append(InlineKeyboardButton(label, callback_data=f"ward_{bureau_code}_{token}_{idx}"))

    markup.add(*buttons)
    markup.row(InlineKeyboardButton("🔄 Tải lại danh sách", callback_data=f"ref_bur_{company_part}_{bureau_code}"))
    markup.row(InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data="back_companies"))
    markup.row(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
    return markup

def show_ward_selection(chat_id, bureau_code, message_id=None, company_code=None):
    loading_message = edit_or_send_message(
        chat_id,
        message_id,
        "🔄 Đang tải lịch cúp điện và phân nhóm theo **xã/phường/khu vực**..."
    )
    target_message_id = getattr(loading_message, 'message_id', message_id)

    company_code = company_code or BUREAU_COMPANY_CACHE.get(bureau_code)
    company_part = company_code or "none"
    company_name = companies_dict.get(company_code, "") if company_code else ""
    bureau_name = BUREAU_NAME_CACHE.get(bureau_code, bureau_code)

    schedule_data = fetch_outage_data(bureau_code, is_customer=False, company_code=company_code)
    if isinstance(schedule_data, str):
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("⬅️ Chọn Điện lực khác", callback_data="back_companies"),
            InlineKeyboardButton("🔄 Tải lại", callback_data=f"ref_bur_{company_part}_{bureau_code}")
        )
        markup.row(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
        edit_or_send_message(chat_id, target_message_id, schedule_data, reply_markup=markup)
        return

    token, wards = create_ward_cache_entry(
        bureau_code,
        schedule_data,
        bureau_name=bureau_name,
        company_code=company_code,
        company_name=company_name
    )
    markup = build_ward_selection_markup(bureau_code, token, wards, company_code=company_code)
    text = (
        f"📅 **LỊCH CÚP ĐIỆN DỰ KIẾN (Từ {schedule_data['tu_ngay']} đến {schedule_data['den_ngay']})**\n"
        f"Tổng số thông báo: {len(schedule_data['outages'])}\n\n"
        "Chọn **xã/phường/thị trấn/khu vực** để xem riêng lịch cúp điện:"
    )
    edit_or_send_message(chat_id, target_message_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('ref_bur_'))
def handle_ref_bureau(call):
    parts = call.data.split('_')
    if len(parts) >= 4:
        company_code = None if parts[2].lower() == "none" else parts[2]
        bureau_code = parts[3]
    else:
        company_code = None
        bureau_code = parts[2]

    safe_answer_callback(call.id, text="Đang tải danh sách khu vực...")
    show_ward_selection(call.message.chat.id, bureau_code, message_id=call.message.message_id, company_code=company_code)

@bot.callback_query_handler(func=lambda call: call.data.startswith('ward_'))
def handle_ward_select(call):
    parts = call.data.split('_')
    if len(parts) < 4:
        safe_answer_callback(call.id, text="Lựa chọn không hợp lệ.")
        return

    bureau_code = parts[1]
    token = parts[2]
    try:
        ward_index = int(parts[3])
    except ValueError:
        safe_answer_callback(call.id, text="Lựa chọn không hợp lệ.")
        return

    entry = get_ward_cache_entry(token, bureau_code=bureau_code)
    if not entry or ward_index >= len(entry['wards']):
        safe_answer_callback(call.id, text="Dữ liệu đã hết hạn, đang tải lại...")
        show_ward_selection(call.message.chat.id, bureau_code, message_id=call.message.message_id)
        return

    safe_answer_callback(call.id, text="Đang hiển thị lịch theo khu vực...")
    ward = entry['wards'][ward_index]
    company_code = entry.get('company_code')
    company_part = company_code or "none"
    outages = filter_outages_by_ward(entry['outages'], ward['key'])
    schedule_data = format_outage_messages(
        outages,
        entry['tu_ngay'],
        entry['den_ngay'],
        ward_name=ward['display'],
        total_source=len(entry['outages'])
    )

    safe_delete_message(call.message.chat.id, call.message.message_id)
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("⬅️ Chọn khu vực khác", callback_data=f"ref_bur_{company_part}_{bureau_code}"),
        InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"refresh_ward_{company_part}_{bureau_code}_{token}_{ward_index}")
    )
    markup.row(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
    send_schedule_result(call.message.chat.id, schedule_data, markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('refresh_ward_'))
def handle_refresh_ward(call):
    parts = call.data.split('_')
    if len(parts) < 5:
        safe_answer_callback(call.id, text="Lựa chọn không hợp lệ.")
        return

    if len(parts) >= 6:
        company_code = None if parts[2].lower() == "none" else parts[2]
        bureau_code = parts[3]
        token = parts[4]
        ward_index_text = parts[5]
    else:
        company_code = None
        bureau_code = parts[2]
        token = parts[3]
        ward_index_text = parts[4]

    try:
        ward_index = int(ward_index_text)
    except ValueError:
        safe_answer_callback(call.id, text="Lựa chọn không hợp lệ.")
        return

    entry = get_ward_cache_entry(token, bureau_code=bureau_code)
    if not entry or ward_index >= len(entry['wards']):
        safe_answer_callback(call.id, text="Dữ liệu đã hết hạn, đang tải lại...")
        show_ward_selection(call.message.chat.id, bureau_code, message_id=call.message.message_id, company_code=company_code)
        return

    company_code = entry.get('company_code') or company_code
    company_part = company_code or "none"
    company_name = entry.get('company_name') or (companies_dict.get(company_code, "") if company_code else "")
    bureau_name = entry.get('bureau_name') or BUREAU_NAME_CACHE.get(bureau_code, bureau_code)
    ward_key = entry['wards'][ward_index]['key']
    ward_name = entry['wards'][ward_index]['display']
    safe_answer_callback(call.id, text=f"Đang tải lại lịch {ward_name}...")

    loading_message = edit_or_send_message(
        call.message.chat.id,
        call.message.message_id,
        f"🔄 Đang tải lại lịch cúp điện cho **{ward_name}**..."
    )
    target_message_id = getattr(loading_message, 'message_id', call.message.message_id)

    fresh_data = fetch_outage_data(bureau_code, is_customer=False, company_code=company_code)
    if isinstance(fresh_data, str):
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("⬅️ Chọn khu vực khác", callback_data=f"ref_bur_{company_part}_{bureau_code}"),
            InlineKeyboardButton("🔄 Tải lại", callback_data=f"ref_bur_{company_part}_{bureau_code}")
        )
        markup.row(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
        edit_or_send_message(call.message.chat.id, target_message_id, fresh_data, reply_markup=markup)
        return

    new_token, wards = create_ward_cache_entry(
        bureau_code,
        fresh_data,
        bureau_name=bureau_name,
        company_code=company_code,
        company_name=company_name
    )
    new_ward_index = next((idx for idx, ward in enumerate(wards) if ward['key'] == ward_key), None)
    if new_ward_index is None:
        markup = build_ward_selection_markup(bureau_code, new_token, wards, company_code=company_code)
        text = (
            f"ℹ️ Hiện không còn lịch cúp điện cho **{ward_name}** trong dữ liệu mới.\n\n"
            "Chọn khu vực khác để xem lịch hiện có:"
        )
        edit_or_send_message(call.message.chat.id, target_message_id, text, reply_markup=markup)
        return

    ward = wards[new_ward_index]
    outages = filter_outages_by_ward(fresh_data['outages'], ward['key'])
    schedule_data = format_outage_messages(
        outages,
        fresh_data['tu_ngay'],
        fresh_data['den_ngay'],
        ward_name=ward['display'],
        total_source=len(fresh_data['outages'])
    )

    safe_delete_message(call.message.chat.id, target_message_id)
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("⬅️ Chọn khu vực khác", callback_data=f"ref_bur_{company_part}_{bureau_code}"),
        InlineKeyboardButton("🔄 Tải lại lịch", callback_data=f"refresh_ward_{company_part}_{bureau_code}_{new_token}_{new_ward_index}")
    )
    markup.row(InlineKeyboardButton("🏠 Giao diện chính", callback_data="back_main"))
    send_schedule_result(call.message.chat.id, schedule_data, markup)

@bot.callback_query_handler(func=lambda call: call.data == 'back_companies')
def handle_back_companies(call):
    safe_answer_callback(call.id)
    show_companies_menu(call.message, is_edit=True)

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Error: Please specify TELEGRAM_BOT_TOKEN in the .env file and run again.")
    else:
        print("Bot is starting polling...")
        init_db()
        start_daily_notification_scheduler()
        bot.infinity_polling()

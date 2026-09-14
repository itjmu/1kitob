import asyncio
import csv
import io
import logging
import os
import random
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramConflictError, TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InputTextMessageContent,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.enums import ParseMode
from dotenv import load_dotenv

logging.getLogger("aiogram.dispatcher.dispatcher").setLevel(logging.CRITICAL)
logging.getLogger("aiogram.event").setLevel(logging.CRITICAL)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "books.sqlite3"
FORMATS = ("Настоящая книга", "Аудио", "Видео", "PDF")
FORMAT_EMOJI = {"Настоящая книга": "📕", "Аудио": "🎧", "Видео": "🎬", "PDF": "📄"}
DEAL_TYPES = ("Подарить", "Обменять", "Продать")
BROADCAST_DELAY = 1.0
PREMIUM_DAYS = 90
REFERRAL_DAYS = 2
REFERRAL_NEW_USER_DAYS = 1
DAILY_BOOK_LIMIT = 5
DAILY_BOOK_LIMIT_PREMIUM = 10
INLINE_CACHE_TIME = 5


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AddBook(StatesGroup):
    title = State(); author = State(); book_format = State(); genre = State()
    city = State(); condition = State(); photo = State(); deal = State(); price = State()


class SearchBook(StatesGroup):
    query = State(); browsing = State()


class ReportUser(StatesGroup):
    target = State(); reason = State()


class AdminBroadcast(StatesGroup):
    message = State()


class AdminEdit(StatesGroup):
    book = State()


class AdminChannel(StatesGroup):
    add = State()


class AdminNews(StatesGroup):
    channel = State()


class AdminPremium(StatesGroup):
    info = State(); grant = State()


class AdminPromo(StatesGroup):
    code = State(); days = State(); uses = State()


class AdminTexts(StatesGroup):
    key = State()


class AdminMsg(StatesGroup):
    user_id = State(); text = State()


class ProfileEdit(StatesGroup):
    contact = State()


class Onboarding(StatesGroup):
    step = State()


class UserSurvey(StatesGroup):
    gender = State(); age = State(); genres = State()


class WriteAdmin(StatesGroup):
    text = State()


class Captcha(StatesGroup):
    answer = State()


MENU_TEXT = "⌂ Меню"
BACK_TEXT = "‹ Назад"
INSTANCE_LOCK_PATH = BASE_DIR / "bot.instance.lock"
INSTANCE_LOCK = None

GENRES = [
    ("fiction", "📚 Художественная"), ("science", "🔬 Научная"),
    ("history", "🗓️ История"), ("tech", "💻 Технология"),
    ("art", "🎨 Искусство"), ("business", "💰 Бизнес"),
    ("selfdev", "🧘 Саморазвитие"), ("fantasy", "🎮 Фантастика"),
    ("other", "📦 Разное"),
]

TEXTS_DEFAULT = {
    "text_faq": "❓ <b>Частые вопросы</b>\n\n<b>Как добавить книгу?</b>\nНажмите ➕ Добавить и следуйте шагам.\n\n<b>Как обменять?</b>\nНайдите книгу в каталоге и нажмите «Запросить обмен».\n\n<b>Как связаться?</b>\nПосле согласия обеих сторон контакты откроются автоматически.",
    "text_support": "💬 <b>Поддержка</b>\n\nПо всем вопросам пишите администратору: @admin",
    "text_help": "➕ Добавьте книгу. 🔍 Ищите по названию или автору.\nЗаявки анонимны до взаимного согласия.\n\n👤 В «Мой профиль» — ваши книги, поиски, заявки, избранное и премиум.\n\n⌂ Меню — главный экран.",
    "text_premium_info": "Оплата: 0.5$ за 3 месяца. Напишите администратору для оформления.",
    "text_rules": "📜 <b>Правила</b>\n\n1. Уважайте других пользователей.\n2. Не публикуйте запрещённые материалы.\n3. Не спамьте.\n4. Обман = бан.",
    "news_channel_id": "",
    "news_channel_enabled": "0",
    "book_of_week_id": "",
    "bot_username": "",
}

_URL_RE = re.compile(r"(https?://\S+|www\.\S+|\bt\.me/\S+)", re.IGNORECASE)
_MENTION_RE = re.compile(r"@\w+", re.UNICODE)


def sanitize_free_text(text):
    """Убирает ссылки и @username из свободного текста."""
    text = text or ""
    text = _URL_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    return " ".join(text.split()).strip()


# ------------------ Кеш премиума ------------------

_PREMIUM_CACHE = {}
_PREMIUM_CACHE_TTL = 60


def invalidate_premium_cache(user_id=None):
    if user_id is None:
        _PREMIUM_CACHE.clear()
    else:
        _PREMIUM_CACHE.pop(user_id, None)


def format_display(fmt_str):
    parts = [p.strip() for p in str(fmt_str or "").split(",") if p.strip()]
    return ", ".join(f"{FORMAT_EMOJI.get(p, '📚')} {p}" for p in parts) or "📚 —"


def acquire_instance_lock():
    global INSTANCE_LOCK
    INSTANCE_LOCK = INSTANCE_LOCK_PATH.open("a+b")
    INSTANCE_LOCK.seek(0)
    if not INSTANCE_LOCK.read(1):
        INSTANCE_LOCK.seek(0); INSTANCE_LOCK.write(b"1"); INSTANCE_LOCK.flush()
    INSTANCE_LOCK.seek(0)
    try:
        if sys.platform == "win32":
            msvcrt.locking(INSTANCE_LOCK.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(INSTANCE_LOCK.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        INSTANCE_LOCK.close(); INSTANCE_LOCK = None
        raise RuntimeError("Бот уже запущен в другом процессе.") from error


def release_instance_lock():
    global INSTANCE_LOCK
    if INSTANCE_LOCK is None:
        return
    try:
        if sys.platform == "win32":
            INSTANCE_LOCK.seek(0)
            msvcrt.locking(INSTANCE_LOCK.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(INSTANCE_LOCK.fileno(), fcntl.LOCK_UN)
    finally:
        INSTANCE_LOCK.close(); INSTANCE_LOCK = None


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def init_db():
    try:
        db = connect()
        db.execute("SELECT name FROM sqlite_master LIMIT 1")
    except sqlite3.DatabaseError:
        try: db.close()
        except UnboundLocalError: pass
        if DB_PATH.exists():
            backup = DB_PATH.with_name(f"{DB_PATH.stem}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3")
            DB_PATH.replace(backup)
        db = connect()

    with closing(db):
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, username TEXT, name TEXT NOT NULL,
            is_banned INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS books (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
            title TEXT NOT NULL, author TEXT NOT NULL, city TEXT NOT NULL, condition TEXT NOT NULL DEFAULT '',
            format TEXT NOT NULL DEFAULT 'Настоящая книга', deal_type TEXT NOT NULL DEFAULT 'Обменять',
            price TEXT, status TEXT NOT NULL DEFAULT 'available', boosted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS saved_searches (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            query TEXT NOT NULL, format TEXT, city TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS exchange_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL, owner_consent INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
            offered_book_ids TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(book_id, requester_id));
        CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER NOT NULL,
            reported_id INTEGER NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS user_sessions (user_id INTEGER PRIMARY KEY, last_action TEXT, last_query TEXT,
            state TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS required_channels (id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL, title TEXT, invite_link TEXT);
        CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS favorites (user_id INTEGER NOT NULL, book_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, book_id));
        CREATE TABLE IF NOT EXISTS ratings (book_id INTEGER NOT NULL, user_id INTEGER NOT NULL, stars INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(book_id, user_id));
        CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY, days INTEGER NOT NULL,
            uses_left INTEGER NOT NULL DEFAULT 1, used_by TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS book_views_log (user_id INTEGER NOT NULL, book_id INTEGER NOT NULL,
            view_date TEXT NOT NULL, PRIMARY KEY(user_id, book_id, view_date));
        CREATE INDEX IF NOT EXISTS idx_books_boosted ON books(boosted);
        CREATE INDEX IF NOT EXISTS idx_books_status_created ON books(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_books_owner ON books(owner_id);
        CREATE INDEX IF NOT EXISTS idx_searches_active ON saved_searches(active, user_id);
        CREATE INDEX IF NOT EXISTS idx_requests_book ON exchange_requests(book_id);
        CREATE INDEX IF NOT EXISTS idx_views_log ON book_views_log(book_id, view_date);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        for col, sql in [
            ("is_banned", "ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0"),
            ("onboarded", "ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0"),
            ("gender", "ALTER TABLE users ADD COLUMN gender TEXT"),
            ("age", "ALTER TABLE users ADD COLUMN age INTEGER"),
            ("favorite_genres", "ALTER TABLE users ADD COLUMN favorite_genres TEXT"),
            ("is_premium", "ALTER TABLE users ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0"),
            ("preferred_contact", "ALTER TABLE users ADD COLUMN preferred_contact TEXT"),
            ("referrer_id", "ALTER TABLE users ADD COLUMN referrer_id INTEGER"),
            ("premium_until", "ALTER TABLE users ADD COLUMN premium_until TEXT"),
            ("last_boost_at", "ALTER TABLE users ADD COLUMN last_boost_at TEXT"),
            ("last_captcha_at", "ALTER TABLE users ADD COLUMN last_captcha_at TEXT"),
            ("books_today", "ALTER TABLE users ADD COLUMN books_today INTEGER NOT NULL DEFAULT 0"),
            ("books_today_date", "ALTER TABLE users ADD COLUMN books_today_date TEXT"),
        ]:
            if col not in columns: db.execute(sql)
        columns = {row[1] for row in db.execute("PRAGMA table_info(books)")}
        for col, sql in [
            ("format", "ALTER TABLE books ADD COLUMN format TEXT NOT NULL DEFAULT 'Настоящая книга'"),
            ("photo_id", "ALTER TABLE books ADD COLUMN photo_id TEXT"),
            ("deal_type", "ALTER TABLE books ADD COLUMN deal_type TEXT NOT NULL DEFAULT 'Обменять'"),
            ("price", "ALTER TABLE books ADD COLUMN price TEXT"),
            ("boosted", "ALTER TABLE books ADD COLUMN boosted INTEGER NOT NULL DEFAULT 0"),
            ("views", "ALTER TABLE books ADD COLUMN views INTEGER NOT NULL DEFAULT 0"),
            ("genre", "ALTER TABLE books ADD COLUMN genre TEXT DEFAULT 'other'"),
            ("rating_sum", "ALTER TABLE books ADD COLUMN rating_sum INTEGER NOT NULL DEFAULT 0"),
            ("rating_count", "ALTER TABLE books ADD COLUMN rating_count INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in columns: db.execute(sql)
        columns = {row[1] for row in db.execute("PRAGMA table_info(exchange_requests)")}
        for col, sql in [
            ("owner_consent", "ALTER TABLE exchange_requests ADD COLUMN owner_consent INTEGER NOT NULL DEFAULT 0"),
            ("offered_book_ids", "ALTER TABLE exchange_requests ADD COLUMN offered_book_ids TEXT"),
            ("rated", "ALTER TABLE exchange_requests ADD COLUMN rated INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in columns: db.execute(sql)
        for key, val in TEXTS_DEFAULT.items():
            if not db.execute("SELECT 1 FROM bot_settings WHERE key=?", (key,)).fetchone():
                db.execute("INSERT INTO bot_settings (key,value) VALUES (?,?)", (key, val))
        db.commit()


def check_user(message):
    user = message.from_user
    if not user:
        return False
    with closing(connect()) as db:
        db.execute("""INSERT INTO users (telegram_id, username, name) VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, name=excluded.name""",
                   (user.id, user.username, user.full_name))
        row = db.execute("SELECT is_banned FROM users WHERE telegram_id=?", (user.id,)).fetchone()
        db.commit()
    return bool(row and row["is_banned"])


def save_session(user_id, action=None, query=None, state=None):
    with closing(connect()) as db:
        db.execute("""INSERT INTO user_sessions (user_id,last_action,last_query,state,updated_at)
            VALUES (?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
            last_action=COALESCE(excluded.last_action,user_sessions.last_action),
            last_query=COALESCE(excluded.last_query,user_sessions.last_query),
            state=COALESCE(excluded.state,user_sessions.state), updated_at=CURRENT_TIMESTAMP""",
                   (user_id, action, query, state))
        db.commit()


def save_search(user_id, query, fmt=None, city=None):
    query = (query or "").strip()
    if not query:
        return
    with closing(connect()) as db:
        existing = db.execute(
            "SELECT id FROM saved_searches WHERE user_id=? AND active=1 AND query=? "
            "AND IFNULL(format,'')=? AND IFNULL(city,'')=?",
            (user_id, query, fmt or "", city or "")
        ).fetchone()
        if existing:
            return
        db.execute("INSERT INTO saved_searches (user_id,query,format,city) VALUES (?,?,?,?)",
                   (user_id, query, fmt, city))
        db.commit()


def admins():
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def is_admin(user_id):
    return user_id in admins()


def banned(user_id):
    with closing(connect()) as db:
        row = db.execute("SELECT is_banned FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    return bool(row and row["is_banned"])


def is_premium_user(user_id):
    if is_admin(user_id):
        return True
    now_ts = time.time()
    cached = _PREMIUM_CACHE.get(user_id)
    if cached and cached[1] > now_ts:
        return cached[0]
    result = False
    with closing(connect()) as db:
        row = db.execute("SELECT is_premium, premium_until FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    if row:
        if row["is_premium"]:
            result = True
        elif row["premium_until"]:
            try:
                until = datetime.fromisoformat(row["premium_until"].split(".")[0])
                if until > utcnow():
                    result = True
            except Exception:
                pass
    _PREMIUM_CACHE[user_id] = (result, now_ts + _PREMIUM_CACHE_TTL)
    return result


def grant_premium_days(user_id, days):
    with closing(connect()) as db:
        row = db.execute("SELECT premium_until FROM users WHERE telegram_id=?", (user_id,)).fetchone()
        if not row:
            return False
        base = utcnow()
        if row["premium_until"]:
            try:
                existing = datetime.fromisoformat(row["premium_until"].split(".")[0])
                if existing > base:
                    base = existing
            except Exception:
                pass
        new_until = (base + timedelta(days=days)).isoformat(timespec="seconds")
        db.execute("UPDATE users SET premium_until=? WHERE telegram_id=?", (new_until, user_id))
        db.commit()
    invalidate_premium_cache(user_id)
    return True


def check_daily_limit(user_id):
    today = time.strftime("%Y-%m-%d")
    with closing(connect()) as db:
        row = db.execute("SELECT books_today, books_today_date FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    if not row:
        return True, 0, 0
    limit = DAILY_BOOK_LIMIT_PREMIUM if is_premium_user(user_id) else DAILY_BOOK_LIMIT
    if row["books_today_date"] != today:
        return True, 0, limit
    return row["books_today"] < limit, row["books_today"], limit


def inc_daily_book(user_id):
    today = time.strftime("%Y-%m-%d")
    with closing(connect()) as db:
        row = db.execute("SELECT books_today, books_today_date FROM users WHERE telegram_id=?", (user_id,)).fetchone()
        if row and row["books_today_date"] == today:
            db.execute("UPDATE users SET books_today=books_today+1 WHERE telegram_id=?", (user_id,))
        else:
            db.execute("UPDATE users SET books_today=1, books_today_date=? WHERE telegram_id=?", (today, user_id))
        db.commit()


def needs_captcha(user_id):
    with closing(connect()) as db:
        row = db.execute("SELECT last_captcha_at FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    if not row or not row["last_captcha_at"]:
        return True
    try:
        last = datetime.fromisoformat(row["last_captcha_at"].split(".")[0])
        return (utcnow() - last).days >= 7
    except Exception:
        return True


def mark_captcha(user_id):
    with closing(connect()) as db:
        db.execute("UPDATE users SET last_captcha_at=? WHERE telegram_id=?",
                   (utcnow().isoformat(timespec="seconds"), user_id))
        db.commit()


def get_setting(key, default=None):
    with closing(connect()) as db:
        row = db.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with closing(connect()) as db:
        db.execute("""INSERT INTO bot_settings (key,value) VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))
        db.commit()


def get_text(key):
    return get_setting(key) or TEXTS_DEFAULT.get(key, "")


def subscription_required():
    return get_setting("subscription_required", "0") == "1"


def news_channel_enabled():
    return get_setting("news_channel_enabled", "0") == "1"


def news_channel_id():
    return (get_setting("news_channel_id", "") or "").strip()


def bot_username_cached():
    return (get_setting("bot_username", "") or "").strip()


def required_channels_list():
    with closing(connect()) as db:
        return db.execute("SELECT * FROM required_channels ORDER BY id").fetchall()


async def check_subscriptions(bot, user_id):
    channels = required_channels_list()
    if not channels:
        return True, []
    missing = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel["chat_id"], user_id)
            if member.status in ("left", "kicked"):
                missing.append(channel)
        except TelegramBadRequest:
            continue
        except Exception:
            continue
    return (len(missing) == 0), missing


def subscription_keyboard(channels):
    rows = []
    for ch in channels:
        if ch["invite_link"]:
            rows.append([InlineKeyboardButton(text=ch["title"] or ch["chat_id"], url=ch["invite_link"])])
        else:
            rows.append([InlineKeyboardButton(text=ch["title"] or ch["chat_id"], callback_data="noop")])
    rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def allowed(message):
    if check_user(message):
        await message.answer("Ваш аккаунт заблокирован администратором.")
        return False
    if subscription_required():
        ok, missing = await check_subscriptions(message.bot, message.from_user.id)
        if not ok:
            await message.answer("Подпишитесь на канал(ы) ниже, затем нажмите «Я подписался»:",
                                 reply_markup=subscription_keyboard(missing))
            return False
    return True


async def safe_edit(coro):
    try:
        await coro
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            raise


def format_contact(user_id, username, name, preferred_contact, is_premium_flag):
    if (is_admin(user_id) or is_premium_flag) and preferred_contact:
        return escape(str(preferred_contact))
    return f"@{username or name}"


# --- Клавиатуры ---

def menu_button_row():
    return [InlineKeyboardButton(text="⌂ Меню", callback_data="menu")]


def keyboard(user_id=None):
    rows = [
        [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="🔍 Поиск")],
        [KeyboardButton(text="📚 Каталог"), KeyboardButton(text="🔎 Ищут")],
        [KeyboardButton(text="🎲 Случайная"), KeyboardButton(text="⭐ Книга недели")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="ℹ️ Помощь")],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([KeyboardButton(text="⚙ Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def back_keyboard():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)


def profile_keyboard(user_id):
    premium = is_premium_user(user_id)
    rows = [
        [InlineKeyboardButton(text="📖 Мои книги", callback_data="profile:books"),
         InlineKeyboardButton(text="❤️ Избранное", callback_data="profile:favs")],
        [InlineKeyboardButton(text="🔔 Мои поиски", callback_data="profile:searches"),
         InlineKeyboardButton(text="🤝 Мои заявки", callback_data="profile:requests")],
        [InlineKeyboardButton(text="⭐ Мои оценки", callback_data="profile:ratings"),
         InlineKeyboardButton(text="🎁 Хочу подарок", callback_data="profile:gift")],
        [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="profile:invite"),
         InlineKeyboardButton(text="🚩 Пожаловаться", callback_data="profile:report")],
    ]
    if premium:
        rows.append([InlineKeyboardButton(text="✏ Контакт для показа", callback_data="profile:contact"),
                     InlineKeyboardButton(text="⭐ Премиум ✅", callback_data="profile:premium")])
    else:
        rows.append([InlineKeyboardButton(text="⭐ Получить премиум", callback_data="profile:premium")])
    rows.append(menu_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def book_text(book, max_len=None):
    text = (f"📖 <b>{escape(str(book['title']))}</b>\n"
            f"Автор: {escape(str(book['author']))}\n"
            f"Формат: {format_display(book['format'])}\n"
            f"Город: {escape(str(book['city'] or 'не требуется'))}\n"
            f"Состояние: {escape(str(book['condition'] or 'не указано'))}\n"
            f"Способ: {escape(str(book['deal_type'] or 'Обменять'))}")
    if book["deal_type"] and "Продать" in book["deal_type"] and book["price"]:
        text += f"\nЦена: {escape(str(book['price']))}"
    try:
        views = book["views"] or 0
    except (KeyError, IndexError):
        views = 0
    text += f"\n🆔 №{book['id']} · 👁 {views}"
    try:
        rc = book["rating_count"] or 0
    except (KeyError, IndexError):
        rc = 0
    if rc:
        rs = book["rating_sum"] or 0
        avg = rs / rc
        text += f" · ⭐ {avg:.1f} ({rc})"
    if max_len and len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def book_deep_link(book_id):
    bot_username = bot_username_cached()
    if not bot_username:
        return ""
    return f"https://t.me/{bot_username}?start=book_{book_id}"


def book_buttons(book_id, deal_types=""):
    types = [item.strip() for item in (deal_types or "").split(",") if item.strip()]
    if len(types) > 1:
        action_text, action_data = "📩 Связаться с владельцем", f"request:{book_id}"
    elif types == ["Продать"]:
        action_text, action_data = "💳 Купить", f"buy:{book_id}"
    elif types == ["Подарить"]:
        action_text, action_data = "🎁 Получить подарок", f"request:{book_id}"
    else:
        action_text, action_data = "🤝 Запросить обмен", f"request:{book_id}"

    rows = [[InlineKeyboardButton(text=action_text, callback_data=action_data)]]
    rows.append([
        InlineKeyboardButton(text="❤️ В избранное", callback_data=f"fav_toggle:{book_id}"),
        InlineKeyboardButton(text="👤 Ещё от автора", callback_data=f"by_author:{book_id}"),
    ])
    share_btn = InlineKeyboardButton(text="📤 Поделиться", switch_inline_query=f"book_{book_id}")
    rows.append([share_btn, InlineKeyboardButton(text="🚩 Пожаловаться", callback_data=f"report_book:{book_id}")])
    rows.append(menu_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_post_keyboard(book_id):
    link = book_deep_link(book_id)
    if not link:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Посмотреть / получить книгу", url=link)]
    ])


def inline_book_keyboard(book_id):
    link = book_deep_link(book_id)
    if not link:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Открыть в боте", url=link)]
    ])


def catalog_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 По жанрам", callback_data="catalog_genres"),
         InlineKeyboardButton(text="📚 Все книги", callback_data="catalog_all:0")],
        menu_button_row(),
    ])


def catalog_keyboard(book_ids, page, total_pages, prefix="catalog_all"):
    rows = []
    for index in range(0, len(book_ids), 5):
        rows.append([InlineKeyboardButton(text=f"{bid}", callback_data=f"detail:{bid}") for bid in book_ids[index:index + 5]])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="‹", callback_data=f"{prefix}:{page - 1}"))
    if page < total_pages - 1: nav.append(InlineKeyboardButton(text="›", callback_data=f"{prefix}:{page + 1}"))
    if nav: rows.append(nav)
    rows.append(menu_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def genres_catalog_keyboard(genre_counts):
    items = [(gid, label, genre_counts.get(gid, 0)) for gid, label in GENRES if genre_counts.get(gid, 0)]
    rows = []
    for i in range(0, len(items), 2):
        row = []
        for gid, label, count in items[i:i + 2]:
            row.append(InlineKeyboardButton(text=f"{label} · {count}", callback_data=f"genre:{gid}"))
        rows.append(row)
    rows.append(menu_button_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def request_buttons(request_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Согласиться", callback_data=f"accept:{request_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline:{request_id}")],
        [InlineKeyboardButton(text="📚 Выбрать книги отправителя", callback_data=f"offers:{request_id}")],
    ])


def offer_keyboard(request_id, books, selected):
    rows = []
    for book in books:
        mark = "✅ " if book["id"] in selected else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{book['id']} · {book['title']}", callback_data=f"offer:{request_id}:{book['id']}")])
    rows.append([InlineKeyboardButton(text="Готово", callback_data=f"offer_done:{request_id}"),
                 InlineKeyboardButton(text="Отмена", callback_data=f"offer_cancel:{request_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def counter_offer_keyboard(request_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять обмен", callback_data=f"counter_accept:{request_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline:{request_id}")],
    ])


def format_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📕 Бумажная", callback_data="format:Настоящая книга"),
         InlineKeyboardButton(text="🎧 Аудио", callback_data="format:Аудио")],
        [InlineKeyboardButton(text="🎬 Видео", callback_data="format:Видео"),
         InlineKeyboardButton(text="📄 PDF", callback_data="format:PDF")],
        [InlineKeyboardButton(text="Готово", callback_data="format_done")],
    ])


def selected_format_keyboard(selected):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✅ " if item in selected else "") + label, callback_data=f"format:{item}")
         for item, label in [("Настоящая книга", "📕 Бумажная"), ("Аудио", "🎧 Аудио")]],
        [InlineKeyboardButton(text=("✅ " if "Видео" in selected else "") + "🎬 Видео", callback_data="format:Видео"),
         InlineKeyboardButton(text=("✅ " if "PDF" in selected else "") + "📄 PDF", callback_data="format:PDF")],
        [InlineKeyboardButton(text="Готово", callback_data="format_done")],
    ])


def genre_keyboard():
    rows = []
    for i in range(0, len(GENRES), 2):
        row = []
        for gid, label in GENRES[i:i + 2]:
            row.append(InlineKeyboardButton(text=label, callback_data=f"setgenre:{gid}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deal_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Подарить"), KeyboardButton(text="Обменять")],
        [KeyboardButton(text="Продать"), KeyboardButton(text="Подарить и обменять")],
        [KeyboardButton(text="Обменять и продать"), KeyboardButton(text="Все варианты")],
        [KeyboardButton(text="❌ Отмена")]], resize_keyboard=True, one_time_keyboard=True)


def rating_keyboard(request_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="1⭐", callback_data=f"rate:{request_id}:1"),
        InlineKeyboardButton(text="2⭐", callback_data=f"rate:{request_id}:2"),
        InlineKeyboardButton(text="3⭐", callback_data=f"rate:{request_id}:3"),
        InlineKeyboardButton(text="4⭐", callback_data=f"rate:{request_id}:4"),
        InlineKeyboardButton(text="5⭐", callback_data=f"rate:{request_id}:5"),
    ]])


def normalize_deal(value):
    v = value.strip().lower()
    return {"подарить": "Подарить", "обменять": "Обменять", "продать": "Продать",
            "подарить и обменять": "Подарить, Обменять",
            "обменять и продать": "Обменять, Продать",
            "все варианты": "Подарить, Обменять, Продать"}.get(v)


def admin_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
         InlineKeyboardButton(text="📚 Книги", callback_data="admin:books")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"),
         InlineKeyboardButton(text="🚩 Жалобы", callback_data="admin:reports")],
        [InlineKeyboardButton(text="🔎 Поиски", callback_data="admin:searches"),
         InlineKeyboardButton(text="📢 Подписки", callback_data="admin:subscriptions")],
        [InlineKeyboardButton(text="📰 Новостной канал", callback_data="admin:news")],
        [InlineKeyboardButton(text="⭐ Премиум", callback_data="admin:premium"),
         InlineKeyboardButton(text="🎁 Промокоды", callback_data="admin:promo")],
        [InlineKeyboardButton(text="✏️ Тексты", callback_data="admin:texts"),
         InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="📥 Экспорт CSV", callback_data="admin:export"),
         InlineKeyboardButton(text="💾 Бэкап БД", callback_data="admin:backup")],
        [InlineKeyboardButton(text="✉️ Написать юзеру", callback_data="admin:msg"),
         InlineKeyboardButton(text="🔍 Найти юзера", callback_data="admin:find")],
        [InlineKeyboardButton(text="✕ Закрыть", callback_data="admin:close")],
    ])


def genres_keyboard_survey(selected):
    rows = []
    for i in range(0, len(GENRES), 2):
        row = []
        for gid, label in GENRES[i:i + 2]:
            mark = "✅ " if gid in selected else ""
            row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"survey:genre:{gid}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="survey:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def condition_prompt_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_condition")]
    ])


def photo_prompt_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_photo")]
    ])


# --- Отправка карточки книги ---

async def send_book_card(target, book, prefix="", chat_id=None, viewer_id=None):
    if viewer_id is None:
        viewer_id = chat_id
    book_dict = dict(book)
    if viewer_id:
        today = time.strftime("%Y-%m-%d")
        try:
            with closing(connect()) as db:
                cur = db.execute("INSERT OR IGNORE INTO book_views_log (user_id, book_id, view_date) VALUES (?,?,?)",
                                 (viewer_id, book["id"], today))
                if cur.rowcount > 0:
                    db.execute("UPDATE books SET views=views+1 WHERE id=?", (book["id"],))
                    db.commit()
                    book_dict["views"] = (book_dict.get("views") or 0) + 1
        except Exception:
            pass
    text = f"{prefix}{book_text(book_dict)}\n\nВладелец не показывается до взаимного согласия."
    markup = book_buttons(book["id"], book["deal_type"] or "Обменять")
    try:
        if book["photo_id"]:
            if chat_id is None:
                await target.answer_photo(book["photo_id"], caption=text, reply_markup=markup, parse_mode=ParseMode.HTML)
            else:
                await target.send_photo(chat_id, book["photo_id"], caption=text, reply_markup=markup, parse_mode=ParseMode.HTML)
        else:
            if chat_id is None:
                await target.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            else:
                await target.send_message(chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        if chat_id is None:
            await target.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        else:
            await target.send_message(chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def publish_to_channel(bot, book):
    channel_id = news_channel_id()
    if not channel_id or not news_channel_enabled():
        return
    text = f"📚 <b>Новая книга в BookHub!</b>\n\n{book_text(book, max_len=900)}"
    kb = channel_post_keyboard(book["id"])
    try:
        if book["photo_id"]:
            await bot.send_photo(channel_id, book["photo_id"], caption=text,
                                 reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(channel_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"⚠️ Не удалось опубликовать в новостной канал: {e}")


# --- Инлайн-режим ---

def _build_inline_result(book):
    caption = book_text(book, max_len=900) + "\n\n👆 Нажмите кнопку чтобы открыть в боте"
    kb = inline_book_keyboard(book["id"])
    if book["photo_id"]:
        return InlineQueryResultCachedPhoto(
            id=f"b{book['id']}",
            photo_file_id=book["photo_id"],
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    return InlineQueryResultArticle(
        id=f"b{book['id']}",
        title=book["title"] or "Книга",
        description=f"{book['author']} · {format_display(book['format'])}",
        input_message_content=InputTextMessageContent(
            message_text=caption,
            parse_mode=ParseMode.HTML,
        ),
        reply_markup=kb,
    )


async def inline_book_query(inline_query: InlineQuery):
    query = (inline_query.query or "").strip()
    results = []
    with closing(connect()) as db:
        if query.startswith("book_") and query[5:].isdigit():
            book = db.execute("SELECT * FROM books WHERE id=? AND status='available'",
                              (int(query[5:]),)).fetchone()
            if book:
                results.append(_build_inline_result(book))
        else:
            if query:
                q_cf = query.casefold()
                candidates = db.execute(
                    "SELECT * FROM books WHERE status='available' "
                    "ORDER BY boosted DESC, views DESC LIMIT 100"
                ).fetchall()
                books = [b for b in candidates
                         if q_cf in (b["title"] or "").casefold()
                         or q_cf in (b["author"] or "").casefold()][:20]
            else:
                books = db.execute(
                    "SELECT * FROM books WHERE status='available' "
                    "ORDER BY boosted DESC, views DESC LIMIT 20"
                ).fetchall()
            for b in books:
                results.append(_build_inline_result(b))
    try:
        await inline_query.answer(results[:50], cache_time=INLINE_CACHE_TIME, is_personal=True)
    except TelegramBadRequest:
        pass


# --- Старт, капча, онбординг ---

def greeting_text(name):
    hour = time.localtime().tm_hour
    greet = "🌅 Доброе утро" if 5 <= hour < 12 else "🏞 Добрый день" if 12 <= hour < 18 else "🌉 Добрый вечер" if 18 <= hour < 23 else "🌃 Доброй ночи"
    return f"{greet}, {name}! 👋"


async def start(message: Message, state: FSMContext):
    if not await allowed(message): return
    args = message.text.split(maxsplit=1) if message.text else []
    payload = args[1].strip() if len(args) > 1 else ""
    if payload.startswith("ref_"):
        try:
            referrer_id = int(payload[4:])
            if referrer_id != message.from_user.id:
                with closing(connect()) as db:
                    row = db.execute("SELECT referrer_id FROM users WHERE telegram_id=?", (message.from_user.id,)).fetchone()
                    if row and not row["referrer_id"]:
                        db.execute("UPDATE users SET referrer_id=? WHERE telegram_id=?", (referrer_id, message.from_user.id))
                        db.commit()
                        grant_premium_days(referrer_id, REFERRAL_DAYS)
                        grant_premium_days(message.from_user.id, REFERRAL_NEW_USER_DAYS)
                        try:
                            await message.bot.send_message(referrer_id,
                                f"🎉 По вашей ссылке зарегистрировался друг!\nВам +{REFERRAL_DAYS} дня премиума.")
                        except Exception: pass
                        try:
                            await message.answer(f"🎁 Вам начислен {REFERRAL_NEW_USER_DAYS} день премиума за регистрацию по ссылке друга!")
                        except Exception: pass
        except Exception: pass
    if payload.startswith("book_"):
        try:
            book_id = int(payload[5:])
            with closing(connect()) as db:
                book = db.execute("SELECT * FROM books WHERE id=? AND status='available'", (book_id,)).fetchone()
            if book:
                await send_book_card(message, book, viewer_id=message.from_user.id)
        except Exception: pass
    if needs_captcha(message.from_user.id):
        a, b = random.randint(2, 9), random.randint(2, 9)
        await state.set_state(Captcha.answer)
        await state.update_data(captcha_answer=a + b)
        await message.answer(f"🤖 Проверка: сколько будет {a} + {b}? Напишите число.")
        return
    with closing(connect()) as db:
        user = db.execute("SELECT onboarded FROM users WHERE telegram_id=?", (message.from_user.id,)).fetchone()
    if user and not user["onboarded"]:
        await state.set_state(Onboarding.step)
        await state.update_data(step=0)
        await onboarding_start(message, state)
    else:
        save_session(message.from_user.id, action="menu")
        await message.answer(greeting_text(message.from_user.first_name), reply_markup=keyboard(message.from_user.id))


async def captcha_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    answer = data.get("captcha_answer")
    if (message.text or "").strip() == str(answer):
        mark_captcha(message.from_user.id)
        await state.clear()
        await message.answer("✅ Проверка пройдена!")
        await start(message, state)
    else:
        await message.answer("❌ Неверно. Попробуйте снова: /start")


async def onboarding_start(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Быстрое обучение", callback_data="onboard:tour")],
        [InlineKeyboardButton(text="➕ Добавить книгу", callback_data="onboard:add_book"),
         InlineKeyboardButton(text="⏭ Пропустить", callback_data="onboard:skip")],
    ])
    await message.answer(
        "🎉 Добро пожаловать в BookHub!\n\nЧто вы хотите сделать?\n\n"
        "📖 <b>Обучение</b> — узнайте как работает бот\n"
        "➕ <b>Добавить книгу</b> — начните сразу\n"
        "⏭ <b>Пропустить</b> — в главное меню",
        reply_markup=kb, parse_mode=ParseMode.HTML)


async def help_message(message: Message):
    if not await allowed(message): return
    kb = None
    if is_premium_user(message.from_user.id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📩 Написать админу", callback_data="help:writeadmin")]
        ])
    await message.answer(get_text("text_help"), reply_markup=kb, parse_mode=ParseMode.HTML)


async def help_writeadmin_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not is_premium_user(callback.from_user.id):
        await callback.answer("Только для премиума.", show_alert=True)
        return
    await state.set_state(WriteAdmin.text)
    await callback.message.answer("📩 Напишите сообщение админу:", reply_markup=back_keyboard())


async def cmd_faq(message: Message):
    await message.answer(get_text("text_faq"), parse_mode=ParseMode.HTML)


async def cmd_support(message: Message):
    await message.answer(get_text("text_support"), parse_mode=ParseMode.HTML)


async def cmd_rules(message: Message):
    await message.answer(get_text("text_rules"), parse_mode=ParseMode.HTML)


async def cmd_id(message: Message):
    await message.answer(f"🆔 Ваш ID: {message.from_user.id}\n💬 @{message.from_user.username or '—'}")


async def cmd_help_commands(message: Message):
    await message.answer(
        "📋 <b>Команды:</b>\n\n"
        "/start — меню\n/id — ваш ID\n/faq — вопросы\n/support — поддержка\n/rules — правила\n"
        "/promo КОД — активировать промокод\n/cancel — отменить",
        parse_mode=ParseMode.HTML)


# --- Добавление книги ---

async def add_start(message, state):
    if not await allowed(message): return
    ok, count, limit = check_daily_limit(message.from_user.id)
    if not ok:
        await message.answer(f"⚠️ Дневной лимит публикаций: {count}/{limit}. Попробуйте завтра.")
        return
    await state.clear()
    save_session(message.from_user.id, action="add_book")
    await state.set_state(AddBook.title)
    await message.answer("📖 <b>Название книги</b>\n\nВведите название:", reply_markup=back_keyboard(), parse_mode=ParseMode.HTML)


async def add_title(message, state):
    await state.update_data(title=(message.text or "").strip())
    await state.set_state(AddBook.author)
    await message.answer("✍️ <b>Автор</b>\n\nВведите автора:", reply_markup=back_keyboard(), parse_mode=ParseMode.HTML)


async def add_author(message, state):
    await state.update_data(author=(message.text or "").strip(), formats=[])
    await state.set_state(AddBook.book_format)
    await message.answer("📚 <b>Формат</b>\n\nМожно выбрать несколько:", reply_markup=format_keyboard(), parse_mode=ParseMode.HTML)


async def format_toggle_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    selected = list(data.get("formats", []))
    value = callback.data.split(":", 1)[1]
    if value in selected: selected.remove(value)
    else: selected.append(value)
    await state.update_data(formats=selected)
    await safe_edit(callback.message.edit_reply_markup(reply_markup=selected_format_keyboard(selected)))


async def format_done_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    selected = data.get("formats", [])
    if not selected:
        await callback.message.answer("Выберите хотя бы один формат.")
        return
    await state.update_data(book_format=", ".join(selected))
    await state.set_state(AddBook.genre)
    await safe_edit(callback.message.edit_text("🎯 <b>Жанр книги</b>\n\nВыберите жанр:",
        reply_markup=genre_keyboard(), parse_mode=ParseMode.HTML))


async def setgenre_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    genre_id = callback.data.split(":", 1)[1]
    await state.update_data(genre=genre_id)
    data = await state.get_data()
    selected = data.get("formats", [])
    if "Настоящая книга" in selected:
        await state.set_state(AddBook.city)
        await safe_edit(callback.message.edit_text("🏙 <b>Город</b>\n\nВведите город:", parse_mode=ParseMode.HTML))
    else:
        await state.update_data(city="")
        await state.set_state(AddBook.condition)
        await safe_edit(callback.message.edit_text(
            "📝 <b>Состояние / описание</b>\n\nНапишите состояние или нажмите «Пропустить».\n"
            "⚠️ Ссылки и @username запрещены.",
            reply_markup=condition_prompt_kb(), parse_mode=ParseMode.HTML))


async def add_city(message, state):
    await state.update_data(city=(message.text or "").strip())
    await state.set_state(AddBook.condition)
    await message.answer(
        "📝 <b>Состояние / описание</b>\n\nНапишите состояние или нажмите «Пропустить».\n"
        "⚠️ Ссылки и @username запрещены.",
        reply_markup=condition_prompt_kb(), parse_mode=ParseMode.HTML)


async def add_condition(message, state):
    raw = (message.text or "").strip()
    clean = sanitize_free_text(raw)
    if raw and not clean:
        await message.answer("⚠️ Уберите ссылки и @username из описания.")
        return
    await state.update_data(condition=clean)
    await state.set_state(AddBook.deal)
    await message.answer("🤝 <b>Как предложить книгу?</b>", reply_markup=deal_keyboard(), parse_mode=ParseMode.HTML)


async def skip_condition_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    cur = await state.get_state()
    if cur != AddBook.condition.state:
        return
    await state.update_data(condition="")
    await state.set_state(AddBook.deal)
    try: await callback.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await callback.message.answer("🤝 <b>Как предложить книгу?</b>", reply_markup=deal_keyboard(), parse_mode=ParseMode.HTML)


async def add_deal(message, state):
    deal = normalize_deal(message.text or "")
    if not deal:
        await message.answer("Выберите вариант кнопкой."); return
    if "Продать" in deal and not is_premium_user(message.from_user.id):
        await message.answer("💳 Продажа только для премиум. Оформите в профиле.", reply_markup=deal_keyboard())
        return
    await state.update_data(deal_type=deal)
    if "Продать" in deal:
        await state.set_state(AddBook.price)
        await message.answer("💰 <b>Цена</b>\n\nУкажите цену, например: 1200 ₽",
                             reply_markup=back_keyboard(), parse_mode=ParseMode.HTML)
    else:
        await state.set_state(AddBook.photo)
        await message.answer("📷 <b>Фото книги</b>\n\nОтправьте фото или нажмите «Пропустить».",
                             reply_markup=photo_prompt_kb(), parse_mode=ParseMode.HTML)


async def add_price(message, state):
    price = (message.text or "").strip()
    if not price:
        await message.answer("Укажите цену."); return
    await state.update_data(price=price)
    await state.set_state(AddBook.photo)
    await message.answer("📷 <b>Фото книги</b>\n\nОтправьте фото или нажмите «Пропустить».",
                         reply_markup=photo_prompt_kb(), parse_mode=ParseMode.HTML)


async def notify_matches(bot, book):
    with closing(connect()) as db:
        searches = db.execute("SELECT * FROM saved_searches WHERE active=1 AND user_id != ?", (book["owner_id"],)).fetchall()
    title = (book["title"] or "").casefold()
    author = (book["author"] or "").casefold()
    book_formats = {f.strip() for f in (book["format"] or "").split(",") if f.strip()}
    for s in searches:
        q = (s["query"] or "").casefold().strip()
        fmt = (s["format"] or "").strip()
        format_ok = not fmt or fmt == "Любой" or fmt in book_formats
        city_ok = not s["city"] or (s["city"] or "").casefold() == (book["city"] or "").casefold()
        if q and (q in title or q in author) and format_ok and city_ok:
            try:
                await bot.send_message(s["user_id"], "🔔 Совпадение с вашим поиском:")
                await send_book_card(bot, book, chat_id=s["user_id"])
            except Exception: pass


async def _finalize_book(target_message, state, bot, photo_id, user_id, user_name):
    data = await state.get_data()
    with closing(connect()) as db:
        existing = db.execute(
            "SELECT id, title, author, format FROM books WHERE owner_id=? AND status='available'",
            (user_id,)
        ).fetchall()
        new_title_cf = (data.get("title") or "").casefold()
        new_author_cf = (data.get("author") or "").casefold()
        new_format = (data.get("book_format") or "").strip()
        dup = None
        for b in existing:
            if ((b["title"] or "").casefold() == new_title_cf
                    and (b["author"] or "").casefold() == new_author_cf
                    and (b["format"] or "").strip() == new_format):
                dup = b
                break
        if dup:
            await state.clear()
            await target_message.answer(
                f"⚠️ У вас уже есть такая же книга (№{dup['id']}) в этом формате.",
                reply_markup=keyboard(user_id))
            return
        cur = db.execute("""INSERT INTO books (owner_id,title,author,city,condition,format,photo_id,deal_type,price,genre)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user_id, data["title"], data["author"], data.get("city", ""), data.get("condition", ""),
             data["book_format"], photo_id, data["deal_type"], data.get("price"), data.get("genre", "other")))
        book = db.execute("SELECT * FROM books WHERE id=?", (cur.lastrowid,)).fetchone()
        db.commit()
    inc_daily_book(user_id)
    await state.clear()
    await notify_matches(bot, book)
    await target_message.answer("✅ <b>Книга опубликована</b>", reply_markup=keyboard(user_id), parse_mode=ParseMode.HTML)
    await publish_to_channel(bot, book)
    for admin_id in admins():
        try:
            await bot.send_message(admin_id, f"📚 Новая книга от {user_name}: «{book['title']}»")
        except Exception: pass


async def add_photo(message, state, bot):
    if not message.photo:
        await message.answer("📷 Отправьте фото или нажмите «Пропустить».")
        return
    await _finalize_book(message, state, bot,
                         message.photo[-1].file_id,
                         message.from_user.id,
                         message.from_user.first_name or message.from_user.full_name)


async def skip_photo_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    cur = await state.get_state()
    if cur != AddBook.photo.state:
        return
    try: await callback.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await _finalize_book(callback.message, state, bot, None,
                         callback.from_user.id,
                         callback.from_user.first_name or callback.from_user.full_name)


# --- Поиск ---

async def search_start(message, state):
    if not await allowed(message): return
    save_session(message.from_user.id, action="search")
    await state.set_state(SearchBook.query)
    await message.answer("🔍 <b>Поиск книги</b>\n\nВведите название или автора:",
                         reply_markup=back_keyboard(), parse_mode=ParseMode.HTML)


async def search_query(message, state):
    data = await state.update_data(query=(message.text or "").strip())
    query = data["query"].casefold()
    save_session(message.from_user.id, action="search_query", query=data["query"])
    with closing(connect()) as db:
        candidates = db.execute("SELECT * FROM books WHERE status='available' AND owner_id!=? ORDER BY boosted DESC, created_at DESC",
                                (message.from_user.id,)).fetchall()
    words = [w for w in query.split() if w]
    books = [b for b in candidates if words and all(w in (b["title"] or "").casefold() or w in (b["author"] or "").casefold() for w in words)][:50]
    if not books:
        save_search(message.from_user.id, data["query"])
        await state.clear()
        await message.answer("Пока нет. Поиск сохранён в 'Мои поиски'.", reply_markup=keyboard(message.from_user.id))
        return
    await state.update_data(found_book_ids=[b["id"] for b in books])
    await state.set_state(SearchBook.browsing)
    await message.answer(f"Найдено: {len(books)}. Выберите:", reply_markup=keyboard(message.from_user.id))
    await message.answer("\n".join(f"{'⭐ ' if b['boosted'] else ''}{b['id']} · {b['title']} · {b['author']}" for b in books),
                         reply_markup=catalog_keyboard([b["id"] for b in books], 0, 1, prefix="noop_page"))


# --- Каталог ---

async def all_books(message):
    if not await allowed(message): return
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM books WHERE status='available' ORDER BY boosted DESC, views DESC, created_at DESC").fetchall()
    if not rows:
        await message.answer("📭 Каталог пуст. Станьте первым — добавьте книгу!", reply_markup=keyboard(message.from_user.id))
        return
    top5 = rows[:5]
    text = "📚 <b>Каталог</b>\n\n<b>Топ-5 книг:</b>\n\n" + "\n".join(
        f"{i+1}. {'⭐' if b['boosted'] else ''}{b['id']} · {b['title']} · {b['author']}" for i, b in enumerate(top5))
    await message.answer(text, reply_markup=catalog_menu_keyboard(), parse_mode=ParseMode.HTML)


async def catalog_genres_callback(callback: CallbackQuery):
    await callback.answer()
    with closing(connect()) as db:
        rows = db.execute("SELECT genre, COUNT(*) c FROM books WHERE status='available' GROUP BY genre").fetchall()
    counts = {r["genre"] or "other": r["c"] for r in rows}
    await safe_edit(callback.message.edit_text("📖 <b>Жанры</b>\n\nВыберите жанр:",
        reply_markup=genres_catalog_keyboard(counts), parse_mode=ParseMode.HTML))


async def genre_callback(callback: CallbackQuery):
    await callback.answer()
    genre_id = callback.data.split(":", 1)[1]
    with closing(connect()) as db:
        rows = db.execute("""SELECT * FROM books WHERE status='available' AND genre=?
            ORDER BY boosted DESC, views DESC LIMIT 10""", (genre_id,)).fetchall()
    label = dict(GENRES).get(genre_id, "Жанр")
    if not rows:
        await safe_edit(callback.message.edit_text(f"{label}\n\nПока книг нет.", reply_markup=catalog_menu_keyboard()))
        return
    text = f"{label} · Топ-10:\n\n" + "\n".join(
        f"{'⭐' if b['boosted'] else ''}{b['id']} · {b['title']} · {b['author']}" for b in rows)
    ids = [b["id"] for b in rows]
    kb_rows = [[InlineKeyboardButton(text=f"{bid}", callback_data=f"detail:{bid}") for bid in ids[i:i+5]] for i in range(0, len(ids), 5)]
    kb_rows.append([InlineKeyboardButton(text="‹ Жанры", callback_data="catalog_genres"),
                    InlineKeyboardButton(text="⌂ Меню", callback_data="menu")])
    await safe_edit(callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)))


async def catalog_all_callback(callback: CallbackQuery):
    await callback.answer()
    page = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM books WHERE status='available' ORDER BY boosted DESC, views DESC, created_at DESC").fetchall()
    await show_catalog_page(callback, rows, page)


async def show_catalog_page(target, rows, page=0):
    page_size = 8
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    selected = rows[page * page_size:(page + 1) * page_size]
    text = f"📚 Все книги · {page + 1}/{total_pages}\n\n" + "\n".join(
        f"{'⭐ ' if b['boosted'] else ''}{b['id']} · {b['title']} · {b['author']}" for b in selected)
    kb = catalog_keyboard([b["id"] for b in selected], page, total_pages, prefix="catalog_all")
    if isinstance(target, CallbackQuery):
        await safe_edit(target.message.edit_text(text, reply_markup=kb))
    else:
        await target.answer(text, reply_markup=kb)


async def all_searches(message):
    if not await allowed(message): return
    with closing(connect()) as db:
        rows = db.execute("SELECT query, COUNT(*) total FROM saved_searches WHERE active=1 GROUP BY query ORDER BY total DESC LIMIT 30").fetchall()
    await message.answer("\n".join(f"🔎 {x['query']} · {x['total']}" for x in rows) or "Пока никто не ищет.",
                         reply_markup=keyboard(message.from_user.id))


async def book_by_id(message):
    if not await allowed(message): return
    try:
        bid = int((message.text or "").strip())
    except ValueError:
        return
    with closing(connect()) as db:
        book = db.execute("SELECT * FROM books WHERE id=? AND status='available'", (bid,)).fetchone()
    if not book:
        await message.answer("Книга не найдена."); return
    await send_book_card(message, book, viewer_id=message.from_user.id)


async def random_book(message):
    if not await allowed(message): return
    with closing(connect()) as db:
        book = db.execute("SELECT * FROM books WHERE status='available' AND owner_id!=? ORDER BY RANDOM() LIMIT 1",
                          (message.from_user.id,)).fetchone()
    if not book:
        await message.answer("Каталог пуст.", reply_markup=keyboard(message.from_user.id)); return
    await send_book_card(message, book, viewer_id=message.from_user.id)


async def book_of_week(message):
    if not await allowed(message): return
    manual_id = get_setting("book_of_week_id", "")
    book = None
    with closing(connect()) as db:
        if manual_id and manual_id.isdigit():
            book = db.execute("SELECT * FROM books WHERE id=? AND status='available'", (int(manual_id),)).fetchone()
        if not book:
            book = db.execute("""SELECT * FROM books WHERE status='available' AND created_at >= datetime('now','-7 day')
                ORDER BY views DESC LIMIT 1""").fetchone()
    if not book:
        await message.answer("Пока нет данных за неделю."); return
    await message.answer("⭐ <b>Книга недели</b>", parse_mode=ParseMode.HTML)
    await send_book_card(message, book, viewer_id=message.from_user.id)


async def book_detail_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    with closing(connect()) as db:
        book = db.execute("SELECT * FROM books WHERE id=? AND status='available'", (int(callback.data.split(":", 1)[1]),)).fetchone()
    if not book:
        await callback.message.answer("Книга недоступна."); return
    cur_state = await state.get_state()
    if cur_state == SearchBook.browsing:
        data = await state.get_data()
        save_search(callback.from_user.id, data.get("query", ""))
        await state.clear()
    await send_book_card(callback.message, book, viewer_id=callback.from_user.id)


async def by_author_callback(callback: CallbackQuery):
    await callback.answer()
    book_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        book = db.execute("SELECT author FROM books WHERE id=?", (book_id,)).fetchone()
        if not book:
            await callback.message.answer("Книга не найдена."); return
        rows = db.execute("SELECT * FROM books WHERE status='available' AND author=? AND id!=? LIMIT 10",
                          (book["author"], book_id)).fetchall()
    if not rows:
        await callback.message.answer("Других книг этого автора нет."); return
    await callback.message.answer(f"👤 Ещё книги автора {escape(book['author'])}:")
    for b in rows:
        await send_book_card(callback.message, b, viewer_id=callback.from_user.id)


# --- Избранное ---

async def fav_toggle_callback(callback: CallbackQuery):
    await callback.answer()
    book_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        exists = db.execute("SELECT 1 FROM favorites WHERE user_id=? AND book_id=?",
                            (callback.from_user.id, book_id)).fetchone()
        if exists:
            db.execute("DELETE FROM favorites WHERE user_id=? AND book_id=?", (callback.from_user.id, book_id))
            msg = "Удалено из избранного."
        else:
            db.execute("INSERT INTO favorites (user_id,book_id) VALUES (?,?)", (callback.from_user.id, book_id))
            msg = "❤️ Добавлено в избранное."
        db.commit()
    await callback.answer(msg, show_alert=True)


async def my_favorites(target, user_id):
    with closing(connect()) as db:
        rows = db.execute("""SELECT b.* FROM favorites f JOIN books b ON b.id=f.book_id
            WHERE f.user_id=? AND b.status='available' ORDER BY f.created_at DESC""", (user_id,)).fetchall()
    if not rows:
        await target.answer("❤️ Избранное пусто."); return
    await target.answer(f"❤️ У вас в избранном: {len(rows)}")
    for b in rows:
        await send_book_card(target, b, viewer_id=user_id)


# --- Заявки ---

async def request_callback(callback: CallbackQuery, bot: Bot):
    action_type = "обменять" if callback.data.startswith("request:") else "купить"
    action_emoji = "🤝" if callback.data.startswith("request:") else "💳"
    await callback.answer("Отправляю...")
    if banned(callback.from_user.id):
        await callback.message.answer("Ваш аккаунт заблокирован."); return
    book_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        book = db.execute("SELECT * FROM books WHERE id=? AND status='available'", (book_id,)).fetchone()
        if not book or book["owner_id"] == callback.from_user.id:
            await callback.message.answer("Книга недоступна."); return
        try:
            db.execute("INSERT INTO exchange_requests (book_id,requester_id) VALUES (?,?)", (book_id, callback.from_user.id))
            db.commit()
        except sqlite3.IntegrityError:
            await callback.message.answer("Вы уже отправляли заявку."); return
        request_id = db.execute("SELECT id FROM exchange_requests WHERE book_id=? AND requester_id=?",
                                (book_id, callback.from_user.id)).fetchone()["id"]
    await bot.send_message(book["owner_id"], f"{action_emoji} Анонимный пользователь хочет {action_type} «{escape(str(book['title']))}».",
                           reply_markup=request_buttons(request_id))
    await callback.message.answer("✅ Заявка отправлена.")


async def offers_callback(callback: CallbackQuery):
    await callback.answer()
    request_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        request = db.execute("SELECT requester_id, offered_book_ids FROM exchange_requests WHERE id=?", (request_id,)).fetchone()
        if not request or request["requester_id"] == callback.from_user.id:
            await callback.message.answer("Заявка не найдена."); return
        books = db.execute("SELECT id,title,author FROM books WHERE owner_id=? AND status='available'",
                           (request["requester_id"],)).fetchall()
    selected = {int(v) for v in (request["offered_book_ids"] or "").split(",") if v.isdigit()}
    if not books:
        await callback.message.answer("У отправителя нет книг."); return
    await callback.message.answer("Выберите книги отправителя:", reply_markup=offer_keyboard(request_id, books, selected))


async def offer_toggle_callback(callback: CallbackQuery):
    await callback.answer()
    _, raw_req, raw_book = callback.data.split(":")
    request_id, book_id = int(raw_req), int(raw_book)
    with closing(connect()) as db:
        request = db.execute("SELECT requester_id, offered_book_ids FROM exchange_requests WHERE id=?", (request_id,)).fetchone()
        if not request: return
        allowed_book = db.execute("SELECT id FROM books WHERE id=? AND owner_id=? AND status='available'",
                                  (book_id, request["requester_id"])).fetchone()
        owner = db.execute("SELECT owner_id FROM books WHERE id=(SELECT book_id FROM exchange_requests WHERE id=?)", (request_id,)).fetchone()
        if not allowed_book or not owner or owner["owner_id"] != callback.from_user.id: return
        selected = {int(v) for v in (request["offered_book_ids"] or "").split(",") if v.isdigit()}
        selected.symmetric_difference_update({book_id})
        value = ",".join(str(x) for x in sorted(selected))
        db.execute("UPDATE exchange_requests SET offered_book_ids=? WHERE id=?", (value, request_id))
        db.commit()
        books = db.execute("SELECT id,title,author FROM books WHERE owner_id=? AND status='available'",
                           (request["requester_id"],)).fetchall()
    await safe_edit(callback.message.edit_reply_markup(reply_markup=offer_keyboard(request_id, books, selected)))


async def offer_done_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    request_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        request = db.execute("SELECT * FROM exchange_requests WHERE id=?", (request_id,)).fetchone()
        owner = db.execute("SELECT owner_id FROM books WHERE id=?", (request["book_id"],)).fetchone() if request else None
        if not request or not owner or owner["owner_id"] != callback.from_user.id:
            await callback.message.answer("Заявка не найдена."); return
        selected = [int(v) for v in (request["offered_book_ids"] or "").split(",") if v.isdigit()]
        if not selected:
            await callback.message.answer("Выберите хотя бы одну книгу."); return
        placeholders = ",".join("?" for _ in selected)
        books = db.execute(f"SELECT id,title FROM books WHERE owner_id=? AND id IN ({placeholders})",
                           [request["requester_id"], *selected]).fetchall()
        db.execute("UPDATE exchange_requests SET status='counter_offer' WHERE id=?", (request_id,))
        db.commit()
        target = db.execute("SELECT owner_id,title FROM books WHERE id=?", (request["book_id"],)).fetchone()
    names = ", ".join(escape(str(b["title"])) for b in books)
    await bot.send_message(request["requester_id"], f"📚 Встречное предложение на «{escape(str(target['title']))}»: {names}",
                           reply_markup=counter_offer_keyboard(request_id))
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    await callback.message.answer("Отправлено.")


async def offer_cancel_callback(callback: CallbackQuery):
    await callback.answer("Отменено")
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))


def _contact_query(db, request_id):
    return db.execute("""SELECT er.*, b.title, b.owner_id,
        requester.username AS requester_username, requester.name AS requester_name,
        requester.preferred_contact AS requester_contact, requester.is_premium AS requester_is_premium,
        owner.username AS owner_username, owner.name AS owner_name,
        owner.preferred_contact AS owner_contact, owner.is_premium AS owner_is_premium
        FROM exchange_requests er JOIN books b ON b.id=er.book_id
        JOIN users requester ON requester.telegram_id=er.requester_id
        JOIN users owner ON owner.telegram_id=b.owner_id WHERE er.id=?""", (request_id,)).fetchone()


async def _reveal_contacts(row, bot, owner_id, requester_id):
    owner_contact = format_contact(row["owner_id"], row["owner_username"], row["owner_name"], row["owner_contact"], row["owner_is_premium"])
    requester_contact = format_contact(row["requester_id"], row["requester_username"], row["requester_name"], row["requester_contact"], row["requester_is_premium"])
    await bot.send_message(owner_id, f"Контакт отправителя: {requester_contact}")
    await bot.send_message(requester_id, f"Контакт владельца: {owner_contact}")


async def counter_accept_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    request_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        row = _contact_query(db, request_id)
        if not row or row["requester_id"] != callback.from_user.id:
            await callback.message.answer("Заявка не найдена."); return
        db.execute("UPDATE exchange_requests SET owner_consent=1,status='accepted' WHERE id=?", (request_id,))
        db.commit()
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    await callback.message.answer("✅ Обмен принят. Контакты раскрыты.")
    await _reveal_contacts(row, bot, row["owner_id"], row["requester_id"])
    try:
        await bot.send_message(row["requester_id"], "Оцените книгу:", reply_markup=rating_keyboard(request_id))
        await bot.send_message(row["owner_id"], "Оцените сделку:", reply_markup=rating_keyboard(request_id))
    except Exception: pass


async def accept_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    if banned(callback.from_user.id):
        await callback.message.answer("Ваш аккаунт заблокирован."); return
    request_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        row = _contact_query(db, request_id)
        if not row or row["owner_id"] != callback.from_user.id:
            await callback.message.answer("Заявка не найдена."); return
        db.execute("UPDATE exchange_requests SET owner_consent=1,status='accepted' WHERE id=?", (request_id,))
        db.commit()
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    await callback.message.answer("✅ Согласовано. Контакты раскрыты.")
    await _reveal_contacts(row, bot, callback.from_user.id, row["requester_id"])
    try:
        await bot.send_message(row["requester_id"], "Оцените сделку:", reply_markup=rating_keyboard(request_id))
        await bot.send_message(callback.from_user.id, "Оцените сделку:", reply_markup=rating_keyboard(request_id))
    except Exception: pass


async def decline_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer("Отклонено")
    request_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        row = db.execute("""SELECT er.requester_id, b.title FROM exchange_requests er
            JOIN books b ON b.id=er.book_id WHERE er.id=? AND b.owner_id=?""",
            (request_id, callback.from_user.id)).fetchone()
        db.execute("UPDATE exchange_requests SET status='declined' WHERE id=?", (request_id,))
        db.commit()
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    await callback.message.answer("Заявка отклонена.")
    if row:
        try: await bot.send_message(row["requester_id"], f"❌ Владелец отклонил вашу заявку на «{escape(str(row['title']))}».")
        except Exception: pass


async def rate_callback(callback: CallbackQuery, bot: Bot):
    _, raw_req, raw_star = callback.data.split(":")
    request_id, stars = int(raw_req), int(raw_star)
    await callback.answer(f"Спасибо! Оценка: {stars}⭐", show_alert=True)
    with closing(connect()) as db:
        req = db.execute("SELECT book_id FROM exchange_requests WHERE id=?", (request_id,)).fetchone()
        if not req: return
        exists = db.execute("SELECT 1 FROM ratings WHERE book_id=? AND user_id=?",
                            (req["book_id"], callback.from_user.id)).fetchone()
        if exists:
            await callback.message.answer("Вы уже оценивали."); return
        db.execute("INSERT INTO ratings (book_id,user_id,stars) VALUES (?,?,?)", (req["book_id"], callback.from_user.id, stars))
        db.execute("UPDATE books SET rating_sum=rating_sum+?, rating_count=rating_count+1 WHERE id=?", (stars, req["book_id"]))
        db.commit()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(f"⭐ Спасибо! Ваша оценка: {stars}")


async def my_requests(target, user_id):
    with closing(connect()) as db:
        rows = db.execute("""SELECT er.id,er.status,b.title FROM exchange_requests er
            JOIN books b ON b.id=er.book_id WHERE er.requester_id=? OR b.owner_id=?""",
            (user_id, user_id)).fetchall()
    pending = sum(1 for x in rows if x["status"] == "pending")
    await target.answer(f"🤝 Всего: {len(rows)} (ждут: {pending})\n\n" +
        ("\n".join(f"{x['id']} · «{x['title']}» — {x['status']}" for x in rows) or "Заявок нет."))


async def my_ratings(target, user_id):
    with closing(connect()) as db:
        given = db.execute("""SELECT r.stars, b.title FROM ratings r JOIN books b ON b.id=r.book_id
            WHERE r.user_id=? ORDER BY r.created_at DESC LIMIT 20""", (user_id,)).fetchall()
        my = db.execute("""SELECT title, rating_sum, rating_count FROM books
            WHERE owner_id=? AND status='available' AND rating_count > 0
            ORDER BY rating_sum*1.0/rating_count DESC""", (user_id,)).fetchall()
    text = "⭐ <b>Мои оценки</b>\n\n"
    if given:
        text += "<b>Я оценивал книги:</b>\n"
        for g in given:
            text += f"• {escape(str(g['title']))} — {g['stars']}⭐\n"
        text += "\n"
    if my:
        text += "<b>Оценки моих книг:</b>\n"
        for b in my:
            avg = b["rating_sum"] / b["rating_count"]
            text += f"• {escape(str(b['title']))} — ⭐ {avg:.1f} ({b['rating_count']} оценок)\n"
    if not given and not my:
        text = "Оценок пока нет."
    await target.answer(text, parse_mode=ParseMode.HTML)


# --- Профиль ---

async def profile_start(message: Message):
    if not await allowed(message): return
    with closing(connect()) as db:
        row = db.execute("SELECT created_at, name FROM users WHERE telegram_id=?", (message.from_user.id,)).fetchone()
    since = row["created_at"][:10] if row else "сегодня"
    await message.answer(f"👤 Ваш профиль\n🆔 ID: {message.from_user.id}\n📅 С нами с: {since}",
                         reply_markup=profile_keyboard(message.from_user.id))


async def premium_info(target, user_id):
    premium = is_premium_user(user_id)
    text = "⭐ <b>Премиум</b>\n\n"
    if premium:
        with closing(connect()) as db:
            row = db.execute("SELECT premium_until FROM users WHERE telegram_id=?", (user_id,)).fetchone()
        until = row["premium_until"][:10] if row and row["premium_until"] else "бессрочно"
        text += f"У вас активен премиум до: {until}\n\n• Продажа книг\n• Поднятие в топ\n• Свой контакт"
        await target.answer(text, parse_mode=ParseMode.HTML); return
    text += ("Премиум даёт:\n• Продажу книг\n• Boost в топ\n• Свой контакт\n\n" + get_text("text_premium_info"))
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил, сообщить админу", callback_data="premium_notify")]])
    await target.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def premium_notify_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer("Отправлено", show_alert=True)
    u = callback.from_user
    for admin_id in admins():
        try:
            await bot.send_message(admin_id,
                f"⭐ @{u.username or u.full_name} (ID: {u.id}) хочет премиум.\nВыдать: ⚙ Админка → ⭐ Премиум → отправьте {u.id}")
        except Exception: pass


async def profile_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if banned(callback.from_user.id):
        await callback.message.answer("Аккаунт заблокирован."); return
    action = callback.data.split(":", 1)[1]
    if action == "books": await my_books(callback.message, callback.from_user.id)
    elif action == "favs": await my_favorites(callback.message, callback.from_user.id)
    elif action == "searches": await my_searches(callback.message, callback.from_user.id)
    elif action == "requests": await my_requests(callback.message, callback.from_user.id)
    elif action == "ratings": await my_ratings(callback.message, callback.from_user.id)
    elif action == "gift":
        with closing(connect()) as db:
            rows = db.execute("SELECT * FROM books WHERE status='available' AND deal_type LIKE '%Подарить%' AND owner_id!=? ORDER BY created_at DESC LIMIT 10",
                              (callback.from_user.id,)).fetchall()
        if not rows:
            await callback.message.answer("🎁 Пока никто не дарит книги.")
        else:
            await callback.message.answer(f"🎁 Дарят книг: {len(rows)}")
            for b in rows:
                await send_book_card(callback.message, b, viewer_id=callback.from_user.id)
    elif action == "report":
        await state.set_state(ReportUser.target)
        await callback.message.answer("Введите Telegram ID нарушителя:")
    elif action == "premium": await premium_info(callback.message, callback.from_user.id)
    elif action == "invite":
        me = await callback.bot.me()
        ref_link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
        share_text = "📚 BookHub — книги и обмены! Заходи по ссылке и получи 1 день премиума 🎁"
        share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_text)}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)],
        ])
        await callback.message.answer(
            f"👥 <b>Пригласить друга</b>\n\n"
            f"Ваша ссылка:\n<code>{escape(ref_link)}</code>\n\n"
            f"🎁 Вам +{REFERRAL_DAYS} дня премиума за друга\n"
            f"🎁 Друг получает +{REFERRAL_NEW_USER_DAYS} день премиума",
            reply_markup=kb, parse_mode=ParseMode.HTML)
    elif action == "contact":
        if not is_premium_user(callback.from_user.id):
            await callback.message.answer("Только для премиума."); return
        await state.set_state(ProfileEdit.contact)
        await callback.message.answer("✏ Отправьте контакт для показа:", reply_markup=back_keyboard())


async def write_admin_save(message, state):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напишите текст."); return
    u = message.from_user
    for admin_id in admins():
        try:
            await message.bot.send_message(admin_id, f"📩 От @{u.username or u.full_name} (ID: {u.id}):\n\n{text}")
        except Exception: pass
    await state.clear()
    await message.answer("✅ Отправлено админу.", reply_markup=keyboard(message.from_user.id))


async def profile_contact_save(message, state):
    if not is_premium_user(message.from_user.id):
        await state.clear(); return
    contact = (message.text or "").strip()
    if not contact:
        await message.answer("Отправьте текст."); return
    with closing(connect()) as db:
        db.execute("UPDATE users SET preferred_contact=? WHERE telegram_id=?", (contact, message.from_user.id))
        db.commit()
    await state.clear()
    await message.answer("✅ Обновлено.", reply_markup=keyboard(message.from_user.id))


async def my_books(target, user_id):
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM books WHERE owner_id=? AND status!='deleted' ORDER BY created_at DESC", (user_id,)).fetchall()
    if not rows:
        await target.answer("У вас нет книг."); return
    await target.answer(f"📖 У вас книг: {len(rows)}")
    premium = is_premium_user(user_id)
    for book in rows:
        buttons = [InlineKeyboardButton(text="🗑", callback_data=f"mybook_del:{book['id']}")]
        if premium and book["status"] == "available":
            bt = "⬇" if book["boosted"] else "🚀"
            buttons.append(InlineKeyboardButton(text=bt, callback_data=f"mybook_boost:{book['id']}"))
        note = "" if book["status"] == "available" else " · неактивна"
        star = "⭐ " if book["boosted"] else ""
        fmt = format_display(book["format"])
        rate_str = ""
        if book["rating_count"]:
            avg = book["rating_sum"] / book["rating_count"]
            rate_str = f" · ⭐ {avg:.1f} ({book['rating_count']})"
        await target.answer(f"{star}#{book['id']} · {book['title']} · {book['author']}\n{fmt}{rate_str}{note}",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]))


async def my_searches(target, user_id):
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM saved_searches WHERE user_id=? AND active=1", (user_id,)).fetchall()
    await target.answer(f"🔔 Поисков: {len(rows)}\n\n" +
        ("\n".join(f"🔔 {x['query']}" for x in rows) or "Поисков нет."))


async def mybook_delete_callback(callback: CallbackQuery):
    await callback.answer()
    book_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        row = db.execute("SELECT owner_id FROM books WHERE id=?", (book_id,)).fetchone()
        if not row or row["owner_id"] != callback.from_user.id:
            await callback.message.answer("Не найдено."); return
        requesters = db.execute("SELECT requester_id FROM exchange_requests WHERE book_id=? AND status='pending'", (book_id,)).fetchall()
        db.execute("UPDATE books SET status='deleted' WHERE id=?", (book_id,))
        db.commit()
    for r in requesters:
        try: await callback.bot.send_message(r["requester_id"], "❌ Книга была удалена.")
        except Exception: pass
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    await callback.message.answer("Удалено.")


async def mybook_boost_callback(callback: CallbackQuery):
    await callback.answer()
    book_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        row = db.execute("SELECT owner_id, boosted FROM books WHERE id=? AND status='available'", (book_id,)).fetchone()
        if not row or row["owner_id"] != callback.from_user.id:
            await callback.message.answer("Не найдено."); return
        new_value = 0 if row["boosted"] else 1
        db.execute("UPDATE books SET boosted=? WHERE id=?", (new_value, book_id))
        db.commit()
    bt = "⬇" if new_value else "🚀"
    await safe_edit(callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑", callback_data=f"mybook_del:{book_id}"),
        InlineKeyboardButton(text=bt, callback_data=f"mybook_boost:{book_id}")]])))


# --- Жалобы ---

async def report_book_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if banned(callback.from_user.id):
        await callback.message.answer("Аккаунт заблокирован."); return
    book_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        book = db.execute("SELECT owner_id FROM books WHERE id=?", (book_id,)).fetchone()
    if not book or book["owner_id"] == callback.from_user.id:
        await callback.message.answer("Нельзя."); return
    await state.update_data(target=book["owner_id"])
    await state.set_state(ReportUser.reason)
    await callback.message.answer("Опишите причину:")


async def report_reason(message, state, bot):
    data = await state.update_data(reason=(message.text or "").strip())
    with closing(connect()) as db:
        db.execute("INSERT INTO reports (reporter_id,reported_id,reason) VALUES (?,?,?)",
                   (message.from_user.id, data["target"], data["reason"]))
        db.commit()
    for admin_id in admins():
        try:
            await bot.send_message(admin_id, f"🚩 Жалоба от {message.from_user.id} на {data['target']}: {data['reason']}\nБан: /ban_{data['target']}")
        except Exception: pass
    await state.clear()
    await message.answer("Жалоба отправлена.")


# --- Промокоды ---

async def promo_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /promo КОД"); return
    code = parts[1].strip().upper()
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
        if not row:
            await message.answer("❌ Неверный код."); return
        if row["uses_left"] <= 0:
            await message.answer("❌ Код использован."); return
        used = set(row["used_by"].split(",")) if row["used_by"] else set()
        if str(message.from_user.id) in used:
            await message.answer("Вы уже использовали этот код."); return
        used.add(str(message.from_user.id))
        db.execute("UPDATE promo_codes SET uses_left=uses_left-1, used_by=? WHERE code=?",
                   (",".join(used), code))
        db.commit()
    grant_premium_days(message.from_user.id, row["days"])
    await message.answer(f"🎉 Промокод активирован! +{row['days']} дней премиума.")


# --- Админка ---

async def admin(message):
    if not is_admin(message.from_user.id): return
    await message.answer("⚙ Админка", reply_markup=admin_menu_keyboard())


async def admin_stats(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    with closing(connect()) as db:
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM users WHERE is_banned=0").fetchone()[0]
        books = db.execute("SELECT COUNT(*) FROM books WHERE status='available'").fetchone()[0]
        searches = db.execute("SELECT COUNT(*) FROM saved_searches WHERE active=1").fetchone()[0]
        requests = db.execute("SELECT COUNT(*) FROM exchange_requests").fetchone()[0]
        reports_count = db.execute("SELECT COUNT(*) FROM reports WHERE status='new'").fetchone()[0]
        premium_count = db.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
        total_views = db.execute("SELECT SUM(views) FROM books").fetchone()[0] or 0
        male = db.execute("SELECT COUNT(*) FROM users WHERE gender='male'").fetchone()[0]
        female = db.execute("SELECT COUNT(*) FROM users WHERE gender='female'").fetchone()[0]
        age_stats = db.execute("SELECT age, COUNT(*) c FROM users WHERE age IS NOT NULL GROUP BY age ORDER BY c DESC").fetchall()
        genre_raw = db.execute("SELECT favorite_genres FROM users WHERE favorite_genres IS NOT NULL AND favorite_genres != ''").fetchall()
        genre_count = {}
        for r in genre_raw:
            for g in r[0].split(","):
                g = g.strip()
                if g: genre_count[g] = genre_count.get(g, 0) + 1
        top_genres = sorted(genre_count.items(), key=lambda x: x[1], reverse=True)[:5]
        top_users = db.execute("""SELECT owner_id, COUNT(*) c FROM books WHERE status!='deleted'
            GROUP BY owner_id ORDER BY c DESC LIMIT 10""").fetchall()
    text = "📊 <b>Статистика</b>\n\n"
    text += f"👥 Пользователей: {users} ({active} активных)\n📚 Книг: {books}\n🔎 Поисков: {searches}\n"
    text += f"🤝 Заявок: {requests}\n🚩 Жалоб: {reports_count}\n⭐ Премиум: {premium_count}\n👁 Просмотров: {total_views}\n\n"
    if users > 0:
        text += f"👨 М: {male} ({int(male*100/users)}%) · 👩 Ж: {female} ({int(female*100/users)}%)\n\n"
    text += "<b>Возраст:</b>\n"
    for age, c in age_stats:
        text += f"• {escape(str(age))}: {c}\n"
    text += "\n<b>Топ жанров:</b>\n"
    for g, c in top_genres: text += f"• {escape(str(g))}: {c}\n"
    text += "\n<b>Топ-10 активных:</b>\n"
    for u in top_users: text += f"• {u['owner_id']} — {u['c']}\n"
    await message.answer(text, reply_markup=admin_menu_keyboard(), parse_mode=ParseMode.HTML)


async def admin_broadcast_start(message, state):
    if is_admin(message.from_user.id):
        await state.set_state(AdminBroadcast.message)
        await message.answer("Отправьте сообщение. /cancel отменяет.")


async def admin_broadcast_send(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    with closing(connect()) as db:
        users = db.execute("SELECT telegram_id FROM users WHERE is_banned=0").fetchall()
    await message.answer(f"Рассылка на {len(users)}...")
    sent = 0
    for u in users:
        try:
            await message.copy_to(u["telegram_id"]); sent += 1
        except Exception: pass
        await asyncio.sleep(BROADCAST_DELAY)
    await state.clear()
    await message.answer(f"Готово: {sent}")


async def admin_backup(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    backup_name = f"books-backup-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
    backup_path = BASE_DIR / backup_name
    try:
        src = connect()
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst)
        dst.close()
        src.close()
        with open(backup_path, "rb") as f:
            data = f.read()
        try: backup_path.unlink()
        except Exception: pass
        file = BufferedInputFile(data, filename=backup_name)
        await message.answer_document(file, caption="💾 Бэкап базы")
    except Exception as e:
        await message.answer(f"❌ Ошибка бэкапа: {e}")


async def admin_export(message, kind, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    try:
        buf = io.StringIO()
        w = csv.writer(buf)
        with closing(connect()) as db:
            if kind == "books":
                w.writerow(["id","owner_id","title","author","city","condition","format","genre","deal_type","price","status","views","created_at"])
                for r in db.execute("SELECT * FROM books").fetchall():
                    w.writerow([r["id"], r["owner_id"], r["title"], r["author"], r["city"], r["condition"],
                                r["format"], r["genre"], r["deal_type"], r["price"], r["status"], r["views"], r["created_at"]])
            else:
                w.writerow(["telegram_id","username","name","is_banned","is_premium","created_at"])
                for r in db.execute("SELECT * FROM users").fetchall():
                    w.writerow([r["telegram_id"], r["username"], r["name"], r["is_banned"], r["is_premium"], r["created_at"]])
        data = buf.getvalue().encode("utf-8")
        file = BufferedInputFile(data, filename=f"{kind}.csv")
        await message.answer_document(file, caption=f"📥 Экспорт {kind}")
    except Exception as e:
        await message.answer(f"❌ Ошибка экспорта: {e}")


async def admin_callback(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    await callback.answer()
    action = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    if action == "stats": await admin_stats(callback.message, admin_id)
    elif action == "books": await admin_books(callback.message, admin_id)
    elif action == "users": await admin_users(callback.message, admin_id)
    elif action == "reports": await reports(callback.message, admin_id)
    elif action == "searches": await admin_searches(callback.message, admin_id)
    elif action == "subscriptions": await admin_channels_menu(callback.message, admin_id)
    elif action == "news": await admin_news_menu(callback.message, admin_id)
    elif action == "premium": await admin_premium_menu(callback.message, admin_id)
    elif action == "promo": await admin_promo_menu(callback.message, admin_id)
    elif action == "texts": await admin_texts_menu(callback.message, admin_id)
    elif action == "backup": await admin_backup(callback.message, admin_id)
    elif action == "export":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📚 Книги", callback_data="admin:export_books"),
             InlineKeyboardButton(text="👥 Юзеры", callback_data="admin:export_users")]])
        await callback.message.answer("Что выгрузить?", reply_markup=kb)
    elif action == "export_books": await admin_export(callback.message, "books", admin_id)
    elif action == "export_users": await admin_export(callback.message, "users", admin_id)
    elif action == "msg":
        await state.set_state(AdminMsg.user_id)
        await callback.message.answer("Введите ID юзера:")
    elif action == "find":
        await callback.message.answer("Использование: /find @username")
    elif action in ("close_channels", "close_premium", "close_promo", "close_texts", "close_news", "close"):
        await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    elif action == "premium_info":
        await state.set_state(AdminPremium.info)
        await callback.message.answer("Пришлите текст условий премиума:", reply_markup=back_keyboard())
    elif action == "premium_grant":
        await state.set_state(AdminPremium.grant)
        await callback.message.answer("Пришлите ID юзера (выдать/снять):", reply_markup=back_keyboard())
    elif action == "promo_add":
        await state.set_state(AdminPromo.code)
        await callback.message.answer("Пришлите код промокода:", reply_markup=back_keyboard())
    elif action.startswith("promo_del:"):
        code = action.split(":", 1)[1]
        with closing(connect()) as db:
            db.execute("DELETE FROM promo_codes WHERE code=?", (code,)); db.commit()
        await admin_promo_menu(callback.message, admin_id)
    elif action.startswith("edit_text:"):
        key = action.split(":", 1)[1]
        await state.update_data(text_key=key)
        await state.set_state(AdminTexts.key)
        await callback.message.answer(f"Пришлите новый текст для '{key}':", reply_markup=back_keyboard())
    elif action == "toggle_sub":
        set_setting("subscription_required", "0" if subscription_required() else "1")
        await admin_channels_menu(callback.message, admin_id)
    elif action == "add_channel":
        await state.set_state(AdminChannel.add)
        await callback.message.answer("Перешлите сообщение из канала или @username:", reply_markup=back_keyboard())
    elif action.startswith("del_channel:"):
        cid = int(action.split(":", 1)[1])
        with closing(connect()) as db:
            db.execute("DELETE FROM required_channels WHERE id=?", (cid,)); db.commit()
        await admin_channels_menu(callback.message, admin_id)
    elif action == "news_toggle":
        set_setting("news_channel_enabled", "0" if news_channel_enabled() else "1")
        await admin_news_menu(callback.message, admin_id)
    elif action == "news_set":
        await state.set_state(AdminNews.channel)
        await callback.message.answer("Перешлите сообщение из канала или пришлите @username:", reply_markup=back_keyboard())
    elif action == "news_clear":
        set_setting("news_channel_id", "")
        await admin_news_menu(callback.message, admin_id)
    elif action == "news_test":
        await admin_news_test(callback.message, admin_id)
    elif action == "broadcast":
        await state.set_state(AdminBroadcast.message)
        await callback.message.answer("Пришлите сообщение:", reply_markup=back_keyboard())
    elif action.startswith("ban:") or action.startswith("unban:"):
        uid = int(action.split(":", 1)[1])
        val = 1 if action.startswith("ban:") else 0
        with closing(connect()) as db:
            db.execute("UPDATE users SET is_banned=? WHERE telegram_id=?", (val, uid)); db.commit()
        await callback.message.answer("Готово.", reply_markup=admin_menu_keyboard())
    elif action.startswith("delete_user:"):
        uid = int(action.split(":", 1)[1])
        if uid in admins(): await callback.message.answer("Нельзя удалить админа."); return
        with closing(connect()) as db:
            db.execute("DELETE FROM books WHERE owner_id=?", (uid,))
            db.execute("DELETE FROM saved_searches WHERE user_id=?", (uid,))
            db.execute("DELETE FROM reports WHERE reporter_id=? OR reported_id=?", (uid, uid))
            db.execute("DELETE FROM favorites WHERE user_id=?", (uid,))
            db.execute("DELETE FROM users WHERE telegram_id=?", (uid,))
            db.commit()
        await callback.message.answer("Удалено.", reply_markup=admin_menu_keyboard())
    elif action.startswith("delete_book:"):
        bid = int(action.split(":", 1)[1])
        with closing(connect()) as db:
            db.execute("UPDATE books SET status='deleted' WHERE id=?", (bid,)); db.commit()
        await callback.message.answer("Удалено.", reply_markup=admin_menu_keyboard())
    elif action.startswith("delete_search:"):
        sid = int(action.split(":", 1)[1])
        with closing(connect()) as db:
            db.execute("UPDATE saved_searches SET active=0 WHERE id=?", (sid,)); db.commit()
        await callback.message.answer("Удалено.", reply_markup=admin_menu_keyboard())
    elif action.startswith("edit_search:"):
        sid = int(action.split(":", 1)[1])
        await state.update_data(search_id=sid)
        await state.set_state(AdminEdit.book)
        await callback.message.answer("Новый текст:", reply_markup=back_keyboard())
    elif action.startswith("edit_book:"):
        await state.update_data(book_id=int(action.split(":", 1)[1]))
        await state.set_state(AdminEdit.book)
        await callback.message.answer("Напишите: название | автор | формат | город | состояние", reply_markup=back_keyboard())


async def admin_books(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM books ORDER BY id DESC LIMIT 50").fetchall()
    if not rows:
        await message.answer("Книг нет.", reply_markup=admin_menu_keyboard()); return
    for b in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✏", callback_data=f"admin:edit_book:{b['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"admin:delete_book:{b['id']}")]])
        await message.answer(f"#{b['id']} · {b['title']} · {b['author']}\n{format_display(b['format'])} · {b['genre'] or '—'}", reply_markup=kb)
    await message.answer("Раздел:", reply_markup=admin_menu_keyboard())


async def admin_users(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    with closing(connect()) as db:
        rows = db.execute("SELECT telegram_id,name,username,is_banned FROM users ORDER BY created_at DESC LIMIT 50").fetchall()
    for u in rows:
        status = "бан" if u["is_banned"] else "ок"
        actions = [InlineKeyboardButton(text="Разбан" if u["is_banned"] else "Бан",
                                        callback_data=f"admin:{'unban' if u['is_banned'] else 'ban'}:{u['telegram_id']}")]
        if u["telegram_id"] not in admins():
            actions.append(InlineKeyboardButton(text="🗑", callback_data=f"admin:delete_user:{u['telegram_id']}"))
        await message.answer(f"{u['username'] or u['name']} · {status}\nID: {u['telegram_id']}",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[actions]))
    await message.answer("Раздел:", reply_markup=admin_menu_keyboard())


async def reports(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM reports WHERE status='new' ORDER BY id DESC").fetchall()
    if not rows:
        await message.answer("Жалоб нет.", reply_markup=admin_menu_keyboard()); return
    for r in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Бан", callback_data=f"admin:ban:{r['reported_id']}")]])
        await message.answer(f"№{r['id']} · {r['reporter_id']} → {r['reported_id']}\n{r['reason']}", reply_markup=kb)
    await message.answer("Раздел:", reply_markup=admin_menu_keyboard())


async def admin_searches(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    with closing(connect()) as db:
        rows = db.execute("SELECT id, query, COUNT(*) c FROM saved_searches WHERE active=1 GROUP BY query ORDER BY c DESC LIMIT 50").fetchall()
    if not rows:
        await message.answer("Поисков нет.", reply_markup=admin_menu_keyboard()); return
    for s in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✏", callback_data=f"admin:edit_search:{s['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"admin:delete_search:{s['id']}")]])
        await message.answer(f"🔎 \"{s['query']}\" · {s['c']}", reply_markup=kb)
    await message.answer("Раздел:", reply_markup=admin_menu_keyboard())


def admin_channels_keyboard():
    channels = required_channels_list()
    required = subscription_required()
    rows = [[InlineKeyboardButton(text=f"❌ {c['title'] or c['chat_id']}", callback_data=f"admin:del_channel:{c['id']}")] for c in channels]
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="admin:add_channel"),
                 InlineKeyboardButton(text="🔴 Выкл" if required else "🟢 Вкл", callback_data="admin:toggle_sub")])
    rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="admin:close_channels")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_channels_menu(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    channels = required_channels_list()
    text = "📢 <b>Подписки</b>\n\nСтатус: " + ("вкл ✅" if subscription_required() else "выкл ❌")
    text += "\n\nКаналы:\n" + ("\n".join(f"• {c['title'] or c['chat_id']}" for c in channels) if channels else "нет")
    await message.answer(text, reply_markup=admin_channels_keyboard(), parse_mode=ParseMode.HTML)


async def admin_channel_add_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    if message.forward_from_chat:
        chat_ref = message.forward_from_chat.id
    else:
        text = (message.text or "").strip()
        if not text:
            await message.answer("Пришлите @username или перешлите пост."); return
        chat_ref = text if text.startswith("@") else f"@{text}"
    try:
        chat = await message.bot.get_chat(chat_ref)
    except Exception:
        await message.answer("Не найден. Бот должен быть в канале."); return
    link = f"https://t.me/{chat.username}" if chat.username else None
    if not link:
        try: link = await message.bot.export_chat_invite_link(chat.id)
        except Exception: link = None
    with closing(connect()) as db:
        db.execute("INSERT INTO required_channels (chat_id,title,invite_link) VALUES (?,?,?)",
                   (str(chat.id), chat.title, link))
        db.commit()
    await state.clear()
    await message.answer(f"✅ «{chat.title}» добавлен.", reply_markup=admin_menu_keyboard())


def admin_news_keyboard():
    enabled = news_channel_enabled()
    channel = news_channel_id()
    rows = []
    if channel:
        rows.append([InlineKeyboardButton(text="👁 Отправить тест-пост", callback_data="admin:news_test")])
        rows.append([InlineKeyboardButton(text="❌ Убрать канал", callback_data="admin:news_clear")])
    rows.append([InlineKeyboardButton(text="✏ Указать канал", callback_data="admin:news_set")])
    rows.append([InlineKeyboardButton(text=("🔴 Выключить" if enabled else "🟢 Включить"), callback_data="admin:news_toggle")])
    rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="admin:close_news")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_news_menu(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    enabled = news_channel_enabled()
    channel = news_channel_id()
    text = "📰 <b>Новостной канал</b>\n\n"
    text += f"Статус: {'включён ✅' if enabled else 'выключен ❌'}\n"
    text += f"Канал ID: {channel or '—'}\n\n"
    text += "Когда включено — каждая новая книга автоматически публикуется в канал с кнопкой «Посмотреть / получить книгу»."
    await message.answer(text, reply_markup=admin_news_keyboard(), parse_mode=ParseMode.HTML)


async def admin_news_channel_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    if message.forward_from_chat:
        chat_ref = message.forward_from_chat.id
    else:
        text = (message.text or "").strip()
        if not text:
            await message.answer("Пришлите @username или перешлите пост из канала."); return
        chat_ref = text if text.startswith("@") else f"@{text}"
    try:
        chat = await message.bot.get_chat(chat_ref)
    except Exception:
        await message.answer("Канал не найден. Бот должен быть добавлен в канал."); return
    set_setting("news_channel_id", str(chat.id))
    await state.clear()
    await message.answer(f"✅ Новостной канал: «{chat.title}».", reply_markup=admin_menu_keyboard())


async def admin_news_test(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    channel = news_channel_id()
    if not channel:
        await message.answer("Сначала укажите канал.")
        return
    with closing(connect()) as db:
        book = db.execute("SELECT * FROM books WHERE status='available' ORDER BY id DESC LIMIT 1").fetchone()
    if not book:
        await message.answer("Нет книг для теста.")
        return
    text = f"🧪 <b>Тест публикации</b>\n\n{book_text(book, max_len=900)}"
    kb = channel_post_keyboard(book["id"])
    try:
        if book["photo_id"]:
            await message.bot.send_photo(channel, book["photo_id"], caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await message.bot.send_message(channel, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        await message.answer("✅ Тест-пост отправлен.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


def admin_premium_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏ Условия оплаты", callback_data="admin:premium_info"),
         InlineKeyboardButton(text="🎁 Выдать/снять", callback_data="admin:premium_grant")],
        [InlineKeyboardButton(text="‹ Назад", callback_data="admin:close_premium")]])


async def admin_premium_menu(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    with closing(connect()) as db:
        cnt = db.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
    await message.answer(f"⭐ <b>Премиум</b>\n\nВ базе: {cnt}\n\nУсловия:\n{get_text('text_premium_info')}",
                         reply_markup=admin_premium_keyboard(), parse_mode=ParseMode.HTML)


async def admin_premium_info_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    set_setting("text_premium_info", (message.text or "").strip())
    await state.clear()
    await message.answer("✅ Обновлено.", reply_markup=admin_menu_keyboard())


async def admin_premium_grant_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    t = (message.text or "").strip()
    if not t.isdigit():
        await message.answer("Нужен числовой ID."); return
    uid = int(t)
    with closing(connect()) as db:
        row = db.execute("SELECT is_premium, premium_until FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not row:
            await message.answer("Пользователь не найден."); return
        try:
            is_active = bool(row["is_premium"]) or (row["premium_until"] and datetime.fromisoformat(row["premium_until"].split(".")[0]) > utcnow())
        except Exception:
            is_active = bool(row["is_premium"])
        if is_active:
            db.execute("UPDATE users SET is_premium=0, premium_until=NULL WHERE telegram_id=?", (uid,))
            res = "снят ❌"
        else:
            db.execute("UPDATE users SET is_premium=1 WHERE telegram_id=?", (uid,))
            res = "выдан ✅"
        db.commit()
    invalidate_premium_cache(uid)
    await state.clear()
    await message.answer(f"Премиум {res}", reply_markup=admin_menu_keyboard())
    try: await message.bot.send_message(uid, f"Ваш премиум-статус: {res}")
    except Exception: pass


def admin_promo_keyboard():
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT 20").fetchall()
    items = [(r["code"], r["days"], r["uses_left"]) for r in rows]
    kb = []
    for i in range(0, len(items), 2):
        row = []
        for code, days, left in items[i:i+2]:
            row.append(InlineKeyboardButton(text=f"❌ {code} (+{days}д, {left})", callback_data=f"admin:promo_del:{code}"))
        kb.append(row)
    kb.append([InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin:promo_add")])
    kb.append([InlineKeyboardButton(text="‹ Назад", callback_data="admin:close_promo")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def admin_promo_menu(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    await message.answer("🎁 <b>Промокоды</b>", reply_markup=admin_promo_keyboard(), parse_mode=ParseMode.HTML)


async def admin_promo_code_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    code = (message.text or "").strip().upper()
    if not code:
        await message.answer("Пришлите код."); return
    await state.update_data(promo_code=code)
    await state.set_state(AdminPromo.days)
    await message.answer("Сколько дней премиума даёт?")


async def admin_promo_days_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    t = (message.text or "").strip()
    if not t.isdigit():
        await message.answer("Нужно число дней."); return
    await state.update_data(promo_days=int(t))
    await state.set_state(AdminPromo.uses)
    await message.answer("Сколько раз можно использовать?")


async def admin_promo_uses_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    t = (message.text or "").strip()
    if not t.isdigit():
        await message.answer("Нужно число."); return
    data = await state.get_data()
    with closing(connect()) as db:
        db.execute("INSERT OR REPLACE INTO promo_codes (code,days,uses_left) VALUES (?,?,?)",
                   (data["promo_code"], data["promo_days"], int(t)))
        db.commit()
    await state.clear()
    await message.answer("✅ Промокод создан.", reply_markup=admin_menu_keyboard())


def admin_texts_keyboard():
    keys = ["text_faq", "text_support", "text_help", "text_rules", "text_premium_info"]
    kb = []
    for i in range(0, len(keys), 2):
        row = [InlineKeyboardButton(text=f"✏ {k}", callback_data=f"admin:edit_text:{k}") for k in keys[i:i+2]]
        kb.append(row)
    kb.append([InlineKeyboardButton(text="‹ Назад", callback_data="admin:close_texts")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def admin_texts_menu(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    await message.answer("✏️ <b>Редактирование текстов</b>\n\nВыберите что изменить:",
                         reply_markup=admin_texts_keyboard(), parse_mode=ParseMode.HTML)


async def admin_text_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    data = await state.get_data()
    key = data.get("text_key")
    if not key:
        await state.clear(); return
    set_setting(key, (message.text or "").strip())
    await state.clear()
    await message.answer("✅ Текст обновлён.", reply_markup=admin_menu_keyboard())


async def admin_msg_userid(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    t = (message.text or "").strip()
    if not t.isdigit():
        await message.answer("Нужен числовой ID."); return
    await state.update_data(target_user=int(t))
    await state.set_state(AdminMsg.text)
    await message.answer("Введите текст сообщения:")


async def admin_msg_send(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    data = await state.get_data()
    try:
        await message.bot.send_message(data["target_user"], f"📩 Сообщение от админа:\n\n{message.text}")
        await message.answer("✅ Отправлено.", reply_markup=admin_menu_keyboard())
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
    await state.clear()


async def cmd_find(message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /find @username"); return
    uname = parts[1].strip().lstrip("@")
    with closing(connect()) as db:
        row = db.execute("SELECT telegram_id, name, username FROM users WHERE username=?", (uname,)).fetchone()
    if not row:
        await message.answer("Не найден."); return
    await message.answer(f"Найден: {row['name']} @{row['username']}\nID: {row['telegram_id']}")


async def admin_edit_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    data = await state.get_data()
    if "search_id" in data:
        with closing(connect()) as db:
            db.execute("UPDATE saved_searches SET query=? WHERE id=?", ((message.text or "").strip(), data["search_id"]))
            db.commit()
        await state.clear()
        await message.answer("✅ Обновлено.", reply_markup=admin_menu_keyboard())
    else:
        try:
            title, author, book_format, city, condition = [x.strip() for x in (message.text or "").split("|", 4)]
            formats_list = [f.strip() for f in book_format.split(",") if f.strip()]
            if not formats_list or not all(f in FORMATS for f in formats_list):
                raise ValueError
        except ValueError:
            await message.answer("Формат: название | автор | формат | город | состояние\n"
                                 "(форматы через запятую: Настоящая книга, Аудио, Видео, PDF)"); return
        with closing(connect()) as db:
            db.execute("UPDATE books SET title=?, author=?, format=?, city=?, condition=? WHERE id=?",
                       (title, author, book_format, city, condition, data["book_id"]))
            db.commit()
        await state.clear()
        await message.answer("✅ Обновлено.", reply_markup=admin_menu_keyboard())


async def ban_action(message):
    if not is_admin(message.from_user.id): return
    cmd, raw = message.text.split("_", 1)
    uid = int(raw)
    with closing(connect()) as db:
        db.execute("UPDATE users SET is_banned=? WHERE telegram_id=?", (1 if cmd == "/ban" else 0, uid))
        db.commit()
    await message.answer("Готово.")


# --- Меню / переходы ---

async def menu_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(greeting_text(callback.from_user.first_name), reply_markup=keyboard(callback.from_user.id))


async def noop_callback(callback: CallbackQuery):
    await callback.answer()


async def check_subs_callback(callback: CallbackQuery):
    ok, missing = await check_subscriptions(callback.bot, callback.from_user.id)
    if ok:
        await callback.answer("Подписка ✅", show_alert=True)
        try: await callback.message.delete()
        except Exception: pass
        await callback.message.answer("Спасибо! Бот доступен.", reply_markup=keyboard(callback.from_user.id))
    else:
        await callback.answer("Подпишитесь на все каналы.", show_alert=True)


async def cancel(message, state):
    await state.clear()
    await message.answer("Отменено.", reply_markup=keyboard(message.from_user.id))


async def go_menu(message, state):
    await state.clear()
    await start(message, state)


async def fallback_message(message: Message, state: FSMContext):
    if not await allowed(message): return
    with closing(connect()) as db:
        user = db.execute("SELECT onboarded FROM users WHERE telegram_id=?", (message.from_user.id,)).fetchone()
    if user and not user["onboarded"]:
        await state.set_state(Onboarding.step)
        await state.update_data(step=0)
        await onboarding_start(message, state); return
    await message.answer("Не понял вас 🙂 Вот меню:", reply_markup=keyboard(message.from_user.id))


# --- Онбординг ---

async def show_tour_step(callback: CallbackQuery, state: FSMContext, step: int):
    texts = [
        "🎯 <b>Шаг 1</b>\n\nВ меню 8 кнопок: ➕ Добавить, 🔍 Поиск, 📚 Каталог, 🔎 Ищут, 🎲 Случайная, ⭐ Книга недели, 👤 Профиль, ℹ️ Помощь.",
        "📚 <b>Шаг 2</b>\n\nЗаполните название, автора, формат, жанр, состояние, фото.",
        "🔍 <b>Шаг 3</b>\n\nПоиск по названию/автору. Можно искать несколькими словами.",
        "🤝 <b>Шаг 4</b>\n\nЗаявки анонимны. Контакты раскроются после взаимного согласия.",
        "🎯 <b>Готово!</b>\n\nПриятного использования!",
    ]
    step = max(0, min(step, len(texts) - 1))
    nav = []
    if step > 0: nav.append(InlineKeyboardButton(text="◀", callback_data=f"onboard:prev:{step}"))
    if step < len(texts) - 1: nav.append(InlineKeyboardButton(text="▶", callback_data=f"onboard:next:{step}"))
    buttons = [nav] if nav else []
    buttons.append([InlineKeyboardButton(text="✅ Завершить", callback_data="onboard:done")])
    await safe_edit(callback.message.edit_text(texts[step], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.HTML))


async def onboarding_callback(callback: CallbackQuery, state: FSMContext):
    try: await callback.answer()
    except Exception: pass
    action = callback.data.split(":", 1)[1] if ":" in callback.data else callback.data
    if action == "tour":
        await state.update_data(step=0); await show_tour_step(callback, state, 0)
    elif action.startswith("next:"):
        await show_tour_step(callback, state, int(action.split(":")[1]) + 1)
    elif action.startswith("prev:"):
        await show_tour_step(callback, state, max(0, int(action.split(":")[1]) - 1))
    elif action == "add_book":
        await state.clear(); await add_start(callback.message, state)
    elif action in ["skip", "done"]:
        await state.set_state(UserSurvey.gender); await survey_gender(callback, state)


async def survey_gender(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужчина", callback_data="survey:gender:male")],
        [InlineKeyboardButton(text="👩 Женщина", callback_data="survey:gender:female")],
        [InlineKeyboardButton(text="⚪ Другое", callback_data="survey:gender:other")]])
    await callback.message.answer("📋 Опрос\n\n👤 Пол?", reply_markup=kb)


async def survey_age(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    gender = callback.data.split(":")[2]
    await state.update_data(gender=gender)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔷 До 18", callback_data="survey:age:1"),
         InlineKeyboardButton(text="🔶 18-25", callback_data="survey:age:2")],
        [InlineKeyboardButton(text="🟡 26-35", callback_data="survey:age:3"),
         InlineKeyboardButton(text="🟠 36-50", callback_data="survey:age:4")],
        [InlineKeyboardButton(text="🔴 50+", callback_data="survey:age:5")]])
    await safe_edit(callback.message.edit_text("📋 Опрос\n\n👤 Возраст?", reply_markup=kb))


async def survey_genres(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    await safe_edit(callback.message.edit_text("📋 Опрос\n\n📖 Жанры?",
        reply_markup=genres_keyboard_survey(data.get("genres", []))))


async def survey_handler(callback: CallbackQuery, state: FSMContext):
    try: await callback.answer()
    except Exception: pass
    parts = callback.data.split(":")
    action = parts[1]
    if action == "gender": await survey_age(callback, state)
    elif action == "age":
        age_map = {"1": "<18", "2": "18-25", "3": "26-35", "4": "36-50", "5": "50+"}
        await state.update_data(age=age_map.get(parts[2], "?"))
        await survey_genres(callback, state)
    elif action == "genre":
        gid = parts[2]
        data = await state.get_data()
        genres = list(data.get("genres", []))
        if gid in genres: genres.remove(gid)
        else: genres.append(gid)
        await state.update_data(genres=genres)
        await survey_genres(callback, state)
    elif action == "done":
        data = await state.get_data()
        with closing(connect()) as db:
            db.execute("UPDATE users SET gender=?, age=?, favorite_genres=?, onboarded=1 WHERE telegram_id=?",
                       (data.get("gender", ""), data.get("age", ""), ",".join(data.get("genres", [])), callback.from_user.id))
            db.commit()
        await state.clear()
        try: await callback.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
        await callback.message.answer("✅ Спасибо!", reply_markup=keyboard(callback.from_user.id))


# --- main ---

async def global_error_handler(event: ErrorEvent):
    exception = event.exception
    if isinstance(exception, TelegramNetworkError):
        return True
    if isinstance(exception, TelegramConflictError):
        print("⚠️ Конфликт инстансов бота.")
        return True
    if isinstance(exception, TelegramBadRequest):
        print(f"⚠️ Bad Request (пропущено): {exception}")
        return True
    print(f"❌ Ошибка: {type(exception).__name__}: {exception}")
    return True


async def main():
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("BOT_TOKEN")
    if not token: raise RuntimeError("BOT_TOKEN не задан в .env")
    acquire_instance_lock()
    init_db()
    bot = Bot(token=token); dp = Dispatcher()

    try:
        me = await bot.get_me()
        set_setting("bot_username", me.username or "")
    except Exception:
        pass

    dp.errors.register(global_error_handler)

    dp.message.register(start, CommandStart())
    dp.message.register(go_menu, F.text.in_({MENU_TEXT, BACK_TEXT, "❌ Отмена"}))
    dp.message.register(cancel, Command("cancel"))
    dp.message.register(help_message, F.text == "ℹ️ Помощь")
    dp.message.register(cmd_faq, Command("faq"))
    dp.message.register(cmd_support, Command("support"))
    dp.message.register(cmd_rules, Command("rules"))
    dp.message.register(cmd_id, Command("id"))
    dp.message.register(cmd_help_commands, Command("help"))
    dp.message.register(promo_cmd, Command("promo"))
    dp.message.register(cmd_find, Command("find"))

    dp.message.register(add_start, F.text.in_({"➕ Добавить", "➕ Добавить книгу"}))
    dp.message.register(search_start, F.text.in_({"🔍 Поиск", "🔍 Найти книгу"}))
    dp.message.register(all_books, F.text.in_({"📚 Каталог", "📚 Книги пользователей"}))
    dp.message.register(all_searches, F.text.in_({"🔎 Ищут", "🔎 Что ищут"}))
    dp.message.register(random_book, F.text == "🎲 Случайная")
    dp.message.register(book_of_week, F.text == "⭐ Книга недели")
    dp.message.register(profile_start, F.text == "👤 Мой профиль")
    dp.message.register(admin, F.text == "⚙ Админка")
    dp.message.register(admin, Command("admin"))
    dp.message.register(admin_stats, Command("stats"))
    dp.message.register(admin_broadcast_start, Command("broadcast"))
    dp.message.register(ban_action, F.text.regexp(r"^/(?:ban|unban)_\d+$"))

    dp.message.register(captcha_answer, Captcha.answer)
    dp.message.register(add_title, AddBook.title)
    dp.message.register(add_author, AddBook.author)
    dp.message.register(add_city, AddBook.city)
    dp.message.register(add_condition, AddBook.condition)
    dp.message.register(add_deal, AddBook.deal)
    dp.message.register(add_price, AddBook.price)
    dp.message.register(add_photo, AddBook.photo)
    dp.message.register(search_query, SearchBook.query)
    dp.message.register(report_reason, ReportUser.reason)
    dp.message.register(admin_broadcast_send, AdminBroadcast.message)
    dp.message.register(admin_edit_save, AdminEdit.book)
    dp.message.register(admin_channel_add_save, AdminChannel.add)
    dp.message.register(admin_news_channel_save, AdminNews.channel)
    dp.message.register(admin_premium_info_save, AdminPremium.info)
    dp.message.register(admin_premium_grant_save, AdminPremium.grant)
    dp.message.register(admin_promo_code_save, AdminPromo.code)
    dp.message.register(admin_promo_days_save, AdminPromo.days)
    dp.message.register(admin_promo_uses_save, AdminPromo.uses)
    dp.message.register(admin_text_save, AdminTexts.key)
    dp.message.register(admin_msg_userid, AdminMsg.user_id)
    dp.message.register(admin_msg_send, AdminMsg.text)
    dp.message.register(profile_contact_save, ProfileEdit.contact)
    dp.message.register(write_admin_save, WriteAdmin.text)
    dp.message.register(book_by_id, F.text.regexp(r"^\d+$"))

    dp.callback_query.register(request_callback, F.data.startswith("request:"))
    dp.callback_query.register(request_callback, F.data.startswith("buy:"))
    dp.callback_query.register(format_toggle_callback, F.data.startswith("format:"))
    dp.callback_query.register(format_done_callback, F.data == "format_done")
    dp.callback_query.register(setgenre_callback, F.data.startswith("setgenre:"))
    dp.callback_query.register(skip_condition_callback, F.data == "skip_condition")
    dp.callback_query.register(skip_photo_callback, F.data == "skip_photo")
    dp.callback_query.register(offers_callback, F.data.startswith("offers:"))
    dp.callback_query.register(offer_toggle_callback, F.data.startswith("offer:"))
    dp.callback_query.register(offer_done_callback, F.data.startswith("offer_done:"))
    dp.callback_query.register(offer_cancel_callback, F.data.startswith("offer_cancel:"))
    dp.callback_query.register(counter_accept_callback, F.data.startswith("counter_accept:"))
    dp.callback_query.register(book_detail_callback, F.data.startswith("detail:"))
    dp.callback_query.register(catalog_all_callback, F.data.startswith("catalog_all:"))
    dp.callback_query.register(catalog_genres_callback, F.data == "catalog_genres")
    dp.callback_query.register(genre_callback, F.data.startswith("genre:"))
    dp.callback_query.register(menu_callback, F.data == "menu")
    dp.callback_query.register(accept_callback, F.data.startswith("accept:"))
    dp.callback_query.register(decline_callback, F.data.startswith("decline:"))
    dp.callback_query.register(report_book_callback, F.data.startswith("report_book:"))
    dp.callback_query.register(onboarding_callback, F.data.startswith("onboard:"))
    dp.callback_query.register(survey_handler, F.data.startswith("survey:"))
    dp.callback_query.register(admin_callback, F.data.startswith("admin:"))
    dp.callback_query.register(check_subs_callback, F.data == "check_subs")
    dp.callback_query.register(noop_callback, F.data == "noop")
    dp.callback_query.register(profile_callback, F.data.startswith("profile:"))
    dp.callback_query.register(help_writeadmin_callback, F.data == "help:writeadmin")
    dp.callback_query.register(premium_notify_callback, F.data == "premium_notify")
    dp.callback_query.register(mybook_delete_callback, F.data.startswith("mybook_del:"))
    dp.callback_query.register(mybook_boost_callback, F.data.startswith("mybook_boost:"))
    dp.callback_query.register(fav_toggle_callback, F.data.startswith("fav_toggle:"))
    dp.callback_query.register(by_author_callback, F.data.startswith("by_author:"))
    dp.callback_query.register(rate_callback, F.data.startswith("rate:"))

    dp.inline_query.register(inline_book_query)

    dp.message.register(fallback_message)

    try:
        await bot.get_updates(offset=-1, timeout=0)
        await dp.start_polling(bot, polling_timeout=30)
    except TelegramConflictError:
        print("⚠️ Бот уже запущен в другом процессе.")
    except TelegramNetworkError as e:
        print(f"🌐 Сетевая ошибка: {e}")
    finally:
        await bot.session.close()
        release_instance_lock()


if __name__ == "__main__":
    asyncio.run(main())
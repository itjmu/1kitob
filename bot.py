import asyncio
import msvcrt
import os
import sqlite3
import time
from html import escape
from contextlib import closing
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramConflictError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.enums import ParseMode
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "books.sqlite3"
FORMATS = ("Настоящая книга", "Аудио", "Видео", "PDF")
DEAL_TYPES = ("Подарить", "Обменять", "Продать")

# Задержка между сообщениями при рассылке (секунды). Поставлена с запасом,
# чтобы Telegram не начал ограничивать/банить бота за спам при большой базе пользователей.
BROADCAST_DELAY = 1.0


class AddBook(StatesGroup):
    title = State(); author = State(); book_format = State(); city = State(); condition = State(); photo = State(); deal = State(); price = State()


class SearchBook(StatesGroup):
    query = State()
    browsing = State()


class ReportUser(StatesGroup):
    target = State(); reason = State()


class AdminBroadcast(StatesGroup):
    message = State()


class AdminEdit(StatesGroup):
    book = State()


class AdminChannel(StatesGroup):
    add = State()


class AdminPremium(StatesGroup):
    info = State()
    grant = State()


class ProfileEdit(StatesGroup):
    contact = State()


class Onboarding(StatesGroup):
    step = State()


class UserSurvey(StatesGroup):
    gender = State()
    age = State()
    genres = State()


MENU_TEXT = "⌂ Меню"
BACK_TEXT = "‹ Назад"
INSTANCE_LOCK_PATH = BASE_DIR / "bot.instance.lock"
INSTANCE_LOCK = None


def acquire_instance_lock():
    global INSTANCE_LOCK
    INSTANCE_LOCK = INSTANCE_LOCK_PATH.open("a+b")
    INSTANCE_LOCK.seek(0)
    if not INSTANCE_LOCK.read(1):
        INSTANCE_LOCK.seek(0)
        INSTANCE_LOCK.write(b"1")
        INSTANCE_LOCK.flush()
    INSTANCE_LOCK.seek(0)
    try:
        msvcrt.locking(INSTANCE_LOCK.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as error:
        INSTANCE_LOCK.close()
        INSTANCE_LOCK = None
        raise RuntimeError("Бот уже запущен в другом окне терминала. Закройте второй экземпляр.") from error


def release_instance_lock():
    global INSTANCE_LOCK
    if INSTANCE_LOCK is None:
        return
    try:
        INSTANCE_LOCK.seek(0)
        msvcrt.locking(INSTANCE_LOCK.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        INSTANCE_LOCK.close()
        INSTANCE_LOCK = None


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
        try:
            db.close()
        except UnboundLocalError:
            pass
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
        CREATE INDEX IF NOT EXISTS idx_books_boosted ON books(boosted);
        CREATE INDEX IF NOT EXISTS idx_books_status_created ON books(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_books_owner ON books(owner_id);
        CREATE INDEX IF NOT EXISTS idx_searches_active ON saved_searches(active, user_id);
        CREATE INDEX IF NOT EXISTS idx_requests_book ON exchange_requests(book_id);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "is_banned" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0")
        if "onboarded" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")
        if "gender" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN gender TEXT")
        if "age" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN age INTEGER")
        if "favorite_genres" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN favorite_genres TEXT")
        if "is_premium" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0")
        if "preferred_contact" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN preferred_contact TEXT")
        columns = {row[1] for row in db.execute("PRAGMA table_info(books)")}
        if "format" not in columns:
            db.execute("ALTER TABLE books ADD COLUMN format TEXT NOT NULL DEFAULT 'Настоящая книга'")
        if "photo_id" not in columns:
            db.execute("ALTER TABLE books ADD COLUMN photo_id TEXT")
        if "deal_type" not in columns:
            db.execute("ALTER TABLE books ADD COLUMN deal_type TEXT NOT NULL DEFAULT 'Обменять'")
        if "price" not in columns:
            db.execute("ALTER TABLE books ADD COLUMN price TEXT")
        if "boosted" not in columns:
            db.execute("ALTER TABLE books ADD COLUMN boosted INTEGER NOT NULL DEFAULT 0")
        columns = {row[1] for row in db.execute("PRAGMA table_info(exchange_requests)")}
        if "owner_consent" not in columns:
            db.execute("ALTER TABLE exchange_requests ADD COLUMN owner_consent INTEGER NOT NULL DEFAULT 0")
        if "offered_book_ids" not in columns:
            db.execute("ALTER TABLE exchange_requests ADD COLUMN offered_book_ids TEXT")
        db.commit()


def save_user(message):
    user = message.from_user
    if user:
        with closing(connect()) as db:
            db.execute("""INSERT INTO users (telegram_id, username, name) VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, name=excluded.name""",
                       (user.id, user.username, user.full_name))
            db.commit()


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


def admins():
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def is_admin(user_id):
    return user_id in admins()


def banned(user_id):
    with closing(connect()) as db:
        row = db.execute("SELECT is_banned FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    return bool(row and row["is_banned"])


def is_premium_user(user_id):
    """Админ всегда считается премиум-пользователем без записи в базе."""
    if is_admin(user_id):
        return True
    with closing(connect()) as db:
        row = db.execute("SELECT is_premium FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    return bool(row and row["is_premium"])


def format_contact(user_id, username, name, preferred_contact, is_premium_flag):
    """Показывает свой указанный контакт премиум-пользователю/админу, иначе — Telegram username."""
    if (is_admin(user_id) or is_premium_flag) and preferred_contact:
        return escape(str(preferred_contact))
    return f"@{username or name}"


# --- Настройки бота (ключ-значение) и обязательная подписка на каналы ---

def get_setting(key, default=None):
    with closing(connect()) as db:
        row = db.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with closing(connect()) as db:
        db.execute("""INSERT INTO bot_settings (key,value) VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))
        db.commit()


def subscription_required():
    return get_setting("subscription_required", "0") == "1"


def required_channels_list():
    with closing(connect()) as db:
        return db.execute("SELECT * FROM required_channels ORDER BY id").fetchall()


async def check_subscriptions(bot, user_id):
    """Возвращает (все_ли_подписан, список_каналов_на_которые_не_подписан)."""
    channels = required_channels_list()
    if not channels:
        return True, []
    missing = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel["chat_id"], user_id)
            if member.status in ("left", "kicked"):
                missing.append(channel)
        except Exception:
            missing.append(channel)
    return (len(missing) == 0), missing


def subscription_keyboard(channels):
    rows = []
    for channel in channels:
        if channel["invite_link"]:
            rows.append([InlineKeyboardButton(text=channel["title"] or channel["chat_id"], url=channel["invite_link"])])
        else:
            rows.append([InlineKeyboardButton(text=channel["title"] or channel["chat_id"], callback_data="noop")])
    rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def allowed(message):
    save_user(message)
    if message.from_user and banned(message.from_user.id):
        await message.answer("Ваш аккаунт заблокирован администратором.")
        return False
    if subscription_required():
        ok, missing = await check_subscriptions(message.bot, message.from_user.id)
        if not ok:
            await message.answer(
                "Чтобы пользоваться ботом, подпишитесь на канал(ы) ниже, затем нажмите «Я подписался»:",
                reply_markup=subscription_keyboard(missing))
            return False
    return True


# --- Безопасное редактирование сообщений (Telegram ругается, если текст/кнопки не изменились) ---

async def safe_edit(coro):
    try:
        await coro
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            raise


def keyboard(user_id=None):
    rows = [
        [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="🔍 Поиск")],
        [KeyboardButton(text="📚 Каталог"), KeyboardButton(text="🔎 Ищут")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="ℹ️ Помощь")],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([KeyboardButton(text="⚙ Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def profile_keyboard(user_id):
    premium = is_premium_user(user_id)
    rows = [
        [InlineKeyboardButton(text="📖 Мои книги", callback_data="profile:books")],
        [InlineKeyboardButton(text="🔔 Мои поиски", callback_data="profile:searches")],
        [InlineKeyboardButton(text="🤝 Мои заявки", callback_data="profile:requests")],
        [InlineKeyboardButton(text="🚩 Пожаловаться на пользователя", callback_data="profile:report")],
    ]
    if premium:
        rows.append([InlineKeyboardButton(text="✏ Контакт для показа", callback_data="profile:contact")])
        rows.append([InlineKeyboardButton(text="⭐ Премиум (активен)", callback_data="profile:premium")])
    else:
        rows.append([InlineKeyboardButton(text="⭐ Получить премиум", callback_data="profile:premium")])
    rows.append([InlineKeyboardButton(text="⌂ Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BACK_TEXT), KeyboardButton(text=MENU_TEXT)]], resize_keyboard=True)


def book_text(book):
    text = (f"📖 <b>{escape(str(book['title']))}</b>\nАвтор: {escape(str(book['author']))}\n"
            f"Формат: {escape(str(book['format']))}\nГород: {escape(str(book['city'] or 'не требуется'))}\n"
            f"Состояние: {escape(str(book['condition'] or 'не указано'))}\n"
            f"Способ: {escape(str(book['deal_type'] or 'Обменять'))}")
    if book["deal_type"] and "Продать" in book["deal_type"] and book["price"]:
        text += f"\nЦена: {escape(str(book['price']))}"
    return text


def book_buttons(book_id, deal_types=""):
    """Динамическая кнопка в зависимости от способа предложения книги."""
    types = [item.strip() for item in (deal_types or "").split(",") if item.strip()]

    if len(types) > 1:
        # Несколько способов сразу — не понятно заранее, что выберет владелец,
        # поэтому предлагаем просто написать/связаться, а не гадать с кнопкой.
        action_text = "📩 Связаться с владельцем"
        action_data = f"request:{book_id}"
    elif types == ["Продать"]:
        action_text = "💳 Купить"
        action_data = f"buy:{book_id}"
    elif types == ["Подарить"]:
        action_text = "🎁 Получить подарок"
        action_data = f"request:{book_id}"
    else:
        action_text = "🤝 Запросить обмен"
        action_data = f"request:{book_id}"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=action_text, callback_data=action_data)],
        [InlineKeyboardButton(text="🚩 Пожаловаться", callback_data=f"report_book:{book_id}")],
    ])


def detail_button(book_id):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"Открыть книгу #{book_id}", callback_data=f"detail:{book_id}")]])


def catalog_keyboard(book_ids, page, total_pages):
    rows = []
    for index in range(0, len(book_ids), 5):
        rows.append([InlineKeyboardButton(text=f"№ {book_id}", callback_data=f"detail:{book_id}") for book_id in book_ids[index:index + 5]])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="‹", callback_data=f"catalog:{page - 1}"))
    if page < total_pages - 1:
        navigation.append(InlineKeyboardButton(text="›", callback_data=f"catalog:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="⌂ Меню", callback_data="menu")])
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
        rows.append([InlineKeyboardButton(text=f"{mark}№ {book['id']} · {book['title']}", callback_data=f"offer:{request_id}:{book['id']}")])
    rows.append([InlineKeyboardButton(text="Готово", callback_data=f"offer_done:{request_id}"), InlineKeyboardButton(text="Отмена", callback_data=f"offer_cancel:{request_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def counter_offer_keyboard(request_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять обмен", callback_data=f"counter_accept:{request_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline:{request_id}")],
    ])


def admin_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"), InlineKeyboardButton(text="📚 Книги", callback_data="admin:books")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"), InlineKeyboardButton(text="🚩 Жалобы", callback_data="admin:reports")],
        [InlineKeyboardButton(text="🔎 Поиски", callback_data="admin:searches")],
        [InlineKeyboardButton(text="📢 Подписка на каналы", callback_data="admin:subscriptions")],
        [InlineKeyboardButton(text="⭐ Премиум", callback_data="admin:premium")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="✕ Закрыть", callback_data="admin:close")],
    ])


async def send_book_card(target, book, prefix="", chat_id=None):
    text = f"{prefix}{book_text(book)}\n\nВладелец не показывается до взаимного согласия."
    markup = book_buttons(book["id"], book["deal_type"] or "Обменять")
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


def choose_format(value, allow_any=False):
    value = value.strip().lower()
    if allow_any and value == "любой":
        return "Любой"
    return next((item for item in FORMATS if item.lower() == value), None)


def format_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📕 Бумажная", callback_data="format:Настоящая книга"), InlineKeyboardButton(text="🎧 Аудио", callback_data="format:Аудио")],
        [InlineKeyboardButton(text="🎬 Видео", callback_data="format:Видео"), InlineKeyboardButton(text="📄 PDF", callback_data="format:PDF")],
        [InlineKeyboardButton(text="Готово", callback_data="format_done")],
    ])


def selected_format_keyboard(selected):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✅ " if item in selected else "") + label, callback_data=f"format:{item}") for item, label in [("Настоящая книга", "📕 Бумажная"), ("Аудио", "🎧 Аудио")]],
        [InlineKeyboardButton(text=("✅ " if "Видео" in selected else "") + "🎬 Видео", callback_data="format:Видео"), InlineKeyboardButton(text=("✅ " if "PDF" in selected else "") + "📄 PDF", callback_data="format:PDF")],
        [InlineKeyboardButton(text="Готово", callback_data="format_done")],
    ])


def deal_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Подарить"), KeyboardButton(text="Обменять")],
        [KeyboardButton(text="Продать"), KeyboardButton(text="Подарить и обменять")],
        [KeyboardButton(text="Обменять и продать"), KeyboardButton(text="Все варианты")],
        [KeyboardButton(text="❌ Отмена")]], resize_keyboard=True, one_time_keyboard=True)


def normalize_deal(value):
    value = value.strip().lower()
    if value == "подарить":
        return "Подарить"
    if value == "обменять":
        return "Обменять"
    if value == "продать":
        return "Продать"
    if value == "подарить и обменять":
        return "Подарить, Обменять"
    if value == "обменять и продать":
        return "Обменять, Продать"
    if value == "все варианты":
        return "Подарить, Обменять, Продать"
    return None


# --- Жанры для опроса при онбординге ---

GENRES = [
    ("fiction", "📚 Художественная"),
    ("science", "🔬 Научная"),
    ("history", "🗓️ История"),
    ("tech", "💻 Технология"),
    ("art", "🎨 Искусство"),
    ("business", "💰 Бизнес"),
    ("selfdev", "🧘 Саморазвитие"),
    ("fantasy", "🎮 Фантастика"),
]


def genres_keyboard(selected):
    rows = []
    for index in range(0, len(GENRES), 2):
        row = []
        for genre_id, label in GENRES[index:index + 2]:
            mark = "✅ " if genre_id in selected else ""
            row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"survey:genre:{genre_id}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="survey:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start(message: Message, state: FSMContext):
    if await allowed(message):
        with closing(connect()) as db:
            user = db.execute("SELECT onboarded FROM users WHERE telegram_id=?", (message.from_user.id,)).fetchone()
        if user and not user["onboarded"]:
            await state.set_state(Onboarding.step)
            await state.update_data(step=0)
            await onboarding_start(message, state)
        else:
            save_session(message.from_user.id, action="menu")
            hour = time.localtime().tm_hour
            greet = "🌅 Доброе утро" if 5 <= hour < 12 else "🏞 Добрый день" if 12 <= hour < 18 else "🌉 Добрый вечер" if 18 <= hour < 23 else "🌃 Доброй ночи"
            await message.answer(f"{greet}, {message.from_user.first_name}! 👋", reply_markup=keyboard(message.from_user.id))


async def onboarding_start(message: Message, state: FSMContext):
    keyboard_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Быстрое обучение", callback_data="onboard:tour")],
        [InlineKeyboardButton(text="➕ Добавить книгу", callback_data="onboard:add_book")],
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="onboard:skip")],
    ])
    await message.answer(
        "🎉 Добро пожаловать в BookHub!\n\n"
        "Вы находитесь в боте для обмена книгами.\n\n"
        "Что вы хотите сделать?\n\n"
        "📖 <b>Обучение</b> - узнайте как работает бот\n"
        "➕ <b>Добавить книгу</b> - начните сразу с добавления вашей книги\n"
        "⏭ <b>Пропустить</b> - перейти в главное меню",
        reply_markup=keyboard_markup,
        parse_mode=ParseMode.HTML
    )


async def help_message(message: Message):
    if await allowed(message):
        await message.answer(
            "➕ Добавьте книгу. 🔍 Ищите по названию или автору.\n"
            "Заявки анонимны до взаимного согласия.\n\n"
            "👤 В разделе «Мой профиль» — ваши книги, поиски, заявки, жалобы и премиум-функции.\n\n"
            "⌂ Меню — главный экран.", reply_markup=back_keyboard())


async def add_start(message, state):
    if await allowed(message):
        save_session(message.from_user.id, action="add_book")
        await state.set_state(AddBook.title); await message.answer("Название:", reply_markup=back_keyboard())


async def add_title(message, state):
    await state.update_data(title=(message.text or "").strip()); await state.set_state(AddBook.author); await message.answer("Автор:", reply_markup=back_keyboard())


async def add_author(message, state):
    await state.update_data(author=(message.text or "").strip(), formats=[]); await state.set_state(AddBook.book_format); await message.answer("Формат (можно несколько):", reply_markup=format_keyboard())


async def add_format(message, state):
    value = choose_format(message.text or "")
    if not value:
        await message.answer("Выберите кнопку формата."); return
    await state.update_data(book_format=value)
    if value == "Настоящая книга":
        await state.set_state(AddBook.city)
        await message.answer("Город:", reply_markup=back_keyboard())
    else:
        await state.update_data(city="")
        await state.set_state(AddBook.condition)
        await message.answer("Состояние или описание:", reply_markup=back_keyboard())


async def format_toggle_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    selected = list(data.get("formats", []))
    value = callback.data.split(":", 1)[1]
    if value in selected:
        selected.remove(value)
    else:
        selected.append(value)
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
    if "Настоящая книга" in selected:
        await state.set_state(AddBook.city)
        await callback.message.answer("Город:", reply_markup=back_keyboard())
    else:
        await state.update_data(city="")
        await state.set_state(AddBook.condition)
        await callback.message.answer("Состояние или описание:", reply_markup=back_keyboard())


async def add_city(message, state):
    await state.update_data(city=(message.text or "").strip()); await state.set_state(AddBook.condition); await message.answer("Состояние:", reply_markup=back_keyboard())


async def notify_matches(bot, book):
    with closing(connect()) as db:
        searches = db.execute("SELECT * FROM saved_searches WHERE active=1 AND user_id != ?", (book["owner_id"],)).fetchall()
    title = book["title"].casefold()
    author = book["author"].casefold()
    matches = []
    for search in searches:
        query = search["query"].casefold().strip()
        format_ok = not search["format"] or search["format"] in {"Любой", book["format"]}
        city_ok = not search["city"] or search["city"].casefold() == (book["city"] or "").casefold()
        if query and (query in title or query in author) and format_ok and city_ok:
            matches.append(search)
    for search in matches:
        try:
            await bot.send_message(search["user_id"], "🔔 Найдено совпадение с вашим поиском:")
            await send_book_card(bot, book, chat_id=search["user_id"])
        except Exception:
            pass


async def add_condition(message, state, bot):
    data = await state.update_data(condition=(message.text or "").strip())
    await state.set_state(AddBook.deal)
    await message.answer("Как предложить книгу?", reply_markup=deal_keyboard())


async def add_deal(message, state):
    deal = normalize_deal(message.text or "")
    if not deal:
        await message.answer("Выберите вариант кнопкой.")
        return
    if "Продать" in deal and not is_premium_user(message.from_user.id):
        await message.answer(
            "💳 Продажа книг доступна только премиум-пользователям.\n"
            "Выберите «Подарить» или «Обменять», либо оформите премиум в разделе «👤 Мой профиль».",
            reply_markup=deal_keyboard())
        return
    await state.update_data(deal_type=deal)
    if "Продать" in deal:
        await state.set_state(AddBook.price)
        await message.answer("Укажите цену и валюту, например: 1200 ₽", reply_markup=back_keyboard())
    else:
        await state.set_state(AddBook.photo)
        await message.answer("Фото или «нет»:", reply_markup=back_keyboard())


async def add_price(message, state):
    price = (message.text or "").strip()
    if not price:
        await message.answer("Укажите цену, например: 1200 ₽")
        return
    await state.update_data(price=price)
    await state.set_state(AddBook.photo)
    await message.answer("Фото или «нет»:", reply_markup=back_keyboard())


async def add_photo(message, state, bot):
    photo_id = message.photo[-1].file_id if message.photo else None
    if not photo_id and (message.text or "").strip().lower() not in {"нет", "нет фото", "/skip"}:
        await message.answer("Отправьте фото или напишите «нет».")
        return
    data = await state.update_data(photo_id=photo_id)
    with closing(connect()) as db:
        cur = db.execute("INSERT INTO books (owner_id,title,author,city,condition,format,photo_id,deal_type,price) VALUES (?,?,?,?,?,?,?,?,?)",
                 (message.from_user.id, data["title"], data["author"], data["city"], data["condition"], data["book_format"], data["photo_id"], data["deal_type"], data.get("price")))
        book = db.execute("SELECT * FROM books WHERE id=?", (cur.lastrowid,)).fetchone(); db.commit()
    await state.clear(); await notify_matches(bot, book); await message.answer("✅ Книга опубликована.", reply_markup=keyboard(message.from_user.id))
    for admin_id in admins():
        try: await bot.send_message(admin_id, f"📚 Новая книга от {message.from_user.first_name}: «{book['title']}» ({book['format']})")
        except Exception: pass


async def search_start(message, state):
    if await allowed(message):
        save_session(message.from_user.id, action="search")
        await state.set_state(SearchBook.query); await message.answer("Название или автор:", reply_markup=back_keyboard())


async def search_query(message, state):
    data = await state.update_data(query=(message.text or "").strip())
    query = data["query"].casefold()
    save_session(message.from_user.id, action="search_query", query=data["query"], state="")
    with closing(connect()) as db:
        candidates = db.execute("SELECT id,title,author,format,boosted FROM books WHERE status='available' AND owner_id!=? ORDER BY boosted DESC, created_at DESC", (message.from_user.id,)).fetchall()
        words = [w for w in query.split() if w]
        books = [book for book in candidates if words and all(w in book["title"].casefold() or w in book["author"].casefold() for w in words)][:50]
    if not books:
        with closing(connect()) as db:
            db.execute("INSERT INTO saved_searches (user_id,query,format,city) VALUES (?,?,?,?)", (message.from_user.id, data["query"], None, None))
            db.commit()
        await state.clear()
        await message.answer("Пока нет. Поиск сохранён в 'Мои поиски'.", reply_markup=keyboard(message.from_user.id))
        return
    await state.update_data(found_book_ids=[book["id"] for book in books])
    await state.set_state(SearchBook.browsing)
    await message.answer("Выберите книгу:", reply_markup=keyboard(message.from_user.id))
    await message.answer("\n".join(f"{'⭐ ' if book['boosted'] else ''}№ {book['id']} · {book['title']} · {book['author']}" for book in books),
                         reply_markup=catalog_keyboard([book["id"] for book in books], 0, 1))


async def all_books(message):
    if not await allowed(message): return
    with closing(connect()) as db: rows = db.execute("SELECT * FROM books WHERE status='available' ORDER BY boosted DESC, created_at DESC").fetchall()
    if not rows:
        await message.answer("Каталог пока пуст.", reply_markup=keyboard(message.from_user.id))
        return
    await show_catalog(message, rows, 0)


async def show_catalog(target, rows, page=0):
    page_size = 8
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    selected = rows[page * page_size:(page + 1) * page_size]
    text = f"📚 Каталог · страница {page + 1}/{total_pages}\n\n" + "\n".join(
        f"{'⭐ ' if book['boosted'] else ''}№ {book['id']} · {book['title']} · {book['author']}" for book in selected
    )
    markup = catalog_keyboard([book["id"] for book in selected], page, total_pages)
    if isinstance(target, CallbackQuery):
        await safe_edit(target.message.edit_text(text, reply_markup=markup))
    else:
        await target.answer(text, reply_markup=markup)


async def catalog_page_callback(callback: CallbackQuery):
    await callback.answer()
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM books WHERE status='available' ORDER BY boosted DESC, created_at DESC").fetchall()
    await show_catalog(callback, rows, int(callback.data.split(":", 1)[1]))


async def all_searches(message):
    if not await allowed(message): return
    with closing(connect()) as db: rows = db.execute("SELECT query,format,city,COUNT(*) total FROM saved_searches WHERE active=1 GROUP BY query,format,city ORDER BY total DESC LIMIT 30").fetchall()
    await message.answer("\n".join(f"🔎 {x['query']} · {x['total']}" for x in rows) or "Пока никто не ищет.", reply_markup=back_keyboard())


async def book_by_id(message):
    if not await allowed(message):
        return
    with closing(connect()) as db:
        book = db.execute("SELECT * FROM books WHERE id=? AND status='available'", (int(message.text.strip()),)).fetchone()
    if not book:
        await message.answer("Книга с таким ID не найдена.")
        return
    await send_book_card(message, book)


async def book_detail_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    with closing(connect()) as db:
        book = db.execute("SELECT * FROM books WHERE id=? AND status='available'", (int(callback.data.split(":", 1)[1]),)).fetchone()
    if not book:
        await callback.message.answer("Книга больше недоступна.")
        return
    current_state = await state.get_state()
    if current_state == SearchBook.browsing:
        data = await state.get_data()
        with closing(connect()) as db:
            db.execute("INSERT INTO saved_searches (user_id,query,format,city) VALUES (?,?,?,?)", 
                      (callback.from_user.id, data.get("query", ""), None, None))
            db.commit()
        await state.clear()
    await send_book_card(callback.message, book)


async def show_tour_step(callback: CallbackQuery, state: FSMContext, step: int):
    """Показать определённый шаг туториала"""
    tour_texts = [
        "🎯 <b>Шаг 1: Главное меню</b>\n\n"
        "В главном меню — 6 кнопок:\n\n"
        "• <b>➕ Добавить</b> - опубликовать свою книгу\n"
        "• <b>🔍 Поиск</b> - найти нужную книгу\n"
        "• <b>📚 Каталог</b> - просмотреть все книги\n"
        "• <b>🔎 Ищут</b> - вижу что ищут люди\n"
        "• <b>👤 Мой профиль</b> - ваши книги, поиски, заявки, жалобы и премиум\n"
        "• <b>ℹ️ Помощь</b> - краткая подсказка",
        
        "📚 <b>Шаг 2: Добавление книги</b>\n\n"
        "При добавлении книги вы указываете:\n\n"
        "• Название и автора\n"
        "• Формат (бумажная, аудио, видео, PDF)\n"
        "• Для бумажной - город\n"
        "• Состояние книги\n"
        "• Как предложить (подарить, обменять, продать — продажа только для премиум)\n"
        "• Цену (если продаёте)\n"
        "• Фото книги\n\n"
        "✨ Совет: <i>Хорошее фото привлекает больше внимания!</i>",
        
        "🔍 <b>Шаг 3: Поиск книг</b>\n\n"
        "Чтобы найти книгу:\n\n"
        "1. Нажмите 🔍 Поиск\n"
        "2. Введите название или автора\n"
        "3. Выберите нужную книгу из результатов\n"
        "4. Если вы выбрали книгу - поиск НЕ сохранится\n"
        "5. Если вернулись без выбора - поиск сохранится в 'Мои поиски'\n\n"
        "🔔 Люди, добавляющие книги, получат уведомление о вашем поиске!",
        
        "🤝 <b>Шаг 4: Обмен и заявки</b>\n\n"
        "Когда вы нашли книгу:\n\n"
        "1. Нажмите кнопку действия (зависит от типа книги)\n"
        "2. Владелец получит анонимный запрос\n"
        "3. Он может выбрать ваши книги для встречного обмена\n"
        "4. После согласия обеих сторон раскрываются контакты\n\n"
        "🔐 Совет: <i>Вся информация анонимна до взаимного согласия!</i>",
        
        "🎯 <b>Шаг 5: Готово!</b>\n\n"
        "Теперь вы готовы к использованию BookHub!\n\n"
        "💡 Подсказки:\n"
        "• Добавляйте книги которые хотите отдать\n"
        "• Используйте поиск чтобы найти нужные\n"
        "• Проверяйте заявки в разделе 🤝 Заявки\n"
        "• При вопросах нажмите ℹ️ Помощь\n\n"
        "Счастливого обмена! 📚💝",
    ]
    
    step = max(0, min(step, len(tour_texts) - 1))
    nav_buttons = []
    if step > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"onboard:prev:{step}"))
    if step < len(tour_texts) - 1:
        nav_buttons.append(InlineKeyboardButton(text="Далее ▶", callback_data=f"onboard:next:{step}"))
    
    buttons = [nav_buttons] if nav_buttons else []
    buttons.append([InlineKeyboardButton(text="✅ Завершить", callback_data="onboard:done")])
    
    keyboard_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    await safe_edit(callback.message.edit_text(tour_texts[step], reply_markup=keyboard_markup, parse_mode=ParseMode.HTML))


async def onboarding_callback(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    
    action = callback.data.split(":", 1)[1] if ":" in callback.data else callback.data.split(":")[0]
    
    if action == "tour":
        await state.update_data(step=0)
        await show_tour_step(callback, state, 0)
    
    elif action.startswith("next:"):
        step = int(action.split(":")[1]) + 1
        await show_tour_step(callback, state, step)
    
    elif action.startswith("prev:"):
        step = max(0, int(action.split(":")[1]) - 1)
        await show_tour_step(callback, state, step)
    
    elif action == "add_book":
        await state.clear()
        await add_start(callback.message, state)
    
    elif action in ["skip", "done"]:
        await state.set_state(UserSurvey.gender)
        await survey_gender(callback, state)


async def survey_gender(callback: CallbackQuery, state: FSMContext):
    """Показать выбор пола"""
    await callback.answer()
    keyboard_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужчина", callback_data="survey:gender:male")],
        [InlineKeyboardButton(text="👩 Женщина", callback_data="survey:gender:female")],
        [InlineKeyboardButton(text="⚪ Предпочитаю не указывать", callback_data="survey:gender:other")],
    ])
    await callback.message.answer(
        "📋 <b>Небольшой опрос</b>\n\n"
        "Это поможет нам лучше разобраться в наших пользователях и улучшить сервис.\n\n"
        "👤 Ваш пол?",
        reply_markup=keyboard_markup,
        parse_mode=ParseMode.HTML
    )


async def survey_age(callback: CallbackQuery, state: FSMContext):
    """Показать выбор возраста"""
    await callback.answer()
    gender = callback.data.split(":")[2]
    await state.update_data(gender=gender)
    
    keyboard_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔷 До 18", callback_data="survey:age:1")],
        [InlineKeyboardButton(text="🔶 18-25", callback_data="survey:age:2")],
        [InlineKeyboardButton(text="🟡 26-35", callback_data="survey:age:3")],
        [InlineKeyboardButton(text="🟠 36-50", callback_data="survey:age:4")],
        [InlineKeyboardButton(text="🔴 50+", callback_data="survey:age:5")],
    ])
    await safe_edit(callback.message.edit_text(
        "📋 <b>Опрос</b>\n\n👤 Ваш возраст?",
        reply_markup=keyboard_markup,
        parse_mode=ParseMode.HTML
    ))


async def survey_genres(callback: CallbackQuery, state: FSMContext):
    """Показать/перерисовать выбор жанров с учётом уже отмеченных."""
    await callback.answer()
    data = await state.get_data()
    selected = data.get("genres", [])
    markup = genres_keyboard(selected)
    await safe_edit(callback.message.edit_text(
        "📋 <b>Опрос</b>\n\n📖 Какие жанры вам нравятся? (можно выбрать несколько)",
        reply_markup=markup,
        parse_mode=ParseMode.HTML
    ))


async def survey_handler(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора опроса"""
    try:
        await callback.answer()
    except Exception:
        pass
    
    data = callback.data.split(":")
    action = data[1]
    
    if action == "gender":
        await survey_age(callback, state)
    elif action == "age":
        age_map = {"1": "<18", "2": "18-25", "3": "26-35", "4": "36-50", "5": "50+"}
        age = age_map.get(data[2], "unknown")
        await state.update_data(age=age)
        await survey_genres(callback, state)
    elif action == "genre":
        genre = data[2]
        state_data = await state.get_data()
        genres = list(state_data.get("genres", []))
        if genre in genres:
            genres.remove(genre)
        else:
            genres.append(genre)
        await state.update_data(genres=genres)
        await survey_genres(callback, state)
    elif action == "done":
        state_data = await state.get_data()
        gender = state_data.get("gender", "")
        age = state_data.get("age", "")
        genres = ",".join(state_data.get("genres", []))
        
        with closing(connect()) as db:
            db.execute("UPDATE users SET gender=?, age=?, favorite_genres=?, onboarded=1 WHERE telegram_id=?",
                      (gender, age, genres, callback.from_user.id))
            db.commit()
        
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("✅ Спасибо за ответы! Теперь мы лучше вас знаем.", reply_markup=keyboard(callback.from_user.id))


async def menu_callback(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("BookHub — книги и обмены", reply_markup=keyboard(callback.from_user.id))


async def my_books(target, user_id):
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM books WHERE owner_id=? AND status!='deleted' ORDER BY created_at DESC", (user_id,)).fetchall()
    if not rows:
        await target.answer("У вас пока нет книг.")
        return
    premium = is_premium_user(user_id)
    for book in rows:
        buttons = [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"mybook_del:{book['id']}")]
        if premium and book["status"] == "available":
            boost_text = "⬇ Убрать из топа" if book["boosted"] else "🚀 Поднять в топ"
            buttons.append(InlineKeyboardButton(text=boost_text, callback_data=f"mybook_boost:{book['id']}"))
        note = "" if book["status"] == "available" else " · неактивна"
        star = "⭐ " if book["boosted"] else ""
        await target.answer(f"{star}#{book['id']} · {book['title']} · {book['author']}{note}",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]))


async def my_searches(target, user_id):
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM saved_searches WHERE user_id=? AND active=1", (user_id,)).fetchall()
    await target.answer("\n".join(f"🔔 {x['query']}" for x in rows) or "Поисков нет.")


async def mybook_delete_callback(callback: CallbackQuery):
    await callback.answer()
    book_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        row = db.execute("SELECT owner_id FROM books WHERE id=?", (book_id,)).fetchone()
        if not row or row["owner_id"] != callback.from_user.id:
            await callback.message.answer("Книга не найдена.")
            return
        db.execute("UPDATE books SET status='deleted' WHERE id=?", (book_id,))
        db.commit()
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    await callback.message.answer("Книга удалена.")


async def mybook_boost_callback(callback: CallbackQuery):
    await callback.answer()
    book_id = int(callback.data.split(":", 1)[1])
    if not is_premium_user(callback.from_user.id):
        await callback.message.answer("Поднятие в топ каталога доступно только премиум-пользователям.")
        return
    with closing(connect()) as db:
        row = db.execute("SELECT owner_id, boosted FROM books WHERE id=? AND status='available'", (book_id,)).fetchone()
        if not row or row["owner_id"] != callback.from_user.id:
            await callback.message.answer("Книга не найдена.")
            return
        new_value = 0 if row["boosted"] else 1
        db.execute("UPDATE books SET boosted=? WHERE id=?", (new_value, book_id))
        db.commit()
    boost_text = "⬇ Убрать из топа" if new_value else "🚀 Поднять в топ"
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"mybook_del:{book_id}"),
        InlineKeyboardButton(text=boost_text, callback_data=f"mybook_boost:{book_id}"),
    ]])
    await safe_edit(callback.message.edit_reply_markup(reply_markup=markup))


async def request_book(message, bot):
    if not await allowed(message): return
    book_id = int(message.text.rsplit("_", 1)[1])
    with closing(connect()) as db:
        book = db.execute("SELECT * FROM books WHERE id=? AND status='available'", (book_id,)).fetchone()
        if not book or book["owner_id"] == message.from_user.id: await message.answer("Книга недоступна или принадлежит вам."); return
        try: db.execute("INSERT INTO exchange_requests (book_id,requester_id) VALUES (?,?)", (book_id, message.from_user.id)); db.commit()
        except sqlite3.IntegrityError: await message.answer("Вы уже отправляли заявку."); return
        request_id = db.execute("SELECT id FROM exchange_requests WHERE book_id=? AND requester_id=?", (book_id, message.from_user.id)).fetchone()["id"]
        await bot.send_message(book["owner_id"], f"🤝 Запрос на «{escape(str(book['title']))}». Можно посмотреть книги отправителя и выбрать несколько для обмена.", reply_markup=request_buttons(request_id))
    await message.answer("✅ Заявка отправлена. Владелец не видит ваш профиль до согласия.")


async def request_callback(callback: CallbackQuery, bot: Bot):
    action_type = "обменять" if callback.data.startswith("request:") else "купить"
    action_emoji = "🤝" if callback.data.startswith("request:") else "💳"
    await callback.answer("Отправляю анонимную заявку...")
    if banned(callback.from_user.id):
        await callback.message.answer("Ваш аккаунт заблокирован.")
        return
    book_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        book = db.execute("SELECT * FROM books WHERE id=? AND status='available'", (book_id,)).fetchone()
        if not book or book["owner_id"] == callback.from_user.id:
            await callback.message.answer("Книга недоступна или принадлежит вам.")
            return
        try:
            db.execute("INSERT INTO exchange_requests (book_id,requester_id) VALUES (?,?)", (book_id, callback.from_user.id))
            db.commit()
        except sqlite3.IntegrityError:
            await callback.message.answer("Вы уже отправляли заявку.")
            return
        request_id = db.execute("SELECT id FROM exchange_requests WHERE book_id=? AND requester_id=?", (book_id, callback.from_user.id)).fetchone()["id"]
    await bot.send_message(book["owner_id"], f"{action_emoji} Анонимный пользователь хочет {action_type} «{book['title']}».", reply_markup=request_buttons(request_id))
    await callback.message.answer("✅ Заявка отправлена. Ваш профиль скрыт до взаимного согласия.")


async def offers_callback(callback: CallbackQuery):
    await callback.answer()
    request_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        request = db.execute("SELECT requester_id, offered_book_ids FROM exchange_requests WHERE id=?", (request_id,)).fetchone()
        if not request or request["requester_id"] == callback.from_user.id:
            await callback.message.answer("Заявка не найдена.")
            return
        books = db.execute("SELECT id,title,author FROM books WHERE owner_id=? AND status='available' ORDER BY created_at DESC", (request["requester_id"],)).fetchall()
    selected = {int(value) for value in (request["offered_book_ids"] or "").split(",") if value.isdigit()}
    if not books:
        await callback.message.answer("У отправителя пока нет доступных книг для встречного обмена.")
        return
    await callback.message.answer("Выберите одну или несколько книг отправителя:", reply_markup=offer_keyboard(request_id, books, selected))


async def offer_toggle_callback(callback: CallbackQuery):
    await callback.answer()
    _, raw_request, raw_book = callback.data.split(":")
    request_id, book_id = int(raw_request), int(raw_book)
    with closing(connect()) as db:
        request = db.execute("SELECT requester_id, offered_book_ids FROM exchange_requests WHERE id=?", (request_id,)).fetchone()
        if not request:
            await callback.message.answer("Заявка не найдена.")
            return
        allowed_book = db.execute("SELECT id FROM books WHERE id=? AND owner_id=? AND status='available'", (book_id, request["requester_id"])).fetchone()
        owner = db.execute("SELECT owner_id FROM books WHERE id=(SELECT book_id FROM exchange_requests WHERE id=?)", (request_id,)).fetchone()
        if not allowed_book or not owner or owner["owner_id"] != callback.from_user.id:
            await callback.message.answer("Эта книга недоступна для выбора.")
            return
        selected = {int(value) for value in (request["offered_book_ids"] or "").split(",") if value.isdigit()}
        selected.symmetric_difference_update({book_id})
        value = ",".join(str(item) for item in sorted(selected))
        db.execute("UPDATE exchange_requests SET offered_book_ids=? WHERE id=?", (value, request_id))
        db.commit()
        books = db.execute("SELECT id,title,author FROM books WHERE owner_id=? AND status='available' ORDER BY created_at DESC", (request["requester_id"],)).fetchall()
    await safe_edit(callback.message.edit_reply_markup(reply_markup=offer_keyboard(request_id, books, selected)))


async def offer_done_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    request_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        request = db.execute("SELECT * FROM exchange_requests WHERE id=?", (request_id,)).fetchone()
        owner = db.execute("SELECT owner_id FROM books WHERE id=?", (request["book_id"],)).fetchone() if request else None
        if not request or not owner or owner["owner_id"] != callback.from_user.id:
            await callback.message.answer("Заявка не найдена.")
            return
        selected = [int(value) for value in (request["offered_book_ids"] or "").split(",") if value.isdigit()]
        if not selected:
            await callback.message.answer("Выберите хотя бы одну книгу.")
            return
        placeholders = ",".join("?" for _ in selected)
        books = db.execute(f"SELECT id,title FROM books WHERE owner_id=? AND id IN ({placeholders})", [request["requester_id"], *selected]).fetchall()
        db.execute("UPDATE exchange_requests SET status='counter_offer' WHERE id=?", (request_id,))
        db.commit()
        target = db.execute("SELECT owner_id,title FROM books WHERE id=?", (request["book_id"],)).fetchone()
    names = ", ".join(escape(str(book["title"])) for book in books)
    await bot.send_message(request["requester_id"], f"📚 Владелец книги «{escape(str(target['title']))}» предлагает обмен на: {names}", reply_markup=counter_offer_keyboard(request_id))
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    await callback.message.answer("Встречное предложение отправлено.")


async def offer_cancel_callback(callback: CallbackQuery):
    await callback.answer("Отменено")
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))


async def counter_accept_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    request_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        row = db.execute("""SELECT er.*, b.title, b.owner_id,
            requester.username AS requester_username, requester.name AS requester_name,
            requester.preferred_contact AS requester_contact, requester.is_premium AS requester_is_premium,
            owner.username AS owner_username, owner.name AS owner_name,
            owner.preferred_contact AS owner_contact, owner.is_premium AS owner_is_premium
            FROM exchange_requests er JOIN books b ON b.id=er.book_id
            JOIN users requester ON requester.telegram_id=er.requester_id
            JOIN users owner ON owner.telegram_id=b.owner_id WHERE er.id=?""", (request_id,)).fetchone()
        if not row or row["requester_id"] != callback.from_user.id:
            await callback.message.answer("Заявка не найдена.")
            return
        db.execute("UPDATE exchange_requests SET owner_consent=1,status='accepted' WHERE id=?", (request_id,))
        db.commit()
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    await callback.message.answer("✅ Обмен принят. Контакты раскрыты обеим сторонам.")
    owner_contact = format_contact(row["owner_id"], row["owner_username"], row["owner_name"], row["owner_contact"], row["owner_is_premium"])
    requester_contact = format_contact(row["requester_id"], row["requester_username"], row["requester_name"], row["requester_contact"], row["requester_is_premium"])
    await bot.send_message(row["owner_id"], f"Контакт отправителя: {requester_contact}")
    await bot.send_message(row["requester_id"], f"Контакт владельца: {owner_contact}")


async def accept_request(message, bot):
    request_id = int(message.text.rsplit("_", 1)[1])
    with closing(connect()) as db:
        row = db.execute("""SELECT er.*, b.title, b.owner_id,
            requester.username AS requester_username, requester.name AS requester_name,
            requester.preferred_contact AS requester_contact, requester.is_premium AS requester_is_premium,
            owner.username AS owner_username, owner.name AS owner_name,
            owner.preferred_contact AS owner_contact, owner.is_premium AS owner_is_premium
            FROM exchange_requests er JOIN books b ON b.id=er.book_id
            JOIN users requester ON requester.telegram_id=er.requester_id
            JOIN users owner ON owner.telegram_id=b.owner_id WHERE er.id=?""", (request_id,)).fetchone()
        if not row or row["owner_id"] != message.from_user.id: await message.answer("Заявка не найдена."); return
        db.execute("UPDATE exchange_requests SET owner_consent=1,status='accepted' WHERE id=?", (request_id,)); db.commit()
    owner_contact = format_contact(row["owner_id"], row["owner_username"], row["owner_name"], row["owner_contact"], row["owner_is_premium"])
    requester_contact = format_contact(row["requester_id"], row["requester_username"], row["requester_name"], row["requester_contact"], row["requester_is_premium"])
    await message.answer("Вы согласились. Контакты раскрыты, так как автор заявки уже согласился.")
    await bot.send_message(row["requester_id"], f"✅ Владелец согласился на обмен «{row['title']}». Контакты:\nВладелец: {owner_contact}")
    await bot.send_message(message.from_user.id, f"Контакт автора заявки на «{row['title']}»:\n{requester_contact}")


async def accept_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    if banned(callback.from_user.id):
        await callback.message.answer("Ваш аккаунт заблокирован.")
        return
    request_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        row = db.execute("""SELECT er.*, b.title, b.owner_id,
            requester.username AS requester_username, requester.name AS requester_name,
            requester.preferred_contact AS requester_contact, requester.is_premium AS requester_is_premium,
            owner.username AS owner_username, owner.name AS owner_name,
            owner.preferred_contact AS owner_contact, owner.is_premium AS owner_is_premium
            FROM exchange_requests er JOIN books b ON b.id=er.book_id
            JOIN users requester ON requester.telegram_id=er.requester_id
            JOIN users owner ON owner.telegram_id=b.owner_id WHERE er.id=?""", (request_id,)).fetchone()
        if not row or row["owner_id"] != callback.from_user.id:
            await callback.message.answer("Заявка не найдена.")
            return
        db.execute("UPDATE exchange_requests SET owner_consent=1,status='accepted' WHERE id=?", (request_id,)); db.commit()
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    owner_contact = format_contact(row["owner_id"], row["owner_username"], row["owner_name"], row["owner_contact"], row["owner_is_premium"])
    requester_contact = format_contact(row["requester_id"], row["requester_username"], row["requester_name"], row["requester_contact"], row["requester_is_premium"])
    await callback.message.answer("Вы согласились. Контакты раскрыты обеим сторонам.")
    await bot.send_message(row["requester_id"], f"✅ Владелец согласился на обмен «{row['title']}».\nКонтакт владельца: {owner_contact}")
    await bot.send_message(callback.from_user.id, f"Контакт автора заявки:\n{requester_contact}")


async def decline_request(message):
    request_id = int(message.text.rsplit("_", 1)[1])
    with closing(connect()) as db:
        db.execute("UPDATE exchange_requests SET status='declined' WHERE id=? AND book_id IN (SELECT id FROM books WHERE owner_id=?)", (request_id, message.from_user.id)); db.commit()
    await message.answer("Заявка отклонена. Контакты не раскрыты.")


async def decline_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer("Заявка отклонена")
    request_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        row = db.execute("""SELECT er.requester_id, b.title FROM exchange_requests er
            JOIN books b ON b.id=er.book_id WHERE er.id=? AND b.owner_id=?""", (request_id, callback.from_user.id)).fetchone()
        db.execute("UPDATE exchange_requests SET status='declined' WHERE id=? AND book_id IN (SELECT id FROM books WHERE owner_id=?)", (request_id, callback.from_user.id)); db.commit()
    await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    await callback.message.answer("Заявка отклонена. Контакты не раскрыты.")
    if row:
        try:
            await bot.send_message(row["requester_id"], f"❌ Владелец отклонил вашу заявку на «{escape(str(row['title']))}».")
        except Exception:
            pass


async def my_requests(target, user_id):
    with closing(connect()) as db:
        rows = db.execute("SELECT er.id,er.status,b.title FROM exchange_requests er JOIN books b ON b.id=er.book_id WHERE er.requester_id=? OR b.owner_id=?", (user_id, user_id)).fetchall()
    await target.answer("\n".join(f"🤝 №{x['id']} «{x['title']}» — {x['status']}" for x in rows) or "Заявок нет.")


# --- Профиль пользователя (личный кабинет) ---

async def profile_start(message: Message):
    if not await allowed(message):
        return
    await message.answer("👤 Ваш профиль:", reply_markup=profile_keyboard(message.from_user.id))


async def premium_info(target, user_id):
    premium = is_premium_user(user_id)
    text = "⭐ <b>Премиум</b>\n\n"
    if premium:
        text += ("У вас активен премиум. Доступно:\n"
                 "• Продажа книг за деньги\n"
                 "• Поднятие своих книг в топ каталога\n"
                 "• Свой контакт для показа вместо Telegram-аккаунта")
        await target.answer(text, parse_mode=ParseMode.HTML)
        return
    info_text = get_setting("premium_info", "Информация об оформлении премиума скоро появится. Обратитесь к администратору.")
    text += ("Премиум даёт:\n"
             "• Продажу книг за деньги\n"
             "• Поднятие своих книг в топ каталога\n"
             "• Свой контакт для показа вместо Telegram-аккаунта\n\n") + info_text
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил, сообщить админу", callback_data="premium_notify")]])
    await target.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def premium_notify_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer("Заявка отправлена администратору.", show_alert=True)
    user = callback.from_user
    for admin_id in admins():
        try:
            await bot.send_message(
                admin_id,
                f"⭐ Пользователь @{user.username or user.full_name} (ID: {user.id}) хочет оформить премиум.\n"
                f"Чтобы выдать: ⚙ Админка → ⭐ Премиум → «Выдать/забрать премиум» → отправьте {user.id}"
            )
        except Exception:
            pass


async def profile_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if banned(callback.from_user.id):
        await callback.message.answer("Ваш аккаунт заблокирован администратором.")
        return
    action = callback.data.split(":", 1)[1]
    if action == "books":
        await my_books(callback.message, callback.from_user.id)
    elif action == "searches":
        await my_searches(callback.message, callback.from_user.id)
    elif action == "requests":
        await my_requests(callback.message, callback.from_user.id)
    elif action == "report":
        await state.set_state(ReportUser.target)
        await callback.message.answer("Введите Telegram ID нарушителя:")
    elif action == "premium":
        await premium_info(callback.message, callback.from_user.id)
    elif action == "contact":
        if not is_premium_user(callback.from_user.id):
            await callback.message.answer("Эта функция доступна только премиум-пользователям.")
            return
        await state.set_state(ProfileEdit.contact)
        await callback.message.answer(
            "Отправьте текст, юзернейм или ссылку, которые будут показываться собеседнику вместо вашего "
            "Telegram-аккаунта при взаимном раскрытии контактов:",
            reply_markup=back_keyboard())


async def profile_contact_save(message, state):
    if not is_premium_user(message.from_user.id):
        await state.clear()
        return
    contact = (message.text or "").strip()
    if not contact:
        await message.answer("Отправьте текст контакта.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE users SET preferred_contact=? WHERE telegram_id=?", (contact, message.from_user.id))
        db.commit()
    await state.clear()
    await message.answer("✅ Контакт для показа обновлён.", reply_markup=keyboard(message.from_user.id))


async def report_book_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if banned(callback.from_user.id):
        await callback.message.answer("Ваш аккаунт заблокирован.")
        return
    book_id = int(callback.data.split(":", 1)[1])
    with closing(connect()) as db:
        book = db.execute("SELECT owner_id FROM books WHERE id=?", (book_id,)).fetchone()
    if not book or book["owner_id"] == callback.from_user.id:
        await callback.message.answer("Нельзя пожаловаться на эту книгу.")
        return
    await state.update_data(target=book["owner_id"])
    await state.set_state(ReportUser.reason)
    await callback.message.answer("Опишите причину жалобы на владельца книги:")


async def report_target(message, state):
    if not (message.text or "").isdigit(): await message.answer("Нужен числовой Telegram ID."); return
    await state.update_data(target=int(message.text)); await state.set_state(ReportUser.reason); await message.answer("Опишите причину жалобы:")


async def report_reason(message, state, bot):
    data = await state.update_data(reason=(message.text or "").strip())
    with closing(connect()) as db: db.execute("INSERT INTO reports (reporter_id,reported_id,reason) VALUES (?,?,?)", (message.from_user.id, data["target"], data["reason"])); db.commit()
    for admin_id in admins(): await bot.send_message(admin_id, f"🚩 Жалоба от {message.from_user.id} на {data['target']}: {data['reason']}\nБан: /ban_{data['target']}")
    await state.clear(); await message.answer("Жалоба отправлена администратору.")


async def admin(message):
    if not is_admin(message.from_user.id):
        return
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
        
        # Статистика по полу
        male = db.execute("SELECT COUNT(*) FROM users WHERE gender='male'").fetchone()[0]
        female = db.execute("SELECT COUNT(*) FROM users WHERE gender='female'").fetchone()[0]
        other = db.execute("SELECT COUNT(*) FROM users WHERE gender='other'").fetchone()[0]
        
        # Статистика по возрасту
        age_stats = db.execute("""
            SELECT age, COUNT(*) as cnt FROM users WHERE age IS NOT NULL GROUP BY age ORDER BY cnt DESC
        """).fetchall()
        
        # Топ жанры
        all_genres_raw = db.execute("SELECT favorite_genres FROM users WHERE favorite_genres IS NOT NULL AND favorite_genres != ''").fetchall()
        genre_count = {}
        for row in all_genres_raw:
            genres = row[0].split(",")
            for genre in genres:
                genre = genre.strip()
                genre_count[genre] = genre_count.get(genre, 0) + 1
        
        top_genres = sorted(genre_count.items(), key=lambda x: x[1], reverse=True)[:5]
    
    # Формируем ответ
    text = "📊 <b>Полная статистика</b>\n\n"
    text += f"<b>Общие данные:</b>\n"
    text += f"👥 Пользователи: {users} ({active} активных)\n"
    text += f"📚 Книги: {books}\n"
    text += f"🔎 Поиски: {searches}\n"
    text += f"🤝 Заявки: {requests}\n"
    text += f"🚩 Жалобы: {reports_count}\n"
    text += f"⭐ Премиум (не считая админов): {premium_count}\n\n"
    
    text += f"<b>Гендерное распределение:</b>\n"
    if users > 0:
        male_pct = int(male * 100 / users)
        female_pct = int(female * 100 / users)
        other_pct = int(other * 100 / users)
        text += f"👨 Мужчины: {male} ({male_pct}%)\n"
        text += f"👩 Женщины: {female} ({female_pct}%)\n"
        text += f"⚪ Другое: {other} ({other_pct}%)\n\n"
    
    text += f"<b>Возрастное распределение:</b>\n"
    age_labels = {"<18": "🔷 До 18", "18-25": "🔶 18-25", "26-35": "🟡 26-35", "36-50": "🟠 36-50", "50+": "🔴 50+"}
    for age, cnt in age_stats:
        label = age_labels.get(age, age)
        text += f"{label}: {cnt}\n"
    
    if age_stats:
        text += "\n"
    
    text += f"<b>Любимые жанры:</b>\n"
    if top_genres:
        for genre, cnt in top_genres:
            text += f"• {genre}: {cnt}\n"
    else:
        text += "Нет данных\n"
    
    await message.answer(text, reply_markup=admin_menu_keyboard(), parse_mode=ParseMode.HTML)


async def admin_broadcast_start(message, state):
    if is_admin(message.from_user.id):
        await state.set_state(AdminBroadcast.message)
        await message.answer("Отправьте одно сообщение для рассылки. Поддерживаются текст, фото, видео, документы и пересланные сообщения. /cancel отменяет рассылку.")


async def admin_broadcast_send(message, state):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    with closing(connect()) as db:
        users = db.execute("SELECT telegram_id FROM users WHERE is_banned=0").fetchall()
    await message.answer(f"Начинаю рассылку на {len(users)} пользователей. Это может занять время — не выключайте бота.")
    sent = 0
    for user in users:
        try:
            await message.copy_to(user["telegram_id"])
            sent += 1
        except Exception:
            pass
        # Пауза между сообщениями, чтобы Telegram не ограничил/забанил бота за спам.
        await asyncio.sleep(BROADCAST_DELAY)
    await state.clear()
    await message.answer(f"Рассылка завершена. Получили сообщение: {sent} пользователей.")


async def admin_callback(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    action = callback.data.split(":", 1)[1]
    if action == "stats":
        await admin_stats(callback.message, callback.from_user.id)
    elif action == "books":
        await admin_books(callback.message, callback.from_user.id)
    elif action == "users":
        await admin_users(callback.message, callback.from_user.id)
    elif action == "reports":
        await reports(callback.message, callback.from_user.id)
    elif action == "searches":
        await admin_searches(callback.message, callback.from_user.id)
    elif action == "subscriptions":
        await admin_channels_menu(callback.message, callback.from_user.id)
    elif action == "close_channels":
        await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    elif action == "premium":
        await admin_premium_menu(callback.message, callback.from_user.id)
    elif action == "close_premium":
        await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    elif action == "premium_info":
        await state.set_state(AdminPremium.info)
        await callback.message.answer("Пришлите новый текст условий оплаты (можно со ссылкой):", reply_markup=back_keyboard())
    elif action == "premium_grant":
        await state.set_state(AdminPremium.grant)
        await callback.message.answer("Пришлите Telegram ID пользователя — статус переключится (выдан ⇄ снят):", reply_markup=back_keyboard())
    elif action == "toggle_sub":
        set_setting("subscription_required", "0" if subscription_required() else "1")
        await admin_channels_menu(callback.message, callback.from_user.id)
    elif action == "add_channel":
        await state.set_state(AdminChannel.add)
        await callback.message.answer(
            "Перешлите любое сообщение из канала, либо пришлите его @username.\n"
            "Бот должен быть добавлен в канал (как минимум участником, лучше — админом).",
            reply_markup=back_keyboard())
    elif action.startswith("del_channel:"):
        channel_id = int(action.split(":")[1])
        with closing(connect()) as db:
            db.execute("DELETE FROM required_channels WHERE id=?", (channel_id,))
            db.commit()
        await admin_channels_menu(callback.message, callback.from_user.id)
    elif action == "broadcast":
        await state.set_state(AdminBroadcast.message)
        await callback.message.answer("📣 Пришлите сообщение для рассылки.", reply_markup=back_keyboard())
    elif action == "close":
        await safe_edit(callback.message.edit_reply_markup(reply_markup=None))
    elif action.startswith("ban:") or action.startswith("unban:"):
        user_id = int(action.split(":")[1])
        value = 1 if action.startswith("ban:") else 0
        with closing(connect()) as db:
            db.execute("UPDATE users SET is_banned=? WHERE telegram_id=?", (value, user_id))
            db.commit()
        await callback.message.answer("Пользователь заблокирован." if value else "Пользователь разблокирован.", reply_markup=admin_menu_keyboard())
    elif action.startswith("delete_user:"):
        user_id = int(action.split(":")[1])
        if user_id in admins():
            await callback.message.answer("Администратора удалить нельзя.")
            return
        with closing(connect()) as db:
            db.execute("DELETE FROM books WHERE owner_id=?", (user_id,))
            db.execute("DELETE FROM saved_searches WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM exchange_requests WHERE requester_id=? OR book_id NOT IN (SELECT id FROM books)", (user_id,))
            db.execute("DELETE FROM reports WHERE reporter_id=? OR reported_id=?", (user_id, user_id))
            db.execute("DELETE FROM users WHERE telegram_id=?", (user_id,))
            db.commit()
        await callback.message.answer("Пользователь и его книги удалены.", reply_markup=admin_menu_keyboard())
    elif action.startswith("delete_book:"):
        book_id = int(action.split(":")[1])
        with closing(connect()) as db:
            db.execute("UPDATE books SET status='deleted' WHERE id=?", (book_id,))
            db.commit()
        await callback.message.answer("Книга удалена.", reply_markup=admin_menu_keyboard())
    elif action.startswith("delete_search:"):
        search_id = int(action.split(":")[1])
        with closing(connect()) as db:
            db.execute("UPDATE saved_searches SET active=0 WHERE id=?", (search_id,))
            db.commit()
        await callback.message.answer("Поиск удален.", reply_markup=admin_menu_keyboard())
    elif action.startswith("edit_search:"):
        search_id = int(action.split(":")[1])
        await state.update_data(search_id=search_id)
        await state.set_state(AdminEdit.book)
        await callback.message.answer("Введите новый текст поиска:", reply_markup=back_keyboard())
    elif action.startswith("edit_book:"):
        await state.update_data(book_id=int(action.split(":")[1]))
        await state.set_state(AdminEdit.book)
        await callback.message.answer("Изменение:\nназвание | автор | формат | город | состояние", reply_markup=back_keyboard())


async def admin_books(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    with closing(connect()) as db: rows = db.execute("SELECT * FROM books ORDER BY id DESC LIMIT 50").fetchall()
    if not rows:
        await message.answer("Книг нет.", reply_markup=admin_menu_keyboard())
        return
    for book in rows:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏ Изменить", callback_data=f"admin:edit_book:{book['id']}"), InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin:delete_book:{book['id']}")],
        ])
        await message.answer(f"#{book['id']} · {book['title']} · {book['author']}\nФормат: {book['format']}", reply_markup=markup)
    await message.answer("Для изменения: /edit_book_ID название | автор | формат | город | состояние", reply_markup=admin_menu_keyboard())


async def admin_users(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    with closing(connect()) as db: rows = db.execute("SELECT telegram_id,name,username,is_banned FROM users ORDER BY created_at DESC LIMIT 50").fetchall()
    if not rows:
        await message.answer("Пользователей нет.", reply_markup=admin_menu_keyboard())
        return
    for user in rows:
        status = "бан" if user["is_banned"] else "активен"
        actions = [InlineKeyboardButton(text="Разбанить" if user["is_banned"] else "Заблокировать", callback_data=f"admin:{'unban' if user['is_banned'] else 'ban'}:{user['telegram_id']}")]
        if user["telegram_id"] not in admins():
            actions.append(InlineKeyboardButton(text="Удалить", callback_data=f"admin:delete_user:{user['telegram_id']}"))
        await message.answer(f"{user['username'] or user['name']} · {status}\nID: {user['telegram_id']}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[actions]))
    await message.answer("Выберите раздел:", reply_markup=admin_menu_keyboard())


async def delete_user(message):
    if not is_admin(message.from_user.id): return
    user_id = int(message.text.rsplit("_", 1)[1])
    if user_id in admins():
        await message.answer("Нельзя удалить администратора.")
        return
    with closing(connect()) as db:
        db.execute("DELETE FROM books WHERE owner_id=?", (user_id,))
        db.execute("DELETE FROM saved_searches WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM exchange_requests WHERE requester_id=? OR book_id NOT IN (SELECT id FROM books)", (user_id,))
        db.execute("DELETE FROM reports WHERE reporter_id=? OR reported_id=?", (user_id, user_id))
        db.execute("DELETE FROM users WHERE telegram_id=?", (user_id,))
        db.commit()
    await message.answer("Пользователь и все его книги удалены.")


async def reports(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    with closing(connect()) as db: rows = db.execute("SELECT * FROM reports WHERE status='new' ORDER BY id DESC").fetchall()
    if not rows:
        await message.answer("Жалоб нет.", reply_markup=admin_menu_keyboard())
        return
    for report in rows:
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Заблокировать", callback_data=f"admin:ban:{report['reported_id']}")]])
        await message.answer(f"№{report['id']} · {report['reporter_id']} → {report['reported_id']}\n{report['reason']}", reply_markup=markup)
    await message.answer("Выберите раздел:", reply_markup=admin_menu_keyboard())


async def admin_searches(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id): return
    with closing(connect()) as db: 
        rows = db.execute("SELECT id, query, COUNT(*) as count FROM saved_searches WHERE active=1 GROUP BY query ORDER BY count DESC LIMIT 50").fetchall()
    if not rows:
        await message.answer("Поисков нет.", reply_markup=admin_menu_keyboard())
        return
    for search in rows:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏ Исправить", callback_data=f"admin:edit_search:{search['id']}"), 
             InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin:delete_search:{search['id']}")],
        ])
        await message.answer(f"🔎 \"{search['query']}\" · поиски: {search['count']}", reply_markup=markup)
    await message.answer("Выберите раздел:", reply_markup=admin_menu_keyboard())


# --- Управление обязательной подпиской на каналы (админка) ---

def admin_channels_keyboard():
    channels = required_channels_list()
    required = subscription_required()
    rows = []
    for channel in channels:
        rows.append([InlineKeyboardButton(text=f"❌ {channel['title'] or channel['chat_id']}", callback_data=f"admin:del_channel:{channel['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="admin:add_channel")])
    toggle_text = "🔴 Выключить обязательную подписку" if required else "🟢 Включить обязательную подписку"
    rows.append([InlineKeyboardButton(text=toggle_text, callback_data="admin:toggle_sub")])
    rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="admin:close_channels")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_channels_menu(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id):
        return
    channels = required_channels_list()
    text = "📢 <b>Обязательная подписка на каналы</b>\n\n"
    text += "Статус: " + ("включена ✅" if subscription_required() else "выключена ❌") + "\n\n"
    text += "Каналы:\n" + ("\n".join(f"• {c['title'] or c['chat_id']}" for c in channels) if channels else "нет добавленных каналов")
    text += "\n\nПока каналов нет — требование ни на кого не действует, даже если включено."
    await message.answer(text, reply_markup=admin_channels_keyboard(), parse_mode=ParseMode.HTML)


async def admin_channel_add_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    if message.forward_from_chat:
        chat_ref = message.forward_from_chat.id
    else:
        text = (message.text or "").strip()
        if not text:
            await message.answer("Пришлите @username канала или перешлите пост из него.")
            return
        chat_ref = text if text.startswith("@") else f"@{text}"
    try:
        chat = await message.bot.get_chat(chat_ref)
    except Exception:
        await message.answer("Канал не найден. Убедитесь, что бот добавлен в канал, и попробуйте снова.")
        return
    invite_link = f"https://t.me/{chat.username}" if chat.username else None
    if not invite_link:
        try:
            invite_link = await message.bot.export_chat_invite_link(chat.id)
        except Exception:
            invite_link = None
    with closing(connect()) as db:
        db.execute("INSERT INTO required_channels (chat_id,title,invite_link) VALUES (?,?,?)",
                   (str(chat.id), chat.title, invite_link))
        db.commit()
    await state.clear()
    await message.answer(f"✅ Канал «{chat.title}» добавлен.", reply_markup=admin_menu_keyboard())


def admin_premium_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏ Изменить условия оплаты", callback_data="admin:premium_info")],
        [InlineKeyboardButton(text="🎁 Выдать/забрать премиум", callback_data="admin:premium_grant")],
        [InlineKeyboardButton(text="‹ Назад", callback_data="admin:close_premium")],
    ])


async def admin_premium_menu(message, admin_id=None):
    if not is_admin(admin_id or message.from_user.id):
        return
    with closing(connect()) as db:
        premium_count = db.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
    info_text = get_setting("premium_info", "не задано")
    text = ("⭐ <b>Премиум</b>\n\n"
            f"Премиум-пользователей в базе: {premium_count} (админы всегда премиум автоматически)\n\n"
            f"Текущие условия оплаты, которые видят пользователи:\n{info_text}")
    await message.answer(text, reply_markup=admin_premium_keyboard(), parse_mode=ParseMode.HTML)


async def admin_premium_info_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришлите текст с условиями оплаты (можно со ссылкой).")
        return
    set_setting("premium_info", text)
    await state.clear()
    await message.answer("✅ Условия оплаты обновлены.", reply_markup=admin_menu_keyboard())


async def admin_premium_grant_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Нужен числовой Telegram ID пользователя.")
        return
    user_id = int(text)
    with closing(connect()) as db:
        row = db.execute("SELECT is_premium FROM users WHERE telegram_id=?", (user_id,)).fetchone()
        if not row:
            await message.answer("Пользователь с таким ID ещё не писал боту.")
            return
        new_value = 0 if row["is_premium"] else 1
        db.execute("UPDATE users SET is_premium=? WHERE telegram_id=?", (new_value, user_id))
        db.commit()
    await state.clear()
    result_text = "выдан ✅" if new_value else "снят ❌"
    await message.answer(f"Премиум для {user_id} {result_text}.", reply_markup=admin_menu_keyboard())
    try:
        if new_value:
            await message.bot.send_message(user_id, "🎉 Вам выдан премиум! Загляните в «👤 Мой профиль», чтобы увидеть новые возможности.")
        else:
            await message.bot.send_message(user_id, "Ваш премиум-статус был снят администратором.")
    except Exception:
        pass


async def noop_callback(callback: CallbackQuery):
    await callback.answer()


async def check_subs_callback(callback: CallbackQuery):
    ok, missing = await check_subscriptions(callback.bot, callback.from_user.id)
    if ok:
        await callback.answer("Подписка подтверждена ✅", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer("Спасибо! Теперь бот доступен.", reply_markup=keyboard(callback.from_user.id))
    else:
        await callback.answer("Вы подписались ещё не на все каналы.", show_alert=True)


async def admin_edit_save(message, state):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    if "search_id" in data:
        search_id = data["search_id"]
        new_query = (message.text or "").strip()
        if not new_query:
            await message.answer("Введите текст поиска.")
            return
        with closing(connect()) as db:
            db.execute("UPDATE saved_searches SET query=? WHERE id=?", (new_query, search_id))
            db.commit()
        await state.clear()
        await message.answer("✅ Поиск обновлён.", reply_markup=admin_menu_keyboard())
    else:
        try:
            title, author, book_format, city, condition = [item.strip() for item in (message.text or "").split("|", 4)]
            if book_format not in FORMATS:
                raise ValueError
        except ValueError:
            await message.answer("Нужно 5 полей через |. Формат: Настоящая книга / Аудио / Видео / PDF")
            return
        with closing(connect()) as db:
            db.execute("UPDATE books SET title=?, author=?, format=?, city=?, condition=? WHERE id=?", (title, author, book_format, city, condition, data["book_id"]))
            db.commit()
        await state.clear()
        await message.answer("✅ Книга обновлена.", reply_markup=keyboard(message.from_user.id))


async def ban_action(message):
    if not is_admin(message.from_user.id): return
    command, raw_id = message.text.split("_", 1); user_id = int(raw_id)
    with closing(connect()) as db: db.execute("UPDATE users SET is_banned=? WHERE telegram_id=?", (1 if command == "/ban" else 0, user_id)); db.commit()
    await message.answer("Пользователь заблокирован." if command == "/ban" else "Пользователь разблокирован.")


async def delete_book(message):
    book_id = int(message.text.rsplit("_", 1)[1])
    with closing(connect()) as db:
        row = db.execute("SELECT owner_id FROM books WHERE id=?", (book_id,)).fetchone()
        if not row or (row["owner_id"] != message.from_user.id and not is_admin(message.from_user.id)): await message.answer("Книга не найдена."); return
        db.execute("UPDATE books SET status='deleted' WHERE id=?", (book_id,)); db.commit()
    await message.answer("Книга удалена.")


async def edit_book(message):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split(maxsplit=1)
    try:
        book_id = int(parts[0].rsplit("_", 1)[1])
        title, author, book_format, city, condition = [item.strip() for item in parts[1].split("|", 4)]
        if book_format not in FORMATS:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer("Формат: /edit_book_ID название | автор | формат | город | состояние")
        return
    with closing(connect()) as db:
        db.execute("UPDATE books SET title=?, author=?, format=?, city=?, condition=? WHERE id=?",
                   (title, author, book_format, city, condition, book_id))
        db.commit()
    await message.answer("Книга обновлена.")


async def cancel(message, state):
    current_state = await state.get_state()
    if current_state == SearchBook.browsing:
        data = await state.get_data()
        with closing(connect()) as db:
            db.execute("INSERT INTO saved_searches (user_id,query,format,city) VALUES (?,?,?,?)", 
                      (message.from_user.id, data.get("query", ""), None, None))
            db.commit()
    await state.clear()
    await message.answer("Отменено.", reply_markup=keyboard(message.from_user.id))


async def go_menu(message, state):
    current_state = await state.get_state()
    if current_state == SearchBook.browsing:
        data = await state.get_data()
        with closing(connect()) as db:
            db.execute("INSERT INTO saved_searches (user_id,query,format,city) VALUES (?,?,?,?)", 
                      (message.from_user.id, data.get("query", ""), None, None))
            db.commit()
    await state.clear()
    await start(message, state)


async def fallback_message(message: Message, state: FSMContext):
    """Ловит любые сообщения/команды, которые не подошли ни под один другой обработчик:
    показывает меню, либо просит завершить анкету, либо просит подписаться (если это включено)."""
    if not await allowed(message):
        return
    with closing(connect()) as db:
        user = db.execute("SELECT onboarded FROM users WHERE telegram_id=?", (message.from_user.id,)).fetchone()
    if user and not user["onboarded"]:
        await state.set_state(Onboarding.step)
        await state.update_data(step=0)
        await onboarding_start(message, state)
        return
    await message.answer("Не совсем понял вас 🙂 Вот главное меню:", reply_markup=keyboard(message.from_user.id))


async def main():
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("BOT_TOKEN")
    if not token: raise RuntimeError("Переменная BOT_TOKEN не задана. Создайте файл .env.")
    acquire_instance_lock()
    init_db(); bot = Bot(token=token); dp = Dispatcher()
    dp.message.register(start, CommandStart()); dp.message.register(go_menu, F.text.in_({MENU_TEXT, BACK_TEXT, "❌ Отмена"})); dp.message.register(cancel, Command("cancel")); dp.message.register(help_message, F.text == "ℹ️ Помощь")
    dp.message.register(add_start, F.text.in_({"➕ Добавить", "➕ Добавить книгу"})); dp.message.register(search_start, F.text.in_({"🔍 Поиск", "🔍 Найти книгу"}))
    dp.message.register(all_books, F.text.in_({"📚 Каталог", "📚 Книги пользователей"})); dp.message.register(all_searches, F.text.in_({"🔎 Ищут", "🔎 Что ищут"}))
    dp.message.register(profile_start, F.text == "👤 Мой профиль")
    dp.message.register(admin, F.text == "⚙ Админка")
    dp.message.register(admin, Command("admin")); dp.message.register(admin_books, Command("admin_books")); dp.message.register(admin_users, Command("admin_users")); dp.message.register(reports, Command("reports"))
    dp.message.register(report_reason, ReportUser.reason); dp.message.register(report_target, ReportUser.target)
    dp.message.register(add_title, AddBook.title); dp.message.register(add_author, AddBook.author); dp.message.register(add_format, AddBook.book_format); dp.message.register(add_city, AddBook.city); dp.message.register(add_condition, AddBook.condition); dp.message.register(add_deal, AddBook.deal); dp.message.register(add_price, AddBook.price)
    dp.message.register(search_query, SearchBook.query)
    dp.message.register(add_photo, AddBook.photo)
    dp.message.register(admin_broadcast_send, AdminBroadcast.message)
    dp.message.register(admin_edit_save, AdminEdit.book)
    dp.message.register(admin_channel_add_save, AdminChannel.add)
    dp.message.register(admin_premium_info_save, AdminPremium.info)
    dp.message.register(admin_premium_grant_save, AdminPremium.grant)
    dp.message.register(profile_contact_save, ProfileEdit.contact)
    dp.message.register(book_by_id, F.text.regexp(r"^\d+$"))
    dp.callback_query.register(request_callback, F.data.startswith("request:"))
    dp.callback_query.register(request_callback, F.data.startswith("buy:"))
    dp.callback_query.register(format_toggle_callback, F.data.startswith("format:"))
    dp.callback_query.register(format_done_callback, F.data == "format_done")
    dp.callback_query.register(offers_callback, F.data.startswith("offers:"))
    dp.callback_query.register(offer_toggle_callback, F.data.startswith("offer:"))
    dp.callback_query.register(offer_done_callback, F.data.startswith("offer_done:"))
    dp.callback_query.register(offer_cancel_callback, F.data.startswith("offer_cancel:"))
    dp.callback_query.register(counter_accept_callback, F.data.startswith("counter_accept:"))
    dp.callback_query.register(book_detail_callback, F.data.startswith("detail:"))
    dp.callback_query.register(catalog_page_callback, F.data.startswith("catalog:"))
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
    dp.callback_query.register(premium_notify_callback, F.data == "premium_notify")
    dp.callback_query.register(mybook_delete_callback, F.data.startswith("mybook_del:"))
    dp.callback_query.register(mybook_boost_callback, F.data.startswith("mybook_boost:"))
    dp.message.register(request_book, F.text.regexp(r"^/request_\d+$")); dp.message.register(accept_request, F.text.regexp(r"^/accept_\d+$")); dp.message.register(decline_request, F.text.regexp(r"^/decline_\d+$")); dp.message.register(delete_book, F.text.regexp(r"^/delete_book_\d+$")); dp.message.register(ban_action, F.text.regexp(r"^/(?:ban|unban)_\d+$"))
    dp.message.register(delete_user, F.text.regexp(r"^/delete_user_\d+$"))
    dp.message.register(edit_book, F.text.regexp(r"^/edit_book_\d+ .+"))
    dp.message.register(admin_stats, Command("stats")); dp.message.register(admin_broadcast_start, Command("broadcast"))
    # Обязательно регистрируем последним: ловит все сообщения, не подошедшие ни под один обработчик выше.
    dp.message.register(fallback_message)
    try:
        await bot.get_updates(offset=-1, timeout=0)
        await dp.start_polling(bot)
    except TelegramConflictError:
        print("Ошибка: этот бот уже запущен в другом процессе. Закройте второй экземпляр и запустите снова.")
    finally:
        await bot.session.close()
        release_instance_lock()


if __name__ == "__main__":
    asyncio.run(main())
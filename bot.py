import asyncio
import logging
import random
import time
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "9235063746:AAEdizOiHGA9zGecwQr0nOTe1FA7zNJAF_g"
ADMIN_PASSWORD = "0408"
SUPPORT_USERNAME = "your_support"   # без @
SUPPORT_URL = "https://t.me/crypto_exchange_bot"  # крипто-бот для пополнения/вывода
ADMIN_IDS = [123456789]  # <-- впиши свой Telegram ID (узнать: @userinfobot)

START_PHOTO = "https://placehold.co/800x400/png?text=Casino+Bot"
START_TEXT = (
    "<b>Добро пожаловать! 🎰</b>\n\n"
    "Здесь ты можешь заработать бесплатные монеты, "
    "следить за балансом и связаться с поддержкой."
)
START_PHOTO_FILE_ID = None  # заполнится при первом использовании

BTN1_TEXT = "💰 Заработать"
BTN2_TEXT = "👤 Профиль"
BTN3_TEXT = "🆘 Поддержка"

COIN_COOLDOWN = 0.25  # сек

# ==================== БД ====================
DB_PATH = "bot.db"

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                free_balance REAL DEFAULT 0,
                paid_balance REAL DEFAULT 0,
                last_click REAL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()
        # дефолтные настройки кнопок
        defaults = {
            "btn1_text": BTN1_TEXT,
            "btn2_text": BTN2_TEXT,
            "btn3_text": BTN3_TEXT,
            "start_photo": START_PHOTO,
            "start_text": START_TEXT,
        }
        for k, v in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v)
            )
        await db.commit()

async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()

async def ensure_user(uid: int, username: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username) VALUES(?,?)",
            (uid, username or ""),
        )
        await db.commit()

async def get_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username, free_balance, paid_balance, last_click "
            "FROM users WHERE user_id=?",
            (uid,),
        )
        return await cur.fetchone()

async def update_balance(uid: int, free_delta=0.0, paid_delta=0.0, last_click=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if last_click is not None:
            await db.execute(
                "UPDATE users SET free_balance = free_balance + ?, "
                "paid_balance = paid_balance + ?, last_click = ? WHERE user_id = ?",
                (free_delta, paid_delta, last_click, uid),
            )
        else:
            await db.execute(
                "UPDATE users SET free_balance = free_balance + ?, "
                "paid_balance = paid_balance + ? WHERE user_id = ?",
                (free_delta, paid_delta, uid),
            )
        await db.commit()

# ==================== FSM ====================
class AdminFSM(StatesGroup):
    waiting_password = State()
    menu = State()
    change_btn = State()
    change_photo = State()
    broadcast = State()
    give_balance = State()
    take_balance = State()

# ==================== КЛАВИАТУРЫ ====================
def main_kb(btn1: str, btn2: str, btn3: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn1, callback_data="earn")],
        [InlineKeyboardButton(text=btn2, callback_data="profile")],
        [InlineKeyboardButton(text=btn3, callback_data="support")],
    ])

def earn_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Получить монету", callback_data="click")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ])

def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Пополнить", url=SUPPORT_URL)],
        [InlineKeyboardButton(text="➖ Вывести", url=SUPPORT_URL)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ])

def support_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать", url=f"https://t.me/{SUPPORT_USERNAME}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ])

def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить текст кнопки", callback_data="adm_btn_text")],
        [InlineKeyboardButton(text="🖼 Изменить фото кнопки/старта", callback_data="adm_photo")],
        [InlineKeyboardButton(text="📢 Дать объявление", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="💵 Выдать баланс", callback_data="adm_give")],
        [InlineKeyboardButton(text="💸 Забрать баланс", callback_data="adm_take")],
        [InlineKeyboardButton(text="⬅️ Выход", callback_data="adm_exit")],
    ])

# ==================== ХЭНДЛЕРЫ ====================
router = Router()

async def send_start(target: Message | CallbackQuery, uid: int):
    photo = await get_setting("start_photo")
    text = await get_setting("start_text")
    b1, b2, b3 = (await get_setting("btn1_text"),
                  await get_setting("btn2_text"),
                  await get_setting("btn3_text"))
    kb = main_kb(b1, b2, b3)
    try:
        if isinstance(target, CallbackQuery):
            await target.message.delete()
            await target.message.answer_photo(photo=photo, caption=text, reply_markup=kb)
        else:
            await target.answer_photo(photo=photo, caption=text, reply_markup=kb)
    except Exception:
        # если фото битое - шлём текст
        if isinstance(target, CallbackQuery):
            await target.message.answer(text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)

@router.message(CommandStart())
async def cmd_start(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.username)
    await send_start(msg, msg.from_user.id)

@router.callback_query(F.data == "back")
async def back(cb: CallbackQuery):
    await send_start(cb, cb.from_user.id)
    await cb.answer()

@router.callback_query(F.data == "earn")
async def earn(cb: CallbackQuery):
    await cb.message.delete()
    await cb.message.answer(
        "🎯 <b>Жми на кнопку каждые 0.25 сек</b>\n"
        "За каждый клик дают от 1 до 10 бесплатных монет.\n\n"
        "⚠️ Слишком частые нажатия не засчитываются.",
        reply_markup=earn_kb(),
    )
    await cb.answer()

@router.callback_query(F.data == "click")
async def click(cb: CallbackQuery):
    uid = cb.from_user.id
    now = time.time()
    user = await get_user(uid)
    if not user:
        await ensure_user(uid, cb.from_user.username)
        user = await get_user(uid)
    last = user[4]
    if now - last < COIN_COOLDOWN:
        await cb.answer("⏳ Подожди чуть-чуть!", show_alert=False)
        return
    gain = random.randint(1, 10)
    await update_balance(uid, free_delta=gain, last_click=now)
    await cb.answer(f"💰 +{gain} монет!")

@router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    uid = cb.from_user.id
    user = await get_user(uid)
    if not user:
        await ensure_user(uid, cb.from_user.username)
        user = await get_user(uid)
    free, paid = user[2], user[3]
    # курс: 100 монет = 1 USD (можешь поменять)
    free_usd = free / 100
    paid_usd = paid / 100
    await cb.message.delete()
    await cb.message.answer(
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"🆓 Бесплатный баланс: <b>{free:.2f}</b> монет (~${free_usd:.2f})\n"
        f"💳 Платный баланс: <b>{paid:.2f}</b> монет (~${paid_usd:.2f})\n\n"
        f"💰 Всего в USD: <b>${free_usd + paid_usd:.2f}</b>",
        reply_markup=profile_kb(),
    )
    await cb.answer()

@router.callback_query(F.data == "support")
async def support(cb: CallbackQuery):
    await cb.message.delete()
    await cb.message.answer(
        "🆘 <b>Поддержка</b>\n\n"
        "Нажми кнопку ниже — перекинет на живого человека.\n"
        "Помогаем с покером, депозитами и любыми вопросами.",
        reply_markup=support_kb(),
    )
    await cb.answer()

# ==================== АДМИН-ПАНЕЛЬ ====================
@router.message(Command("adm"))
async def adm_cmd(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await msg.answer("⛔ Нет доступа.")
        return
    await state.set_state(AdminFSM.waiting_password)
    await msg.answer("🔐 Введи пароль:")

@router.message(AdminFSM.waiting_password)
async def adm_pass(msg: Message, state: FSMContext):
    if msg.text != ADMIN_PASSWORD:
        await msg.answer("❌ Неверный пароль.")
        await state.clear()
        return
    await state.set_state(AdminFSM.menu)
    await msg.answer("✅ Добро пожаловать в админ-панель.", reply_markup=admin_kb())

@router.callback_query(F.data == "adm_exit")
async def adm_exit(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("👋 Вы вышли из админ-панели.")
    await cb.answer()

# --- изменить текст кнопки ---
@router.callback_query(F.data == "adm_btn_text")
async def adm_btn_text(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.change_btn)
    b1, b2, b3 = (await get_setting("btn1_text"),
                  await get_setting("btn2_text"),
                  await get_setting("btn3_text"))
    await cb.message.edit_text(
        "Отправь в формате:\n"
        "<code>1|Новый текст</code>  — кнопка 1 (Заработать)\n"
        "<code>2|Новый текст</code>  — кнопка 2 (Профиль)\n"
        "<code>3|Новый текст</code>  — кнопка 3 (Поддержка)\n\n"
        f"Сейчас:\n1️⃣ {b1}\n2️⃣ {b2}\n3️⃣ {b3}"
    )
    await cb.answer()

@router.message(AdminFSM.change_btn)
async def adm_btn_text_set(msg: Message, state: FSMContext):
    try:
        num, text = msg.text.split("|", 1)
        num = num.strip()
        text = text.strip()
        key = {"1": "btn1_text", "2": "btn2_text", "3": "btn3_text"}[num]
        await set_setting(key, text)
        await msg.answer(f"✅ Кнопка {num} обновлена: {text}", reply_markup=admin_kb())
        await state.set_state(AdminFSM.menu)
    except Exception:
        await msg.answer("❌ Формат: <code>1|Текст</code>")

# --- изменить фото ---
@router.callback_query(F.data == "adm_photo")
async def adm_photo(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.change_photo)
    await cb.message.edit_text(
        "Отправь фото (или ссылку на картинку) для стартового экрана:"
    )
    await cb.answer()

@router.message(AdminFSM.change_photo)
async def adm_photo_set(msg: Message, state: FSMContext):
    if msg.photo:
        file_id = msg.photo[-1].file_id
        await set_setting("start_photo", file_id)
    elif msg.text:
        await set_setting("start_photo", msg.text.strip())
    else:
        await msg.answer("❌ Отправь фото или ссылку.")
        return
    await msg.answer("✅ Фото обновлено.", reply_markup=admin_kb())
    await state.set_state(AdminFSM.menu)

# --- рассылка ---
@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.broadcast)
    await cb.message.edit_text("Отправь текст объявления для всех пользователей:")
    await cb.answer()

@router.message(AdminFSM.broadcast)
async def adm_broadcast_send(msg: Message, state: FSMContext, bot: Bot):
    text = msg.html_text if msg.text else msg.caption
    if not text:
        await msg.answer("❌ Нужен текст.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        users = [r[0] for r in await cur.fetchall()]
    ok, fail = 0, 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 <b>Объявление</b>\n\n{text}")
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await msg.answer(f"✅ Отправлено: {ok}\n❌ Ошибок: {fail}", reply_markup=admin_kb())
    await state.set_state(AdminFSM.menu)

# --- выдать/забрать баланс ---
@router.callback_query(F.data == "adm_give")
async def adm_give(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.give_balance)
    await cb.message.edit_text(
        "Формат: <code>user_id amount тип</code>\n"
        "тип: <code>free</code> или <code>paid</code>\n\n"
        "Пример: <code>123456789 100 free</code>"
    )
    await cb.answer()

@router.callback_query(F.data == "adm_take")
async def adm_take(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.take_balance)
    await cb.message.edit_text(
        "Формат: <code>user_id amount тип</code>\n"
        "тип: <code>free</code> или <code>paid</code>\n\n"
        "Пример: <code>123456789 50 paid</code>"
    )
    await cb.answer()

@router.message(AdminFSM.give_balance)
async def adm_give_do(msg: Message, state: FSMContext):
    try:
        parts = msg.text.split()
        uid = int(parts[0])
        amount = float(parts[1])
        typ = parts[2].lower()
        if typ == "free":
            await update_balance(uid, free_delta=amount)
        elif typ == "paid":
            await update_balance(uid, paid_delta=amount)
        else:
            raise ValueError
        await msg.answer(f"✅ Выдано {amount} ({typ}) пользователю {uid}",
                         reply_markup=admin_kb())
        await state.set_state(AdminFSM.menu)
    except Exception:
        await msg.answer("❌ Формат: <code>user_id amount free|paid</code>")

@router.message(AdminFSM.take_balance)
async def adm_take_do(msg: Message, state: FSMContext):
    try:
        parts = msg.text.split()
        uid = int(parts[0])
        amount = float(parts[1])
        typ = parts[2].lower()
        if typ == "free":
            await update_balance(uid, free_delta=-amount)
        elif typ == "paid":
            await update_balance(uid, paid_delta=-amount)
        else:
            raise ValueError
        await msg.answer(f"✅ Списано {amount} ({typ}) у {uid}",
                         reply_markup=admin_kb())
        await state.set_state(AdminFSM.menu)
    except Exception:
        await msg.answer("❌ Формат: <code>user_id amount free|paid</code>")

# ==================== ЗАПУСК ====================
async def main():
    logging.basicConfig(level=logging.INFO)
    await db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
import yt_dlp

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [994444870]  # Ваш Telegram ID (узнать можно у @userinfobot)

# Список каналов для обязательной подписки.
# ВАЖНО: Бота нужно добавить в эти каналы АДМИНИСТРАТОРОМ!
REQUIRED_CHANNELS = [
    {"id": "-1006059916339", "url": "https://t.me/+J-_eEaHZUyU3MjVi", "name": "ПОДПИСАТЬСЯ"},
    # Можно добавить второй канал:
    # {"id": -1001234567890, "url": "https://t.me", "name": "Закрытый канал"},
]
# =============================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# --- РАБОТА С БАЗОЙ ДАННЫХ (SQLite) ---
def init_db():
    with sqlite3.connect("bot.db") as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                joined_at TEXT,
                downloads INTEGER DEFAULT 0
            )
        """)


def add_user(user_id: int):
    with sqlite3.connect("bot.db") as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?, ?)",
            (user_id, datetime.now().isoformat()),
        )


def increment_downloads(user_id: int):
    with sqlite3.connect("bot.db") as conn:
        conn.execute(
            "UPDATE users SET downloads = downloads + 1 WHERE user_id = ?", (user_id,)
        )


def get_stats():
    with sqlite3.connect("bot.db") as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        new_today = conn.execute(
            "SELECT COUNT(*) FROM users WHERE joined_at >= ?", (yesterday,)
        ).fetchone()[0]
        total_downloads = (
            conn.execute("SELECT SUM(downloads) FROM users").fetchone()[0] or 0
        )
        return total_users, new_today, total_downloads


def get_all_users():
    with sqlite3.connect("bot.db") as conn:
        return [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]


# --- ПРОВЕРКА ПОДПИСКИ ---
async def check_subscription(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel["id"], user_id=user_id)
            if member.status in ["left", "kicked"]:
                return False
        except Exception as e:
            logging.error(f"Ошибка проверки канала {channel['id']}: {e}")
            # Если бот не админ в канале, пропускаем проверку, чтобы бот не сломался
            continue
    return True


def get_sub_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"📢 {ch['name']}", url=ch["url"])]
        for ch in REQUIRED_CHANNELS
    ]
    buttons.append(
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- СКАЧИВАНИЕ ВИДЕО ---
def download_instagram_video(url: str) -> str:
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


# --- ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЕЙ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    add_user(message.from_user.id)
    if not await check_subscription(message.from_user.id):
        await message.answer(
            "👋 Привет! Чтобы пользоваться ботом, подпишись на наши каналы:",
            reply_markup=get_sub_keyboard(),
        )
        return

    await message.answer(
        "👋 Привет! Отправь мне ссылку на открытый Reels или видео из Instagram, и я скачаю его."
    )


@dp.callback_query(F.data == "check_sub")
async def callback_check_sub(callback: CallbackQuery):
    if await check_subscription(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Спасибо за подписку! Теперь отправь ссылку на видео из Instagram."
        )
    else:
        await callback.answer("❌ Вы подписались не на все каналы!", show_alert=True)


@dp.message(F.text.regexp(r"https?://(www\.)?instagram\.com/(reel|p|tv)/"))
async def handle_instagram_link(message: types.Message):
    add_user(message.from_user.id)

    if not await check_subscription(message.from_user.id):
        await message.answer(
            "🔒 Для скачивания видео необходимо подписаться на каналы:",
            reply_markup=get_sub_keyboard(),
        )
        return

    url = message.text.strip()
    status_msg = await message.answer("⏳ Скачиваю видео, подождите...")
    file_path = None

    try:
        file_path = await asyncio.to_thread(download_instagram_video, url)
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)

        if file_size_mb > 49.5:
            await status_msg.edit_text(
                f"❌ Видео весит {file_size_mb:.1f} МБ. Лимит Telegram для ботов — 50 МБ."
            )
            return

        await status_msg.edit_text("📤 Отправляю видео...")
        video = FSInputFile(file_path)
        await message.answer_video(video=video, caption="Готово! 📥")
        increment_downloads(message.from_user.id)
        await status_msg.delete()

    except Exception as e:
        logging.error(f"Ошибка при скачивании {url}: {e}")
        await status_msg.edit_text(
            "❌ Не удалось скачать видео. Убедитесь, что профиль открыт, или попробуйте позже."
        )
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


# --- АДМИН-ПАНЕЛЬ И РАССЫЛКА ---
class BroadcastState(StatesGroup):
    waiting_for_post = State()


def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="✉️ Сделать рассылку", callback_data="admin_broadcast")],
        ]
    )


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🛠 <b>Панель администратора</b>", reply_markup=get_admin_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    total, new_today, downloads = get_stats()
    text = (
        "📊 <b>Статистика бота:</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"🆕 Новых за 24 часа: <b>{new_today}</b>\n"
        f"📥 Скачано видео: <b>{downloads}</b>"
    )
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")]]
    )
    await callback.message.edit_text(
        "✉️ Отправьте сообщение для рассылки (текст, фото, видео или кружок):",
        reply_markup=cancel_kb,
    )
    await state.set_state(BroadcastState.waiting_for_post)


@dp.callback_query(F.data == "admin_cancel")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🛠 <b>Панель администратора</b>", reply_markup=get_admin_keyboard(), parse_mode="HTML")


@dp.message(BroadcastState.waiting_for_post)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()

    users = get_all_users()
    status_msg = await message.answer(f"🚀 Начинаю рассылку по {len(users)} пользователям...")

    sent = 0
    blocked = 0

    for user_id in users:
        try:
            # copy_message пересылает любой тип контента без пометки "Переслано от"
            await message.copy_to(chat_id=user_id)
            sent += 1
            await asyncio.sleep(0.05)  # Защита от спам-лимитов Telegram (20 сообщений в сек)
        except TelegramForbiddenError:
            blocked += 1  # Пользователь заблокировал бота
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await message.copy_to(chat_id=user_id)
            sent += 1
        except Exception:
            blocked += 1

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📩 Успешно доставлено: <b>{sent}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b>",
        parse_mode="HTML",
    )


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

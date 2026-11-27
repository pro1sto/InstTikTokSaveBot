import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta

import yt_dlp
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [994444870]  # Ваш Telegram ID (узнать можно у @userinfobot)

# Список каналов для обязательной подписки.
# ВАЖНО: Бота нужно добавить в эти каналы АДМИНИСТРАТОРОМ!
REQUIRED_CHANNELS = [
    {"id": "-1006003972835", "url": "https://t.me/+afPTm145m0JhNjcy", "name": "ПОДПИСАТЬСЯ"},
    # Можно добавить второй канал:
    # {"id": "-1001234567890", "url": "https://t.me/...", "name": "Закрытый канал"},
]
# =============================================

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot.db")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

is_sending = False


# --- РАБОТА С БАЗОЙ ДАННЫХ (SQLite) ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                joined_at TEXT,
                downloads INTEGER DEFAULT 0
            )
        """)


def add_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, joined_at) VALUES (?, ?)",
            (user_id, datetime.now().isoformat()),
        )


def increment_downloads(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET downloads = downloads + 1 WHERE user_id = ?",
            (user_id,),
        )


def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
        return [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]


def remove_users_bulk(user_ids: list[int]):
    if not user_ids:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "DELETE FROM users WHERE user_id = ?",
            [(uid,) for uid in user_ids],
        )


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


# --- ЗАГРУЗЧИК INSTAGRAM ---
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


# --- ЗАГРУЗЧИК TIKTOK ---
def get_tiktok_media_sync(url: str):
    api_url = "https://www.tikwm.com/api/"
    post_data = urllib.parse.urlencode({"url": url, "hd": 1}).encode("utf-8")

    req = urllib.request.Request(
        api_url,
        data=post_data,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.tikwm.com/",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))

        if result.get("code") != 0:
            logging.error("TikWM error: %s", result)
            return None

        data = result.get("data") or {}

        # Фото-слайдшоу
        if data.get("images"):
            return {"type": "images", "urls": data["images"]}

        # Видео
        play_url = data.get("play")
        if play_url:
            play_url = urllib.parse.urljoin("https://www.tikwm.com/", play_url)
            return {"type": "video", "url": play_url}

        logging.error("Медиа не найдено: %s", data)
        return None
    except Exception:
        logging.exception("Ошибка TikTok API")
        return None


def download_tiktok_video_sync(url: str):
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.tiktok.com/",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if "video" not in content_type and "octet-stream" not in content_type:
                logging.warning("Неожиданный тип файла: %s", content_type)

            temp = tempfile.NamedTemporaryFile(
                dir=DOWNLOAD_DIR, suffix=".mp4", delete=False
            )
            with temp:
                shutil.copyfileobj(response, temp)

            if os.path.getsize(temp.name) == 0:
                os.remove(temp.name)
                return None

            return temp.name
    except Exception:
        logging.exception("Ошибка скачивания видео TikTok")
        return None


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
        "👋 <b>Привет!</b>

"
        "Отправь мне ссылку на видео или фото-пост из <b>TikTok</b>, "
        "либо на открытый Reels / видео из <b>Instagram</b>, и я скачаю его.",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "check_sub")
async def callback_check_sub(callback: CallbackQuery):
    if await check_subscription(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Спасибо за подписку! Теперь отправь ссылку на видео из TikTok или Instagram."
        )
    else:
        await callback.answer("❌ Вы подписались не на все каналы!", show_alert=True)


async def process_instagram(message: types.Message, url: str):
    status_msg = await message.answer("⏳ Скачиваю видео из Instagram, подождите...")
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
        logging.error(f"Ошибка при скачивании Instagram {url}: {e}")
        await status_msg.edit_text(
            "❌ Не удалось скачать видео. Убедитесь, что профиль открыт, или попробуйте позже."
        )
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


async def process_tiktok(message: types.Message, url: str):
    status_msg = await message.answer("⚡ Получаю данные TikTok...")
    tt_data = await asyncio.to_thread(get_tiktok_media_sync, url)

    if not tt_data:
        await status_msg.edit_text(
            "❌ Не удалось получить медиа.

"
            "Возможно, ролик удалён, закрыт или сервис загрузки временно недоступен."
        )
        return

    temp_path = None
    try:
        if tt_data["type"] == "images":
            photos = tt_data["urls"]
            if not photos:
                raise ValueError("Список фотографий пуст")

            for idx in range(0, len(photos), 10):
                media_group = [
                    InputMediaPhoto(media=p) for p in photos[idx : idx + 10]
                ]
                await message.answer_media_group(media=media_group)

            increment_downloads(message.from_user.id)

        elif tt_data["type"] == "video":
            await status_msg.edit_text("📥 Скачиваю видео...")
            temp_path = await asyncio.to_thread(
                download_tiktok_video_sync, tt_data["url"]
            )

            if not temp_path:
                await status_msg.edit_text("❌ Не удалось скачать видео.")
                return

            file_size_mb = os.path.getsize(temp_path) / (1024 * 1024)
            if file_size_mb > 49.5:
                await status_msg.edit_text(
                    f"❌ Видео весит {file_size_mb:.1f} МБ. Лимит Telegram — 50 МБ."
                )
                return

            await status_msg.edit_text("📤 Отправляю видео в Telegram...")
            video = FSInputFile(temp_path, filename="tiktok_video.mp4")
            await message.answer_video(
                video=video,
                supports_streaming=True,
                caption="Готово! 📥",
            )
            increment_downloads(message.from_user.id)

        await status_msg.delete()

    except Exception:
        logging.exception("Ошибка отправки медиа TikTok")
        try:
            await status_msg.edit_text(
                "❌ Ошибка отправки файла.
"
                "Возможно, видео слишком большое или Telegram не поддерживает его формат."
            )
        except Exception:
            pass
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@dp.message(F.text)
async def handle_links(message: types.Message):
    add_user(message.from_user.id)

    url = message.text.strip()
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()

    instagram_domains = {"instagram.com", "www.instagram.com"}
    tiktok_domains = {
        "tiktok.com",
        "www.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
    }

    if parsed.scheme not in ("http", "https") or host not in (
        instagram_domains | tiktok_domains
    ):
        await message.answer(
            "⚠️ Пожалуйста, отправь корректную ссылку на видео из <b>TikTok</b> или <b>Instagram</b>.",
            parse_mode="HTML",
        )
        return

    if not await check_subscription(message.from_user.id):
        await message.answer(
            "🔒 Для скачивания необходимо подписаться на каналы:",
            reply_markup=get_sub_keyboard(),
        )
        return

    if host in instagram_domains:
        await process_instagram(message, url)
    elif host in tiktok_domains:
        await process_tiktok(message, url)


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
    await message.answer(
        "🛠 <b>Панель администратора</b>",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Command("stop_send"))
async def stop_send_cmd(message: types.Message):
    global is_sending
    if message.from_user.id not in ADMIN_IDS:
        return
    if is_sending:
        is_sending = False
        await message.answer("🛑 Останавливаю рассылку...")
    else:
        await message.answer("ℹ️ Активная рассылка не запущена.")


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    total, new_today, downloads = get_stats()
    text = (
        "📊 <b>Статистика бота:</b>

"
        f"👥 Всего пользователей: <b>{total}</b>
"
        f"🆕 Новых за 24 часа: <b>{new_today}</b>
"
        f"📥 Скачано файлов: <b>{downloads}</b>"
    )
    await callback.message.edit_text(
        text, reply_markup=get_admin_keyboard(), parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    if is_sending:
        await callback.answer("⚠️ Рассылка уже идёт!", show_alert=True)
        return

    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")]
        ]
    )
    await callback.message.edit_text(
        "✉️ Отправьте сообщение для рассылки (текст, фото, видео или кружок):
"
        "<i>Для экстренной остановки используйте /stop_send</i>",
        reply_markup=cancel_kb,
        parse_mode="HTML",
    )
    await state.set_state(BroadcastState.waiting_for_post)


@dp.callback_query(F.data == "admin_cancel")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🛠 <b>Панель администратора</b>",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML",
    )


@dp.message(BroadcastState.waiting_for_post)
async def process_broadcast(message: types.Message, state: FSMContext):
    global is_sending
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()

    users = get_all_users()
    total = len(users)
    status_msg = await message.answer(
        f"🚀 Начинаю рассылку по {total} пользователям..."
    )

    is_sending = True
    sent = 0
    blocked = 0
    dead_batch = []

    try:
        for i, user_id in enumerate(users, start=1):
            if not is_sending:
                break

            try:
                await message.copy_to(chat_id=user_id)
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await message.copy_to(chat_id=user_id)
                    sent += 1
                except Exception:
                    dead_batch.append(user_id)
                    blocked += 1
            except (TelegramForbiddenError, TelegramBadRequest):
                dead_batch.append(user_id)
                blocked += 1
            except Exception:
                logging.exception("Ошибка рассылки пользователю %s", user_id)

            await asyncio.sleep(0.05)

            if i % 100 == 0:
                remove_users_bulk(dead_batch)
                dead_batch.clear()
                try:
                    await status_msg.edit_text(
                        f"⏳ Рассылка: {i}/{total}
"
                        f"📩 Доставлено: {sent}
"
                        f"🗑 Удалено мёртвых: {blocked}"
                    )
                except TelegramBadRequest:
                    pass
    finally:
        remove_users_bulk(dead_batch)
        is_sending = False

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>

"
        f"📩 Успешно доставлено: <b>{sent}</b>
"
        f"🗑 Удалено из базы (заблокировали): <b>{blocked}</b>
"
        f"👥 Активных в базе: <b>{get_stats()[0]}</b>",
        parse_mode="HTML",
    )


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Объединенный бот Instagram + TikTok запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен.")


import os
import sqlite3
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    BusinessConnectionHandler,
    ContextTypes,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DB = "users.db"
MOSCOW = ZoneInfo("Europe/Moscow")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def database():
    with sqlite3.connect(DB) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                connection_id TEXT NOT NULL,
                user_chat_id INTEGER NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT,
                enabled INTEGER DEFAULT 1
            )
        """)
        db.commit()


def get_users():
    with sqlite3.connect(DB) as db:
        return db.execute(
            "SELECT user_id, connection_id, first_name, "
            "last_name FROM users WHERE enabled=1"
        ).fetchall()


def get_user(user_id):
    with sqlite3.connect(DB) as db:
        return db.execute(
            "SELECT connection_id, first_name, last_name, enabled "
            "FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()


def update_status(user_id, enabled):
    with sqlite3.connect(DB) as db:
        db.execute(
            "UPDATE users SET enabled=? WHERE user_id=?",
            (enabled, user_id)
        )
        db.commit()


def make_name(base):
    time_text = datetime.now(MOSCOW).strftime("%H:%M")
    suffix = f" | {time_text} МСК"
    return base[:64 - len(suffix)] + suffix


async def update_name(bot, connection_id, first_name, last_name):
    new_first_name = make_name(first_name)

    return await bot.set_business_account_name(
        business_connection_id=connection_id,
        first_name=new_first_name,
        last_name=last_name or None,
    )


async def business_connected(update: Update, context):
    bc = update.business_connection
    user_id = bc.user.id

    if not bc.is_enabled:
        update_status(user_id, 0)
        log.info("Business connection disabled: %s", user_id)
        return

    rights = bc.rights
    if not rights or not rights.can_edit_name:
        log.warning("No permission to edit name: %s", user_id)
        return

    first_name = bc.user.first_name or "Пользователь"
    last_name = bc.user.last_name or ""

    with sqlite3.connect(DB) as db:
        db.execute("""
            INSERT INTO users (
                user_id, connection_id, user_chat_id,
                first_name, last_name, enabled
            )
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                connection_id=excluded.connection_id,
                user_chat_id=excluded.user_chat_id,
                enabled=1
        """, (
            user_id,
            bc.id,
            bc.user_chat_id,
            first_name,
            last_name,
        ))
        db.commit()

    try:
        await update_name(
            context.bot, bc.id, first_name, last_name
        )
        log.info("Name updated for user %s", user_id)
    except TelegramError:
        log.exception("Could not update name for %s", user_id)


async def clock_loop(app):
    while True:
        for user_id, connection_id, first_name, last_name in get_users():
            try:
                await update_name(
                    app.bot, connection_id, first_name, last_name
                )
            except TelegramError:
                log.exception("Update failed for user %s", user_id)

        await asyncio.sleep(60)


async def post_init(app):
    app.create_task(clock_loop(app))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🕒 Привет! Я бот московского времени.\n\n"
        "Подключи меня через:\n"
        "Настройки → Telegram для бизнеса → Чат-боты.\n\n"
        "Разреши изменение имени профиля.\n\n"
        "После подключения время будет обновляться "
        "автоматически каждую минуту.\n\n"
        "/status — статус\n"
        "/pause — остановить и вернуть исходное имя\n"
        "/resume — возобновить обновление"
    )


async def status(update: Update, context):
    row = get_user(update.effective_user.id)

    if not row:
        message = "Бизнес-аккаунт ещё не подключён."
    elif row[3]:
        message = "✅ Обновление времени включено."
    else:
        message = "⏸ Обновление времени остановлено."

    await update.message.reply_text(message)


async def pause(update: Update, context):
    user_id = update.effective_user.id
    row = get_user(user_id)

    if not row:
        await update.message.reply_text("Сначала подключи бота.")
        return

    connection_id, first_name, last_name, enabled = row

    try:
        await context.bot.set_business_account_name(
            business_connection_id=connection_id,
            first_name=first_name,
            last_name=last_name or None,
        )
        update_status(user_id, 0)
        await update.message.reply_text(
            "⏸ Автообновление остановлено. "
            "Исходное имя восстановлено."
        )
    except TelegramError as e:
        await update.message.reply_text(
            f"Не удалось изменить имя: {e}"
        )


async def resume(update: Update, context):
    user_id = update.effective_user.id
    row = get_user(user_id)

    if not row:
        await update.message.reply_text("Сначала подключи бота.")
        return

    connection_id, first_name, last_name, enabled = row

    try:
        await update_name(
            context.bot, connection_id, first_name, last_name
        )
        update_status(user_id, 1)
        await update.message.reply_text(
            "✅ Московское время снова обновляется!"
        )
    except TelegramError as e:
        await update.message.reply_text(
            f"Не удалось включить обновление: {e}"
        )


def main():
    if not TOKEN:
        raise RuntimeError("Укажи BOT_TOKEN в файле .env")

    database()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(
        BusinessConnectionHandler(business_connected)
    )

    log.info("Moscow Time Bot started")
    app.run_polling(
        allowed_updates=["message", "business_connection"]
    )


if __name__ == "__main__":
    main()

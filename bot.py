"""Точка входа: запуск бота, фоновые задачи мониторинга и автопостинга."""
import asyncio
import logging
import os
import sys

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

import channel_parser
import database as db
import handlers
import monitor
import scheduler
import web_monitor
from ai import providers as ai_providers
from config import BOT_TOKEN, DATA_DIR, DB_PATH, LOG_FILE, LOG_LEVEL

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

if LOG_FILE:
    try:
        fh = logging.FileHandler(os.path.join(DATA_DIR, LOG_FILE))
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось писать в лог-файл: %s", e)


def main() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан. Заполни .env")
        return

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    handlers.register(app)
    logger.info("Бот запускается...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


async def post_init(app: Application) -> None:
    await db.init_db()
    await ai_providers.startup()

    channel_parser.set_bot(app.bot)

    await monitor.start()
    await scheduler.start()
    await web_monitor.start()
    logger.info("БД, AI, мониторинг, автопостинг и web-сбор готовы. БД: %s", DB_PATH)


async def post_shutdown(app: Application) -> None:
    await monitor.stop()
    await scheduler.stop()
    await web_monitor.stop()
    await ai_providers.shutdown()
    await db.close_db()
    logger.info("Остановка завершена")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

"""Точка входа: запуск бота, фоновые задачи мониторинга и автопостинга."""
import asyncio
import logging
import os
import sys

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

import database as db
import handlers
import monitor
import scheduler
import session_manager as sess
from ai import providers as ai_providers
from config import DATA_DIR, LOG_FILE, LOG_LEVEL, BOT_TOKEN, DB_PATH

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
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    handlers.register(app)
    logger.info("Бот запускается…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


async def post_init(app: Application) -> None:
    await db.init_db()
    await ai_providers.startup()

    # подключаем аккаунт, если есть сессия (не критично для старта бота)
    try:
        await sess.init_client()
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось инициализировать аккаунт на старте: %s", e)

    await monitor.start()
    await scheduler.start()
    logger.info("БД, AI, мониторинг и автопостинг готовы. БД: %s", DB_PATH)


async def post_shutdown(app: Application) -> None:
    await monitor.stop()
    await scheduler.stop()
    await ai_providers.shutdown()
    await sess.disconnect()
    await db.close_db()
    logger.info("Остановка завершена")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
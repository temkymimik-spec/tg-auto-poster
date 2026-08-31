"""Отправка постов в каналы через Telegram Bot API (без Telethon)."""
import logging
import os

from telegram import Bot

from config import MEDIA_DIR

logger = logging.getLogger(__name__)

_bot: Bot | None = None


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


def get_bot() -> Bot | None:
    return _bot


async def send_post(channel_id: str, text: str, media_path: str | None = None,
                    media_url: str | None = None) -> bool:
    """Отправляет пост в канал через Bot API.

    Бот должен быть администратором канала.
    """
    if _bot is None:
        logger.error("Bot не инициализирован")
        return False
    try:
        cid = int(channel_id) if channel_id.lstrip("-").isdigit() else channel_id
        if media_path and os.path.isfile(media_path):
            with open(media_path, "rb") as f:
                await _bot.send_photo(chat_id=cid, photo=f, caption=text or None)
        elif media_url:
            await _bot.send_photo(chat_id=cid, photo=media_url, caption=text or None)
        else:
            await _bot.send_message(chat_id=cid, text=text)
        logger.info("Пост отправлен в %s", channel_id)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось отправить в %s: %s", channel_id, e)
        return False


async def send_document(channel_id: str, file_path: str, caption: str = "") -> bool:
    """Отправляет файл в канал."""
    if _bot is None:
        return False
    try:
        cid = int(channel_id) if channel_id.lstrip("-").isdigit() else channel_id
        with open(file_path, "rb") as f:
            await _bot.send_document(chat_id=cid, document=f, caption=caption or None)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось отправить документ в %s: %s", channel_id, e)
        return False

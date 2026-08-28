"""Работа с каналами через аккаунт (Telethon): резолв, чтение, отправка."""
import logging

from telethon.tl.types import Channel

from session_manager import get_client

logger = logging.getLogger(__name__)

TIME_BETWEEN_SENDS = 1.5


def normalize_ref(raw: str) -> str:
    """Приводит ссылку/username/id к виду, который Telethon поймёт."""
    ref = raw.strip()
    if ref.startswith("https://t.me/"):
        ref = "@" + ref[len("https://t.me/") :]
    elif ref.startswith("t.me/"):
        ref = "@" + ref[len("t.me/") :]
    elif ref.startswith("https://") or ref.startswith("http://"):
        return ref  # пусть Telethon решает по ссылке
    return ref.split("?")[0]


async def resolve_channel(identifier: str) -> dict | None:
    """Резолвит канал в {id, title, username}. Требует подключённый аккаунт."""
    client = await get_client()
    if client is None:
        return None
    try:
        entity = await client.get_entity(normalize_ref(identifier))
        if isinstance(entity, Channel):
            return {
                "id": str(entity.id),
                "title": entity.title,
                "username": entity.username or "",
            }
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось зарезолвить %s: %s", identifier, e)
        return None


async def fetch_recent_posts(identifier: str, limit: int = 5) -> list[dict]:
    """Последние посты канала. Только текстовые, с датой и наличием медиа."""
    client = await get_client()
    if client is None:
        return []
    try:
        entity = await client.get_entity(normalize_ref(identifier))
        posts = []
        async for m in client.iter_messages(entity, limit=limit):
            if not m.text:
                continue
            posts.append({
                "id": m.id,
                "text": m.text,
                "date": m.date.isoformat() if m.date else "",
                "has_media": bool(m.media),
            })
        return posts
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось прочитать посты %s: %s", identifier, e)
        return []


async def iter_new_posts(identifier: str, after_id: int, limit: int = 10):
    """Генератор новых постов после after_id (сверху вниз по дате)."""
    client = await get_client()
    if client is None:
        return
    try:
        entity = await client.get_entity(normalize_ref(identifier))
        async for m in client.iter_messages(entity, limit=limit):
            if m.id <= after_id:
                return
            if m.text:
                yield m
    except Exception as e:  # noqa: BLE001
        logger.warning("Ошибка чтения %s: %s", identifier, e)
        return


async def send_post(channel_id: str, text: str, media_path: str | None = None) -> bool:
    """Отправляет пост в канал через аккаунт."""
    client = await get_client()
    if client is None:
        return False
    try:
        entity = await client.get_entity(int(channel_id) if str(channel_id).lstrip("-").isdigit() else channel_id)
        if media_path:
            await client.send_file(entity, media_path, caption=text or None)
        else:
            await client.send_message(entity, text)
        logger.info("Пост отправлен в %s", channel_id)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось отправить в %s: %s", channel_id, e)
        return False


async def get_my_channels() -> list[dict]:
    """Список каналов, где аккаунт состоит/админит."""
    client = await get_client()
    if client is None:
        return []
    try:
        out = []
        async for d in client.iter_dialogs():
            if d.is_channel:
                out.append({
                    "id": str(d.entity.id),
                    "title": d.name,
                    "username": d.entity.username or "",
                })
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось получить каналы: %s", e)
        return []
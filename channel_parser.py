"""Работа с каналами через аккаунт (Telethon): резолв, чтение, отправка."""
import asyncio
import logging

from telethon.tl.types import Channel

from session_manager import get_client

logger = logging.getLogger(__name__)

TIME_BETWEEN_SENDS = 1.5

# Кэш резолвнутых entity: id(int) -> entity. Сбрасывается при реконнекте.
_entity_cache: dict[int, object] = {}
_dialogs_loaded = asyncio.Event()

# Держим все username, которые мы уже знаем, чтобы уметь резолвить по ним
# даже без тяжёлого iter_dialogs.
_known_usernames: set[str] = set()
_dialog_lock = asyncio.Lock()


def _as_int(ref: str) -> int | None:
    """Возвращает int если строка — число (срезая возможный -100 префикс)."""
    s = (ref or "").strip()
    if not s:
        return None
    # Telegram иногда хранит id supergroup/канала с префиксом -100. Срезаем
    # только полный префикс (а не посимвольно), иначе теряем цифры.
    if s.startswith("-100"):
        s = s[4:]
    if s.lstrip("-").isdigit():
        return int(s)
    return None


def normalize_ref(raw: str) -> str:
    """Приводит ссылку/username/id к виду, который Telethon поймёт."""
    ref = (raw or "").strip()
    if ref.startswith("https://t.me/"):
        ref = "@" + ref[len("https://t.me/") :]
    elif ref.startswith("t.me/"):
        ref = "@" + ref[len("t.me/") :]
    elif ref.startswith("https://") or ref.startswith("http://"):
        return ref
    return ref.split("?")[0].split("/")[0]


async def _load_dialogs(client, force: bool = False) -> None:
    """Читает все диалоги один раз и наполняет кэш entity.

    Это единственный способ по-честному резолвить числовые ID каналов,
    в которых состоит аккаунт (они нет в кэше, пока не прочитаны диалоги).
    """
    if _dialogs_loaded.is_set() and not force:
        return
    async with _dialog_lock:
        try:
            logger.info("Загружаю диалоги для кэша entity…")
            async for d in client.iter_dialogs(limit=None):
                e = d.entity
                if hasattr(e, "id"):
                    _entity_cache[int(e.id)] = e
                if getattr(e, "username", None):
                    _known_usernames.add(e.username.lower())
            logger.info("Кэш entity: %d каналов/чатов", len(_entity_cache))
        except Exception as e:  # noqa: BLE001
            logger.warning("Не удалось загрузить диалоги: %s", e)
    _dialogs_loaded.set()


async def _resolve_entity(client, identifier: str):
    """Надёжный резолв entity: username/ссылка/числовой ID.

    Порядок:
    1) числовой ID: пробуем int (сразу, если уже в кэше/знаком клиенту).
    2) username/ссылку: обычный get_entity.
    3) если не вышло числом — читаем диалоги и пробуем ещё раз.
    """
    ref = normalize_ref(identifier)
    num = _as_int(ref) if (not ref.startswith("@") and not ref.startswith("http")) else None

    # 1) числовой ID — сначала из кэша
    if num is not None:
        ent = _entity_cache.get(num)
        if ent is not None:
            return ent
        try:
            ent = await client.get_entity(num)
            _entity_cache[num] = ent
            return ent
        except ValueError:
            pass
        # 2) последний шанс для числового — читаем диалоги
        await _load_dialogs(client)
        try:
            ent = await client.get_entity(num)
            _entity_cache[num] = ent
            return ent
        except ValueError:
            logger.warning("Числовой ID %s не найден ни в кэше, ни в диалогах. "
                           "Аккаунт должен быть подписан на канал.", identifier)
            return None

    # 3) username / ссылка
    try:
        ent = await client.get_entity(ref)
        if hasattr(ent, "id"):
            _entity_cache[int(ent.id)] = ent
        if getattr(ent, "username", None):
            _known_usernames.add(ent.username.lower())
        return ent
    except (ValueError, TypeError):
        # возможно строку с числом уже обработали выше; иначе — читаем диалоги
        await _load_dialogs(client)
        try:
            ent = await client.get_entity(ref)
            if hasattr(ent, "id"):
                _entity_cache[int(ent.id)] = ent
            return ent
        except (ValueError, TypeError):
            logger.warning("Не удалось зарезолвить %s", identifier)
            return None


async def preload_dialogs() -> None:
    """Предзагрузка диалогов на старте (для ускорения мониторинга)."""
    client = await get_client()
    if client is None:
        return
    await _load_dialogs(client)


async def resolve_channel(identifier: str) -> dict | None:
    """Резолвит канал в {id, title, username}. Требует подключённый аккаунт."""
    client = await get_client()
    if client is None:
        return None
    ent = await _resolve_entity(client, identifier)
    if ent is None:
        return None
    if isinstance(ent, Channel):
        return {
            "id": str(ent.id),
            "title": ent.title,
            "username": ent.username or "",
        }
    logger.warning("Объект %s — не канал (%s)", identifier, type(ent).__name__)
    return None


async def fetch_recent_posts(identifier: str, limit: int = 5) -> list[dict]:
    """Последние посты канала. Только текстовые, с датой и наличием медиа."""
    client = await get_client()
    if client is None:
        return []
    try:
        entity = await _resolve_entity(client, identifier)
        if entity is None:
            return []
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
        entity = await _resolve_entity(client, identifier)
        if entity is None:
            return
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
        entity = await _resolve_entity(client, channel_id)
        if entity is None:
            return False
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

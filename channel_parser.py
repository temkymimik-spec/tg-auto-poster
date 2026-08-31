"""Работа с каналами через аккаунт (Telethon): резолв, чтение, отправка."""
import asyncio
import logging
import os

from telethon.tl.types import Channel

from config import MEDIA_DIR
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


async def _ensure_dialogs(client, force: bool = False) -> None:
    """Читает все диалоги СВЕЖИМ сетевым запросом и наполняет наш кэш.

    Важно: мы НЕ полагаемся на `client.get_entity(num)` для числовых ID,
    потому что Telethon читает entity из кэша своей сессии. У пользователя
    сессия создана другой версией Telethon → при чтении кэша падает
    `Could not find a matching Constructor ID`. Поэтому берём entity только
    из свежих диалогов (iter_dialogs делает HTTP-запрос и возвращает живые
    объекты, не задевая битый кэш сессии).
    """
    if _dialogs_loaded.is_set() and not force:
        return
    async with _dialog_lock:
        try:
            logger.info("Загружаю диалоги для кэша entity…")
            async for d in client.iter_dialogs(limit=None):
                e = d.entity
                if hasattr(e, "id") and e.id is not None:
                    _entity_cache[int(e.id)] = e
                if getattr(e, "username", None):
                    _known_usernames.add(e.username.lower())
            logger.info("Кэш entity: %d каналов/чатов", len(_entity_cache))
        except Exception as e:  # noqa: BLE001
            logger.warning("Не удалось загрузить диалоги: %s", e)
    _dialogs_loaded.set()


async def _by_numeric_id(client, num: int):
    """Ищет канал по числовому ID только в нашем кэше свежих диалогов."""
    ent = _entity_cache.get(num)
    if ent is not None:
        return ent
    # кэш мог быть построен до того, как донор подписался — обновим диалоги
    await _ensure_dialogs(client, force=True)
    return _entity_cache.get(num)


def _username_of(ref: str) -> str | None:
    r = ref.strip()
    if r.startswith("@"):
        return r[1:].lower()
    if r.lower().startswith("http"):
        # берем из последней части пути t.me/xxx
        last = r.rstrip("/").split("/")[-1]
        if last and "+" not in last:
            return last.lower()
    return None


async def _by_username(client, username: str):
    """Ищет канал по @username: сначала у нас, потом сетевым резолвом."""
    low = username.lower()
    for eid, ent in _entity_cache.items():
        if getattr(ent, "username", None) and ent.username.lower() == low:
            return ent
    try:
        ent = await client.get_entity(f"@{username}")
        if ent is not None:
            if hasattr(ent, "id") and ent.id is not None:
                _entity_cache[int(ent.id)] = ent
            _known_usernames.add(low)
            return ent
    except Exception as e:  # noqa: BLE001
        logger.warning("Сетевой резолв @%s не удался: %s", username, e)
    return None


async def _resolve_entity(client, identifier: str):
    """Надёжный резолв entity без обращения к битому кэшу сессии."""
    ref = normalize_ref(identifier)
    await _ensure_dialogs(client)

    num = _as_int(ref) if (not ref.startswith("@") and not ref.startswith("http")) else None
    if num is not None:
        ent = await _by_numeric_id(client, num)
        if ent is None:
            logger.warning("Числовой ID %s не найден в диалогах. "
                           "Аккаунт должен быть подписан на этот канал.", identifier)
        return ent

    user = _username_of(ref)
    if user:
        return await _by_username(client, user)

    logger.warning("Не могу разобрать ссылку %s", identifier)
    return None


async def preload_dialogs() -> None:
    """Предзагрузка диалогов на старте (для ускорения мониторинга)."""
    client = await get_client()
    if client is None:
        return
    await _ensure_dialogs(client)


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


async def iter_recent_posts(identifier: str, limit: int = 20):
    """Генератор последних постов (история), без фильтра по after_id.

    Используется кнопкой «заполнить память» для разового прогона в прошлое.
    Отдаёт Message-объекты с медиа, чтобы можно было и медиа скопировать.
    """
    client = await get_client()
    if client is None:
        return
    try:
        entity = await _resolve_entity(client, identifier)
        if entity is None:
            return
        async for m in client.iter_messages(entity, limit=limit):
            if m.text:
                yield m
    except Exception as e:  # noqa: BLE001
        logger.warning("Ошибка чтения истории %s: %s", identifier, e)
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


async def download_media(message) -> str | None:
    """Скачивает медиа из сообщения донора в MEDIA_DIR, возвращает путь или None.

    Пропускает файлы без медиа, чаты-папки и стикеры/интерактивные медиа,
    которые нельзя перепостить как файл. Возвращает строку пути.
    """
    if not hasattr(message, "media") or not message.media:
        return None
    client = await get_client()
    if client is None:
        return None
    try:
        media = message.media
        # проходим по обёрткам до реального media и его файлу
        inner = media
        # media like MessageMediaDocument везёт document, а не сам файл
        doc = getattr(media, "document", None)
        photo = getattr(media, "photo", None)
        ttl = getattr(media, "ttl_seconds", None)
        if ttl:
            return None  # исчезающие фото/видео копировать нельзя

        # игнорируем медиа-группы-папки, стикеры, голосовые — оставляем только
        # картинки и видео (их обычно можно перепостить)
        if photo is None and doc is None:
            return None

        os.makedirs(MEDIA_DIR, exist_ok=True)
        path = await client.download_media(message, file=MEDIA_DIR)
        if not path:
            return None
        # download_media возвращает либо путь, либо bytes; нормализуем
        if isinstance(path, bytes):
            ext = ".bin"
            mime = getattr(getattr(doc or photo, "mime_type", None), "split", None)
            if mime:
                mime = mime("/")
                ext = "." + (mime[1] if len(mime) > 1 else "bin")
            fname = f"media_{message.id}{ext}"
            fpath = os.path.join(MEDIA_DIR, fname)
            with open(fpath, "wb") as f:
                f.write(path)
            return fpath
        return os.path.abspath(str(path))
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось скачать медиа поста %s: %s", getattr(message, "id", "?"), e)
        return None


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

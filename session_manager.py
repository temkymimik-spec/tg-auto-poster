"""Управление Telethon-сессией аккаунта.

- Если в SESSIONS_DIR лежит *.session — клиент подключается к нему автоматически.
- Если задан SESSION_STRING (.env) — используется он.
- Файловую сессию можно загрузить через бота: просто сбрось .session в чат.
- Если сессия не авторизована, клиент остаётся подключённым — можно завершить
  вход через бота, указав номер телефона и код из Telegram.
"""
import asyncio
import logging
import os

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    AuthKeyUnregisteredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.help import GetConfigRequest

from config import API_HASH, API_ID, SESSION_STRING, SESSIONS_DIR

logger = logging.getLogger(__name__)

_client: TelegramClient | None = None
_me: dict | None = None
_phone: str | None = None
_lock = asyncio.Lock()

SESSION_FILE = "main.session"


def session_path() -> str:
    return os.path.join(SESSIONS_DIR, SESSION_FILE)


def list_session_files() -> list[str]:
    """Ищет *.session файлы в папке сессий."""
    if not os.path.isdir(SESSIONS_DIR):
        return []
    return sorted(
        f for f in os.listdir(SESSIONS_DIR)
        if f.endswith(".session") and os.path.isfile(os.path.join(SESSIONS_DIR, f))
    )


def _can_run() -> bool:
    return bool(API_ID and API_HASH)


async def init_client(force: bool = False) -> TelegramClient | None:
    """Инициализирует клиент, подключается к сети.

    Возвращает подключённого клиента (даже если он ещё не авторизован),
    чтобы можно было завершить вход по коду прямо в боте.
    """
    global _client, _me, _phone
    if not _can_run():
        logger.warning("API_ID/API_HASH не заданы — аккаунт недоступен")
        return None
    if not force and _client is not None and _client.is_connected():
        return _client

    async with _lock:
        if not force and _client is not None and _client.is_connected():
            return _client

        try:
            if _client is not None:
                await _client.disconnect()

            if SESSION_STRING:
                client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
                session_kind = "string"
            elif os.path.exists(session_path()):
                client = TelegramClient(session_path(), API_ID, API_HASH)
                session_kind = "file: main.session"
            else:
                str_path = os.path.join(SESSIONS_DIR, "session_string.txt")
                if os.path.exists(str_path):
                    with open(str_path) as f:
                        s = f.read().strip()
                    client = TelegramClient(StringSession(s), API_ID, API_HASH)
                    session_kind = "string file"
                elif list_session_files():
                    path = os.path.join(SESSIONS_DIR, list_session_files()[0])
                    client = TelegramClient(path, API_ID, API_HASH)
                    session_kind = f"file: {list_session_files()[0]}"
                else:
                    return None

            await client.connect()
            _client = client

            if await client.is_user_authorized():
                me = await client.get_me()
                _me = {
                    "id": getattr(me, "id", None),
                    "first_name": getattr(me, "first_name", ""),
                    "username": getattr(me, "username", ""),
                }
                _phone = getattr(me, "phone", None)
                logger.info("Аккаунт подключён: %s (@%s) %s",
                            _me["first_name"], _me["username"], _phone or "")
            else:
                _me = None
                _phone = None
                logger.info("Сессия (%s) подключена, но не авторизована — можно войти кодом",
                            session_kind)

            return client
        except (ApiIdInvalidError, AuthKeyUnregisteredError) as e:
            logger.error("Недействительная сессия/API: %s", e)
            if _client is not None:
                try:
                    await _client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            _client = None
            _me = None
            _phone = None
            return None
        except Exception as e:  # noqa: BLE001
            logger.error("Не удалось подключить аккаунт: %s", e)
            return None


async def get_client(force: bool = False) -> TelegramClient | None:
    """Возвращает подключённого авторизованного клиента или None."""
    client = await init_client(force=force)
    if client is None or not client.is_connected() or not await client.is_user_authorized():
        return None
    return client


async def me() -> dict | None:
    return _me


def phone() -> str | None:
    return _phone


def is_connected() -> bool:
    return _client is not None and _client.is_connected() and _me is not None


def raw_client() -> TelegramClient | None:
    """Текущий клиент (в т.ч. в процессе логина)."""
    return _client


async def save_uploaded_session(data: bytes, filename: str = "main.session") -> bool:
    """Сохраняет загруженный .session файл и переподключает клиент.

    Возвращает True если аккаунт авторизовался. Если сессия не авторизована,
    клиент остаётся подключённым — заверши вход кодом/паролем через бота.
    """
    global _me, _phone
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    base = os.path.basename(filename or SESSION_FILE)
    if not base.endswith(".session"):
        base = SESSION_FILE
    path = os.path.join(SESSIONS_DIR, base)
    with open(path, "wb") as f:
        f.write(data)
    logger.info("Сохранена сессия: %s (%d байт)", base, len(data))
    await init_client(force=True)
    return is_connected()


async def generate_string_session_export() -> str | None:
    """Экспортирует текущую сессию как string (для переноса в .env)."""
    client = await init_client()
    if client is None or not client.is_connected() or not await client.is_user_authorized():
        return None
    return StringSession.save(client.session)


# ------------------------------------------------------------------- логин
async def login_start() -> TelegramClient | None:
    """Подготавливает клиент к логину (подключён, но может быть не авторизован)."""
    global _client, _me, _phone
    if not _can_run():
        return None
    if _client is not None and _client.is_connected():
        return _client
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    _client = client
    _me = None
    _phone = None
    return client


async def send_code(client: TelegramClient, phone: str) -> None:
    try:
        await client.send_code_request(phone)
    except PhoneNumberInvalidError as e:
        raise ValueError("Неверный номер телефона") from e


async def login_code(client: TelegramClient, phone: str, code: str) -> dict:
    """Пытается войти по коду. Возвращает {status: 'ok'|'password'}."""
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        return {"status": "password"}
    except PhoneCodeInvalidError as e:
        raise ValueError("Код неверный, попробуй ещё раз") from e
    await _finalize_login(client, phone)
    return {"status": "ok"}


async def login_password(client: TelegramClient, password: str) -> dict:
    try:
        await client.sign_in(password=password)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Пароль не принят: {e}") from e
    await _finalize_login(client)
    return {"status": "ok", "password": True}


async def _finalize_login(client: TelegramClient, phone: str | None = None) -> None:
    global _client, _me, _phone
    me_ = await client.get_me()
    _me = {
        "id": getattr(me_, "id", None),
        "first_name": getattr(me_, "first_name", ""),
        "username": getattr(me_, "username", ""),
    }
    _phone = getattr(me_, "phone", None) or phone
    _client = client
    # сохранить как session string, чтобы переживала рестарт
    try:
        session_str = StringSession.save(client.session)
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        with open(os.path.join(SESSIONS_DIR, "session_string.txt"), "w") as f:
            f.write(session_str)
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось сохранить session string: %s", e)
    logger.info("Логин выполнен: %s (@%s) %s", _me["first_name"], _me["username"], _phone or "")


async def disconnect() -> None:
    global _client, _me, _phone
    if _client is not None and _client.is_connected():
        try:
            await _client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    _client = None
    _me = None
    _phone = None


async def ping_network() -> bool:
    """Проверка сетевой доступности Telegram."""
    try:
        client = await init_client()
        if client is None:
            return False
        await client(GetConfigRequest())
        return True
    except Exception:  # noqa: BLE001
        return False
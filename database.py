"""Единый доступ к SQLite: один постоянный коннект (WAL) + все таблицы.

Таблицы и память мониторинга объединены сюда же — так нет дублирующих модулей.
"""
import asyncio
import logging
import time

import aiosqlite

from config import DB_PATH, ENV_KEYS_BY_PROVIDER, PROVIDERS

logger = logging.getLogger(__name__)

_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT UNIQUE NOT NULL,
    channel_title TEXT DEFAULT '',
    channel_username TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    style_prompt TEXT DEFAULT '',
    custom_instruction TEXT DEFAULT '',
    post_interval_min INTEGER DEFAULT 60,
    last_post_time REAL DEFAULT 0,
    created_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    source_channel_id TEXT NOT NULL,
    source_channel_title TEXT DEFAULT '',
    UNIQUE(channel_id, source_channel_id)
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    post_text TEXT DEFAULT '',
    post_media_path TEXT DEFAULT '',
    source_channel_id TEXT DEFAULT '',
    source_post_id INTEGER DEFAULT 0,
    status TEXT DEFAULT 'draft',
    scheduled_time REAL DEFAULT 0,
    published_time REAL DEFAULT 0,
    ai_provider TEXT DEFAULT '',
    ai_model TEXT DEFAULT '',
    created_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    ad_text TEXT DEFAULT '',
    ad_media_path TEXT DEFAULT '',
    scheduled_time REAL DEFAULT 0,
    status TEXT DEFAULT 'draft',
    created_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ai_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    api_key TEXT NOT NULL,
    model TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    fail_count INTEGER DEFAULT 0,
    usage_count INTEGER DEFAULT 0,
    updated_at REAL DEFAULT 0,
    created_at REAL DEFAULT 0,
    UNIQUE(provider, api_key)
);

CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    source_channel_id TEXT DEFAULT '',
    topic TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    keywords TEXT DEFAULT '',
    importance INTEGER DEFAULT 5,
    emotion TEXT DEFAULT 'neutral',
    raw_text TEXT DEFAULT '',
    media_path TEXT DEFAULT '',
    source_post_id INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS monitor_state (
    channel_id TEXT PRIMARY KEY,
    last_post_id INTEGER DEFAULT 0,
    last_check_time REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    posts_count INTEGER DEFAULT 0,
    ads_count INTEGER DEFAULT 0,
    updated_at REAL DEFAULT 0
);
"""

# Агрегирующая статистика по каналам/постам/рекламе кэшируется в settings, чтобы
# не делать тяжёлых COUNT на каждый запрос меню.
STATS_CACHE_KEY = "stats_json_cache"


# ------------------------------------------------------------- коннект к БД
_conn: None
_conn_closed = False


async def get_conn() -> aiosqlite.Connection:
    global _conn, _conn_closed
    if _conn is None or _conn_closed:
        _conn = await aiosqlite.connect(DB_PATH)
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA busy_timeout=5000")
        await _conn.execute("PRAGMA synchronous=NORMAL")
        _conn_closed = False
    return _conn


async def init_db() -> None:
    conn = await get_conn()
    async with _write_lock:
        await conn.executescript(SCHEMA)
        # мягкая миграция: добавляем недостающие колонки в уже существующие таблицы
        await _migrate(conn)
        await conn.commit()
    await _seed_ai_keys()


async def _migrate(conn) -> None:
    """Добавляет новые колонки, которых ещё нет в старых БД."""
    try:
        cur = await conn.execute("PRAGMA table_info(memory)")
        rows = await cur.fetchall()
        cols = {row[0] for row in rows}
        if "media_path" in cols:
            return
    except Exception:
        # не смогли прочитать схему — всё равно пробуем добавить колонку ниже
        pass
    try:
        await conn.execute("ALTER TABLE memory ADD COLUMN media_path TEXT DEFAULT ''")
    except Exception as e:  # noqa: BLE001
        if "duplicate column" not in str(e).lower():
            logger.warning("Миграция memory.media_path не удалась: %s", e)


async def _seed_ai_keys() -> None:
    """Заносит ключи из .env в БД как источник истины для ротации."""
    conn = await get_conn()
    now = time.time()
    async with _write_lock:
        for provider, keys in ENV_KEYS_BY_PROVIDER.items():
            if provider not in PROVIDERS:
                continue
            for key in keys:
                if not key or not key.strip():
                    continue
                await conn.execute(
                    "INSERT OR IGNORE INTO ai_keys"
                    " (provider, api_key, enabled, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                    (provider, key.strip(), now, now),
                )
        await conn.commit()


# ------------------------------------------------------------------- settings
async def get_setting(key: str, default: str = "") -> str:
    conn = await get_conn()
    cur = await conn.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cur.fetchone()
    return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    conn = await get_conn()
    async with _write_lock:
        await conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await conn.commit()


# ------------------------------------------------------------------- channels
async def add_channel(channel_id: str, title: str = "", username: str = "",
                      style: str = "", interval: int = 60) -> bool:
    conn = await get_conn()
    async with _write_lock:
        try:
            await conn.execute(
                "INSERT INTO channels"
                " (channel_id, channel_title, channel_username, style_prompt,"
                "  post_interval_min, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (channel_id, title, username, style, interval, time.time()),
            )
            await conn.commit()
            return True
        except Exception:
            return False


async def get_channels(active_only: bool = True) -> list[dict]:
    conn = await get_conn()
    sql = "SELECT * FROM channels WHERE is_active=1" if active_only else "SELECT * FROM channels"
    cur = await conn.execute(sql + " ORDER BY channel_title")
    return [dict(r) for r in await cur.fetchall()]


async def get_channel(channel_id: str) -> dict | None:
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM channels WHERE channel_id=?", (channel_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_channel(channel_id: str, **kwargs) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k}=?" for k in kwargs)
    conn = await get_conn()
    async with _write_lock:
        await conn.execute(f"UPDATE channels SET {cols} WHERE channel_id=?", (*kwargs.values(), channel_id))
        await conn.commit()


async def remove_channel(channel_id: str) -> None:
    conn = await get_conn()
    async with _write_lock:
        await conn.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
        await conn.execute("DELETE FROM source_channels WHERE channel_id=?", (channel_id,))
        await conn.execute("DELETE FROM monitor_state WHERE channel_id=?", (channel_id,))
        await conn.commit()


# -------------------------------------------------------------------- sources
async def add_source_channel(channel_id: str, source_id: str, source_title: str = "") -> bool:
    conn = await get_conn()
    async with _write_lock:
        try:
            await conn.execute(
                "INSERT OR IGNORE INTO source_channels"
                " (channel_id, source_channel_id, source_channel_title) VALUES (?, ?, ?)",
                (channel_id, source_id, source_title),
            )
            await conn.commit()
            return True
        except Exception:
            return False


async def get_source_channels(channel_id: str | None = None) -> list[dict]:
    conn = await get_conn()
    sql = "SELECT * FROM source_channels"
    args = ()
    if channel_id:
        sql += " WHERE channel_id=?"
        args = (channel_id,)
    cur = await conn.execute(sql, args)
    return [dict(r) for r in await cur.fetchall()]


async def remove_source_channel(channel_id: str, source_id: str) -> None:
    conn = await get_conn()
    async with _write_lock:
        await conn.execute(
            "DELETE FROM source_channels WHERE channel_id=? AND source_channel_id=?",
            (channel_id, source_id),
        )
        await conn.commit()


# ---------------------------------------------------------------------- posts
async def save_post(channel_id: str, text: str, media: str = "", source_id: str = "",
                    source_post_id: int = 0, scheduled: float = 0,
                    ai_provider: str = "", ai_model: str = "") -> int:
    conn = await get_conn()
    async with _write_lock:
        cur = await conn.execute(
            "INSERT INTO posts (channel_id, post_text, post_media_path, source_channel_id,"
            " source_post_id, status, scheduled_time, ai_provider, ai_model, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (channel_id, text, media, source_id, source_post_id,
             "pending" if scheduled else "draft", scheduled, ai_provider, ai_model, time.time()),
        )
        await conn.commit()
        return cur.lastrowid


async def get_draft_posts(channel_id: str) -> list[dict]:
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT * FROM posts WHERE channel_id=? AND status='draft' ORDER BY created_at DESC LIMIT 15",
        (channel_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_pending_due(now: float) -> list[dict]:
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT * FROM posts WHERE status='pending' AND scheduled_time>0 AND scheduled_time<=?"
        " ORDER BY scheduled_time",
        (now,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_post(post_id: int) -> dict | None:
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM posts WHERE id=?", (post_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_post_status(post_id: int, status: str, published_time: float | None = None) -> None:
    conn = await get_conn()
    async with _write_lock:
        if published_time is None:
            published_time = time.time()
        await conn.execute(
            "UPDATE posts SET status=?, published_time=? WHERE id=?",
            (status, published_time, post_id),
        )
        await conn.commit()


async def delete_post(post_id: int) -> None:
    conn = await get_conn()
    async with _write_lock:
        await conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
        await conn.commit()


# ----------------------------------------------------------------------- ads
async def add_ad(channel_id: str, text: str, media: str = "", scheduled: float = 0) -> int:
    conn = await get_conn()
    async with _write_lock:
        cur = await conn.execute(
            "INSERT INTO ads (channel_id, ad_text, ad_media_path, scheduled_time, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, text, media, scheduled, "pending" if scheduled else "draft", time.time()),
        )
        await conn.commit()
        return cur.lastrowid


async def get_ads(channel_id: str | None = None) -> list[dict]:
    conn = await get_conn()
    sql = "SELECT * FROM ads"
    args: tuple = ()
    if channel_id:
        sql += " WHERE channel_id=?"
        args = (channel_id,)
    cur = await conn.execute(sql + " ORDER BY created_at DESC LIMIT 15", args)
    return [dict(r) for r in await cur.fetchall()]


async def get_pending_ads_due(now: float) -> list[dict]:
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT * FROM ads WHERE status='pending' AND scheduled_time>0 AND scheduled_time<=?"
        " ORDER BY scheduled_time",
        (now,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_ad(ad_id: int) -> dict | None:
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM ads WHERE id=?", (ad_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_ad_status(ad_id: int, status: str) -> None:
    conn = await get_conn()
    async with _write_lock:
        await conn.execute("UPDATE ads SET status=? WHERE id=?", (status, ad_id))
        await conn.commit()


# ---------------------------------------------------------------- AI ключи
async def list_ai_keys(provider: str | None = None) -> list[dict]:
    conn = await get_conn()
    sql = "SELECT * FROM ai_keys"
    args: tuple = ()
    if provider:
        sql += " WHERE provider=?"
        args = (provider,)
    cur = await conn.execute(sql + " ORDER BY provider, id", args)
    return [dict(r) for r in await cur.fetchall()]


async def add_ai_key(provider: str, api_key: str, model: str = "") -> bool:
    conn = await get_conn()
    async with _write_lock:
        try:
            await conn.execute(
                "INSERT OR IGNORE INTO ai_keys"
                " (provider, api_key, model, enabled, created_at, updated_at)"
                " VALUES (?, ?, ?, 1, ?, ?)",
                (provider, api_key.strip(), model, time.time(), time.time()),
            )
            await conn.commit()
            return True
        except Exception:
            return False


async def remove_ai_key(key_id: int) -> bool:
    conn = await get_conn()
    async with _write_lock:
        cur = await conn.execute("DELETE FROM ai_keys WHERE id=?", (key_id,))
        await conn.commit()
        return cur.rowcount > 0


async def set_ai_key_enabled(key_id: int, enabled: bool) -> bool:
    conn = await get_conn()
    async with _write_lock:
        cur = await conn.execute(
            "UPDATE ai_keys SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, time.time(), key_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def set_ai_key_model(key_id: int, model: str) -> bool:
    conn = await get_conn()
    async with _write_lock:
        cur = await conn.execute("UPDATE ai_keys SET model=?, updated_at=? WHERE id=?", (model, time.time(), key_id))
        await conn.commit()
        return cur.rowcount > 0


async def ai_key_success(provider: str, api_key: str) -> None:
    conn = await get_conn()
    async with _write_lock:
        await conn.execute(
            "UPDATE ai_keys SET fail_count=0, usage_count=usage_count+1, enabled=1, updated_at=? "
            "WHERE provider=? AND api_key=?",
            (time.time(), provider, api_key),
        )
        await conn.commit()


async def ai_key_failed(provider: str, api_key: str, max_fails: int) -> None:
    conn = await get_conn()
    async with _write_lock:
        await conn.execute(
            "UPDATE ai_keys SET fail_count=fail_count+1, updated_at=? WHERE provider=? AND api_key=?",
            (time.time(), provider, api_key),
        )
        # последовательные ошибки >= лимита — выключаем ключ автоматически
        await conn.execute(
            "UPDATE ai_keys SET enabled=0, updated_at=? WHERE provider=? AND api_key=? AND fail_count>=?",
            (time.time(), provider, api_key, max_fails),
        )
        await conn.commit()


# -------------------------------------------------------------------- память
async def save_to_memory(channel_id: str, source_channel_id: str, topic: str, summary: str,
                         keywords: str, importance: int, emotion: str,
                         raw_text: str, source_post_id: int = 0, media_path: str = "") -> int:
    conn = await get_conn()
    async with _write_lock:
        try:
            cur = await conn.execute(
                "INSERT INTO memory (channel_id, source_channel_id, topic, summary, keywords,"
                " importance, emotion, raw_text, media_path, source_post_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (channel_id, source_channel_id, topic, summary, keywords,
                 importance, emotion, raw_text, media_path, source_post_id, time.time()),
            )
        except Exception as e:  # noqa: BLE001
            # Старая БД без колонки media_path — мигрируем и повторяем вставку.
            if "no column named media_path" in str(e):
                await _migrate(conn)
                await conn.commit()
                cur = await conn.execute(
                    "INSERT INTO memory (channel_id, source_channel_id, topic, summary, keywords,"
                    " importance, emotion, raw_text, media_path, source_post_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (channel_id, source_channel_id, topic, summary, keywords,
                     importance, emotion, raw_text, media_path, source_post_id, time.time()),
                )
            else:
                raise
        await conn.commit()
        return cur.lastrowid


async def get_memory(channel_id: str, limit: int = 20) -> list[dict]:
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT * FROM memory WHERE channel_id=? ORDER BY importance DESC, created_at DESC LIMIT ?",
        (channel_id, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_recent_memory(channel_id: str, hours: int = 48, min_importance: int = 5, limit: int = 10) -> list[dict]:
    since = time.time() - hours * 3600
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT * FROM memory WHERE channel_id=? AND created_at>? AND importance>=?"
        " ORDER BY importance DESC, created_at DESC LIMIT ?",
        (channel_id, since, min_importance, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def search_memory(channel_id: str, query: str) -> list[dict]:
    conn = await get_conn()
    like = f"%{query}%"
    cur = await conn.execute(
        "SELECT * FROM memory WHERE channel_id=? AND (topic LIKE ? OR summary LIKE ? OR keywords LIKE ?)"
        " ORDER BY importance DESC, created_at DESC LIMIT 10",
        (channel_id, like, like, like),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_memory_count(channel_id: str) -> int:
    conn = await get_conn()
    cur = await conn.execute("SELECT COUNT(*) FROM memory WHERE channel_id=?", (channel_id,))
    row = await cur.fetchone()
    return row[0] if row else 0


async def clear_memory(channel_id: str, days: int = 30) -> None:
    cutoff = time.time() - days * 86400
    conn = await get_conn()
    async with _write_lock:
        await conn.execute("DELETE FROM memory WHERE channel_id=? AND created_at<? AND importance<7", (channel_id, cutoff))
        await conn.commit()


async def delete_memory_entry(entry_id: int) -> None:
    conn = await get_conn()
    async with _write_lock:
        await conn.execute("DELETE FROM memory WHERE id=?", (entry_id,))
        await conn.commit()


async def update_monitor_state(channel_id: str, last_post_id: int) -> None:
    conn = await get_conn()
    async with _write_lock:
        await conn.execute(
            "INSERT OR REPLACE INTO monitor_state (channel_id, last_post_id, last_check_time)"
            " VALUES (?, ?, ?)",
            (channel_id, last_post_id, time.time()),
        )
        await conn.commit()


async def get_monitor_state(channel_id: str) -> dict:
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM monitor_state WHERE channel_id=?", (channel_id,))
    row = await cur.fetchone()
    if row:
        return dict(row)
    return {"channel_id": channel_id, "last_post_id": 0, "last_check_time": 0}


# -------------------------------------------------------------------- статы
async def get_stats() -> dict:
    """Куда более лёгкий вариант: считаем только строки таблиц один раз."""
    stats = {"channels": 0, "posts_total": 0, "posts_published": 0, "posts_pending": 0,
             "posts_draft": 0, "ads_total": 0, "ads_published": 0, "ads_pending": 0,
             "memory_total": 0, "ai_keys": 0, "ai_keys_enabled": 0}
    conn = await get_conn()
    queries = {
        "channels": "SELECT COUNT(*) FROM channels",
        "posts_total": "SELECT COUNT(*) FROM posts",
        "posts_published": "SELECT COUNT(*) FROM posts WHERE status='published'",
        "posts_pending": "SELECT COUNT(*) FROM posts WHERE status='pending'",
        "posts_draft": "SELECT COUNT(*) FROM posts WHERE status='draft'",
        "ads_total": "SELECT COUNT(*) FROM ads",
        "ads_published": "SELECT COUNT(*) FROM ads WHERE status='published'",
        "ads_pending": "SELECT COUNT(*) FROM ads WHERE status='pending'",
        "memory_total": "SELECT COUNT(*) FROM memory",
        "ai_keys": "SELECT COUNT(*) FROM ai_keys",
        "ai_keys_enabled": "SELECT COUNT(*) FROM ai_keys WHERE enabled=1",
    }
    for k, sql in queries.items():
        cur = await conn.execute(sql)
        row = await cur.fetchone()
        stats[k] = row[0] if row else 0
    return stats


async def close_db() -> None:
    global _conn, _conn_closed
    if _conn is not None:
        try:
            await _conn.close()
        except Exception:  # noqa: BLE001
            pass
    _conn = None
    _conn_closed = True
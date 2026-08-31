"""Мониторинг каналов-конкурентов: читает новые посты и складывает в память.

Плюс разовый «бэкфилл» — наполнение памяти историей постов донора
(кнопка в боте), чтобы не ждать, пока появятся новые посты.
"""
import asyncio
import logging

import channel_parser as cp
import database as db
from ai.analyzer import analyze_post
from config import FETCH_POST_DELAY, IMPORTANCE_MIN, MONITOR_INTERVAL_SEC, MONITOR_LOOKBACK
from session_manager import get_client

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_run_flag = asyncio.Event()


def is_running() -> bool:
    return _task is not None and not _task.done()


async def start() -> None:
    global _task
    _run_flag.set()
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    _run_flag.clear()
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None


async def _loop() -> None:
    while _run_flag.is_set():
        try:
            await cycle()
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка цикла мониторинга: %s", e)
        try:
            await asyncio.wait_for(_run_flag.wait(), timeout=MONITOR_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


async def cycle() -> None:
    if await get_client() is None:
        return  # аккаунт не подключён — мониторить нечем
    channels = await db.get_channels(active_only=True)
    for ch in channels:
        sources = await db.get_source_channels(ch["channel_id"])
        if not sources:
            continue
        state = await db.get_monitor_state(ch["channel_id"])
        await analyze_channel(ch, sources, state)


def _fresh_stats() -> dict:
    return {"new_posts": 0, "saved": 0, "analyzed_fail": 0, "skipped": 0}


async def _process_post(stats: dict, m, ch: dict, source_ref: str) -> None:
    """Анализирует один пост донора. Если он важен и не реклама — сохраняет в память."""
    text = (m.text or "").strip()
    if not text:
        return  # чистый медиа-пост без текста — переписывать нечего

    analysis = await analyze_post(text, "")
    if analysis.get("is_ad"):
        stats["skipped"] += 1
        return

    if analysis.get("importance", 1) >= IMPORTANCE_MIN:
        media_path = ""
        if getattr(m, "media", None):
            media_path = await cp.download_media(m) or ""
        await db.save_to_memory(
            channel_id=ch["channel_id"],
            source_channel_id=source_ref,
            topic=analysis.get("topic", ""),
            summary=analysis.get("summary", ""),
            keywords=analysis.get("keywords", ""),
            importance=analysis.get("importance", 5),
            emotion=analysis.get("emotion", "neutral"),
            raw_text=text[:3000],
            source_post_id=m.id,
            media_path=media_path,
        )
        stats["saved"] += 1
    elif analysis.get("importance", 1) == 0:
        stats["analyzed_fail"] += 1


async def analyze_channel(ch: dict, sources: list[dict], state: dict,
                          lookback: int | None = None) -> dict:
    """Анализирует новые посты по источникам канала. Возвращает статистику прогона."""
    lookback = lookback or MONITOR_LOOKBACK
    client = await get_client()
    if client is None:
        return {"error": "аккаунт не подключён"}

    stats = _fresh_stats()
    last_id = state.get("last_post_id", 0)
    new_last = last_id

    for source in sources:
        source_ref = source["source_channel_id"]
        try:
            async for m in cp.iter_new_posts(source_ref, after_id=last_id, limit=lookback):
                if m.id <= last_id:
                    continue
                stats["new_posts"] += 1
                new_last = max(new_last, m.id)
                try:
                    await _process_post(stats, m, ch, source_ref)
                finally:
                    await asyncio.sleep(FETCH_POST_DELAY)
        except Exception as e:  # noqa: BLE001
            logger.warning("Ошибка мониторинга источника %s: %s", source_ref, e)

    if new_last > last_id:
        await db.update_monitor_state(ch["channel_id"], new_last)

    logger.info("Мониторинг %s: %s", ch.get("channel_title", ch["channel_id"]), stats)
    return stats


async def backfill_memory(ch: dict, sources: list[dict], limit: int = 20) -> dict:
    """Разовый прогон истории постов донора в память.

    Читает ПОСЛЕДНИЕ `limit` постов каждого источника (в прошлое) и анализирует.
    НЕ трогает last_post_id — чтобы свежие новые посты потом тоже ловились.
    Возвращает статистику: {posts_checked, saved, analyzed_fail, skipped, errors}.
    """
    client = await get_client()
    if client is None:
        return {"error": "аккаунт не подключён"}

    stats = _fresh_stats()
    stats["posts_checked"] = 0

    for source in sources:
        source_ref = source["source_channel_id"]
        try:
            async for m in cp.iter_recent_posts(source_ref, limit=limit):
                stats["posts_checked"] += 1
                try:
                    await _process_post(stats, m, ch, source_ref)
                finally:
                    await asyncio.sleep(FETCH_POST_DELAY)
        except Exception as e:  # noqa: BLE001
            logger.warning("Ошибка бэкфилла источника %s: %s", source_ref, e)
            stats["skipped"] += 1

    logger.info("Бэкфилл %s: %s", ch.get("channel_title", ch["channel_id"]), stats)
    return stats

"""Мониторинг каналов-конкурентов: читает новые посты и складывает в память."""
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


async def analyze_channel(ch: dict, sources: list[dict], state: dict,
                          lookback: int | None = None) -> dict:
    """Анализирует новые посты по источникам канала. Возвращает статистику прогона."""
    lookback = lookback or MONITOR_LOOKBACK
    client = await get_client()
    if client is None:
        return {"error": "аккаунт не подключён"}

    stats = {"new_posts": 0, "saved": 0, "analyzed_fail": 0}
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

                analysis = await analyze_post(m.text, source.get("source_channel_title", ""))
                if analysis.get("importance", 1) >= IMPORTANCE_MIN:
                    await db.save_to_memory(
                        channel_id=ch["channel_id"],
                        source_channel_id=source_ref,
                        topic=analysis.get("topic", ""),
                        summary=analysis.get("summary", ""),
                        keywords=analysis.get("keywords", ""),
                        importance=analysis.get("importance", 5),
                        emotion=analysis.get("emotion", "neutral"),
                        raw_text=m.text[:3000],
                        source_post_id=m.id,
                    )
                    stats["saved"] += 1
                elif analysis.get("importance", 1) == 0:
                    stats["analyzed_fail"] += 1

                await asyncio.sleep(FETCH_POST_DELAY)
        except Exception as e:  # noqa: BLE001
            logger.warning("Ошибка мониторинга источника %s: %s", source_ref, e)

    if new_last > last_id:
        await db.update_monitor_state(ch["channel_id"], new_last)

    logger.info("Мониторинг %s: %s", ch.get("channel_title", ch["channel_id"]), stats)
    return stats
"""Наполнение памяти каналов контентом: AI сам генерит посты по описанию канала + дата.

Без интернета — модель использует свои знания, привязываясь к сегодняшней дате.
"""
import asyncio
import logging

import database as db
from ai.analyzer import generate_content_items
from config import MONITOR_INTERVAL_SEC

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
            logger.exception("Ошибка цикла наполнения памяти: %s", e)
        try:
            await asyncio.wait_for(_run_flag.wait(), timeout=MONITOR_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


async def cycle() -> None:
    """Периодически добирает контент в память для каналов с описанием."""
    channels = await db.get_channels(active_only=True)
    for ch in channels:
        desc = (ch.get("channel_description") or "").strip()
        if not desc:
            continue
        state = await db.get_web_collect_state(ch["channel_id"])
        interval = max(int(ch.get("post_interval_min") or 60), 1) * 60
        now = asyncio.get_event_loop().time()
        if now - float(state.get("last_collect_time") or 0) < interval:
            continue
        try:
            await collect_for_channel(ch)
            await db.update_web_collect_state(ch["channel_id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("Наполнение канала %s: %s", ch["channel_id"], e)


async def collect_for_channel(ch: dict) -> dict:
    """Генерирует готовые посты для канала и сохраняет в память."""
    desc = (ch.get("channel_description") or "").strip()
    stats = {"generated": 0, "saved": 0, "errors": 0}
    if not desc:
        return stats

    items = await generate_content_items(desc, ch.get("style_prompt", ""))
    stats["generated"] = len(items)

    for it in items:
        try:
            await db.save_to_memory(
                channel_id=ch["channel_id"],
                source_url="",
                topic=it["topic"] or "Идея",
                summary=it["content"][:500],
                keywords="",
                importance=7,
                emotion="neutral",
                raw_text=it["content"],
                media_path="",
                media_url="",
            )
            stats["saved"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Ошибка сохранения сгенерированного поста: %s", e)
            stats["errors"] += 1

    logger.info("Наполнение %s: %s", ch.get("channel_title", ch["channel_id"]), stats)
    return stats


async def manual_collect(ch: dict) -> dict:
    """Ручной запуск генерации контента для канала."""
    return await collect_for_channel(ch)

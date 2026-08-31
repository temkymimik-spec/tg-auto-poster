"""Фоновый web-монитор: периодически пополняет память каналов контентом из интернета."""
import asyncio
import logging

import database as db
from config import WEB_COLLECT_INTERVAL_MIN

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
            logger.exception("Ошибка web-монитора: %s", e)
        try:
            await asyncio.wait_for(_run_flag.wait(), timeout=WEB_COLLECT_INTERVAL_MIN * 60)
        except asyncio.TimeoutError:
            pass


async def cycle() -> None:
    """Для каждого активного канала с описанием — сбор контента из интернета."""
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
            from monitor import collect_for_channel
            await collect_for_channel(ch)
            await db.update_web_collect_state(ch["channel_id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("Web-монитор канала %s: %s", ch["channel_id"], e)

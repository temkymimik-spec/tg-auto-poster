"""Мониторинг: собирает контент из интернета по темам каналов и сохраняет в память.

Работает через AI-генерацию поисковых запросов → DuckDuckGo → извлечение
контента → анализ → сохранение в memory.
"""
import asyncio
import logging

import database as db
import web_fetch as wf
from ai.analyzer import analyze_post, make_search_queries
from config import IMPORTANCE_MIN, MONITOR_INTERVAL_SEC, WEB_MAX_ITEMS, WEB_MAX_QUERIES

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
    """Периодический цикл: для каждого активного канала с описанием — сбор из интернета."""
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
            logger.warning("Мониторинг канала %s: %s", ch["channel_id"], e)


async def collect_for_channel(ch: dict) -> dict:
    """Собирает контент для канала из интернета по его описанию."""
    desc = (ch.get("channel_description") or "").strip()
    stats = {"queries": 0, "saved": 0, "errors": 0}
    if not desc:
        return stats

    queries = await make_search_queries(desc)
    stats["queries"] = len(queries)

    for q in queries[:WEB_MAX_QUERIES]:
        try:
            results = await wf.search_ddg(q, max_results=WEB_MAX_ITEMS)
            for r in results:
                try:
                    await _save_result(ch, r)
                    stats["saved"] += 1
                    await asyncio.sleep(1.5)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Ошибка сохранения результата: %s", e)
                    stats["errors"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Ошибка поиска '%s': %s", q, e)

    logger.info("Мониторинг %s: %s", ch.get("channel_title", ch["channel_id"]), stats)
    return stats


async def _save_result(ch: dict, result: dict) -> None:
    """Извлекает контент со страницы и сохраняет в память."""
    url = result.get("url", "")
    title = result.get("title", "")
    snippet = result.get("snippet", "")

    text = f"{title}\n{snippet}".strip()
    if not text or len(text) < 20:
        return

    media_path = ""
    media_url = ""
    page = await wf.extract_article(url)
    if page.get("image_url"):
        media_url = page["image_url"]
        media_path = await wf.download_image(page["image_url"])

    analysis = await analyze_post(text[:3000], ch.get("channel_description", ""))

    await db.save_to_memory(
        channel_id=ch["channel_id"],
        source_url=url,
        topic=analysis.get("topic", "") or title[:120],
        summary=analysis.get("summary", "") or text[:500],
        keywords=analysis.get("keywords", ""),
        importance=analysis.get("importance", 5),
        emotion=analysis.get("emotion", "neutral"),
        raw_text=text[:3000],
        media_path=media_path,
        media_url=media_url,
    )


async def manual_collect(ch: dict) -> dict:
    """Ручной запуск сбора контента для канала."""
    return await collect_for_channel(ch)

"""Автопостинг: публикация отложенных постов/рекламы и генерация по расписанию."""
import asyncio
import logging
import time

import channel_parser as cp
import database as db
from ai.analyzer import generate_from_text
from config import AUTOPOST_LOOP_SEC, COPY_DELAY, POST_INTERVAL_DEFAULT
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
            logger.exception("Ошибка цикла автопостинга: %s", e)
        try:
            await asyncio.wait_for(_run_flag.wait(), timeout=AUTOPOST_LOOP_SEC)
        except asyncio.TimeoutError:
            pass


async def cycle() -> None:
    now = time.time()
    # 1) отложенные посты и реклама
    for post in await db.get_pending_due(now):
        try:
            if await publish_post(post["id"]):
                logger.info("Отложенный пост #%s опубликован", post["id"])
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка публикации отложенного поста #%s: %s", post["id"], e)

    for ad in await db.get_pending_ads_due(now):
        try:
            await cp.send_post(ad["channel_id"], ad["ad_text"], ad.get("ad_media_path") or None)
            await db.update_ad_status(ad["id"], "published")
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка публикации рекламы #%s: %s", ad["id"], e)

    # 2) авто-генерация по интервалу
    if await get_client() is None:
        return
    for ch in await db.get_channels(active_only=True):
        interval = max(int(ch.get("post_interval_min") or POST_INTERVAL_DEFAULT), 1) * 60
        if now - float(ch.get("last_post_time") or 0) < interval:
            continue
        await auto_post_for(ch)
        await db.update_channel(ch["channel_id"], last_post_time=time.time())


async def auto_post_for(ch: dict) -> bool:
    """Пытается опубликовать в канал: черновик → пост из памяти → из источника."""
    ch_id = ch["channel_id"]

    # 1) если есть готовые черновики — публикуем самый свежий
    drafts = await db.get_draft_posts(ch_id)
    if drafts:
        post = drafts[0]
        ok = await publish_post(post["id"])
        return ok

    # 2) генерируем из памяти
    memory = await db.get_recent_memory(ch_id, hours=48, min_importance=3, limit=10)
    if memory:
        from ai.analyzer import generate_from_memory
        result = await generate_from_memory(memory, ch.get("style_prompt", ""))
        if result.get("text"):
            ok = await _publish(ch_id, result["text"], provider=result["provider"], model=result["model"])
            await asyncio.sleep(COPY_DELAY)
            return ok

    # 3) генерируем из последнего поста источника
    sources = await db.get_source_channels(ch_id)
    for source in sources:
        posts = await cp.fetch_recent_posts(source["source_channel_id"], limit=1)
        if not posts:
            continue
        result = await generate_from_text(posts[0]["text"], ch.get("style_prompt", ""),
                                          ch.get("custom_instruction", ""))
        if result.get("text"):
            ok = await _publish(ch_id, result["text"],
                                source_id=source["source_channel_id"],
                                source_post_id=posts[0]["id"],
                                provider=result["provider"], model=result["model"])
            await asyncio.sleep(COPY_DELAY)
            return ok
        break  # один источник на цикл

    return False


async def publish_post(post_id: int) -> bool:
    post = await db.get_post(post_id)
    if not post:
        return False
    ok = await _publish(post["channel_id"], post["post_text"], post.get("post_media_path"))
    if ok:
        await db.update_post_status(post_id, "published")
    return ok


async def _publish(channel_id: str, text: str, media: str = "",
                   source_id: str = "", source_post_id: int = 0,
                   provider: str = "", model: str = "") -> bool:
    ok = await cp.send_post(channel_id, text, media or None)
    if ok:
        post_id = await db.save_post(channel_id=channel_id, text=text, media=media,
                                     source_id=source_id, source_post_id=source_post_id,
                                     ai_provider=provider, ai_model=model)
        await db.update_post_status(post_id, "published")
    return ok
"""Автопостинг: публикация отложенных постов/рекламы и авто-расписание.

Два режима:
1) Точное расписание: POSTS_PER_DAY раз в день в заданные часы (POST_HOURS)
   публикуется один сгенерированный пост. Канал выбирается по кругу (round-robin)
   среди активных каналов.
2) Интервальный (fallback, когда POSTS_PER_DAY=0): каждый канал публикует
   по своему интервалу post_interval_min.
"""
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import channel_parser as cp
import database as db
from ai.analyzer import generate_from_memory, generate_from_text
from config import (
    AUTOPOST_LOOP_SEC,
    COPY_DELAY,
    POSTS_PER_DAY,
    POST_HOURS,
    SCHEDULE_TZ,
)
from session_manager import get_client

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_run_flag = asyncio.Event()

# ключи в таблице settings
_CURSOR_KEY = "schedule_cursor"
_LAST_KEY = "schedule_last"

try:
    _TZ = ZoneInfo(SCHEDULE_TZ)
except Exception:  # noqa: BLE001
    _TZ = ZoneInfo("Europe/Moscow")


def _now_msk() -> datetime:
    return datetime.now(_TZ)


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


def _today() -> str:
    return _now_msk().strftime("%Y-%m-%d")


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

    if await get_client() is None:
        return

    # 2) авто-расписание по слотам времени
    if POSTS_PER_DAY and POST_HOURS:
        await _run_schedule()
        return

    # 3) интервальный режим (fallback)
    for ch in await db.get_channels(active_only=True):
        interval = max(int(ch.get("post_interval_min") or 60), 1) * 60
        if now - float(ch.get("last_post_time") or 0) < interval:
            continue
        await auto_post_for(ch)
        await db.update_channel(ch["channel_id"], last_post_time=time.time())


async def _run_schedule() -> None:
    """Публикует пост, если наступил час-слот и в этот слот ещё не постили сегодня."""
    channels = [c for c in await db.get_channels(active_only=True)]
    if not channels:
        return

    hour = _now_msk().hour
    slots = sorted(POST_HOURS)
    if hour not in slots:
        return

    # считаем индекс слота: сколько слотов уже прошло сегодня до текущего часа
    slot_index = len([h for h in slots if h <= hour])
    key = f"{_today()}:{slot_index}"
    last = await db.get_setting(_LAST_KEY, "")
    if last == key:
        return  # в этот слот уже публиковали

    # round-robin: следующий канал
    try:
        cursor = int(await db.get_setting(_CURSOR_KEY, "0"))
    except ValueError:
        cursor = 0
    ch = channels[cursor % len(channels)]
    cursor = (cursor + 1) % len(channels)
    await db.set_setting(_CURSOR_KEY, str(cursor))

    logger.info("Авто-расписание: слот %d, публикую в %s", hour, ch.get("channel_title") or ch["channel_id"])
    await auto_post_for(ch)
    await db.set_setting(_LAST_KEY, key)


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
    memory = await db.get_recent_memory(ch_id, hours=48, min_importance=5, limit=8)
    if memory:
        result = await generate_from_memory(memory, ch.get("style_prompt", ""))
        if result.get("text"):
            media = memory[0].get("media_path") or ""
            ok = await _publish(ch_id, result["text"], media=media,
                                provider=result["provider"], model=result["model"])
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
        await db.save_post(channel_id=channel_id, text=text, media=media,
                           source_id=source_id, source_post_id=source_post_id,
                           ai_provider=provider, ai_model=model)
    return ok

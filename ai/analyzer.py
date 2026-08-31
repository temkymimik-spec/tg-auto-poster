"""AI-анализ постов и генерация контента."""
import logging
from datetime import datetime

from ai import providers
from ai.prompts import (
    ANALYZE_SYSTEM,
    CONTENT_SYSTEM,
    MEMORY_WRITER_SYSTEM,
    analyze_prompt,
    content_prompt,
    memory_prompt,
)

logger = logging.getLogger(__name__)


def _season(month: int) -> str:
    if month in (12, 1, 2):
        return "зима"
    if month in (3, 4, 5):
        return "весна"
    if month in (6, 7, 8):
        return "лето"
    return "осень"


async def generate_content_items(channel_desc: str, style_prompt: str = "") -> list[dict]:
    """AI сам генерит готовые посты по описанию канала + текущая дата.

    Возвращает [{topic, content}]. Без интернета, только знания модели.
    """
    desc = (channel_desc or "").strip()
    if not desc:
        return []
    today = datetime.now()
    _m = today.month
    _months = ("январе", "феврале", "марте", "апреле", "мае", "июне",
               "июле", "августе", "сентябре", "октябре", "ноябре", "декабре")
    month_name = _months[_m - 1]
    season = _season(_m)
    system = CONTENT_SYSTEM.format(
        today=today.strftime("%d.%m.%Y"),
        month=month_name,
        season=season,
    )
    user = content_prompt(desc, style_prompt)
    try:
        data = await providers.parse_json_ai(system, user, temperature=0.8, max_tokens=4000)
        items = data.get("items") or []
        if not isinstance(items, list):
            return []
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("content"):
                out.append({
                    "topic": str(it.get("topic", ""))[:150],
                    "content": str(it["content"]).strip(),
                })
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось сгенерировать контент: %s", e)
        return []


def _default_result() -> dict:
    return {"topic": "", "summary": "", "keywords": "",
            "importance": 1, "emotion": "neutral"}


async def analyze_post(post_text: str, channel_context: str = "") -> dict:
    """Анализирует один пост -> {topic, summary, keywords, importance, emotion}."""
    if not post_text or not post_text.strip():
        return _default_result()

    user = analyze_prompt(post_text[:3000], channel_context)
    try:
        data = await providers.parse_json_ai(ANALYZE_SYSTEM, user, temperature=0.2, max_tokens=600)
        try:
            importance = int(data.get("importance", 1))
        except (TypeError, ValueError):
            importance = 1
        importance = max(1, min(10, importance))
        emotion = data.get("emotion", "neutral")
        if emotion not in ("positive", "negative", "neutral", "urgent"):
            emotion = "neutral"
        return {
            "topic": str(data.get("topic", ""))[:120],
            "summary": str(data.get("summary", ""))[:500],
            "keywords": str(data.get("keywords", ""))[:300],
            "importance": importance,
            "emotion": emotion,
        }
    except providers.AIError as e:
        logger.warning("Не удалось проанализировать пост: %s", e)
        return _default_result()
    except Exception as e:  # noqa: BLE001
        logger.exception("Ошибка анализа поста: %s", e)
        return _default_result()


async def generate_from_text(source_text: str, style_prompt: str = "",
                             custom_instruction: str = "") -> dict:
    """Генерирует уникальный пост по тексту из источника."""
    if not source_text or not source_text.strip():
        return {"text": "", "error": "Пустой текст источника"}
    user = copywrite_prompt(source_text[:3000], style_prompt, custom_instruction)
    try:
        text, provider, model = await providers.generate(
            COPYWRITER_SYSTEM, user, temperature=0.8, max_tokens=2000,
        )
        return {"text": text, "provider": provider, "model": model, "error": None}
    except providers.AIError as e:
        return {"text": "", "error": str(e)}


async def generate_from_memory(memory_items: list[dict], style_prompt: str = "",
                               target_topic: str = "") -> dict:
    """Генерирует пост по накопленной в памяти информации."""
    if not memory_items:
        return {"text": "", "error": "Нет данных в памяти мониторинга"}
    user = memory_prompt(memory_items, style_prompt, target_topic)
    try:
        text, provider, model = await providers.generate(
            MEMORY_WRITER_SYSTEM, user, temperature=0.8, max_tokens=2000,
        )
        return {"text": text, "provider": provider, "model": model, "error": None}
    except providers.AIError as e:
        return {"text": "", "error": str(e)}
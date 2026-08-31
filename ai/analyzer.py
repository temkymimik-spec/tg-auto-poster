"""AI-анализ постов и генерация контента."""
import logging

from ai import providers
from ai.prompts import (
    AD_FILTER_SYSTEM,
    ANALYZE_SYSTEM,
    COPYWRITER_SYSTEM,
    MEMORY_WRITER_SYSTEM,
    analyze_prompt,
    copywrite_prompt,
    memory_prompt,
)

logger = logging.getLogger(__name__)


async def is_reklama(post_text: str) -> bool:
    """Быстрый дешёвый фильтр рекламы: AI отвечает одним числом 0 или 1.

    Возвращает True, если пост — явная реклама (пост целиком про продажу).
    """
    text = (post_text or "").strip()[:2000]
    if not text:
        return False
    try:
        raw, _, _ = await providers.generate(
            AD_FILTER_SYSTEM,
            f"Пост:\n{text}\n\nРеклама? Ответь ТОЛЬКО 0 или 1.",
            temperature=0.0,
            max_tokens=5,
        )
        return raw.strip() == "1"
    except Exception:  # noqa: BLE001
        return False


def _default_result() -> dict:
    return {"topic": "", "summary": "", "keywords": "",
            "importance": 1, "emotion": "neutral", "is_ad": 0, "post_type": "content"}


async def analyze_post(post_text: str, channel_context: str = "") -> dict:
    """Анализирует один пост -> {topic, summary, keywords, importance, emotion, is_ad, post_type}."""
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
        post_type = str(data.get("post_type", "content")).strip().lower()
        if post_type not in ("content", "prompt", "news", "instruction", "promo"):
            post_type = "content"
        # реклама — только явно promo-посты. Всё остальное (контент/промпт/новости/
        # инструкции) НИКОГДА не считается рекламой и не пропускается.
        is_ad = 1 if post_type == "promo" else 0
        if is_ad:
            importance = 1
        return {
            "topic": str(data.get("topic", ""))[:120],
            "summary": str(data.get("summary", ""))[:500],
            "keywords": str(data.get("keywords", ""))[:300],
            "importance": importance,
            "emotion": emotion,
            "is_ad": is_ad,
            "post_type": post_type,
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
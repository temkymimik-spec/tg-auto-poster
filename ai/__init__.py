"""Пакет AI: провайдеры с ротацией ключей + генерация/анализ контента."""
from ai.providers import (
    AIError,
    KeyRejected,
    generate,
    parse_json_ai,
    startup as providers_startup,
    shutdown as providers_shutdown,
)
from ai.analyzer import (
    analyze_post,
    generate_from_memory,
    generate_from_text,
)

__all__ = [
    "AIError",
    "KeyRejected",
    "generate",
    "parse_json_ai",
    "providers_startup",
    "providers_shutdown",
    "analyze_post",
    "generate_from_memory",
    "generate_from_text",
]
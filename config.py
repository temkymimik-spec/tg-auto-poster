"""Центральная конфигурация. Читает .env и переменные окружения."""
import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------- пути и данные
DATA_DIR = os.getenv("DATA_DIR", "data")
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "bot_data.db"))
SESSIONS_DIR = os.getenv("SESSIONS_DIR", os.path.join(DATA_DIR, "sessions"))
MEDIA_DIR = os.getenv("MEDIA_DIR", os.path.join(DATA_DIR, "media"))
LOG_FILE = os.getenv("LOG_FILE", "")

for _d in (DATA_DIR, SESSIONS_DIR, MEDIA_DIR):
    os.makedirs(_d, exist_ok=True)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _csv(name: str) -> list[str]:
    raw = os.getenv(name, "")
    if not raw:
        return []
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


def _csv_int(name: str) -> list[int]:
    out = []
    for v in _csv(name):
        try:
            out.append(int(v))
        except ValueError:
            continue
    return out


# ------------------------------------------------------------- Telegram / бот
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = _csv_int("ADMIN_IDS") or ([_int("ADMIN_ID", 0)] if os.getenv("ADMIN_ID") else [])
API_ID = _int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

# ------------------------------------------------------------ AI провайдеры
# Ключи задаются через *_KEYS (через запятую — несколько ключей для ротации),
# либо одним *_API_KEY (обратная совместимость).
def _ai_keys(plural: str, single: str) -> list[str]:
    vals = _csv(plural)
    if not vals and os.getenv(single):
        vals = [os.getenv(single)]
    return vals


def _ai_models(plural: str, defaults: list[str]) -> list[str]:
    vals = _csv(plural)
    return vals or defaults


OPENROUTER_KEYS = _ai_keys("OPENROUTER_KEYS", "OPENROUTER_API_KEY")
GEMINI_KEYS = _ai_keys("GEMINI_KEYS", "GEMINI_API_KEY")
GROQ_KEYS = _ai_keys("GROQ_KEYS", "GROQ_API_KEY")
MISTRAL_KEYS = _ai_keys("MISTRAL_KEYS", "MISTRAL_API_KEY")
CEREBRAS_KEYS = _ai_keys("CEREBRAS_KEYS", "CEREBRAS_API_KEY")

OPENROUTER_MODELS = _ai_models("OPENROUTER_MODELS", [
    "google/gemini-2.5-flash",
    "openai/gpt-4o-mini",
    "meta-llama/llama-4-maverick",
])
GEMINI_MODELS = _ai_models("GEMINI_MODELS", ["gemini-2.5-flash", "gemini-2.0-flash"])
GROQ_MODELS = _ai_models("GROQ_MODELS", ["llama-3.3-70b-versatile"])
MISTRAL_MODELS = _ai_models("MISTRAL_MODELS", ["mistral-small-latest"])
CEREBRAS_MODELS = _ai_models("CEREBRAS_MODELS", ["llama3.3-70b"])

# Спецификации провайдеров (kind = формат API, как у OpenAI или Gemini).
PROVIDERS: dict[str, dict] = {
    "openrouter": {
        "kind": "openai",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models": OPENROUTER_MODELS,
        "help": "openrouter.ai/keys",
    },
    "gemini": {
        "kind": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "models": GEMINI_MODELS,
        "help": "aistudio.google.com/apikey",
    },
    "groq": {
        "kind": "openai",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models": GROQ_MODELS,
        "help": "console.groq.com/keys",
    },
    "mistral": {
        "kind": "openai",
        "url": "https://api.mistral.ai/v1/chat/completions",
        "models": MISTRAL_MODELS,
        "help": "console.mistral.ai/api-keys/",
    },
    "cerebras": {
        "kind": "openai",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "models": CEREBRAS_MODELS,
        "help": "cloud.cerebras.ai/",
    },
}

# Ключи из окружения по провайдеру (для первичного seeding в БД).
ENV_KEYS_BY_PROVIDER = {
    "openrouter": OPENROUTER_KEYS,
    "gemini": GEMINI_KEYS,
    "groq": GROQ_KEYS,
    "mistral": MISTRAL_KEYS,
    "cerebras": CEREBRAS_KEYS,
}

# Лимит последовательных ошибок до авто-отключения ключа.
AI_MAX_FAILS = _int("AI_MAX_FAILS", 5)

# ------------------------------------------------------- мониторинг и постинг
MONITOR_INTERVAL_SEC = _int("MONITOR_INTERVAL_SEC", 60)
MONITOR_LOOKBACK = _int("MONITOR_LOOKBACK", 25)       # сколько последних постов перечитывать за цикл
IMPORTANCE_MIN = _int("IMPORTANCE_MIN", 4)            # порог важности для сохранения в память
POST_INTERVAL_DEFAULT = _int("POST_INTERVAL_DEFAULT", 60)  # минут
AUTOPOST_LOOP_SEC = _int("AUTOPOST_LOOP_SEC", 60)
FETCH_POST_DELAY = _float("FETCH_POST_DELAY", 0.3)    # пауза между постами при анализе
COPY_DELAY = _float("COPY_DELAY", 1.0)

MAX_TEXT_LEN = _int("MAX_TEXT_LEN", 4096)
MAX_CAPTION_LEN = _int("MAX_CAPTION_LEN", 1024)
MAX_INPUT_POST_LEN = _int("MAX_INPUT_POST_LEN", 3000)

AI_TIMEOUT = _int("AI_TIMEOUT", 40)                   # HTTP timeout для AI (запрос)
AI_CONNECT_TIMEOUT = _int("AI_CONNECT_TIMEOUT", 10)   # timeout на соединение (анти-зависание)
KEY_POOL_TTL = _int("KEY_POOL_TTL", 60)               # перечитывать ключи каждые N сек

# ---------------------------------------------------------------- логирование
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
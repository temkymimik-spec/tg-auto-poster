"""Движок ротации AI-ключей.

Суть: все ключи живут в БД (таблица ai_keys) и перечитываются каждые
KEY_POOL_TTL секунд. При генерации проходит по ключам от "самых здоровых"
к проблемным; на любой ошибке ключа/модели автоматически переключается на
следующий ключ/провайдер/модель. Ключ с N последовательными ошибками
отключается сам.
"""
import asyncio
import json
import logging
import random

import aiohttp

import database as db
from config import (
    AI_CONNECT_TIMEOUT,
    AI_MAX_FAILS,
    AI_TIMEOUT,
    KEY_POOL_TTL,
    PROVIDERS,
)

logger = logging.getLogger(__name__)

MAX_MODELS_PER_KEY = 3


class AIError(Exception):
    pass


class KeyRejected(Exception):
    pass


def _strip_fence(raw: str) -> str:
    t = raw.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t.replace("```", "")
        if t.endswith("```"):
            t = t[: -3]
    return t.strip()


class RotationEngine:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._keys: list[dict] = []
        self._refreshed = 0.0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=AI_TIMEOUT, connect=AI_CONNECT_TIMEOUT)
            )
        await self.refresh()

    async def stop(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def refresh(self) -> None:
        """Перечитывает ключи из БД (env-ключи туда уже посеяны при init_db)."""
        self._keys = await db.list_ai_keys()
        self._refreshed = asyncio.get_event_loop().time()

    def _maybe_refresh(self) -> None:
        if self._keys and (asyncio.get_event_loop().time() - self._refreshed) < KEY_POOL_TTL:
            return
        # лёгкий фоновый обход без блокировки основного потока
        asyncio.get_event_loop().create_task(self.refresh())

    # ------------------------------------------------------------- helpers
    def _models_for(self, key: dict) -> list[str]:
        cfg = PROVIDERS.get(key["provider"])
        models = []
        if key.get("model"):
            models.append(key["model"])
        if cfg:
            base = cfg.get("models") or []
            random.shuffle(base)
            for m in base:
                if m not in models:
                    models.append(m)
        return models[:MAX_MODELS_PER_KEY]

    def _candidates(self) -> list[tuple[dict, str]]:
        self._maybe_refresh()
        usable = [k for k in self._keys if k.get("enabled") and k["provider"] in PROVIDERS]
        # наименее проблемные и наименее заюзанные идут первыми
        usable.sort(key=lambda k: (k.get("fail_count", 0), k.get("usage_count", 0), random.random()))
        return [(k, m) for k in usable for m in self._models_for(k)]

    # -------------------------------------------------------------- calls
    async def _call_openai(self, key: dict, model: str, system: str, user: str,
                           temperature: float, max_tokens: int, json_mode: bool) -> str:
        cfg = PROVIDERS[key["provider"]]
        headers = {
            "Authorization": f"Bearer {key['api_key']}",
            "Content-Type": "application/json",
        }
        if key["provider"] == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/tg-auto-poster"
            headers["X-Title"] = "TG Auto-Poster"
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        async with self._session.post(cfg["url"], json=payload, headers=headers) as resp:
            body = await resp.text()
            if resp.status in (401, 403):
                raise KeyRejected(f"HTTP {resp.status}: {body[:150]}")
            if resp.status == 429:
                raise AIError("Квота/rate-limit (429)")
            if resp.status != 200:
                raise AIError(f"HTTP {resp.status}: {body[:150]}")
            try:
                data = json.loads(body)
                return data["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, ValueError) as e:
                raise AIError(f"Неожиданный ответ: {body[:150]}") from e

    async def _call_gemini(self, key: dict, model: str, system: str, user: str,
                           temperature: float, max_tokens: int, json_mode: bool) -> str:
        cfg = PROVIDERS[key["provider"]]
        url = cfg["url"].format(model=model)
        payload: dict = {
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        async with self._session.post(url, params={"key": key["api_key"]}, json=payload) as resp:
            body = await resp.text()
            if resp.status in (401, 403):
                raise KeyRejected(f"HTTP {resp.status}: {body[:150]}")
            if resp.status == 429:
                raise AIError("Квота ключа исчерпана (429)")
            if resp.status != 200:
                raise AIError(f"HTTP {resp.status}: {body[:150]}")
            try:
                data = json.loads(body)
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError, ValueError) as e:
                raise AIError(f"Неожиданный ответ: {body[:150]}") from e

    async def _call(self, key: dict, model: str, system: str, user: str,
                    temperature: float, max_tokens: int, json_mode: bool) -> str:
        if PROVIDERS[key["provider"]]["kind"] == "gemini":
            return await self._call_gemini(key, model, system, user, temperature, max_tokens, json_mode)
        return await self._call_openai(key, model, system, user, temperature, max_tokens, json_mode)

    # ------------------------------------------------------------- public
    async def generate(self, system: str, user: str, temperature: float = 0.8,
                       max_tokens: int = 2000, json_mode: bool = False) -> tuple[str, str, str]:
        """Возвращает (text, provider, model). Бросает AIError если всё плохо."""
        candidates = self._candidates()
        if not candidates:
            raise AIError("Нет рабочих AI-ключей. Добавь: /add_key <провайдер> <ключ>")

        errors: list[str] = []
        for key, model in candidates:
            p, k = key["provider"], key["api_key"]
            try:
                text = await self._call(key, model, system, user, temperature, max_tokens, json_mode)
                await db.ai_key_success(p, k)
                logger.info("AI OK: %s/%s", p, model)
                return text, p, model
            except KeyRejected as e:
                await db.ai_key_failed(p, k, AI_MAX_FAILS)
                errors.append(f"{p}: ключ отклонён ({str(e)[:60]})")
                logger.warning("AI ключ отклонён (%s): %s", p, str(e)[:120])
                await self.refresh()
            except AIError as e:
                await db.ai_key_failed(p, k, AI_MAX_FAILS)
                errors.append(f"{p}:{model}: {str(e)[:80]}")
                logger.warning("AI ошибка (%s/%s): %s", p, model, str(e)[:120])
            except Exception as e:  # сетевые и прочие
                await db.ai_key_failed(p, k, AI_MAX_FAILS)
                errors.append(f"{p}:{model}: {str(e)[:80]}")
                logger.warning("AI исключение (%s/%s): %s", p, model, str(e)[:120])

        raise AIError(" | ".join(errors[-6:]) if errors else "Нет ключей")

    async def test_key(self, key: dict) -> tuple[bool, str]:
        """Быстрая проверка ключа 'скажи OK'."""
        try:
            text, p, m = await self.generate("Ты помощник, отвечай коротко.", "Скажи OK")
            return True, f"{p} / {m}: {text[:50]}"
        except AIError as e:
            return False, str(e)


_engine: RotationEngine | None = None


def get_engine() -> RotationEngine:
    global _engine
    if _engine is None:
        _engine = RotationEngine()
    return _engine


async def startup() -> None:
    await get_engine().start()


async def shutdown() -> None:
    await get_engine().stop()


async def generate(system: str, user: str, temperature: float = 0.8,
                   max_tokens: int = 2000, json_mode: bool = False) -> tuple[str, str, str]:
    return await get_engine().generate(system, user, temperature, max_tokens, json_mode)


async def parse_json_ai(system: str, user: str, temperature: float = 0.2,
                        max_tokens: int = 800) -> dict:
    """AI-вызов с обязательным JSON-ответом; парсит в словарь (по возможности)."""
    raw, _p, _m = await generate(system, user, temperature, max_tokens, json_mode=True)
    text = _strip_fence(raw)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
        raise AIError(f"AI вернул не JSON: {text[:200]}")
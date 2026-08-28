# 🤖 tg-auto-poster

Telegram-бот для копирования постов из каналов-источников в свои каналы с помощью AI.

## Возможности

- **Авто-подключение к аккаунту** — закинь `.session` файл или `SESSION_STRING` в `.env`
- **Анализ последних постов** каналов-источников, определение важности, сохранение в память
- **Автопостинг** в свои каналы по расписанию
- **Мульти-ключи AI** — `OPENROUTER_KEYS=key1,key2,key3` (ротация при ошибках)
- **5 провайдеров**: OpenRouter, Gemini, Groq, Mistral, Cerebras — с авто-отключением нерабочего ключа
- **Всё через кнопки** (inline-меню), команды только `/start /help /status /login /upload_session /analyze`
- SQLite (WAL), фоновые циклы без сторонних планировщиков
- Python 3.11 (подготовлено для bothost.ru)

## Быстрый старт

1. Скопируй `.env.example` в `.env` и заполни:
   - `BOT_TOKEN` — токен от [@BotFather](https://t.me/BotFather)
   - `ADMIN_ID` — твой Telegram ID
   - `API_ID` и `API_HASH` — от [my.telegram.org](https://my.telegram.org)
   - Хотя бы один AI-ключ (например `OPENROUTER_KEYS=sk-or-...`)

2. Закинь `.session` файл в папку `data/sessions/` (или укажи `SESSION_STRING=`)

3. Запуск:
   ```bash
   # локально
   python3.11 bot.py
   # или Docker
   docker compose up -d --build
   ```

4. Открой бота в Telegram → нажми `/start` → добавь каналы-источники через меню.

## Деплой на bothost.ru

1. Загрузи `tg-auto-poster-bothost-2.tar.gz` и распакуй.
2. Положи `.session` в `data/sessions/`.
3. Заполни `.env`.
4. Собери и запусти:
   ```bash
   docker compose up -d --build
   ```

## AI-ключи

Задаются через переменные окружения (несколько — через запятую):

| Провайдер  | Переменная             | Получить ключ |
|-----------|----------------------|---------------|
| OpenRouter | `OPENROUTER_KEYS`   | openrouter.ai/keys |
| Gemini    | `GEMINI_KEYS`        | aistudio.google.com/apikey |
| Groq      | `GROQ_KEYS`          | console.groq.com/keys |
| Mistral   | `MISTRAL_KEYS`        | console.mistral.ai/api-keys/ |
| Cerebras  | `CEREBRAS_KEYS`       | cloud.cerebras.ai |

Ключ с `AI_MAX_FAILS` (по умолчанию 5) последовательными ошибками отключается автоматически.

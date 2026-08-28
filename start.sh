#!/usr/bin/env bash
# Простой запуск бота на Python 3.11 (или Docker ниже).
set -e
cd "$(dirname "$0")"

if command -v python3.11 >/dev/null 2>&1; then
    PY=python3.11
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    echo "Нет python3" >&2
    exit 1
fi

exec "$PY" bot.py

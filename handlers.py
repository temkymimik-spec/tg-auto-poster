"""Telegram-интерфейс бота: меню, команды, диалоговые состояния."""
import asyncio
import logging
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import channel_parser as cp
import database as db
import monitor
import scheduler
import session_manager as sess
from ai import providers
from ai.analyzer import generate_from_memory, generate_from_text
from config import (
    ADMIN_IDS,
    API_HASH,
    API_ID,
    PROVIDERS,
    BOT_TOKEN,
)

logger = logging.getLogger(__name__)

# user_states[user_id] = (action, extra) — свободные текстовые вводы
user_states: dict[int, tuple] = {}

BACK = "◀️ Назад"


# ================================================================ helpers
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def back_btn(data: str = "menu_main") -> InlineKeyboardButton:
    return InlineKeyboardButton(BACK, callback_data=data)


async def deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Нет доступа.")


def short(text: str, n: int = 40) -> str:
    text = (text or "").replace("\n", " ")
    return text[:n] + "…" if len(text) > n else text


# ================================================================ главное меню
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny(update)
    await show_main(update.effective_message or update.callback_query)


async def show_main(msg) -> None:
    keyboard = [
        [InlineKeyboardButton("📺 Каналы", callback_data="menu_channels")],
        [InlineKeyboardButton("🧠 Память (мониторинг)", callback_data="menu_memory")],
        [InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
        [InlineKeyboardButton("🔑 Аккаунт", callback_data="menu_account")],
        [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="broadcast")],
    ]
    text = "🤖 *TG Auto-Poster*\nУправление каналами через ИИ.\n\nИспользуй кнопки ниже."
    if callable(getattr(msg, "edit_message_text", None)):
        await msg.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    else:
        await msg.reply_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny(update)
    await show_main(update.message)
    await update.message.reply_text(
        "*ℹ️ Управление — кнопками.*\n\n"
        "Для быстрого запуска: `/start`\n"
        "Состояние: `/status`\n"
        "Вход в аккаунт: `/login`",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny(update)
    await show_main(update.message)
    await send_status(update.message)


async def send_status(msg: Message) -> None:
    me = await sess.me()
    acc = f"✅ @{me['username']}" if me else "❌ не подключён"
    sess_files = len(sess.list_session_files())
    keys = await db.list_ai_keys()
    total = len(keys)
    enabled = sum(1 for k in keys if k["enabled"])
    channels = await db.get_channels(active_only=False)
    sources = await db.get_source_channels()
    stats = await db.get_stats()
    pending_posts = stats["posts_pending"]
    pending_ads = stats["ads_pending"]

    text = (
        f"*📊 Статус*\n\n"
        f"🔑 Аккаунт: {acc}\n"
        f"📁 .session файлов: {sess_files}\n"
        f"🤖 AI ключей: {enabled}/{total}\n"
        f"📺 Каналов: {len(channels)} (актив {stats['channels']})\n"
        f"📥 Источников: {len(sources)}\n"
        f"📝 Ожидают: посты {pending_posts}, реклама {pending_ads}\n"
        f"🧠 Записей памяти: {stats['memory_total']}\n"
        f"⏱ Мониторинг: {'✅ вкл' if monitor.is_running() else '❌ выкл'}\n"
        f"⏱ Автопостинг: {'✅ вкл' if scheduler.is_running() else '❌ выкл'}"
    )
    await msg.reply_text(text, parse_mode="Markdown")


# ================================================================ каналы
async def menu_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    channels = await db.get_channels(active_only=False)
    keyboard = [[InlineKeyboardButton("➕ Добавить канал", callback_data="channel_add")]]
    for ch in channels:
        status = "🟢" if ch["is_active"] else "🔴"
        title = ch.get("channel_title") or ch["channel_id"]
        keyboard.append([InlineKeyboardButton(f"{status} {title}", callback_data=f"ch_open|{ch['channel_id']}")])
    keyboard.append([back_btn()])
    text = "*📺 Каналы*\n" + ("Нет каналов." if not channels else "")
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def channel_open(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("Канал не найден.", reply_markup=kb([[back_btn("menu_channels")]]))
        await query.answer()
        return
    sources = await db.get_source_channels(channel_id)
    status = "🟢 активен" if ch["is_active"] else "🔴 выключен"
    text = (
        f"*{ch.get('channel_title') or channel_id}*\n"
        f"ID: `{channel_id}`\n"
        f"Статус: {status}\n"
        f"Интервал: {ch.get('post_interval_min') or 60} мин\n"
        f"Источников: {len(sources)}\n"
        f"Стиль: {short(ch.get('style_prompt') or 'не задан', 60)}"
    )
    keyboard = [
        [InlineKeyboardButton("🎨 Стиль", callback_data=f"ch_style|{channel_id}")],
        [InlineKeyboardButton("⏱ Интервал", callback_data=f"ch_interval|{channel_id}")],
        [InlineKeyboardButton("🟢/🔴 Вкл/Выкл", callback_data=f"ch_toggle|{channel_id}")],
        [InlineKeyboardButton("🔄 Сгенерировать", callback_data=f"ch_gen|{channel_id}")],
        [InlineKeyboardButton("📥 Посты", callback_data=f"ch_posts|{channel_id}")],
        [InlineKeyboardButton("📡 Источники", callback_data=f"ch_sources|{channel_id}")],
        [InlineKeyboardButton("🧠 Память", callback_data=f"mem_stats|{channel_id}")],
        [InlineKeyboardButton("📢 Реклама", callback_data=f"ch_ads|{channel_id}")],
        [InlineKeyboardButton("🔎 Анализ сейчас", callback_data=f"ch_analyze|{channel_id}")],
        [InlineKeyboardButton("❌ Удалить", callback_data=f"ch_del|{channel_id}")],
        [back_btn("menu_channels")],
    ]
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def channel_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("add_channel", None)
    await query.edit_message_text(
        "✏️ Отправь @username, числовой ID или ссылку на канал.\n"
        "_(требуется подключённый аккаунт)_",
    )
    await query.answer()


async def channel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    await db.remove_channel(channel_id)
    user_states.pop(query.from_user.id, None)
    await query.edit_message_text("🗑 Канал удалён.", reply_markup=kb([[back_btn("menu_channels")]]))
    await query.answer()


async def channel_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    if ch:
        await db.update_channel(channel_id, is_active=0 if ch["is_active"] else 1)
    await channel_open(update, context, channel_id)
    await query.answer()


async def channel_style(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("set_style", channel_id)
    await query.edit_message_text(
        "🎨 Опиши стиль постов для этого канала (тон, эмодзи, оформление):",
    )
    await query.answer()


async def channel_interval(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("set_interval", channel_id)
    await query.edit_message_text("⏱ Интервал автопостинга в минутах (минимум 5):")
    await query.answer()


async def channel_generate(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    await query.edit_message_text("🔄 Генерирую пост из памяти/источников…")
    ch = await db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("Канал не найден.")
        await query.answer()
        return
    memory = await db.get_recent_memory(channel_id, hours=48, min_importance=5, limit=8)
    result = None
    if memory:
        result = await generate_from_memory(memory, ch.get("style_prompt", ""))
    if not result or not result.get("text"):
        sources = await db.get_source_channels(channel_id)
        for src in sources:
            posts = await cp.fetch_recent_posts(src["source_channel_id"], limit=1)
            if not posts:
                continue
            result = await generate_from_text(posts[0]["text"], ch.get("style_prompt", ""),
                                              ch.get("custom_instruction", ""))
            break
    if not result or not result.get("text"):
        await query.edit_message_text(f"❌ {result.get('error', 'Нет источников/памяти')}",
                                      reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]))
        await query.answer()
        return
    post_id = await db.save_post(channel_id, result["text"],
                                 ai_provider=result["provider"], ai_model=result["model"])
    keyboard = [
        [InlineKeyboardButton("📤 Опубликовать", callback_data=f"post_pub|{post_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"post_del|{post_id}")],
        [back_btn(f"ch_open|{channel_id}")],
    ]
    preview = result["text"][:1500]
    await query.edit_message_text(
        f"🤖 *Пост* `{result['provider']}/{result['model']}`\n\n---\n{preview}\n---",
        reply_markup=kb(keyboard), parse_mode="Markdown",
    )
    await query.answer()


async def channel_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    drafts = await db.get_draft_posts(channel_id)
    text = f"*Черновики:* {len(drafts)}\n"
    keyboard = []
    for p in drafts:
        keyboard.append([InlineKeyboardButton(f"📤 {short(p['post_text'], 35)}",
                                              callback_data=f"post_pub|{p['id']}")])
    keyboard.append([back_btn(f"ch_open|{channel_id}")])
    await query.edit_message_text(text or "Черновиков нет.", reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def post_publish(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: str) -> None:
    query = update.callback_query
    ok = await scheduler.publish_post(int(post_id))
    post = await db.get_post(int(post_id))
    ch_id = post["channel_id"] if post else ""
    if ok:
        await query.edit_message_text("✅ Опубликовано.", reply_markup=kb([[back_btn(f"ch_open|{ch_id}")]]))
    else:
        await query.edit_message_text("❌ Не удалось опубликовать (аккаунт подключён?).",
                                      reply_markup=kb([[back_btn(f"ch_open|{ch_id}")]]))
    await query.answer()


async def post_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: str) -> None:
    query = update.callback_query
    post = await db.get_post(int(post_id))
    ch_id = post["channel_id"] if post else ""
    await db.delete_post(int(post_id))
    await query.edit_message_text("🗑 Пост удалён.", reply_markup=kb([[back_btn(f"ch_open|{ch_id}")]]))
    await query.answer()


async def channel_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    await query.edit_message_text("🔎 Анализирую последние посты источников…")
    sources = await db.get_source_channels(channel_id)
    state = await db.get_monitor_state(channel_id)
    if ch:
        stats = await monitor.analyze_channel(ch, sources, state)
        await asyncio.sleep(0.2)
        err = f"\n⚠️ {stats.get('error')}" if stats.get("error") else ""
        await query.edit_message_text(
            f"✅ Анализ завершён.\nНовых постов: {stats.get('new_posts', 0)}\n"
            f"Сохранено в память: {stats.get('saved', 0)}{err}",
            reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]),
        )
    else:
        await query.edit_message_text("Канал не найден.")
    await query.answer()


# ================================================================ источники
async def channel_sources(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    sources = await db.get_source_channels(channel_id)
    text = "*📡 Источники:*\n"
    keyboard = []
    for s in sources:
        title = s.get("source_channel_title") or s["source_channel_id"]
        keyboard.append([InlineKeyboardButton(f"❌ {title}",
                                              callback_data=f"src_del|{channel_id}|{s['source_channel_id']}")])
    keyboard.append([InlineKeyboardButton("➕ Добавить источник", callback_data=f"src_add|{channel_id}")])
    keyboard.append([back_btn(f"ch_open|{channel_id}")])
    if not sources:
        text += "Нет источников."
    else:
        text += "\n".join(f"• {s.get('source_channel_title') or s['source_channel_id']}" for s in sources)
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def source_add(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("add_source", channel_id)
    await query.edit_message_text("✏️ Отправь @username или ссылку на канал-конкурент:")
    await query.answer()


async def source_del(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str, source_id: str) -> None:
    query = update.callback_query
    await db.remove_source_channel(channel_id, source_id)
    await channel_sources(update, context, channel_id)
    await query.answer()


# ================================================================ реклама
async def channel_ads(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ads = await db.get_ads(channel_id)
    text = f"*📢 Реклама:* {len(ads)}\n"
    keyboard = []
    for a in ads:
        st = "📤" if a["status"] == "draft" else a["status"]
        keyboard.append([InlineKeyboardButton(f"{st} {short(a['ad_text'], 30)} → {channel_id}",
                                              callback_data=f"ad_pub|{a['id']}")])
    keyboard.append([InlineKeyboardButton("➕ Добавить рекламу", callback_data=f"ad_add|{channel_id}")])
    keyboard.append([back_btn(f"ch_open|{channel_id}")])
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def ad_add(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("add_ad", channel_id)
    await query.edit_message_text("✏️ Отправь текст рекламного поста:")
    await query.answer()


async def ad_publish(update: Update, context: ContextTypes.DEFAULT_TYPE, ad_id: str) -> None:
    query = update.callback_query
    row = await db.get_ad(int(ad_id))
    if not row:
        await query.edit_message_text("Реклама не найдена.")
        await query.answer()
        return
    ok = await cp.send_post(row["channel_id"], row["ad_text"], row.get("ad_media_path") or None)
    if ok:
        await db.update_ad_status(row["id"], "published")
        await query.edit_message_text("✅ Реклама опубликована.")
    else:
        await query.edit_message_text("❌ Не опубликована (аккаунт подключён?).")
    await query.answer()


# ================================================================ память
async def menu_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    channels = await db.get_channels(active_only=False)
    keyboard = []
    for ch in channels:
        count = await db.get_memory_count(ch["channel_id"])
        title = ch.get("channel_title") or ch["channel_id"]
        keyboard.append([InlineKeyboardButton(f"🧠 {title} ({count})", callback_data=f"mem_stats|{ch['channel_id']}")])
    keyboard.append([back_btn()])
    await query.edit_message_text("🧠 *Память бота*\nAI накапливает важное из мониторинга.",
                                  reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def mem_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    memory = await db.get_recent_memory(channel_id, hours=24, min_importance=1)
    total = len(memory)
    topics = {}
    for m in memory:
        topics[m.get("topic") or "Другое"] = topics.get(m.get("topic") or "Другое", 0) + 1
    topics_text = "\n".join(f"  • {t} ({c})" for t, c in list(topics.items())[:5]) or "  нет данных"
    title = ch.get("channel_title", channel_id) if ch else channel_id
    text = (
        f"*🧠 Память: {title}*\n"
        f"За 24ч: {total}\n\n"
        f"*Топ тем:*\n{topics_text}"
    )
    keyboard = [
        [InlineKeyboardButton("📋 Записи", callback_data=f"mem_list|{channel_id}")],
        [InlineKeyboardButton("🔍 Поиск", callback_data=f"mem_search|{channel_id}")],
        [InlineKeyboardButton("🔄 Сгенерировать пост", callback_data=f"mem_gen|{channel_id}")],
        [InlineKeyboardButton("📝 По теме", callback_data=f"mem_topic|{channel_id}")],
        [back_btn(f"ch_open|{channel_id}")],
    ]
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def mem_list(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    memory = await db.get_memory(channel_id, limit=10)
    text = f"*📋 Записи ({len(memory)}):*\n\n"
    for m in memory:
        imp = "🔴" if m["importance"] >= 8 else "🟡" if m["importance"] >= 5 else "⚪"
        text += f"{imp} *{m['topic']}* ({m['importance']})\n{short(m['summary'], 90)}\n\n"
    if not memory:
        text = "Пока нет записей."
    keyboard = [[back_btn(f"mem_stats|{channel_id}")]]
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def mem_search(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("memory_search", channel_id)
    await query.edit_message_text("🔍 Введи ключевое слово:")
    await query.answer()


async def mem_generate(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    await query.edit_message_text("🔄 Генерирую из памяти…")
    memory = await db.get_recent_memory(channel_id, hours=48, min_importance=5, limit=8)
    result = await generate_from_memory(memory, (ch.get("style_prompt") if ch else ""))
    if result.get("text"):
        post_id = await db.save_post(channel_id, result["text"],
                                     ai_provider=result["provider"], ai_model=result["model"])
        keyboard = [
            [InlineKeyboardButton("📤 Опубликовать", callback_data=f"post_pub|{post_id}")],
            [back_btn(f"mem_stats|{channel_id}")],
        ]
        await query.edit_message_text(f"🤖 {result['text'][:1500]}\n`{result['provider']}/{result['model']}`",
                                      reply_markup=kb(keyboard), parse_mode="Markdown")
    else:
        await query.edit_message_text(f"❌ {result.get('error', 'Ошибка')}",
                                      reply_markup=kb([[back_btn(f"mem_stats|{channel_id}")]]))
    await query.answer()


async def mem_topic(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("memory_gen_topic", channel_id)
    await query.edit_message_text("📝 Введи тему для поста:")
    await query.answer()


# ================================================================ AI / ключи
async def menu_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    keys = await db.list_ai_keys()
    by_provider = {}
    for k in keys:
        by_provider.setdefault(k["provider"], [0, 0])
        by_provider[k["provider"]][1] += 1
        if k["enabled"]:
            by_provider[k["provider"]][0] += 1

    text = "*🤖 AI*\n\n"
    if keys:
        for p, (en, total) in by_provider.items():
            text += f"• {p}: {en}/{total} актив\n"
    else:
        text += "Ключей нет. Добавь: /add_key провайдер ключ\n"
    text += "\nКлючи автоматически ротируются; сломанные отключаются сами."

    keyboard = [
        [InlineKeyboardButton("🔑 Список ключей", callback_data="ai_keys")],
        [InlineKeyboardButton("➕ Добавить ключ", callback_data="ai_add")],
        [InlineKeyboardButton("🧪 Проверить ключи", callback_data="ai_test")],
        [back_btn()],
    ]
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def ai_keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    keys = await db.list_ai_keys()
    text = "*🔑 AI ключи:*\n\n"
    keyboard = []
    if not keys:
        text += "Пусто."
    for k in keys:
        enabled = "✅" if k["enabled"] else "⛔️"
        mask = "…" + k["api_key"][-6:]
        text += f"{enabled} #{k['id']} {k['provider']} `{mask}` фейлов:{k['fail_count']} юзов:{k['usage_count']}\n"
        label = f"⛔ выкл #{k['id']}" if k["enabled"] else f"✅ вкл #{k['id']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"ai_toggle|{k['id']}")])
        keyboard.append([InlineKeyboardButton(f"🗑 {k['provider']} {mask}", callback_data=f"ai_del|{k['id']}")])
    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data="ai_add")])
    keyboard.append([back_btn("menu_ai")])
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def ai_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("add_key", None)
    await query.edit_message_text(
        "✏️ Формат: <code>провайдер ключ</code>\n"
        "Провайдеры: " + ", ".join(PROVIDERS) + "\n\n"
        "Можно несколько ключей одного провайдера — будет ротация и фолловер.",
        parse_mode="HTML",
    )
    await query.answer()


async def ai_del(update: Update, context: ContextTypes.DEFAULT_TYPE, key_id: str) -> None:
    query = update.callback_query
    await db.remove_ai_key(int(key_id))
    await ai_keys_menu(update, context)
    await query.answer()


async def ai_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, key_id: str) -> None:
    query = update.callback_query
    row = next((k for k in await db.list_ai_keys() if k["id"] == int(key_id)), None)
    if row:
        await db.set_ai_key_enabled(int(key_id), not row["enabled"])
    await ai_keys_menu(update, context)
    await query.answer()


async def ai_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.edit_message_text("🧪 Тест ключей… (может занять время)")
    keys = await db.list_ai_keys()
    usable = [k for k in keys if k["enabled"]]
    if not usable:
        await query.edit_message_text("Нет активных ключей.")
        await query.answer()
        return
    # тестируем по одному через движок
    ok = 0
    lines = []
    for k in usable:
        okk, info = await providers.get_engine().test_key(k)
        lines.append(f"{'✅' if okk else '❌'} {k['provider']} …{k['api_key'][-6:]}: {short(info, 60)}")
        if okk:
            ok += 1
    await query.edit_message_text(f"Результат: {ok}/{len(usable)}\n\n" + "\n".join(lines),
                                  reply_markup=kb([[back_btn("menu_ai")]]))
    await query.answer()





# ================================================================ аккаунт
async def menu_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await sess.init_client()
    me = await sess.me()
    if me:
        ph = sess.phone()
        text = (f"*🔑 Аккаунт:*\n✅ @{me['username']} ({me['first_name']})\n"
                f"id: {me['id']}")
        if ph:
            text += f"\n📱 {ph}"
    else:
        text = ("*🔑 Аккаунт:*\n❌ не подключён\n\n"
                "📁 Просто сбрось `.session` файл прямо в этот чат — бот всё сам "
                "подключит.\nЛибо нажми «Вход по телефону» и введи номер — придёт код.\n\n"
                "💡 Если сессия ещё не активирована, бот всё равно войдёт по номеру + код.")

    files = "\n".join(f"• {f}" for f in sess.list_session_files()) or "нет"
    text += f"\n\n*Файлы сессий:*\n{files}"

    keyboard = [
        [InlineKeyboardButton("📁 Загрузить .session", callback_data="account_upload")],
        [InlineKeyboardButton("👤 Вход по телефону", callback_data="account_login")],
        [InlineKeyboardButton("📤 Экспорт session string", callback_data="account_export")],
        [back_btn()],
    ]
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


async def account_upload_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("upload_session", None)
    await query.edit_message_text("📎 Отправь файл `.session` (Telethon session file).\n"
                                  "Его можно получить на любом устройстве, где ты залогинен.",
                                  parse_mode="Markdown")
    await query.answer()


async def account_login_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not API_ID or not API_HASH:
        await query.edit_message_text("❌ API_ID/API_HASH не заданы в .env.",
                                      reply_markup=kb([[back_btn("menu_account")]]))
        await query.answer()
        return
    client = await sess.login_start()
    if client is None:
        await query.edit_message_text("❌ Не удалось инициализировать клиент.",
                                      reply_markup=kb([[back_btn("menu_account")]]))
        await query.answer()
        return
    user_states[query.from_user.id] = ("login_phone", None)
    await query.edit_message_text("👤 Введи номер телефона в формате +79991234567:")
    await query.answer()


async def account_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    s = await sess.generate_string_session_export()
    if s:
        await query.edit_message_text("📤 Session string скопируй и сохрани в .env как SESSION_STRING",
                                      reply_markup=kb([[back_btn("menu_account")]]))
        try:
            await query.message.reply_text(f"<code>{s}</code>", parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
    else:
        await query.edit_message_text("❌ Аккаунт не подключён.", reply_markup=kb([[back_btn("menu_account")]]))
    await query.answer()


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny(update)
    if not API_ID or not API_HASH:
        await update.message.reply_text("❌ API_ID/API_HASH не заданы в .env.")
        return
    client = await sess.login_start()
    if client is None:
        await update.message.reply_text("❌ Не удалось инициализировать клиент.")
        return
    user_states[update.effective_user.id] = ("login_phone", None)
    await update.message.reply_text("👤 Введи номер телефона в формате +79991234567:")


async def cmd_upload_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny(update)
    user_states[update.effective_user.id] = ("upload_session", None)
    await update.message.reply_text("📎 Отправь файл `.session`", parse_mode="Markdown")


# ================================================================ статистика
async def menu_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    stats = await db.get_stats()
    text = (
        "*📊 Статистика*\n\n"
        f"Каналы (актив): {stats['channels']}\n"
        f"Посты: всего {stats['posts_total']} "
        f"(✅ {stats['posts_published']}, ⏳ {stats['posts_pending']}, ✏️ {stats['posts_draft']})\n"
        f"Реклама: всего {stats['ads_total']} (✅ {stats['ads_published']}, ⏳ {stats['ads_pending']})\n"
        f"Память: {stats['memory_total']}\n"
        f"AI ключей: {stats['ai_keys_enabled']}/{stats['ai_keys']}"
    )
    keyboard = [[back_btn()]]
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await query.answer()


# ================================================================ рассылка
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user_states[q.from_user.id] = ("broadcast", None)
    await q.edit_message_text("📢 Отправь текст для рассылки во все *активные* каналы:", parse_mode="Markdown")
    await q.answer()


# ================================================================ текстовые состояния
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    text = (update.message.text or "").strip()
    state = user_states.get(user_id)

    if not state:
        # вне диалога — короткое меню
        await show_main(update.message)
        return

    action, extra = state

    # --- AI ключи
    if action == "add_key":
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or parts[0].lower() not in PROVIDERS:
            await update.message.reply_text("❌ Формат: провайдер ключ. Доступны: " + ", ".join(PROVIDERS))
            return
        provider = parts[0].lower()
        key = parts[1].strip()
        model = parts[2].strip() if len(parts) == 3 else ""
        ok = await db.add_ai_key(provider, key, model)
        await update.message.reply_text("✅ Ключ добавлен" if ok else "❌ Не добавлен (уже есть?)")
        user_states.pop(user_id, None)
        return

    # --- каналы
    if action == "add_channel":
        resolved = await cp.resolve_channel(text)
        if not resolved:
            await update.message.reply_text("❌ Канал не найден. Проверь @username/sсылку/ID")
            user_states.pop(user_id, None)
            return
        ok = await db.add_channel(resolved["id"], resolved["title"], resolved["username"],
                                  interval=60)
        await update.message.reply_text(
            f"✅ Канал *{resolved['title']}* добавлен!" if ok else "❌ Уже есть в базе.",
            parse_mode="Markdown",
        )
        user_states.pop(user_id, None)
        return

    if action == "add_source" and extra:
        resolved = await cp.resolve_channel(text)
        if not resolved:
            await update.message.reply_text("❌ Источник не найден.")
            user_states.pop(user_id, None)
            return
        await db.add_source_channel(extra, resolved["id"], resolved["title"])
        await update.message.reply_text(f"✅ Источник *{resolved['title']}* добавлен.", parse_mode="Markdown")
        user_states.pop(user_id, None)
        return

    if action == "set_style" and extra:
        await db.update_channel(extra, style_prompt=text)
        await update.message.reply_text("✅ Стиль сохранён.")
        user_states.pop(user_id, None)
        return

    if action == "set_interval" and extra:
        try:
            interval = max(int(text), 5)
        except ValueError:
            await update.message.reply_text("❌ Введи число (минуты).")
            return
        await db.update_channel(extra, post_interval_min=interval)
        await update.message.reply_text(f"✅ Интервал: {interval} мин.")
        user_states.pop(user_id, None)
        return

    if action == "add_ad" and extra:
        await db.add_ad(extra, text)
        await update.message.reply_text("✅ Реклама создана (будет опубликована при нажатии).")
        user_states.pop(user_id, None)
        return

    if action == "broadcast":
        channels = await db.get_channels(active_only=True)
        ok = 0
        if not text:
            await update.message.reply_text("❌ Пустой текст.")
            user_states.pop(user_id, None)
            return
        for ch in channels:
            if await cp.send_post(ch["channel_id"], text):
                ok += 1
            await asyncio.sleep(1)
        await update.message.reply_text(f"✅ Рассылка: {ok}/{len(channels)} каналов.")
        user_states.pop(user_id, None)
        return

    if action == "memory_search" and extra:
        results = await db.search_memory(extra, text)
        if results:
            msg = f"🔍 *«{text}»* ({len(results)})\n\n"
            for m in results[:5]:
                imp = "🔴" if m["importance"] >= 8 else "🟡" if m["importance"] >= 5 else "⚪"
                msg += f"{imp} *{m['topic']}* ({m['importance']})\n{short(m['summary'], 90)}\n\n"
        else:
            msg = "Ничего не найдено."
        await update.message.reply_text(msg, parse_mode="Markdown")
        user_states.pop(user_id, None)
        return

    if action == "memory_gen_topic" and extra:
        await update.message.reply_text("🔄 Генерирую…")
        ch = await db.get_channel(extra)
        memory = await db.get_recent_memory(extra, hours=48, min_importance=5, limit=8)
        result = await generate_from_memory(memory, (ch.get("style_prompt") if ch else ""), target_topic=text)
        if result.get("text"):
            post_id = await db.save_post(extra, result["text"],
                                         ai_provider=result["provider"], ai_model=result["model"])
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            k = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Опубликовать", callback_data=f"post_pub|{post_id}")]])
            await update.message.reply_text(result["text"][:3900], reply_markup=k)
        else:
            await update.message.reply_text(f"❌ {result.get('error', 'Ошибка')}")
        user_states.pop(user_id, None)
        return

    if not is_admin(user_id):
        return

    # --- логин в аккаунт
    if action == "login_phone":
        if not text.startswith("+"):
            await update.message.reply_text("❌ Формат: +79991234567")
            return
        try:
            await sess.send_code(sess.raw_client(), text)
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}")
            return
        user_states[user_id] = ("login_code", text)
        await update.message.reply_text("📨 Код отправлен. Введи код из Telegram:")
        return

    if action == "login_code":
        phone = extra
        try:
            res = await sess.login_code(sess.raw_client(), phone, text)
        except ValueError as e:
            await update.message.reply_text(f"❌ {e} (введи код ещё раз)")
            return
        if res["status"] == "password":
            user_states[user_id] = ("login_password", None)
            await update.message.reply_text("🔐 Нужен пароль 2FA. Введи пароль:")
        else:
            user_states.pop(user_id, None)
            await update.message.reply_text("✅ Аккаунт подключён! /status для деталей")
        return

    if action == "login_password":
        try:
            await sess.login_password(sess.raw_client(), text)
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}")
            return
        user_states.pop(user_id, None)
        await update.message.reply_text("✅ Аккаунт подключён! /status для деталей")
        return


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".session"):
        return
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    ok = await sess.save_uploaded_session(bytes(data), doc.file_name)
    if ok:
        me = await sess.me()
        ph = sess.phone()
        who = f"@{me['username']} {ph or ''}" if me else ""
        await update.message.reply_text(f"✅ Сессия подключена! Аккаунт: {who}")
        user_states.pop(user_id, None)
    else:
        user_states[user_id] = ("login_phone", None)
        await update.message.reply_text(
            "📁 Сессия сохранена, но не активирована.\n"
            "👉 Введи номер телефона формате +79991234567 — бот получит код "
            "и завершит вход прямо здесь.")


# ================================================================ маршрутизация callback
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return
    data = q.data

    # простая таблица маршрутов
    if data == "menu_main":
        await show_main(q)
    elif data == "menu_channels":
        await menu_channels(update, context)
    elif data == "menu_memory":
        await menu_memory(update, context)
    elif data == "menu_ai":
        await menu_ai(update, context)
    elif data == "menu_account":
        await menu_account(update, context)
    elif data == "menu_stats":
        await menu_stats(update, context)
    elif data == "broadcast":
        await broadcast_start(update, context)
    elif data == "channel_add":
        await channel_add_prompt(update, context)
    elif data == "ai_keys":
        await ai_keys_menu(update, context)
    elif data == "ai_add":
        await ai_add_prompt(update, context)
    elif data == "ai_test":
        await ai_test(update, context)
    elif data == "account_upload":
        await account_upload_prompt(update, context)
    elif data == "account_login":
        await account_login_prompt(update, context)
    elif data == "account_export":
        await account_export(update, context)
    elif data.startswith("ch_open|"):
        await channel_open(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_del|"):
        await channel_delete(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_toggle|"):
        await channel_toggle(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_style|"):
        await channel_style(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_interval|"):
        await channel_interval(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_gen|"):
        await channel_generate(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_posts|"):
        await channel_posts(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_sources|"):
        await channel_sources(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_ads|"):
        await channel_ads(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_analyze|"):
        await channel_analyze(update, context, data.split("|", 1)[1])
    elif data.startswith("src_add|"):
        await source_add(update, context, data.split("|", 1)[1])
    elif data.startswith("src_del|"):
        _, ch, src = data.split("|")
        await source_del(update, context, ch, src)
    elif data.startswith("post_pub|"):
        await post_publish(update, context, data.split("|", 1)[1])
    elif data.startswith("post_del|"):
        await post_delete(update, context, data.split("|", 1)[1])
    elif data.startswith("ad_add|"):
        await ad_add(update, context, data.split("|", 1)[1])
    elif data.startswith("ad_pub|"):
        await ad_publish(update, context, data.split("|", 1)[1])
    elif data.startswith("mem_stats|"):
        await mem_stats(update, context, data.split("|", 1)[1])
    elif data.startswith("mem_list|"):
        await mem_list(update, context, data.split("|", 1)[1])
    elif data.startswith("mem_search|"):
        await mem_search(update, context, data.split("|", 1)[1])
    elif data.startswith("mem_gen|"):
        await mem_generate(update, context, data.split("|", 1)[1])
    elif data.startswith("mem_topic|"):
        await mem_topic(update, context, data.split("|", 1)[1])
    elif data.startswith("ai_toggle|"):
        await ai_toggle(update, context, data.split("|", 1)[1])
    elif data.startswith("ai_del|"):
        await ai_del(update, context, data.split("|", 1)[1])
    else:
        await q.answer()


def register(app) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("upload_session", cmd_upload_session))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny(update)
    channels = await db.get_channels(active_only=True)
    report = []
    for ch in channels:
        sources = await db.get_source_channels(ch["channel_id"])
        if not sources:
            continue
        state = await db.get_monitor_state(ch["channel_id"])
        stats = await monitor.analyze_channel(ch, sources, state)
        title = ch.get("channel_title") or ch["channel_id"]
        report.append(f"• {title}: новых {stats.get('new_posts', 0)}, в память {stats.get('saved', 0)}")
    if not report:
        await update.message.reply_text("Нет активных каналов с источниками.")
        return
    await update.message.reply_text("🔎 *Анализ завершён*\n\n" + "\n".join(report), parse_mode="Markdown")
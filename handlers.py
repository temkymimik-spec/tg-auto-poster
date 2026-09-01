"""Telegram-интерфейс бота: меню, команды, диалоговые состояния."""
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from ai import providers
from ai.analyzer import generate_from_memory
from config import ADMIN_IDS, POSTS_PER_DAY, POST_HOURS, PROVIDERS

logger = logging.getLogger(__name__)

user_states: dict[int, tuple] = {}

BACK = "◀️ Назад"


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def back_btn(data: str = "menu_main") -> InlineKeyboardButton:
    return InlineKeyboardButton(BACK, callback_data=data)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Нет доступа.")


def short(text: str, n: int = 40) -> str:
    text = (text or "").replace("\n", " ")
    return text[:n] + "…" if len(text) > n else text


def esc(text: str) -> str:
    """Экранирует текст для HTML parse_mode, чтобы спецсимволы (*, _, &, <) не ломали разметку."""
    return html.escape(str(text or ""), quote=False)


async def safe_answer(query) -> None:
    try:
        await query.answer()
    except Exception:  # noqa: BLE001
        pass


# ================================================================ главное меню
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny(update)
    await show_main(update.effective_message or update.callback_query)


async def show_main(msg) -> None:
    keyboard = [
        [InlineKeyboardButton("📺 Каналы", callback_data="menu_channels")],
        [InlineKeyboardButton("🧠 Память", callback_data="menu_memory")],
        [InlineKeyboardButton("🧠 Сгенерировать контент (кнопка сбора)", callback_data="menu_collect")],
        [InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
        [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="broadcast")],
    ]
    text = "🤖 *Auto-Poster*\nБот сам ведёт каналы по их описанию (ИИ генерит контент).\n\nИспользуй кнопки ниже."
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
        "Статус: `/status`",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny(update)
    await show_main(update.message)
    await send_status(update.message)


async def send_status(msg) -> None:
    keys = await db.list_ai_keys()
    total = len(keys)
    enabled = sum(1 for k in keys if k["enabled"])
    channels = await db.get_channels(active_only=False)
    stats = await db.get_stats()
    schedule = f"{POSTS_PER_DAY} поста в {POST_HOURS}" if POSTS_PER_DAY and POST_HOURS else "интервалы"

    text = (
        f"*📊 Статус*\n\n"
        f"🤖 AI ключей: {enabled}/{total}\n"
        f"📺 Каналов: {len(channels)} (актив {stats['channels']})\n"
        f"📝 Ожидают: посты {stats['posts_pending']}, реклама {stats['ads_pending']}\n"
        f"🧠 Записей памяти: {stats['memory_total']}\n"
        f"⏱ Мониторинг: {'✅ вкл' if monitor.is_running() else '❌ выкл'}\n"
        f"⏱ Автопостинг: {'✅ вкл' if scheduler.is_running() else '❌ выкл'}\n"
        f"📅 Расписание: {schedule}"
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
    await safe_answer(query)


async def channel_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("add_channel", None)
    await query.edit_message_text(
        "✏️ Отправь @username ID канала или ссылку.\n"
        "Важно: бот должен быть администратором этого канала.\n"
        "После добавления обязательно опиши, о чём канал (кнопка «✏️ Описание») — "
        "по этому описанию бот сам будет вести канал и генерить посты.",
    )
    await safe_answer(query)


async def channel_open(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("Канал не найден.", reply_markup=kb([[back_btn("menu_channels")]]))
        await safe_answer(query)
        return
    status = "🟢 активен" if ch["is_active"] else "🔴 выключен"
    desc = ch.get("channel_description") or "не задано"
    text = (
        f"*{ch.get('channel_title') or channel_id}*\n"
        f"ID: `{channel_id}`\n"
        f"Статус: {status}\n"
        f"Интервал: {ch.get('post_interval_min') or 60} мин\n"
        f"📝 Описание: {short(desc, 80)}\n"
        f"🎨 Стиль: {short(ch.get('style_prompt') or 'не задан', 60)}"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Описание канала (о чём он)", callback_data=f"ch_desc|{channel_id}")],
        [InlineKeyboardButton("🎨 Стиль", callback_data=f"ch_style|{channel_id}")],
        [InlineKeyboardButton("⏱ Интервал", callback_data=f"ch_interval|{channel_id}")],
        [InlineKeyboardButton("🟢/🔴 Вкл/Выкл", callback_data=f"ch_toggle|{channel_id}")],
        [InlineKeyboardButton("🔄 Сгенерировать пост из памяти", callback_data=f"ch_gen|{channel_id}")],
        [InlineKeyboardButton("🧪 Тест поста (не публикует)", callback_data=f"ch_test|{channel_id}")],
        [InlineKeyboardButton("⚡ Выложить сейчас", callback_data=f"ch_now|{channel_id}")],
        [InlineKeyboardButton("🚀 Тест-пост в канал", callback_data=f"ch_quick_test|{channel_id}")],
        [InlineKeyboardButton("📥 Черновики", callback_data=f"ch_posts|{channel_id}")],
        [InlineKeyboardButton("🧠 Память", callback_data=f"mem_stats|{channel_id}")],
        [InlineKeyboardButton("📢 Реклама", callback_data=f"ch_ads|{channel_id}")],
        [InlineKeyboardButton("🔍 Проверить канал", callback_data=f"ch_validate|{channel_id}")],
        [InlineKeyboardButton("❌ Удалить", callback_data=f"ch_del|{channel_id}")],
        [back_btn("menu_channels")],
    ]
    await query.edit_message_text(
        f"<b>{esc(ch.get('channel_title') or channel_id)}</b>\n"
        f"ID: <code>{esc(channel_id)}</code>\n"
        f"Статус: {status}\n"
        f"Интервал: {ch.get('post_interval_min') or 60} мин\n"
        f"📝 Описание: {esc(short(desc, 80))}\n"
        f"🎨 Стиль: {esc(short(ch.get('style_prompt') or 'не задан', 60))}",
        reply_markup=kb(keyboard), parse_mode="HTML",
    )
    await safe_answer(query)


async def channel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    await db.remove_channel(channel_id)
    user_states.pop(query.from_user.id, None)
    await query.edit_message_text("🗑 Канал удалён.", reply_markup=kb([[back_btn("menu_channels")]]))
    await safe_answer(query)


async def channel_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    if ch:
        await db.update_channel(channel_id, is_active=0 if ch["is_active"] else 1)
    await channel_open(update, context, channel_id)
    await safe_answer(query)


async def channel_description_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("set_desc", channel_id)
    await query.edit_message_text(
        "📝 Опиши, о чём этот канал — тематику, аудиторию, что нужно публиковать.\n"
        "Например: «Канал про инвестиции в крипту для новичков, простым языком, "
        "новости и обзоры, без хайпа».\n\n"
        "По этому описанию ИИ будет сам генерить контент по описанию и вести канал.",
    )
    await safe_answer(query)


async def channel_style(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("set_style", channel_id)
    await query.edit_message_text(
        "🎨 Опиши стиль постов (тон, эмодзи, оформление):",
    )
    await safe_answer(query)


async def channel_interval(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("set_interval", channel_id)
    await query.edit_message_text("⏱ Интервал автопостинга в минутах (минимум 5):")
    await safe_answer(query)


async def channel_generate(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    await query.edit_message_text("🔄 Генерирую пост из памяти…")
    ch = await db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("Канал не найден.")
        await safe_answer(query)
        return
    memory = await db.get_recent_memory(channel_id, hours=48, min_importance=5, limit=8)
    if not memory:
        await query.edit_message_text(
            "❌ В памяти нет данных. Сначала нажми «🧠 Сгенерировать контент» "
            "или задай описание канала.",
            reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]),
        )
        await safe_answer(query)
        return
    result = await generate_from_memory(memory, ch.get("style_prompt", ""))
    if not result.get("text"):
        await query.edit_message_text(f"❌ {result.get('error', 'Ошибка')}",
                                      reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]))
        await safe_answer(query)
        return
    media = memory[0].get("media_path") or ""
    media_url = memory[0].get("media_url") or ""
    post_id = await db.save_post(channel_id, result["text"], media, media_url,
                                 ai_provider=result["provider"], ai_model=result["model"])
    keyboard = [
        [InlineKeyboardButton("📤 Опубликовать", callback_data=f"post_pub|{post_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"post_del|{post_id}")],
        [back_btn(f"ch_open|{channel_id}")],
    ]
    await query.edit_message_text(
        f"🤖 <b>Пост</b> <code>{esc(result['provider'])}/{esc(result['model'])}</code>\n\n---\n{esc(result['text'][:1500])}\n---",
        reply_markup=kb(keyboard), parse_mode="HTML",
    )
    await safe_answer(query)


async def channel_test(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    """Генерирует пост и показывает его в чате, НЕ публикуя."""
    from ai.analyzer import generate_content_items
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("Канал не найден.", reply_markup=kb([[back_btn("menu_channels")]]))
        await safe_answer(query)
        return
    await query.edit_message_text("🧪 Генерирую тестовый пост…")
    try:
        status_msg = await query.message.reply_text("🧠 AI думает…")
    except Exception:  # noqa: BLE001
        status_msg = None

    memory = await db.get_recent_memory(channel_id, hours=48, min_importance=5, limit=8)
    if memory:
        result = await generate_from_memory(memory, ch.get("style_prompt", ""))
    else:
        items = await generate_content_items(ch.get("channel_description", ""), ch.get("style_prompt", ""))
        result = {"text": items[0]["content"] if items else "", "provider": "ai", "model": ""}

    if not result.get("text"):
        msg = "❌ Не удалось сгенерировать (задай описание канала или собери инфу)."
        if status_msg is not None:
            try:
                await status_msg.edit_text(msg)
            except Exception:  # noqa: BLE001
                pass
        await query.edit_message_text(msg, reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]))
        await safe_answer(query)
        return

    text = f"🧪 <b>Тестовый пост</b> (не опубликован)\n\n---\n{esc(result['text'][:1500])}\n---"
    if status_msg is not None:
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
    await query.edit_message_text(text, reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]),
                                  parse_mode="HTML")
    await safe_answer(query)


async def channel_generate_now(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("Канал не найден.", reply_markup=kb([[back_btn("menu_channels")]]))
        return
    try:
        await query.edit_message_text(f"⚡ Генерирую и публикую в «{ch.get('channel_title') or channel_id}»…")
    except Exception:  # noqa: BLE001
        pass
    status_msg = await query.message.reply_text("🧠 AI думает над постом…")
    ok = await scheduler.auto_post_for(ch)
    try:
        await status_msg.edit_text("✅ Пост сгенерирован и опубликован!" if ok else
                                   "❌ Не удалось (нет памяти/черновиков или бот не админ канала).")
    except Exception:  # noqa: BLE001
        pass
    await query.edit_message_text(
        "✅ Готово." if ok else "❌ Не удалось опубликовать.",
        reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]),
    )
    await safe_answer(query)


async def channel_quick_test(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    """Мгновенно шлёт тестовый сообщение в канал (без AI, для проверки связи)."""
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("Канал не найден.", reply_markup=kb([[back_btn("menu_channels")]]))
        await safe_answer(query)
        return
    title = ch.get("channel_title") or channel_id
    try:
        await query.edit_message_text(f"🚀 Отправляю тест-пост в «{title}»…")
    except Exception:
        pass
    test_text = (
        "🧪 Тестовый пост\n\n"
        "Бот работает. Если вы видите это сообщение — всё ок.\n"
        f"{channel_id}"
    )
    ok = await cp.send_post(channel_id, test_text)
    if ok:
        await query.edit_message_text(
            f"✅ Тест-пост доставлен в «{title}»!",
            reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]),
        )
    else:
        await query.edit_message_text(
            f"❌ Не удалось доставить в «{esc(title)}».\n\n"
            "Проверь:\n"
            "• Бот добавлен в канал как администратор?\n"
            "• Права на публикацию постов включены?\n"
            f"• Username/ID канала корректен: <code>{esc(channel_id)}</code>",
            reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]),
            parse_mode="HTML",
        )
    await safe_answer(query)


async def channel_validate(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    """Проверяет доступ бота к каналу и показывает результат."""
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("Канал не найден.", reply_markup=kb([[back_btn("menu_channels")]]))
        await safe_answer(query)
        return
    try:
        await query.edit_message_text("🔍 Проверяю канал…")
    except Exception:
        pass
    valid, info = await cp.validate_channel(channel_id)
    await query.edit_message_text(
        f"<b>{esc(ch.get('channel_title') or channel_id)}</b>\nID: <code>{esc(channel_id)}</code>\n\n{esc(info)}",
        reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]),
        parse_mode="HTML",
    )
    await safe_answer(query)


async def channel_collect(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("Канал не найден.", reply_markup=kb([[back_btn("menu_channels")]]))
        await safe_answer(query)
        return
    desc = (ch.get("channel_description") or "").strip()
    if not desc:
        await query.edit_message_text(
            "❌ Сначала задай описание канала (кнопка «✏️ Описание канала»), "
            "чтобы ИИ знал, что искать.",
            reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]),
        )
        await safe_answer(query)
        return
    await query.edit_message_text("🧠 Генерирую контент по описанию канала… "
                                  "(это может занять 1-2 минуты)")
    status_msg = None
    try:
        status_msg = await query.message.reply_text("⏳ Идёт сбор и анализ контента…")
    except Exception:  # noqa: BLE001
        pass
    stats = await monitor.manual_collect(ch)
    await db.update_web_collect_state(channel_id)
    text = (
        f"🧠 Генерация завершена.\n"
        f"Запросов сгенерировано: {stats.get('queries', 0)}\n"
        f"Сохранено в память: {stats.get('saved', 0)}\n"
        f"Ошибок: {stats.get('errors', 0)}"
    )
    if status_msg is not None:
        try:
            await status_msg.edit_text(text)
        except Exception:  # noqa: BLE001
            pass
    await query.edit_message_text(text, reply_markup=kb([[back_btn(f"ch_open|{channel_id}")]]))
    await safe_answer(query)


async def channel_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    drafts = await db.get_draft_posts(channel_id)
    text = f"*Черновики:* {len(drafts)}\n"
    keyboard = []
    for p in drafts:
        keyboard.append([InlineKeyboardButton(f"📤 {short(p['post_text'], 35)}",
                                              callback_data=f"post_pub|{p['id']}")])
    keyboard.append([back_btn(f"ch_open|{channel_id}")])
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="Markdown")
    await safe_answer(query)


async def post_publish(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: str) -> None:
    query = update.callback_query
    ok = await scheduler.publish_post(int(post_id))
    post = await db.get_post(int(post_id))
    ch_id = post["channel_id"] if post else ""
    if ok:
        await query.edit_message_text("✅ Опубликовано.", reply_markup=kb([[back_btn(f"ch_open|{ch_id}")]]))
    else:
        await query.edit_message_text("❌ Не удалось опубликовать (бот админ канала?).",
                                      reply_markup=kb([[back_btn(f"ch_open|{ch_id}")]]))
    await safe_answer(query)


async def post_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: str) -> None:
    query = update.callback_query
    post = await db.get_post(int(post_id))
    ch_id = post["channel_id"] if post else ""
    await db.delete_post(int(post_id))
    await query.edit_message_text("🗑 Пост удалён.", reply_markup=kb([[back_btn(f"ch_open|{ch_id}")]]))
    await safe_answer(query)


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
    await safe_answer(query)


async def ad_add(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("add_ad", channel_id)
    await query.edit_message_text("✏️ Отправь текст рекламного поста:")
    await safe_answer(query)


async def ad_publish(update: Update, context: ContextTypes.DEFAULT_TYPE, ad_id: str) -> None:
    query = update.callback_query
    row = await db.get_ad(int(ad_id))
    if not row:
        await query.edit_message_text("Реклама не найдена.")
        await safe_answer(query)
        return
    ok = await cp.send_post(row["channel_id"], row["ad_text"], row.get("ad_media_path") or None)
    if ok:
        await db.update_ad_status(row["id"], "published")
        await query.edit_message_text("✅ Реклама опубликована.")
    else:
        await query.edit_message_text("❌ Не опубликована (бот админ канала?).")
    await safe_answer(query)


# ================================================================ сбор инфы
async def menu_collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    channels = await db.get_channels(active_only=False)
    keyboard = []
    for ch in channels:
        title = ch.get("channel_title") or ch["channel_id"]
        has_desc = "✅" if (ch.get("channel_description") or "").strip() else "❌"
        keyboard.append([InlineKeyboardButton(f"🔎 {has_desc} {title}", callback_data=f"ch_collect|{ch['channel_id']}")])
    keyboard.append([InlineKeyboardButton("🧠 Сгенерировать во всех каналах", callback_data="collect_all")])
    keyboard.append([back_btn()])
    await query.edit_message_text(
        "🧠 *Генерация контента*\n\n"
        "Бот по описанию каждого канала сам генерит контент и сохраняет в память.\n"
        "✅ = описание задано, ❌ = нет описания.",
        reply_markup=kb(keyboard), parse_mode="Markdown",
    )
    await safe_answer(query)


async def collect_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.edit_message_text("🧠 Генерирую контент во всех каналах… (может занять несколько минут)")
    try:
        status_msg = await query.message.reply_text("⏳ Идёт сбор…")
    except Exception:  # noqa: BLE001
        status_msg = None
    channels = await db.get_channels(active_only=True)
    lines = []
    for ch in channels:
        name = esc(ch.get('channel_title') or ch['channel_id'])
        desc = (ch.get("channel_description") or "").strip()
        if not desc:
            lines.append(f"— {name}: нет описания, пропущен")
            continue
        try:
            stats = await monitor.manual_collect(ch)
            await db.update_web_collect_state(ch["channel_id"])
            lines.append(f"✅ {name}: +{stats.get('saved', 0)}")
        except Exception as e:  # noqa: BLE001
            logger.warning("Сбор %s: %s", ch["channel_id"], e)
            lines.append(f"❌ {name}: {esc(e)}")
    text = "🧠 <b>Генерация завершена</b>\n\n" + "\n".join(lines)
    if status_msg is not None:
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
    await query.edit_message_text(text, reply_markup=kb([[back_btn("menu_collect")]]), parse_mode="HTML")
    await safe_answer(query)


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
    await query.edit_message_text("🧠 *Память бота*\nКонтент, сгенерированный ИИ.",
                                  reply_markup=kb(keyboard), parse_mode="Markdown")
    await safe_answer(query)


async def mem_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    memory = await db.get_recent_memory(channel_id, hours=24 * 30, min_importance=1)
    total = len(memory)
    topics = {}
    for m in memory:
        topics[m.get("topic") or "Другое"] = topics.get(m.get("topic") or "Другое", 0) + 1
    topics_text = "\n".join(f"  • {t} ({c})" for t, c in list(topics.items())[:5]) or "  нет данных"
    title = ch.get("channel_title", channel_id) if ch else channel_id
    text = (
        f"<b>🧠 Память: {esc(title)}</b>\n"
        f"Записей: {total}\n\n"
        f"<b>Топ тем:</b>\n{esc(topics_text)}"
    )
    keyboard = [
        [InlineKeyboardButton("📋 Записи", callback_data=f"mem_list|{channel_id}")],
        [InlineKeyboardButton("🔍 Поиск", callback_data=f"mem_search|{channel_id}")],
        [InlineKeyboardButton("🔄 Сгенерировать пост", callback_data=f"mem_gen|{channel_id}")],
        [InlineKeyboardButton("📝 По теме", callback_data=f"mem_topic|{channel_id}")],
        [back_btn(f"ch_open|{channel_id}")],
    ]
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="HTML")
    await safe_answer(query)


async def mem_list(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    memory = await db.get_memory(channel_id, limit=10)
    text = f"<b>📋 Записи ({len(memory)}):</b>\n\n"
    for m in memory:
        imp = "🔴" if m["importance"] >= 8 else "🟡" if m["importance"] >= 5 else "⚪"
        text += f"{imp} <b>{esc(m['topic'])}</b> ({m['importance']})\n{esc(short(m['summary'], 90))}\n\n"
    if not memory:
        text = "Пока нет записей."
    keyboard = [[back_btn(f"mem_stats|{channel_id}")]]
    await query.edit_message_text(text, reply_markup=kb(keyboard), parse_mode="HTML")
    await safe_answer(query)


async def mem_search(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("memory_search", channel_id)
    await query.edit_message_text("🔍 Введи ключевое слово:")
    await safe_answer(query)


async def mem_generate(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    ch = await db.get_channel(channel_id)
    await query.edit_message_text("🔄 Генерирую из памяти…")
    memory = await db.get_recent_memory(channel_id, hours=48, min_importance=5, limit=8)
    result = await generate_from_memory(memory, (ch.get("style_prompt") if ch else ""))
    if result.get("text"):
        media = memory[0].get("media_path", "") if memory else ""
        media_url = memory[0].get("media_url", "") if memory else ""
        post_id = await db.save_post(channel_id, result["text"], media, media_url,
                                     ai_provider=result["provider"], ai_model=result["model"])
        keyboard = [
            [InlineKeyboardButton("📤 Опубликовать", callback_data=f"post_pub|{post_id}")],
            [back_btn(f"mem_stats|{channel_id}")],
        ]
        await query.edit_message_text(
            f"🤖 {esc(result['text'][:1500])}\n<code>{esc(result['provider'])}/{esc(result['model'])}</code>",
            reply_markup=kb(keyboard), parse_mode="HTML")
    else:
        await query.edit_message_text(f"❌ {result.get('error', 'Ошибка')}",
                                      reply_markup=kb([[back_btn(f"mem_stats|{channel_id}")]]))
    await safe_answer(query)


async def mem_topic(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("memory_gen_topic", channel_id)
    await query.edit_message_text("📝 Введи тему для поста:")
    await safe_answer(query)


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
    await safe_answer(query)


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
    await safe_answer(query)


async def ai_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_states[query.from_user.id] = ("add_key", None)
    await query.edit_message_text(
        "✏️ Формат: <code>провайдер ключ</code>\n"
        "Провайдеры: " + ", ".join(PROVIDERS) + "\n\n"
        "Можно несколько ключей одного провайдера — будет ротация.",
        parse_mode="HTML",
    )
    await safe_answer(query)


async def ai_del(update: Update, context: ContextTypes.DEFAULT_TYPE, key_id: str) -> None:
    query = update.callback_query
    await db.remove_ai_key(int(key_id))
    await ai_keys_menu(update, context)
    await safe_answer(query)


async def ai_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, key_id: str) -> None:
    query = update.callback_query
    row = next((k for k in await db.list_ai_keys() if k["id"] == int(key_id)), None)
    if row:
        await db.set_ai_key_enabled(int(key_id), not row["enabled"])
    await ai_keys_menu(update, context)
    await safe_answer(query)


async def ai_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.edit_message_text("🧪 Тест ключей… (может занять время)")
    keys = await db.list_ai_keys()
    usable = [k for k in keys if k["enabled"]]
    if not usable:
        await query.edit_message_text("Нет активных ключей.")
        await safe_answer(query)
        return
    ok = 0
    lines = []
    for k in usable:
        okk, info = await providers.get_engine().test_key(k)
        lines.append(f"{'✅' if okk else '❌'} {k['provider']} …{k['api_key'][-6:]}: {short(info, 60)}")
        if okk:
            ok += 1
    await query.edit_message_text(f"Результат: {ok}/{len(usable)}\n\n" + "\n".join(lines),
                                  reply_markup=kb([[back_btn("menu_ai")]]))
    await safe_answer(query)


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
    await safe_answer(query)


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
        await show_main(update.message)
        return

    action, extra = state

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

    if action == "add_channel":
        cid, title = normalize_channel_input(text)
        if not cid:
            await update.message.reply_text(
                "❌ Не удалось разобрать. Введи числовой ID канала или @username "
                "(без полной ссылки). Бот должен быть админом канала.")
            return
        ok = await db.add_channel(cid, title, "", interval=60)
        if ok:
            valid, info = await cp.validate_channel(cid)
            await update.message.reply_text(
                f"✅ Канал <b>{esc(title or cid)}</b> добавлен!\n\n{esc(info)}\n\n"
                "📝 Теперь открой канал и задай описание (о чём он) — по нему бот "
                "сам будет вести канал.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("❌ Уже есть в базе.")
        user_states.pop(user_id, None)
        return

    if action == "set_desc" and extra:
        await db.update_channel(extra, channel_description=text)
        await update.message.reply_text(
            "✅ Описание сохранено.\n\n"
            "Теперь можно нажать «🧠 Сгенерировать контент» — ИИ найдёт контент "
            "по описанию и сохранит в память, а автопостинг будет вести канал.")
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
        import asyncio
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
            msg = f"🔍 <b>«{esc(text)}»</b> ({len(results)})\n\n"
            for m in results[:5]:
                imp = "🔴" if m["importance"] >= 8 else "🟡" if m["importance"] >= 5 else "⚪"
                msg += f"{imp} <b>{esc(m['topic'])}</b> ({m['importance']})\n{esc(short(m['summary'], 90))}\n\n"
        else:
            msg = "Ничего не найдено."
        await update.message.reply_text(msg, parse_mode="HTML")
        user_states.pop(user_id, None)
        return

    if action == "memory_gen_topic" and extra:
        await update.message.reply_text("🔄 Генерирую…")
        ch = await db.get_channel(extra)
        memory = await db.get_recent_memory(extra, hours=48, min_importance=5, limit=8)
        result = await generate_from_memory(memory, (ch.get("style_prompt") if ch else ""), target_topic=text)
        if result.get("text"):
            media = memory[0].get("media_path", "") if memory else ""
            media_url = memory[0].get("media_url", "") if memory else ""
            post_id = await db.save_post(extra, result["text"], media, media_url,
                                         ai_provider=result["provider"], ai_model=result["model"])
            k = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Опубликовать", callback_data=f"post_pub|{post_id}")]])
            await update.message.reply_text(result["text"][:3900], reply_markup=k)
        else:
            await update.message.reply_text(f"❌ {result.get('error', 'Ошибка')}")
        user_states.pop(user_id, None)
        return


def normalize_channel_input(text: str) -> tuple[str, str]:
    """Возвращает (channel_id, title) с учётом @username и числового ID."""
    ref = (text or "").strip()
    if ref.startswith("https://t.me/"):
        ref = ref[len("https://t.me/"):].split("/")[0].split("?")[0]
    elif ref.startswith("t.me/"):
        ref = ref[len("t.me/"):].split("/")[0].split("?")[0]
    if ref.startswith("@"):
        return ref[1:], ""
    if ref.lstrip("-").isdigit():
        return ref, ""
    return ref, ""


# ================================================================ маршрутизация callback
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return
    try:
        await q.answer()
    except Exception:  # noqa: BLE001
        pass
    data = q.data

    if data == "menu_main":
        await show_main(q)
    elif data == "menu_channels":
        await menu_channels(update, context)
    elif data == "menu_memory":
        await menu_memory(update, context)
    elif data == "menu_collect":
        await menu_collect(update, context)
    elif data == "collect_all":
        await collect_all(update, context)
    elif data == "menu_ai":
        await menu_ai(update, context)
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
    elif data.startswith("ch_open|"):
        await channel_open(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_del|"):
        await channel_delete(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_toggle|"):
        await channel_toggle(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_desc|"):
        await channel_description_prompt(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_style|"):
        await channel_style(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_interval|"):
        await channel_interval(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_gen|"):
        await channel_generate(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_test|"):
        await channel_test(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_now|"):
        await channel_generate_now(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_quick_test|"):
        await channel_quick_test(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_validate|"):
        await channel_validate(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_collect|"):
        await channel_collect(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_posts|"):
        await channel_posts(update, context, data.split("|", 1)[1])
    elif data.startswith("ch_ads|"):
        await channel_ads(update, context, data.split("|", 1)[1])
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


def register(app) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

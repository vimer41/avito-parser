import asyncio
import html
import logging
import re
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database
from config import MAX_SEARCHES_PER_USER
from parser import AvitoParser

logger = logging.getLogger(__name__)
router = Router()


class SearchStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_stop_word = State()
    waiting_for_template = State()


# ── Добавление радара ──────────────────────────────────────────────────

@router.message(F.text.in_({"🎯 Добавить радар", "➕ Добавить радар", "➕ Добавить поиск"}))
@router.callback_query(F.data == "add_search_prompt")
async def btn_add_search(event, state: FSMContext):
    if isinstance(event, CallbackQuery):
        message = event.message
        await event.answer()
    else:
        message = event

    await state.set_state(SearchStates.waiting_for_url)
    text = (
        "🎯 <b>НАСТРОЙКА НОВОГО РАДАРА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Отправьте в чат ссылку с настроенным поиском на Авито:\n\n"
        "1. Откройте <b>avito.ru</b> в браузере (Chrome, Safari, Яндекс).\n"
        "2. Введите название нужного товара (например, <i>PlayStation 5</i>).\n"
        "3. Выставите фильтры: город, диапазон цены, состояние.\n"
        "4. Скопируйте ссылку из адресной строки и отправьте её сюда.\n\n"
        "<i>Пример корректной ссылки:\n"
        "<code>https://www.avito.ru/moskva/tovary_dlya_kompyutera/...</code></i>"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)avito\.ru"))
@router.message(SearchStates.waiting_for_url)
async def process_avito_url(message: Message, state: FSMContext):
    raw_url = message.text.strip()
    user_id = message.from_user.id

    # Проверяем, не на одно ли объявление ссылка
    if "/item/" in raw_url or re.search(r"_\d{8,}", raw_url):
        await message.answer(
            "⚠️ <b>Это ссылка на конкретное объявление, а не на каталог!</b>\n\n"
            "Чтобы радар ловил все новые предложения, отправьте ссылку со <b>страницы поиска / каталога</b>.",
            parse_mode="HTML"
        )
        return

    # Проверяем лимит поисков
    current_count = await database.count_user_searches(user_id)
    if current_count >= MAX_SEARCHES_PER_USER:
        await message.answer(
            f"⚠️ <b>Достигнут лимит радаров ({MAX_SEARCHES_PER_USER})!</b>\n\n"
            "Удалите ненужный радар в меню <b>📁 Мои радары</b>, чтобы освободить слот.",
            parse_mode="HTML"
        )
        await state.clear()
        return

    # Нормализуем ссылку
    try:
        normalized_url = AvitoParser.normalize_url(raw_url)
    except Exception as e:
        await message.answer(f"❌ Ошибка в ссылке: {e}\nУбедитесь, что ссылка ведёт на avito.ru.")
        return

    status_msg = await message.answer(
        "⏳ <b>Подключаюсь к Авито и калибрую радар...</b>",
        parse_mode="HTML"
    )

    parser = AvitoParser()
    html_content = await asyncio.to_thread(parser.fetch_page, normalized_url)

    if not html_content:
        await status_msg.edit_text(
            "❌ <b>Не удалось подключиться к странице Авито.</b>\n\n"
            "Возможно, ссылка недействительна или сработала защита сайта. Попробуйте еще раз через минуту.",
            parse_mode="HTML"
        )
        return

    search_title = AvitoParser.extract_search_title(html_content, normalized_url)
    items = AvitoParser.parse_items(html_content)

    # Сохраняем в БД
    search_id = await database.add_search(user_id, search_title, normalized_url)

    # Запоминаем текущие объявления, чтобы не спамить старыми
    item_ids = [it.item_id for it in items]
    if item_ids:
        await database.mark_items_seen_bulk(search_id, item_ids)

    await state.clear()

    clean_title = html.escape(search_title)
    text = (
        "┌ 🎯 <b>РАДАР УСПЕШНО АКТИВИРОВАН!</b>\n"
        f"├ 📌 <b>Цель:</b> <b>{clean_title}</b>\n"
        f"├ 📦 <b>Существующих объявлений:</b> <b>{len(items)}</b> (пропущены)\n"
        f"├ 📡 <b>Статус:</b> 🟢 Мониторинг 24/7 активен\n"
        f"└ ⚡️ <b>Частота сканирования:</b> каждые ~15 секунд\n\n"
        "<i>Как только появится свежий лот — бот пришлёт фото в HD, цену, контакты и готовый отклик продавцу!</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚫 Стоп-слова", callback_data=f"manage_stops:{search_id}"),
            InlineKeyboardButton(text="💬 Свой отклик", callback_data=f"manage_tmpl:{search_id}")
        ],
        [
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches"),
            InlineKeyboardButton(text="↗️ Открыть на Авито", url=normalized_url)
        ]
    ])

    await status_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ── Список радаров ─────────────────────────────────────────────────────

@router.message(F.text.in_({"📁 Мои радары", "📂 Мои радары", "📋 Мои поиски", "Мои радары"}))
@router.callback_query(F.data == "list_searches")
async def show_searches(event, state: FSMContext):
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
        await event.answer()
    else:
        message = event
        user_id = message.from_user.id

    await state.clear()
    searches = await database.get_user_searches(user_id)

    if not searches:
        text = (
            "📁 <b>У вас пока нет активных радаров</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Вы можете отслеживать до <b>10 разных категорий</b> одновременно.\n\n"
            "Нажмите кнопку ниже или просто отправьте ссылку на поиск с Авито в этот чат!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Добавить первый радар", callback_data="add_search_prompt")]
        ])
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    active_cnt = sum(1 for s in searches if s.get("is_active"))
    header_text = (
        f"📁 <b>ВАШИ РАДАРЫ</b> (Всего: <b>{len(searches)}</b> · Активно: <b>{active_cnt}</b> 🟢)\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await message.answer(header_text, parse_mode="HTML")

    for i, s in enumerate(searches, 1):
        s_id = s["id"]
        title = html.escape(s["title"])
        is_active = bool(s["is_active"])
        status_icon = "🟢 <b>Активен (24/7)</b>" if is_active else "⏸ <b>На паузе</b>"
        stops_count = len(s["stop_words"])
        tmpl_status = "индивидуальный" if s["template"] else "общий"

        card_text = (
            f"╭─ 🎯 <b>Радар #{i}: {title}</b>\n"
            f"│ 📡 <b>Статус:</b> {status_icon}\n"
            f"│ 🛡 <b>Стоп-слов:</b> <code>{stops_count}</code>\n"
            f"╰ 💬 <b>Отклик:</b> <i>{tmpl_status}</i>"
        )

        toggle_btn_text = "⏸ На паузу" if is_active else "▶️ Запустить"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=toggle_btn_text, callback_data=f"toggle_search:{s_id}"),
                InlineKeyboardButton(text=f"🛡 Стоп-слова ({stops_count})", callback_data=f"manage_stops:{s_id}")
            ],
            [
                InlineKeyboardButton(text="💬 Текст отклика", callback_data=f"manage_tmpl:{s_id}"),
                InlineKeyboardButton(text="↗️ На Авито", url=s["url"])
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить радар", callback_data=f"delete_search:{s_id}")
            ]
        ])

        await message.answer(card_text, reply_markup=kb, parse_mode="HTML")


# ── Пауза / Возобновление ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("toggle_search:"))
async def cb_toggle_search(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    new_status = await database.toggle_search_active(search_id, call.from_user.id)

    if new_status is None:
        await call.answer("Радар не найден", show_alert=True)
        return

    status_str = "возобновлён 🟢" if new_status else "приостановлен ⏸"
    await call.answer(f"Радар {status_str}!")

    s = await database.get_search(search_id)
    if not s:
        return

    title = html.escape(s["title"])
    is_active = bool(s["is_active"])
    status_icon = "🟢 <b>Активен (24/7)</b>" if is_active else "⏸ <b>На паузе</b>"
    stops_count = len(s["stop_words"])
    tmpl_status = "индивидуальный" if s["template"] else "общий"

    card_text = (
        f"╭─ 🎯 <b>Радар: {title}</b>\n"
        f"│ 📡 <b>Статус:</b> {status_icon}\n"
        f"│ 🛡 <b>Стоп-слов:</b> <code>{stops_count}</code>\n"
        f"╰ 💬 <b>Отклик:</b> <i>{tmpl_status}</i>"
    )

    toggle_btn_text = "⏸ На паузу" if is_active else "▶️ Запустить"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=toggle_btn_text, callback_data=f"toggle_search:{search_id}"),
            InlineKeyboardButton(text=f"🛡 Стоп-слова ({stops_count})", callback_data=f"manage_stops:{search_id}")
        ],
        [
            InlineKeyboardButton(text="💬 Текст отклика", callback_data=f"manage_tmpl:{search_id}"),
            InlineKeyboardButton(text="↗️ На Авито", url=s["url"])
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить радар", callback_data=f"delete_search:{search_id}")
        ]
    ])

    try:
        await call.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


# ── Удаление поиска ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("delete_search:"))
async def cb_delete_search(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    title = html.escape(s["title"]) if s else "радар"

    text = (
        f"⚠️ <b>Удаление радара «{title}»</b>\n\n"
        "Вы уверены, что хотите остановить и безвозвратно удалить этот радар?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"confirm_delete:{search_id}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"cancel_delete:{search_id}")
        ]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("confirm_delete:"))
async def cb_confirm_delete(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    await database.delete_search(search_id, call.from_user.id)
    await call.message.edit_text("🗑 <b>Радар успешно удалён!</b>", parse_mode="HTML")
    await call.answer("Удалено")


@router.callback_query(F.data.startswith("cancel_delete:"))
async def cb_cancel_delete(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s:
        await call.message.delete()
        return

    title = html.escape(s["title"])
    is_active = bool(s["is_active"])
    status_icon = "🟢 <b>Активен (24/7)</b>" if is_active else "⏸ <b>На паузе</b>"
    stops_count = len(s["stop_words"])
    tmpl_status = "индивидуальный" if s["template"] else "общий"

    card_text = (
        f"╭─ 🎯 <b>Радар: {title}</b>\n"
        f"│ 📡 <b>Статус:</b> {status_icon}\n"
        f"│ 🛡 <b>Стоп-слов:</b> <code>{stops_count}</code>\n"
        f"╰ 💬 <b>Отклик:</b> <i>{tmpl_status}</i>"
    )

    toggle_btn_text = "⏸ На паузу" if is_active else "▶️ Запустить"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=toggle_btn_text, callback_data=f"toggle_search:{search_id}"),
            InlineKeyboardButton(text=f"🛡 Стоп-слова ({stops_count})", callback_data=f"manage_stops:{search_id}")
        ],
        [
            InlineKeyboardButton(text="💬 Текст отклика", callback_data=f"manage_tmpl:{search_id}"),
            InlineKeyboardButton(text="↗️ На Авито", url=s["url"])
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить радар", callback_data=f"delete_search:{search_id}")
        ]
    ])
    await call.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ── Управление стоп-словами ────────────────────────────────────────────

@router.callback_query(F.data.startswith("manage_stops:"))
async def cb_manage_stops(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s:
        await call.answer("Радар не найден", show_alert=True)
        return

    stop_words = s["stop_words"]
    title = html.escape(s["title"])

    lines = [
        f"🛡 <b>ФИЛЬТР СТОП-СЛОВ</b>\n"
        f"🎯 Радар: <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    ]
    if stop_words:
        lines.append("<i>Лоты, содержащие эти слова в названии или описании, будут отсекаться:</i>\n")
        for idx, w in enumerate(stop_words, 1):
            lines.append(f"• <code>{html.escape(w)}</code>")
    else:
        lines.append("<i>Стоп-слова пока не заданы. Примеры: «на запчасти», «реплика», «дефект», «копия»</i>")

    buttons = []
    # Кнопки быстрого удаления по 2 в ряд
    current_row = []
    for w in stop_words:
        btn = InlineKeyboardButton(text=f"❌ {w}", callback_data=f"rm_stop:{search_id}:{w}")
        current_row.append(btn)
        if len(current_row) == 2:
            buttons.append(current_row)
            current_row = []
    if current_row:
        buttons.append(current_row)

    buttons.append([InlineKeyboardButton(text="➕ Добавить стоп-слово", callback_data=f"add_stop:{search_id}")])
    buttons.append([InlineKeyboardButton(text="📁 К списку радаров", callback_data="list_searches")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("add_stop:"))
@router.callback_query(F.data.startswith("add_stop_from_item:"))
async def cb_add_stop_word_prompt(call: CallbackQuery, state: FSMContext):
    search_id = int(call.data.split(":")[1])
    await state.set_state(SearchStates.waiting_for_stop_word)
    await state.update_data(search_id=search_id)

    await call.message.answer(
        "✍️ <b>Введите стоп-слово или фразу:</b>\n\n"
        "<i>Например:\n"
        "<code>на запчасти, копия, без комплекта</code>\n"
        "(Можно прислать несколько слов через запятую)</i>",
        parse_mode="HTML"
    )
    await call.answer()


@router.message(SearchStates.waiting_for_stop_word)
async def process_stop_word_input(message: Message, state: FSMContext):
    data = await state.get_data()
    search_id = data.get("search_id")
    raw_text = message.text.strip()

    words = [w.strip() for w in raw_text.split(",") if w.strip()]
    added = []
    for w in words:
        if await database.add_stop_word_to_search(search_id, w):
            added.append(w)

    await state.clear()
    added_str = ", ".join(f"«{html.escape(w)}»" for w in added)
    await message.answer(
        f"✅ <b>Стоп-слова успешно добавлены:</b> {added_str}\n\n"
        "Объявления с ними больше не будут вас беспокоить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛡 К списку стоп-слов", callback_data=f"manage_stops:{search_id}")],
            [InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")]
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("rm_stop:"))
async def cb_remove_stop_word(call: CallbackQuery):
    parts = call.data.split(":", 2)
    search_id = int(parts[1])
    word = parts[2]

    await database.remove_stop_word_from_search(search_id, word)
    await call.answer(f"Слово «{word}» удалено")
    await cb_manage_stops(call)


# ── Управление шаблоном сообщения для конкретного радара ─────────────

@router.callback_query(F.data.startswith("manage_tmpl:"))
async def cb_manage_tmpl(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s:
        await call.answer("Радар не найден", show_alert=True)
        return

    user = await database.get_or_create_user(call.from_user.id)
    default_tmpl = user.get("default_template") or database.DEFAULT_SELLER_MESSAGE
    current_tmpl = s.get("template") or f"{default_tmpl} (общий профиля)"

    text = (
        f"💬 <b>ШАБЛОН ОТКЛИКА ДЛЯ РАДАРА</b>\n"
        f"🎯 Радар: <b>{html.escape(s['title'])}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>{html.escape(current_tmpl)}</code>\n\n"
        "Этот текст будет копироваться по кнопке <b>«📋 Скопировать отклик»</b> под каждым найденным товаром в этом радаре."
    )

    buttons = [
        [InlineKeyboardButton(text="✏️ Задать свой текст", callback_data=f"set_tmpl:{search_id}")],
    ]
    if s.get("template"):
        buttons.append([InlineKeyboardButton(text="🔄 Сбросить на общий профиля", callback_data=f"reset_tmpl:{search_id}")])

    buttons.append([InlineKeyboardButton(text="📁 К списку радаров", callback_data="list_searches")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("set_tmpl:"))
async def cb_set_tmpl_prompt(call: CallbackQuery, state: FSMContext):
    search_id = int(call.data.split(":")[1])
    await state.set_state(SearchStates.waiting_for_template)
    await state.update_data(search_id=search_id)

    await call.message.answer(
        "✍️ <b>Введите персональный текст сообщения продавцу для этого радара:</b>\n\n"
        "<i>Пример:\n"
        "Здравствуйте! Интересует данный лот. Готов забрать самовывозом прямо сейчас, оплата наличными.</i>",
        parse_mode="HTML"
    )
    await call.answer()


@router.message(SearchStates.waiting_for_template)
async def process_template_input(message: Message, state: FSMContext):
    data = await state.get_data()
    search_id = data.get("search_id")
    text = message.text.strip()

    await database.update_search_template(search_id, message.from_user.id, text)
    await state.clear()

    await message.answer(
        "✅ <b>Индивидуальный отклик для радара сохранён!</b>\n\n"
        f"<code>{html.escape(text)}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📁 К списку радаров", callback_data="list_searches")]
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("reset_tmpl:"))
async def cb_reset_tmpl(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    await database.update_search_template(search_id, call.from_user.id, None)
    await call.answer("Сброшено на общий шаблон")
    await cb_manage_tmpl(call)


# ── Кнопка «Скопировать сообщение» под уведомлением ────────────────────

@router.callback_query(F.data.startswith("copy_msg:"))
async def cb_copy_message(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)

    tmpl = None
    if s and s.get("template"):
        tmpl = s["template"]
    else:
        user = await database.get_or_create_user(call.from_user.id)
        tmpl = user.get("default_template") or database.DEFAULT_SELLER_MESSAGE

    text = (
        "📋 <b>Сообщение для продавца</b> (коснитесь текста для копирования):\n\n"
        f"<code>{html.escape(tmpl)}</code>"
    )
    await call.message.answer(text, parse_mode="HTML")
    await call.answer("Текст скопирован!")

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


# ── Добавление поиска ──────────────────────────────────────────────────

@router.message(F.text == "➕ Добавить поиск")
async def btn_add_search(message: Message, state: FSMContext):
    await state.set_state(SearchStates.waiting_for_url)
    text = (
        "🔗 <b>Пришлите ссылку на поиск с Авито:</b>\n\n"
        "1. Откройте <code>avito.ru</code> в браузере.\n"
        "2. Настройте фильтры (город, цену, модель и т.д.).\n"
        "3. Скопируйте адресную строку и отправьте сюда.\n\n"
        "<i>Пример ссылки:\n"
        "https://www.avito.ru/moskva/telefony/apple/iphone_15...</i>"
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
            "⚠️ <b>Это ссылка на конкретное объявление, а не на поиск!</b>\n\n"
            "Чтобы бот мог отслеживать появление новых товаров, скопируйте ссылку со <b>страницы со списком объявлений</b> (с результатами поиска).",
            parse_mode="HTML"
        )
        return

    # Проверяем лимит поисков
    current_count = await database.count_user_searches(user_id)
    if current_count >= MAX_SEARCHES_PER_USER:
        await message.answer(
            f"⚠️ У вас уже создано максимальное количество поисков ({MAX_SEARCHES_PER_USER}).\n"
            "Удалите ненужный поиск в <b>📋 Мои поиски</b>, чтобы добавить новый.",
            parse_mode="HTML"
        )
        await state.clear()
        return

    # Нормализуем ссылку
    try:
        normalized_url = AvitoParser.normalize_url(raw_url)
    except Exception as e:
        await message.answer(f"❌ Ошибка в ссылке: {e}\nУбедитесь, что ссылка ведет на avito.ru.")
        return

    status_msg = await message.answer("⏳ <b>Проверяю ссылку и настраиваю мониторинг...</b>", parse_mode="HTML")

    parser = AvitoParser()
    html_content = await asyncio.to_thread(parser.fetch_page, normalized_url)

    if not html_content:
        await status_msg.edit_text(
            "❌ <b>Не удалось загрузить страницу Авито.</b>\n\n"
            "Возможно, ссылка недействительна или на сайте временные сбои. Попробуйте еще раз через минуту.",
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

    text = (
        "✅ <b>Поиск успешно добавлен!</b>\n\n"
        f"🔍 Название: <b>{html.escape(search_title)}</b>\n"
        f"📦 Найдено текущих объявлений: <b>{len(items)}</b> (они не будут присылаться)\n"
        "⚡️ <b>Мониторинг запущен 24/7!</b> Как только продавец опубликует новый товар — вы сразу получите уведомление."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚫 Стоп-слова", callback_data=f"manage_stops:{search_id}"),
            InlineKeyboardButton(text="📝 Шаблон сообщения", callback_data=f"manage_tmpl:{search_id}")
        ],
        [
            InlineKeyboardButton(text="📋 Мои поиски", callback_data="list_searches"),
            InlineKeyboardButton(text="🗑 Удалить поиск", callback_data=f"delete_search:{search_id}")
        ]
    ])

    await status_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ── Список поисков ─────────────────────────────────────────────────────

@router.message(F.text == "📋 Мои поиски")
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
            "📋 <b>У вас пока нет активных поисков.</b>\n\n"
            "Нажмите <b>➕ Добавить поиск</b> или просто отправьте ссылку на поиск с Авито!"
        )
        await message.answer(text, parse_mode="HTML")
        return

    await message.answer(f"📋 <b>Ваши поиски (всего: {len(searches)}):</b>", parse_mode="HTML")

    for s in searches:
        s_id = s["id"]
        title = html.escape(s["title"])
        status_icon = "🟢 Мониторится" if s["is_active"] else "⏸ На паузе"
        stops_count = len(s["stop_words"])
        tmpl_status = "индивидуальный" if s["template"] else "по умолчанию"

        text = (
            f"🔍 <b>{title}</b>\n"
            f"Статус: {status_icon}\n"
            f"🚫 Стоп-слов: <b>{stops_count}</b>\n"
            f"📝 Шаблон: <b>{tmpl_status}</b>"
        )

        toggle_text = "⏸ Пауза" if s["is_active"] else "▶️ Запустить"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=toggle_text, callback_data=f"toggle_search:{s_id}"),
                InlineKeyboardButton(text=f"🚫 Стоп-слова ({stops_count})", callback_data=f"manage_stops:{s_id}")
            ],
            [
                InlineKeyboardButton(text="📝 Шаблон", callback_data=f"manage_tmpl:{s_id}"),
                InlineKeyboardButton(text="↗ Ссылка", url=s["url"])
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить поиск", callback_data=f"delete_search:{s_id}")
            ]
        ])

        await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ── Пауза / Возобновление ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("toggle_search:"))
async def cb_toggle_search(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    new_status = await database.toggle_search_active(search_id, call.from_user.id)

    if new_status is None:
        await call.answer("Поиск не найден", show_alert=True)
        return

    status_str = "🟢 возобновлен" if new_status else "⏸ приостановлен"
    await call.answer(f"Мониторинг {status_str}!")

    # Обновляем сообщение
    s = await database.get_search(search_id)
    if not s:
        return

    title = html.escape(s["title"])
    status_icon = "🟢 Мониторится" if s["is_active"] else "⏸ На паузе"
    stops_count = len(s["stop_words"])
    tmpl_status = "индивидуальный" if s["template"] else "по умолчанию"

    text = (
        f"🔍 <b>{title}</b>\n"
        f"Статус: {status_icon}\n"
        f"🚫 Стоп-слов: <b>{stops_count}</b>\n"
        f"📝 Шаблон: <b>{tmpl_status}</b>"
    )

    toggle_text = "⏸ Пауза" if s["is_active"] else "▶️ Запустить"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=toggle_text, callback_data=f"toggle_search:{search_id}"),
            InlineKeyboardButton(text=f"🚫 Стоп-слова ({stops_count})", callback_data=f"manage_stops:{search_id}")
        ],
        [
            InlineKeyboardButton(text="📝 Шаблон", callback_data=f"manage_tmpl:{search_id}"),
            InlineKeyboardButton(text="↗ Ссылка", url=s["url"])
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить поиск", callback_data=f"delete_search:{search_id}")
        ]
    ])

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


# ── Удаление поиска ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("delete_search:"))
async def cb_delete_search(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚠️ Да, удалить", callback_data=f"confirm_delete:{search_id}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"cancel_delete:{search_id}")
        ]
    ])
    await call.message.edit_text("❓ <b>Вы уверены, что хотите удалить этот поиск?</b>", reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("confirm_delete:"))
async def cb_confirm_delete(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    await database.delete_search(search_id, call.from_user.id)
    await call.message.edit_text("🗑 <b>Поиск успешно удален!</b>", parse_mode="HTML")
    await call.answer("Удалено")


@router.callback_query(F.data.startswith("cancel_delete:"))
async def cb_cancel_delete(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s:
        await call.message.delete()
        return

    title = html.escape(s["title"])
    status_icon = "🟢 Мониторится" if s["is_active"] else "⏸ На паузе"
    stops_count = len(s["stop_words"])
    tmpl_status = "индивидуальный" if s["template"] else "по умолчанию"

    text = (
        f"🔍 <b>{title}</b>\n"
        f"Статус: {status_icon}\n"
        f"🚫 Стоп-слов: <b>{stops_count}</b>\n"
        f"📝 Шаблон: <b>{tmpl_status}</b>"
    )
    toggle_text = "⏸ Пауза" if s["is_active"] else "▶️ Запустить"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=toggle_text, callback_data=f"toggle_search:{search_id}"),
            InlineKeyboardButton(text=f"🚫 Стоп-слова ({stops_count})", callback_data=f"manage_stops:{search_id}")
        ],
        [
            InlineKeyboardButton(text="📝 Шаблон", callback_data=f"manage_tmpl:{search_id}"),
            InlineKeyboardButton(text="↗ Ссылка", url=s["url"])
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить поиск", callback_data=f"delete_search:{search_id}")
        ]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ── Управление стоп-словами ────────────────────────────────────────────

@router.callback_query(F.data.startswith("manage_stops:"))
async def cb_manage_stops(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s:
        await call.answer("Поиск не найден", show_alert=True)
        return

    stop_words = s["stop_words"]
    title = html.escape(s["title"])

    lines = [f"🚫 <b>Стоп-слова для поиска «{title}»:</b>\n"]
    if stop_words:
        lines.append("<i>Объявления с этими словами в названии или описании будут пропускаться:</i>")
        for idx, w in enumerate(stop_words, 1):
            lines.append(f"{idx}. <code>{html.escape(w)}</code>")
    else:
        lines.append("<i>Стоп-слова пока не добавлены. Например: на запчасти, не работает, копия</i>")

    buttons = []
    # Кнопки для удаления каждого слова
    for w in stop_words:
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить «{w}»", callback_data=f"rm_stop:{search_id}:{w}")])

    buttons.append([InlineKeyboardButton(text="➕ Добавить стоп-слово", callback_data=f"add_stop:{search_id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к поискам", callback_data="list_searches")])

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
        "✍️ <b>Отправьте стоп-слово или фразу сообщением:</b>\n\n"
        "<i>Например: на запчасти\n"
        "(Можно прислать несколько слов через запятую)</i>",
        parse_mode="HTML"
    )
    await call.answer()


@router.message(SearchStates.waiting_for_stop_word)
async def process_stop_word_input(message: Message, state: FSMContext):
    data = await state.get_data()
    search_id = data.get("search_id")
    raw_text = message.text.strip()

    # Поддерживаем добавление через запятую
    words = [w.strip() for w in raw_text.split(",") if w.strip()]
    added = []
    for w in words:
        if await database.add_stop_word_to_search(search_id, w):
            added.append(w)

    await state.clear()
    added_str = ", ".join(f"«{html.escape(w)}»" for w in added)
    await message.answer(
        f"✅ <b>Стоп-слова успешно добавлены:</b> {added_str}\n\n"
        "Объявления с этими словами больше не будут вас беспокоить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 К списку стоп-слов", callback_data=f"manage_stops:{search_id}")],
            [InlineKeyboardButton(text="📋 Мои поиски", callback_data="list_searches")]
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

    # Обновляем меню стоп-слов
    await cb_manage_stops(call)


# ── Управление шаблоном сообщения ─────────────────────────────────────

@router.callback_query(F.data.startswith("manage_tmpl:"))
async def cb_manage_tmpl(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s:
        await call.answer("Поиск не найден", show_alert=True)
        return

    user = await database.get_or_create_user(call.from_user.id)
    default_tmpl = user.get("default_template") or database.DEFAULT_SELLER_MESSAGE
    current_tmpl = s.get("template") or f"{default_tmpl} (по умолчанию)"

    text = (
        f"📝 <b>Шаблон для поиска «{html.escape(s['title'])}»:</b>\n\n"
        f"<code>{html.escape(current_tmpl)}</code>\n\n"
        "Этот текст мгновенно отправляется при нажатии кнопки <b>«📋 Скопировать сообщение»</b> под уведомлением."
    )

    buttons = [
        [InlineKeyboardButton(text="✏️ Задать свой текст", callback_data=f"set_tmpl:{search_id}")],
    ]
    if s.get("template"):
        buttons.append([InlineKeyboardButton(text="🔄 Сбросить на стандартный", callback_data=f"reset_tmpl:{search_id}")])

    buttons.append([InlineKeyboardButton(text="⬅️ Назад к поискам", callback_data="list_searches")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("set_tmpl:"))
async def cb_set_tmpl_prompt(call: CallbackQuery, state: FSMContext):
    search_id = int(call.data.split(":")[1])
    await state.set_state(SearchStates.waiting_for_template)
    await state.update_data(search_id=search_id)

    await call.message.answer(
        "✍️ <b>Отправьте текст сообщения для продавца:</b>\n\n"
        "<i>Например: Здравствуйте! Торг уместен? Готов забрать самовывозом прямо сейчас.</i>",
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
        "✅ <b>Шаблон для этого поиска сохранен!</b>\n\n"
        f"<code>{html.escape(text)}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои поиски", callback_data="list_searches")]
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("reset_tmpl:"))
async def cb_reset_tmpl(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    await database.update_search_template(search_id, call.from_user.id, None)
    await call.answer("Сброшено на стандартный шаблон")
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
        "📋 <b>Текст для продавца</b> (нажмите, чтобы скопировать):\n\n"
        f"<code>{html.escape(tmpl)}</code>"
    )
    await call.message.answer(text, parse_mode="HTML")
    await call.answer("Текст готов к копированию!")

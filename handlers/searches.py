import asyncio
import html
import logging
import re
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from typing import Optional, Tuple, List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import config
import database
from config import MAX_SEARCHES_PER_USER, ADMIN_IDS
from parser import AvitoParser
from logger_setup import log_user_event

logger = logging.getLogger(__name__)
router = Router()


class SearchStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_stop_word = State()
    waiting_for_custom_discount = State()


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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm"),
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")
        ]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")



@router.callback_query(F.data == "cancel_fsm")
async def cb_cancel_fsm(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ <b>Действие отменено.</b>", parse_mode="HTML")
    await call.answer()


@router.message(F.text.regexp(r"(?i)avito\.ru"))
@router.message(SearchStates.waiting_for_url)
async def process_avito_url(message: Message, state: FSMContext):
    raw_url = message.text.strip()
    user_id = message.from_user.id

    menu_buttons = {
        "🎯 Добавить радар", "➕ Добавить радар", "➕ Добавить поиск",
        "📁 Мои радары", "📂 Мои радары", "📋 Мои поиски", "Мои радары",
        "💳 Тарифы", "💳 Тарифы и подписка", "Тарифы", "Подписка", "💳 Подписка",
        "⚡️ Быстрый отклик", "💬 Быстрый отклик", "⚙️ Шаблон по умолчанию",
        "👤 Мой профиль", "📊 Мой статус", "Мой профиль",
        "📖 Инструкция", "❓ Помощь", "Инструкция",
        "👑 Админ-панель", "👑 Админка",
        "❌ Отмена", "Отмена"
    }

    if raw_url in menu_buttons or raw_url.startswith("/"):
        await state.clear()
        if raw_url in {"📁 Мои радары", "📂 Мои радары", "📋 Мои поиски", "Мои радары"}:
            return await show_searches(message, state)
        if raw_url in {"🎯 Добавить радар", "➕ Добавить радар"}:
            return await btn_add_search(message, state)
        if raw_url in {"💳 Тарифы", "💳 Тарифы и подписка", "Тарифы", "Подписка", "💳 Подписка"}:
            from handlers.common import cmd_subscription_menu
            return await cmd_subscription_menu(message)
        return

    # Проверяем, что ссылка действительно с avito.ru
    if "avito.ru" not in raw_url.lower():
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", f"Ошибка: ссылка не с avito.ru («{raw_url[:80]}»)", status="WARN")
        await message.answer(
            "⚠️ <b>Ссылка должна быть с сайта avito.ru!</b>\n\n"
            "Пример корректной ссылки:\n"
            "<code>https://www.avito.ru/moskva/telefony/apple...</code>\n\n"
            "<i>Если передумали добавлять радар — нажмите кнопку отмены:</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить добавление", callback_data="cancel_fsm")],
                [InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")]
            ]),
            parse_mode="HTML"
        )
        return

    # Проверяем, не на одно ли объявление ссылка
    if "/item/" in raw_url or re.search(r"_\d{8,}", raw_url):
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", "Ошибка: ссылка на объявление (/item/), а не на поиск", status="WARN")
        await message.answer(
            "⚠️ <b>Это ссылка на конкретное объявление, а не на каталог!</b>\n\n"
            "Чтобы радар ловил все новые предложения, отправьте ссылку со <b>страницы поиска / каталога</b>.",
            parse_mode="HTML"
        )
        return

    # Проверяем лимит поисков по тарифу
    current_count = await database.count_user_searches(user_id)
    max_radars = await database.get_user_max_radars(user_id)
    if current_count >= max_radars:
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", f"Ошибка: лимит {max_radars} исчерпан", status="WARN")
        await message.answer(
            f"⚠️ <b>Достигнут лимит активных радаров ({max_radars})!</b>\n\n"
            "Вы можете освободить место в меню <b>📁 Мои радары</b> или расширить лимит до 5 или 15 радаров в меню <b>💳 Тарифы</b>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Расширить лимит (Тарифы)", callback_data="open_subscription_menu")],
                [InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")]
            ]),
            parse_mode="HTML"
        )
        await state.clear()
        return

    # Нормализуем ссылку
    try:
        normalized_url = AvitoParser.normalize_url(raw_url)
    except Exception as e:
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", f"Ошибка синтаксиса ссылки: {e}", status="WARN")
        await message.answer(f"❌ Ошибка в ссылке: {e}\nУбедитесь, что ссылка ведёт на avito.ru.")
        return


    status_msg = await message.answer(
        "⏳ <b>Подключаюсь к Авито и калибрую радар...</b>",
        parse_mode="HTML"
    )

    parser = AvitoParser()
    html_content = await asyncio.to_thread(parser.fetch_page, normalized_url)

    if not html_content:
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Калибровка радара", f"Ошибка: Авито не вернул HTML («{normalized_url[:80]}»)", status="ERROR")
        await status_msg.edit_text(
            "❌ <b>Не удалось подключиться к странице Авито.</b>\n\n"
            "Возможно, ссылка недействительна или сработала защита сайта. Попробуйте еще раз через минуту.",
            parse_mode="HTML"
        )
        return

    search_title = AvitoParser.extract_search_title(html_content, normalized_url)
    items = AvitoParser.parse_items(html_content)

    # Определяем область выборки: если в ссылке есть bt=1 — поиск строго по названиям
    is_title_only = 1 if "bt=1" in normalized_url else 0

    # Сохраняем в БД
    search_id = await database.add_search(user_id, search_title, normalized_url, search_in_title_only=is_title_only)

    # Запоминаем текущие объявления, чтобы не спамить старыми
    item_ids = [it.item_id for it in items]
    if item_ids:
        await database.mark_items_seen_bulk(search_id, item_ids)

    log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Радар создан", f"Радар #{search_id} «{search_title}» (лотов: {len(items)})", status="OK")
    logger.info(f"🎯 [РАДАР] Пользователь ID:{user_id} создал радар #{search_id}: «{search_title}» ({len(items)} базовых лотов проиндексировано)")

    await state.clear()


    clean_title = html.escape(search_title)
    scope_icon = "🏷 Только в названиях" if is_title_only else "📄 Название + описание"
    scope_btn = "🔍 Выборка: Только в названиях 🏷" if is_title_only else "🔍 Выборка: Название + описание 📄"
    text = (
        "┌ 🎯 <b>РАДАР УСПЕШНО АКТИВИРОВАН!</b>\n"
        f"├ 📌 <b>Цель:</b> <b>{clean_title}</b>\n"
        f"├ 🔍 <b>Область выборки:</b> <b>{scope_icon}</b>\n"
        f"├ 📦 <b>Существующих объявлений:</b> <b>{len(items)}</b> (пропущены)\n"
        f"├ 📡 <b>Статус:</b> 🟢 Мониторинг 24/7 активен\n"
        f"└ ⚡️ <b>Частота сканирования:</b> каждые ~15 секунд\n\n"
        "<i>Как только появится свежий лот — бот пришлёт фото в HD, цену, контакты и готовый отклик продавцу!</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=scope_btn, callback_data=f"toggle_title_scope:{search_id}")
        ],
        [
            InlineKeyboardButton(text="📉 Алерт на скидки 💎", callback_data=f"set_discount:{search_id}"),
            InlineKeyboardButton(text="🚫 Стоп-слова", callback_data=f"manage_stops:{search_id}")
        ],
        [
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches"),
            InlineKeyboardButton(text="↗️ Открыть на Авито", url=normalized_url)
        ]
    ])

    await status_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ── Список радаров и компактное меню ────────────────────────────────────

def build_radar_card(s: dict) -> Tuple[str, InlineKeyboardMarkup]:
    """Формирует детальную карточку конкретного радара и клавиатуру управления."""
    search_id = s["id"]
    title = html.escape(s["title"])
    is_active = bool(s["is_active"])
    status_icon = "🟢 <b>Активен (24/7)</b>" if is_active else "⏸ <b>На паузе</b>"
    stops_count = len(s["stop_words"])
    disc_val = s.get("min_discount_percent") or 0
    if disc_val > 0:
        disc_info = f"<b>от -{disc_val}%</b> 🟢"
        disc_btn_label = f"📉 Алерт на скидки: от -{disc_val}% 💎 ⚙️"
    else:
        disc_info = "<code>Выключено</code> ⚪️"
        disc_btn_label = "📉 Отслеживать скидки продавцов 💎"

    title_only = bool(s.get("search_in_title_only", 0))
    if title_only:
        scope_info = "<b>Только в названиях</b> 🏷"
        scope_btn_label = "🔍 Выборка: Только в названиях 🏷"
    else:
        scope_info = "<b>Название + описание</b> 📄"
        scope_btn_label = "🔍 Выборка: Название + описание 📄"

    card_text = (
        f"🎯 <b>РАДАР: {title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Статус:</b> {status_icon}\n"
        f"🔍 <b>Область выборки:</b> {scope_info}\n"
        f"📉 <b>Скидки продавцов:</b> {disc_info}\n"
        f"🛡 <b>Стоп-слов:</b> <code>{stops_count}</code>\n"
        f"🔗 <b>Ссылка:</b> <a href=\"{s['url']}\">Открыть выдачу на Авито</a>\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    toggle_btn_text = "⏸ Поставить на паузу" if is_active else "▶️ Запустить радар"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=scope_btn_label, callback_data=f"toggle_title_scope:{search_id}")
        ],
        [
            InlineKeyboardButton(text=disc_btn_label, callback_data=f"set_discount:{search_id}")
        ],
        [
            InlineKeyboardButton(text=toggle_btn_text, callback_data=f"toggle_search:{search_id}"),
            InlineKeyboardButton(text=f"🛡 Стоп-слова ({stops_count})", callback_data=f"manage_stops:{search_id}")
        ],
        [
            InlineKeyboardButton(text="↗️ Открыть на Авито", url=s["url"]),
            InlineKeyboardButton(text="🗑 Удалить радар", callback_data=f"delete_search:{search_id}")
        ],
        [
            InlineKeyboardButton(text="« Назад ко всем радарам", callback_data="list_searches")
        ]
    ])
    return card_text, kb


@router.message(F.text.in_({"📁 Мои радары", "📂 Мои радары", "📋 Мои поиски", "Мои радары"}))
@router.callback_query(F.data == "list_searches")
async def show_searches(event, state: Optional[FSMContext] = None):
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
        await event.answer()
    else:
        message = event
        user_id = message.from_user.id

    if state:
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
        if isinstance(event, CallbackQuery):
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
        return

    from scheduler import get_last_scan_info
    scan_info = get_last_scan_info()

    active_cnt = sum(1 for s in searches if s.get("is_active"))
    lines = [
        f"📁 <b>ВАШИ РАДАРЫ</b> (Всего: <b>{len(searches)}</b> · В работе: <b>{active_cnt}</b> 🟢)",
        f"📡 <i>Сканирование Авито: <b>{scan_info}</b></i>",
        "━━━━━━━━━━━━━━━━━━━━"
    ]

    for i, s in enumerate(searches, 1):
        title = html.escape(s["title"])
        is_active = bool(s["is_active"])
        status_dot = "🟢" if is_active else "⏸"
        disc_val = s.get("min_discount_percent") or 0
        disc_str = f" 🏷 <i>(-{disc_val}%)</i>" if disc_val > 0 else ""
        stops_cnt = len(s["stop_words"])
        stops_str = f" · 🛡 {stops_cnt}" if stops_cnt > 0 else ""
        pause_str = " <i>(пауза)</i>" if not is_active else ""
        lines.append(f"<b>{i}.</b> {status_dot} <b>{title}</b>{pause_str}{disc_str}{stops_str}")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>Выберите радар для перехода к настройкам:</i>")
    text = "\n".join(lines)

    buttons = []
    row = []
    for i, s in enumerate(searches, 1):
        status_dot = "🟢" if s.get("is_active") else "⏸"
        raw_title = s["title"].strip()
        short_title = (raw_title[:15] + "…") if len(raw_title) > 16 else raw_title
        btn_label = f"{status_dot} {i}. {short_title}"
        row.append(InlineKeyboardButton(text=btn_label, callback_data=f"open_search:{s['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="📉 Настройка скидок (PRO) 💎", callback_data="all_discounts_menu")
    ])
    buttons.append([
        InlineKeyboardButton(text="➕ Добавить радар", callback_data="add_search_prompt"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="list_searches")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("open_search:"))
async def cb_open_search(call: CallbackQuery):
    """Открывает детальную карточку конкретного радара."""
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s or s.get("user_id") != call.from_user.id:
        await call.answer("Радар не найден", show_alert=True)
        return

    card_text, kb = build_radar_card(s)
    try:
        await call.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        await call.message.answer(card_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    await call.answer()


# ── Пауза / Возобновление ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("toggle_search:"))
async def cb_toggle_search(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    new_status = await database.toggle_search_active(search_id, call.from_user.id)

    if new_status is None:
        await call.answer("Радар не найден", show_alert=True)
        return

    status_str = "возобновлён 🟢" if new_status else "приостановлен ⏸"
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Статус радара", f"Радар #{search_id} -> {status_str}", status="OK")
    logger.info(f"⏯ [РАДАР] Пользователь ID:{call.from_user.id} переключил статус радара #{search_id} -> {status_str}")
    await call.answer(f"Радар {status_str}!")

    s = await database.get_search(search_id)
    if not s:
        return

    card_text, kb = build_radar_card(s)
    try:
        await call.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass


# ── Переключение области выборки (Только в названиях / Название + описание) ──

@router.callback_query(F.data.startswith("toggle_title_scope:"))
async def cb_toggle_title_scope(call: CallbackQuery):
    user_id = call.from_user.id
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s or s.get("user_id") != user_id:
        await call.answer("Радар не найден", show_alert=True)
        return

    current_val = int(s.get("search_in_title_only") or 0)
    new_val = 0 if current_val == 1 else 1
    old_url = s["url"]

    parsed = urlparse(old_url)
    params = parse_qs(parsed.query)

    if new_val == 1:
        params["bt"] = ["1"]
    else:
        params.pop("bt", None)

    new_query = urlencode(sorted(params.items()), doseq=True)
    new_url = urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path, parsed.params, new_query, ""))
    new_url = AvitoParser.normalize_url(new_url)

    await database.update_search_title_scope(search_id, user_id, new_val, new_url)

    toast = (
        "🏷 Выборка: ТОЛЬКО В НАЗВАНИЯХ!\nСпам из описаний теперь отсекается."
        if new_val == 1 else
        "📄 Выборка: НАЗВАНИЕ + ОПИСАНИЕ\nПоиск совпадений по всему тексту."
    )
    await call.answer(toast, show_alert=True)

    s["search_in_title_only"] = new_val
    s["url"] = new_url
    card_text, kb = build_radar_card(s)
    try:
        await call.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass


# ── Быстрый выбор радара для скидки (PRO) ──────────────────────────────

@router.callback_query(F.data == "all_discounts_menu")
async def cb_all_discounts_menu(call: CallbackQuery):
    user_id = call.from_user.id
    user = await database.get_or_create_user(user_id)
    is_pro = (user_id in config.ADMIN_IDS or (user.get("tariff_plan") or "").lower() == "pro")

    if not is_pro:
        await call.answer(
            "🔒 Отслеживание снижения цен доступно только на тарифе PRO!\n\n"
            "Оформите подписку PRO в меню «Мой профиль», чтобы бот моментально ловил скидки перекупов.",
            show_alert=True
        )
        return

    searches = await database.get_user_searches(user_id)
    if not searches:
        await call.answer("У вас пока нет активных радаров!", show_alert=True)
        return

    if len(searches) == 1:
        call.data = f"set_discount:{searches[0]['id']}"
        return await cb_set_discount_menu(call)

    text = (
        "📉 <b>НАСТРОЙКА АЛЕРТОВ НА СКИДКИ (PRO)</b> 💎\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите радар, для которого хотите настроить порог скидки:\n\n"
        "<i>(Как только продавец скинет цену на этот процент или более — "
        "бот сразу пришлёт пуш с расчётом вашей выгоды!)</i>"
    )

    buttons = []
    for i, s in enumerate(searches, 1):
        s_id = s["id"]
        raw_title = s["title"].strip()
        short_title = (raw_title[:16] + "…") if len(raw_title) > 17 else raw_title
        disc_val = s.get("min_discount_percent") or 0
        disc_label = f"-{disc_val}% 🟢" if disc_val > 0 else "Выкл ⚪️"
        btn_text = f"#{i}. {short_title} [{disc_label}]"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"set_discount:{s_id}")])

    buttons.append([InlineKeyboardButton(text="« Назад ко всем радарам", callback_data="list_searches")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ── Настройка скидки для PRO (Price Drop Alert) ─────────────────────────

@router.callback_query(F.data.startswith("set_discount:"))
async def cb_set_discount_menu(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    user_id = call.from_user.id
    user = await database.get_or_create_user(user_id)
    is_pro = (user_id in config.ADMIN_IDS or (user.get("tariff_plan") or "").lower() == "pro")

    if not is_pro:
        await call.answer(
            "🔒 Отслеживание снижения цен доступно только на тарифе PRO!\n\n"
            "Оформите подписку PRO в меню «Мой профиль», чтобы бот моментально ловил скидки перекупов.",
            show_alert=True
        )
        return

    s = await database.get_search(search_id)
    if not s or s.get("user_id") != user_id:
        await call.answer("Радар не найден", show_alert=True)
        return

    cur_disc = s.get("min_discount_percent") or 0
    cur_label = f"-{cur_disc}%" if cur_disc > 0 else "Выключено"

    text = (
        f"📉 <b>ОТСЛЕЖИВАНИЕ СНИЖЕНИЯ ЦЕНЫ</b> 💎\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Радар: <b>{html.escape(s['title'])}</b>\n"
        f"Текущий порог: <b>{cur_label}</b>\n\n"
        f"Выберите процент скидки для мгновенного уведомления или введите свой собственный:\n"
        f"<i>(Как только продавец скинет цену на этот процент или более — бот сразу пришлёт пуш)</i>"
    )

    options = [
        ("5%", 5), ("10%", 10), ("15%", 15),
        ("20%", 20), ("30%", 30), ("50%", 50)
    ]
    buttons = []
    row = []
    for label, val in options:
        btn_mark = " ✓" if cur_disc == val else ""
        row.append(InlineKeyboardButton(text=f"{label}{btn_mark}", callback_data=f"save_discount:{search_id}:{val}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="✏️ Ввести свой процент...", callback_data=f"custom_discount:{search_id}")])
    buttons.append([InlineKeyboardButton(text="❌ Отключить отслеживание", callback_data=f"save_discount:{search_id}:0")])
    buttons.append([
        InlineKeyboardButton(text="« Назад к радару", callback_data=f"open_search:{search_id}"),
        InlineKeyboardButton(text="📁 Ко всем радарам", callback_data="list_searches")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("save_discount:"))
async def cb_save_discount(call: CallbackQuery):
    parts = call.data.split(":")
    search_id = int(parts[1])
    val = int(parts[2])
    user_id = call.from_user.id

    await database.update_search_discount_percent(search_id, user_id, val)
    status_text = f"скидки от -{val}%" if val > 0 else "отслеживание скидок отключено"
    log_user_event(user_id, call.from_user.username, call.from_user.first_name, "Настройка скидки", f"Радар #{search_id} -> {status_text}", status="OK")
    await call.answer(f"✅ Сохранено: {status_text}!")
    s = await database.get_search(search_id)
    if s:
        card_text, kb = build_radar_card(s)
        try:
            await call.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
            return
        except Exception:
            pass
    await show_searches(call)


@router.callback_query(F.data.startswith("custom_discount:"))
async def cb_prompt_custom_discount(call: CallbackQuery, state: FSMContext):
    search_id = int(call.data.split(":")[1])
    await state.set_state(SearchStates.waiting_for_custom_discount)
    await state.update_data(discount_search_id=search_id)

    text = (
        "✍️ <b>ВВЕДИТЕ ЖЕЛАЕМЫЙ ПРОЦЕНТ СКИДКИ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Отправьте в чат число от <b>1</b> до <b>99</b> "
        "(например: <code>7</code>, <code>12</code>, <code>25</code> или <code>40</code>).\n\n"
        "<i>Бот пришлёт уведомление, как только продавец скинет цену на указанный процент!</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"open_search:{search_id}")]
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.message(SearchStates.waiting_for_custom_discount)
async def process_custom_discount_input(message: Message, state: FSMContext):
    raw_val = message.text.strip().replace("%", "")
    menu_buttons = {
        "🎯 Добавить радар", "➕ Добавить радар", "➕ Добавить поиск",
        "📁 Мои радары", "📂 Мои радары", "📋 Мои поиски", "Мои радары",
        "⚡️ Быстрый отклик", "💬 Быстрый отклик", "⚙️ Шаблон по умолчанию",
        "👤 Мой профиль", "📊 Мой статус", "Мой профиль",
        "📖 Инструкция", "❓ Помощь", "Инструкция",
        "👑 Админ-панель", "👑 Админка",
        "❌ Отмена", "Отмена"
    }
    if message.text.strip() in menu_buttons or message.text.strip().startswith("/"):
        await state.clear()
        if message.text.strip() in {"📁 Мои радары", "📂 Мои радары", "📋 Мои поиски", "Мои радары"}:
            return await show_searches(message, state)
        return

    if not raw_val.isdigit():
        await message.answer(
            "⚠️ <b>Пожалуйста, введите целое число от 1 до 99</b> (например: <code>7</code> или <code>15</code>):",
            parse_mode="HTML"
        )
        return

    percent = int(raw_val)
    if percent < 1 or percent > 99:
        await message.answer(
            "⚠️ <b>Процент должен быть в диапазоне от 1% до 99%!</b> Попробуйте ещё раз:",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    search_id = data.get("discount_search_id")
    await state.clear()

    if search_id:
        await database.update_search_discount_percent(search_id, message.from_user.id, percent)
        log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name, "Настройка скидки", f"Радар #{search_id} -> свой порог: {percent}%", status="OK")
        await message.answer(f"✅ <b>Отлично! Алерт установлен на снижение цены от -{percent}%!</b>", parse_mode="HTML")

    await show_searches(message, state)


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
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Удаление радара", f"Удален радар #{search_id}", status="OK")
    logger.info(f"🗑 [РАДАР] Пользователь ID:{call.from_user.id} удалил радар #{search_id}")
    await call.answer("Радар удален!")
    await show_searches(call)


@router.callback_query(F.data.startswith("cancel_delete:"))
async def cb_cancel_delete(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s:
        await show_searches(call)
        return

    card_text, kb = build_radar_card(s)
    try:
        await call.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass
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
    buttons.append([
        InlineKeyboardButton(text="« Назад к радару", callback_data=f"open_search:{search_id}"),
        InlineKeyboardButton(text="📁 Ко всем радарам", callback_data="list_searches")
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await call.answer()

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("add_stop:"))
@router.callback_query(F.data.startswith("add_stop_from_item:"))
async def cb_add_stop_word_prompt(call: CallbackQuery, state: FSMContext):
    search_id = int(call.data.split(":")[1])
    await state.set_state(SearchStates.waiting_for_stop_word)
    await state.update_data(search_id=search_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")]
    ])
    await call.message.answer(
        "✍️ <b>Введите стоп-слово или фразу:</b>\n\n"
        "<i>Например:\n"
        "<code>на запчасти, копия, без комплекта</code>\n"
        "(Можно прислать несколько слов через запятую)</i>",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await call.answer()


@router.message(SearchStates.waiting_for_stop_word)
async def process_stop_word_input(message: Message, state: FSMContext):
    raw_text = message.text.strip()
    menu_buttons = {
        "🎯 Добавить радар", "➕ Добавить радар", "➕ Добавить поиск",
        "📁 Мои радары", "📂 Мои радары", "📋 Мои поиски", "Мои радары",
        "⚡️ Быстрый отклик", "💬 Быстрый отклик", "⚙️ Шаблон по умолчанию",
        "👤 Мой профиль", "📊 Мой статус", "Мой профиль",
        "📖 Инструкция", "❓ Помощь", "Инструкция",
        "👑 Админ-панель", "👑 Админка",
        "❌ Отмена", "Отмена"
    }
    if raw_text in menu_buttons or raw_text.startswith("/"):
        await state.clear()
        if raw_text in {"📁 Мои радары", "📂 Мои радары", "📋 Мои поиски", "Мои радары"}:
            return await show_searches(message, state)
        return

    data = await state.get_data()
    search_id = data.get("search_id")

    words = [w.strip() for w in raw_text.split(",") if w.strip()]
    added = []
    for w in words:
        if await database.add_stop_word_to_search(search_id, w):
            added.append(w)

    await state.clear()
    log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name, "Добавление стоп-слов", f"Добавлены слова: {added} к радару #{search_id}", status="OK")
    logger.info(f"🛡 [СТОП-СЛОВА] Пользователь ID:{message.from_user.id} добавил к радару #{search_id}: {added}")

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
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Удаление стоп-слова", f"Удалено «{word}» из радара #{search_id}", status="OK")
    logger.info(f"🛡 [СТОП-СЛОВА] Пользователь ID:{call.from_user.id} удалил «{word}» из радара #{search_id}")
    await call.answer(f"Слово «{word}» удалено")
    await cb_manage_stops(call)




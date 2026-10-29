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
from source_cache import SOURCE_SNAPSHOTS
from logger_setup import log_user_event

logger = logging.getLogger(__name__)
router = Router()

# Множество нормализованных URL и ID радаров, где радар был только что снят с паузы
RESUMED_SOURCE_URLS: set[str] = set()
RESUMED_RADAR_IDS: set[int] = set()


def pop_resumed_source_urls() -> set[str]:
    res = set(RESUMED_SOURCE_URLS)
    RESUMED_SOURCE_URLS.clear()
    return res


def consume_resumed_radar_id(radar_id: int) -> bool:
    if radar_id in RESUMED_RADAR_IDS:
        RESUMED_RADAR_IDS.discard(radar_id)
        return True
    return False


class SearchStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_stop_word = State()
    waiting_for_custom_age = State()


# ── Добавление радара ──────────────────────────────────────────────────

_AVITO_URL_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)*avito\.ru/[^\s<>\"']+", re.IGNORECASE)


def extract_avito_urls(text: str) -> List[str]:
    """Все ссылки на Авито из текста, в порядке появления, без повторов."""
    seen: set[str] = set()
    urls = []
    for match in _AVITO_URL_RE.findall(text or ""):
        url = match if match.lower().startswith("http") else f"https://{match}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


async def bulk_add_radars(message: Message, state: FSMContext, urls: List[str]) -> None:
    """Админ присылает много ссылок одним сообщением: каждая становится радаром.
    Калибровка откладывается до первого скана, чтобы не делать десятки запросов к Авито разом."""
    user_id = message.from_user.id
    await state.clear()
    limit = int(config.BULK_RADAR_MAX)
    extra_urls = max(0, len(urls) - limit)
    urls = urls[:limit]

    existing = {s["url"] for s in await database.get_user_searches(user_id)}
    max_radars = await database.get_user_max_radars(user_id)
    current_count = await database.count_user_searches(user_id)

    added, duplicates, invalid, over_limit = [], 0, 0, 0
    for raw in urls:
        if "/item/" in raw or re.search(r"_\d{8,}", raw):
            invalid += 1
            continue
        try:
            normalized_url = AvitoParser.normalize_url(raw)
        except Exception:
            invalid += 1
            continue
        if normalized_url in existing:
            duplicates += 1
            continue
        if current_count >= max_radars:
            over_limit += 1
            continue
        title = AvitoParser.extract_search_title("", normalized_url)
        try:
            search_id = await database.add_search(
                user_id, title, normalized_url,
                search_in_title_only=1 if "bt=1" in normalized_url else 0,
                initial_items=None,
            )
        except database.RadarLimitError:
            over_limit += 1
            continue
        RESUMED_SOURCE_URLS.add(normalized_url)
        existing.add(normalized_url)
        current_count += 1
        added.append((search_id, title))

    log_user_event(
        user_id, message.from_user.username, message.from_user.first_name, "Массовое добавление радаров",
        f"добавлено={len(added)} повторы={duplicates} ошибки={invalid} сверх лимита={over_limit + extra_urls}",
        status="OK" if added else "WARN",
    )
    lines = [
        "📦 <b>МАССОВОЕ ДОБАВЛЕНИЕ РАДАРОВ</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"✅ Добавлено: <b>{len(added)}</b>",
    ]
    if duplicates:
        lines.append(f"♻️ Уже были: <b>{duplicates}</b>")
    if invalid:
        lines.append(f"⚠️ Не ссылки на поиск: <b>{invalid}</b>")
    if over_limit:
        lines.append(f"🚫 Не влезли в лимит ({max_radars}): <b>{over_limit}</b>")
    if extra_urls:
        lines.append(f"✂️ Сверх {limit} ссылок за раз: <b>{extra_urls}</b> (пришлите отдельным сообщением)")
    for _sid, title in added[:15]:
        lines.append(f"• {html.escape(title)}")
    if len(added) > 15:
        lines.append(f"• … и ещё {len(added) - 15}")
    lines.append("")
    lines.append("<i>Калибровка пройдёт при первом скане: уже выложенные лоты не придут.</i>")

    kb_rows = [[InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")]]
    if over_limit:
        kb_rows.insert(0, [InlineKeyboardButton(text="➕ +10 к моему лимиту", callback_data="admin_radar_limit:self:10:0")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")

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
    if config.is_admin(event.from_user.id):
        text += (
            f"\n\n👑 <b>Админ:</b> можно прислать сразу до {int(config.BULK_RADAR_MAX)} ссылок, "
            "по одной на строку — каждая станет отдельным радаром."
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm"),
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")
        ],
        [
            InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")
        ]
    ])
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "cancel_fsm")
async def cb_cancel_fsm(call: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="start_approved")
        ]
    ])
    if call.message:
        try:
            await call.message.edit_text("❌ <b>Действие отменено.</b>", reply_markup=kb, parse_mode="HTML")
        except Exception:
            await call.message.answer("❌ <b>Действие отменено.</b>", reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.message(F.text.regexp(r"(?i)avito\.ru"))
@router.message(SearchStates.waiting_for_url)
async def process_avito_url(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Отправьте текстовую ссылку на поиск Авито.")
        return
    raw_url = message.text.strip()
    user_id = message.from_user.id

    menu_buttons = {
        "🎯 Добавить радар", "➕ Добавить радар", "➕ Добавить поиск",
        "📁 Мои радары", "📂 Мои радары", "📋 Мои поиски", "Мои радары",
        "💳 Тарифы", "💳 Тарифы и подписка", "Тарифы", "Подписка", "💳 Подписка",
        "⚡️ Быстрый отклик", "💬 Быстрый отклик", "⚙️ Шаблон по умолчанию",
        "👤 Мой профиль", "📊 Мой статус", "Мой профиль",
        "📖 Инструкция", "❓ Помощь", "Инструкция",
        "📄 Документы", "Документы",
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
        if raw_url in {"📄 Документы", "Документы"}:
            from handlers.common import cmd_docs_hub
            return await cmd_docs_hub(message)
        return

    if config.is_admin(user_id):
        bulk_urls = extract_avito_urls(raw_url)
        if len(bulk_urls) > 1:
            await bulk_add_radars(message, state, bulk_urls)
            return

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

    if "/item/" in raw_url or re.search(r"_\d{8,}", raw_url):
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", "Ошибка: ссылка на объявление (/item/), а не на поиск", status="WARN")
        await message.answer(
            "⚠️ <b>Это ссылка на конкретное объявление, а не на каталог!</b>\n\n"
            "Чтобы радар ловил все новые предложения, отправьте ссылку со <b>страницы поиска / каталога</b>.",
            parse_mode="HTML"
        )
        return

    current_count = await database.count_user_searches(user_id)
    max_radars = await database.get_user_max_radars(user_id)
    if current_count >= max_radars:
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", f"Ошибка: лимит {max_radars} исчерпан", status="WARN")
        limit_button = (
            InlineKeyboardButton(text="➕ +10 к моему лимиту", callback_data="admin_radar_limit:self:10:0")
            if config.is_admin(user_id)
            else InlineKeyboardButton(text="💳 Расширить лимит (Тарифы)", callback_data="open_subscription_menu")
        )
        await message.answer(
            f"⚠️ <b>Достигнут лимит активных радаров ({max_radars})!</b>\n\n"
            "Вы можете освободить место в меню <b>📁 Мои радары</b> или расширить лимит до 4 или 9 радаров в меню <b>💳 Тарифы</b>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [limit_button],
                [InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")]
            ]),
            parse_mode="HTML"
        )
        await state.clear()
        return

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

    try:
        snapshot = await SOURCE_SNAPSHOTS.load(normalized_url)
    except Exception as e:
        logger.warning("Ошибка калибровки: %s", e)
        snapshot = None

    if snapshot is None:
        search_title = AvitoParser.extract_search_title("", normalized_url)
        items = []
        initial_items_arg = None
        RESUMED_SOURCE_URLS.add(normalized_url)
        log_user_event(
            user_id,
            message.from_user.username,
            message.from_user.first_name,
            "Калибровка радара",
            f"Отложенная калибровка («{normalized_url[:80]}»)",
            status="WARN",
        )
    else:
        search_title = snapshot.title
        items = list(snapshot.items)
        initial_items_arg = [(item.item_id, item.raw_price) for item in items]

    is_title_only = 1 if "bt=1" in normalized_url else 0

    try:
        search_id = await database.add_search(
            user_id, search_title, normalized_url, search_in_title_only=is_title_only,
            initial_items=initial_items_arg,
        )
    except (database.RadarLimitError, PermissionError) as exc:
        await status_msg.edit_text(f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
        await state.clear()
        return

    log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Радар создан", f"Радар #{search_id} «{search_title}» (лотов: {len(items)})", status="OK")
    logger.info(f"🎯 [РАДАР] Пользователь ID:{user_id} создал радар #{search_id}: «{search_title}» ({len(items)} базовых лотов проиндексировано)")

    await state.clear()

    clean_title = html.escape(search_title)
    scope_icon = "🏷 Только в названиях" if is_title_only else "📄 Название + описание"
    scope_btn = "🔍 Выборка: Только в названиях 🏷" if is_title_only else "🔍 Выборка: Название + описание 📄"
    existing_line = (
        f"├ 📦 <b>Существующих объявлений:</b> <b>{len(items)}</b> (пропущены)\n"
        if snapshot is not None
        else "├ 📦 <b>Калибровка:</b> <i>выполнится автоматически при первом скане</i>\n"
    )
    text = (
        "┌ 🎯 <b>РАДАР УСПЕШНО АКТИВИРОВАН!</b>\n"
        f"├ 📌 <b>Цель:</b> <b>{clean_title}</b>\n"
        f"├ 🔍 <b>Область выборки:</b> <b>{scope_icon}</b>\n"
        f"{existing_line}"
        f"├ 📡 <b>Статус:</b> 🟢 Мониторинг 24/7 активен\n"
        f"└ ⚡️ <b>Целевой интервал опроса:</b> {int(config.SCAN_INTERVAL_MIN)}–{int(config.SCAN_INTERVAL_MAX)} сек.\n\n"
        "<i>При обнаружении нового лота бот сразу отправит уведомление с ценой, ссылкой и фото (для тарифов Standart и PRO).</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=scope_btn, callback_data=f"toggle_title_scope:{search_id}")
        ],
        [
            InlineKeyboardButton(text="🚫 Стоп-слова", callback_data=f"manage_stops:{search_id}")
        ],
        [
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches"),
            InlineKeyboardButton(text="↗️ Открыть на Авито", url=normalized_url)
        ]
    ])

    await status_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ── Список радаров и компактное меню ────────────────────────────────────

def format_max_age_label(max_age_minutes: Optional[int]) -> str:
    if not max_age_minutes or max_age_minutes <= 0:
        max_age_minutes = 5
    elif max_age_minutes > 30:
        max_age_minutes = 30
    return f"до {max_age_minutes} мин. ⚡️"


def build_radar_card(s: dict) -> Tuple[str, InlineKeyboardMarkup]:
    search_id = s["id"]
    title = html.escape(s["title"])
    is_active = bool(s["is_active"])
    status_icon = "🟢 <b>Активен (24/7)</b>" if is_active else "⏸ <b>На паузе</b>"
    stops_count = len(s["stop_words"])

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
            InlineKeyboardButton(text=toggle_btn_text, callback_data=f"toggle_search:{search_id}"),
            InlineKeyboardButton(text=f"🛡 Стоп-слова ({stops_count})", callback_data=f"manage_stops:{search_id}")
        ],
        [
            InlineKeyboardButton(text="↗️ Открыть на Авито", url=s["url"]),
            InlineKeyboardButton(text="🗑 Удалить радар", callback_data=f"delete_search:{search_id}")
        ],
        [
            InlineKeyboardButton(text="« Назад ко всем радарам", callback_data="list_searches"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="start_approved")
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
            "Вы можете отслеживать несколько категорий одновременно.\n\n"
            "Нажмите кнопку ниже или просто отправьте ссылку на поиск с Авито в этот чат!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Добавить первый радар", callback_data="add_search_prompt")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")]
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
        stops_cnt = len(s["stop_words"])
        stops_str = f" · 🛡 {stops_cnt}" if stops_cnt > 0 else ""
        pause_str = " <i>(пауза)</i>" if not is_active else ""
        lines.append(f"<b>{i}.</b> {status_dot} <b>{title}</b>{pause_str}{stops_str}")

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
        InlineKeyboardButton(text="➕ Добавить радар", callback_data="add_search_prompt"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="list_searches")
    ])
    buttons.append([
        InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")
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
    try:
        search_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректная кнопка радара", show_alert=True)
        return
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

    if new_status:
        RESUMED_RADAR_IDS.add(search_id)
        try:
            RESUMED_SOURCE_URLS.add(AvitoParser.normalize_url(s["url"]))
        except Exception:
            pass
    else:
        RESUMED_RADAR_IDS.discard(search_id)

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


# ── Настройка свежести лотов (max_age_minutes) ──────────────────────────

@router.callback_query(F.data.startswith("set_age:"))
async def cb_set_age_menu(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    user_id = call.from_user.id

    s = await database.get_search(search_id)
    if not s or s.get("user_id") != user_id:
        await call.answer("Радар не найден", show_alert=True)
        return

    cur_age = s.get("max_age_minutes")
    if not cur_age or cur_age <= 0:
        cur_age = 5
    elif cur_age > 30:
        cur_age = 30

    cur_label = format_max_age_label(cur_age)

    text = (
        f"⏱ <b>НАСТРОЙКА СВЕЖЕСТИ ОБЪЯВЛЕНИЙ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Радар: <b>{html.escape(s['title'])}</b>\n"
        f"Текущий фильтр: <b>{cur_label}</b>\n\n"
        f"Выберите, сколько минут назад лот должен быть выложен на Авито (максимум до 30 минут):\n"
        f"<i>(Лоты старше выбранного времени будут автоматически отсекаться, чтобы не присылать старый спам)</i>"
    )

    options = [
        ("⚡️ 3 мин", 3),
        ("⚡️ 5 мин (дефолт)", 5),
        ("⏱ 10 мин", 10),
        ("⏱ 15 мин", 15),
        ("⏱ 20 мин", 20),
        ("⏱ 30 мин (макс)", 30),
    ]

    buttons = []
    row = []
    for label, val in options:
        btn_mark = " ✓" if cur_age == val else ""
        row.append(InlineKeyboardButton(text=f"{label}{btn_mark}", callback_data=f"save_age:{search_id}:{val}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="✏️ Ввести минуты вручную (1-30)...", callback_data=f"custom_age:{search_id}")])
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


@router.callback_query(F.data.startswith("save_age:"))
async def cb_save_age(call: CallbackQuery):
    parts = call.data.split(":")
    search_id = int(parts[1])
    val = min(30, max(1, int(parts[2])))
    user_id = call.from_user.id

    s = await database.get_search(search_id)
    if not s or s.get("user_id") != user_id:
        await call.answer("Радар не найден", show_alert=True)
        return

    await database.update_search_max_age(search_id, user_id, val)
    s["max_age_minutes"] = val

    ans_text = f"Установлен фильтр: {format_max_age_label(val)}"
    log_user_event(user_id, call.from_user.username, call.from_user.first_name, "Настройка свежести", f"Радар #{search_id} -> {ans_text}", status="OK")
    await call.answer(f"✅ {ans_text}")
    card_text, kb = build_radar_card(s)
    try:
        await call.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass


@router.callback_query(F.data.startswith("custom_age:"))
async def cb_prompt_custom_age(call: CallbackQuery, state: FSMContext):
    search_id = int(call.data.split(":")[1])
    user_id = call.from_user.id
    s = await database.get_search(search_id)
    if not s or s.get("user_id") != user_id:
        await call.answer("Радар не найден", show_alert=True)
        return

    await state.set_state(SearchStates.waiting_for_custom_age)
    await state.update_data(age_search_id=search_id)

    text = (
        f"✏️ <b>РУЧНОЙ ВВОД ВРЕМЕНИ СВЕЖЕСТИ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Радар: <b>{html.escape(s['title'])}</b>\n\n"
        f"Отправьте сообщением максимальный возраст объявления <b>в минутах</b> (число от <b>1</b> до <b>30</b>):\n\n"
        f"<i>Примеры:\n"
        f"• <code>2</code> — супер-быстрый перехват (до 2 минут)\n"
        f"• <code>5</code> — стандарт (до 5 минут)\n"
        f"• <code>15</code> — четверть часа (до 15 минут)\n"
        f"• <code>30</code> — максимум каталога (до 30 минут)</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Отмена", callback_data=f"set_age:{search_id}")]
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.message(SearchStates.waiting_for_custom_age)
async def process_custom_age_input(message: Message, state: FSMContext):
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

    text = message.text.strip()
    if not text.isdigit():
        await message.answer(
            "⚠️ <b>Пожалуйста, введите целое число минут от 1 до 30</b> (например: <code>5</code> или <code>15</code>):",
            parse_mode="HTML"
        )
        return

    val = int(text)
    if val < 1 or val > 30:
        await message.answer(
            "⚠️ <b>Число минут должно быть от 1 до 30 (максимум 30 минут)!</b> Попробуйте ещё раз:",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    search_id = data.get("age_search_id")
    await state.clear()

    if search_id:
        await database.update_search_max_age(search_id, message.from_user.id, val)
        label = format_max_age_label(val)
        log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name, "Настройка свежести", f"Радар #{search_id} -> свой порог: {val} мин.", status="OK")
        await message.answer(f"✅ <b>Отлично! Фильтр свежести установлен: {label}!</b>", parse_mode="HTML")

        s = await database.get_search(search_id)
        if s:
            card_text, kb = build_radar_card(s)
            await message.answer(card_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
            return

    await show_searches(message, state)


# ── Удаление поиска ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("delete_search:"))
async def cb_delete_search(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s or s.get("user_id") != call.from_user.id:
        await call.answer("Радар не найден", show_alert=True)
        return
    title = html.escape(s["title"])

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
    if not s or s.get("user_id") != call.from_user.id:
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
    if not s or s.get("user_id") != call.from_user.id:
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


async def _start_stop_word_fsm(call: CallbackQuery, state: FSMContext, search_id: int) -> None:
    await state.set_state(SearchStates.waiting_for_stop_word)
    await state.update_data(search_id=search_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")]
    ])
    if call.message:
        await call.message.answer(
            "✍️ <b>Введите стоп-слово или фразу:</b>\n\n"
            "<i>Например:\n"
            "<code>на запчасти, копия, без комплекта</code>\n"
            "(Можно прислать несколько слов через запятую)</i>",
            reply_markup=kb,
            parse_mode="HTML"
        )
    await call.answer()


@router.callback_query(F.data.startswith("event_stop:"))
async def cb_event_stop_word(call: CallbackQuery, state: FSMContext):
    """
    Обработчик кнопки «Стоп-слово» под агрегированным уведомлением (по event_id):
    - 1 активный радар -> сразу открывает ввод стоп-слова
    - >1 активных радаров -> показывает выбор радара (до 5 кнопок в ряд и Отмена)
    - удалено / очищено -> отвечает 'Уведомление устарело, откройте радар в меню'
    """
    parts = call.data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
        return
    event_id = int(parts[1].strip())
    event = await database.get_event_for_callback(event_id)
    if event is None or int(event.get("user_id") or 0) != int(call.from_user.id):
        await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
        return

    active_radars = event.get("active_radars") or []
    if not active_radars:
        await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
        return

    if len(active_radars) == 1:
        await _start_stop_word_fsm(call, state, int(active_radars[0]["search_id"]))
        return

    # Несколько активных радаров — показываем inline-выбор (до 5 кнопок в ряд + Отмена)
    lines = [
        "🛡 <b>Выберите радар для добавления стоп-слова:</b>\n"
    ]
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for idx, r in enumerate(active_radars, start=1):
        sid = int(r["search_id"])
        rtitle = html.escape(str(r.get("radar_title") or f"Радар #{sid}"))
        lines.append(f"<b>{idx}.</b> {rtitle}")
        short_label = str(r.get("radar_title") or f"#{sid}").strip()
        if len(short_label) > 16:
            short_label = short_label[:15] + "…"
        current_row.append(
            InlineKeyboardButton(text=f"{idx}. {short_label}", callback_data=f"add_stop:{sid}")
        )
        if len(current_row) == 5:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fsm")])

    if call.message:
        await call.message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("add_stop:"))
@router.callback_query(F.data.startswith("add_stop_from_item:"))
async def cb_add_stop_word_prompt(call: CallbackQuery, state: FSMContext):
    try:
        search_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
        return
    search = await database.get_search(search_id)
    if not search or search["user_id"] != call.from_user.id:
        await call.answer("Радар не найден", show_alert=True)
        return
    await _start_stop_word_fsm(call, state, search_id)


@router.message(SearchStates.waiting_for_stop_word, F.text)
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
        if await database.add_stop_word_to_search(search_id, w, message.from_user.id):
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

    if not await database.remove_stop_word_from_search(search_id, word, call.from_user.id):
        await call.answer("Радар или стоп-слово не найдено", show_alert=True)
        return
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Удаление стоп-слова", f"Удалено «{word}» из радара #{search_id}", status="OK")
    logger.info(f"🛡 [СТОП-СЛОВА] Пользователь ID:{call.from_user.id} удалил «{word}» из радара #{search_id}")
    await call.answer(f"Слово «{word}» удалено")
    await cb_manage_stops(call)

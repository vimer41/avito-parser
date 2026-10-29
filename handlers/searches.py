import html
import logging
import re
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from typing import Optional, Tuple, List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import config
import database
import ui
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


async def route_menu_text(message: Message, state: FSMContext) -> bool:
    """A bottom-menu button or a command pressed in the middle of an input step: leave the step
    and open what was pressed. Returns False when the text is ordinary input."""
    text = (message.text or "").strip()
    if text not in ui.ALL_MENU_TEXTS and not text.startswith("/"):
        return False
    await state.clear()
    from handlers import common
    if text in ui.menu_texts("add"):
        await btn_add_search(message, state)
    elif text in ui.menu_texts("radars"):
        await show_searches(message, state)
    elif text in ui.menu_texts("tariffs"):
        await common.cmd_subscription_menu(message)
    elif text in ui.menu_texts("profile"):
        await common.cmd_profile(message)
    elif text in ui.menu_texts("help"):
        await common.cmd_help(message)
    elif text in ui.menu_texts("docs"):
        await common.cmd_docs_hub(message)
    elif text in ui.menu_texts("support"):
        await common.cmd_support(message)
    elif text in ui.menu_texts("admin") and config.is_admin(message.from_user.id):
        from handlers.admin import cmd_admin_home
        await cmd_admin_home(message)
    elif text.startswith("/start"):
        await common.cmd_start(message, state)
    else:
        await message.answer(f"{ui.e('cancel')} Ввод отменён.", reply_markup=ui.kb([ui.home_btn()]), parse_mode="HTML")
    return True


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
        f"{ui.e('box')} <b>Массовое добавление</b>",
        "",
        f"{ui.e('ok')} Добавлено: <b>{len(added)}</b>",
    ]
    if duplicates:
        lines.append(f"♻️ Уже были: <b>{duplicates}</b>")
    if invalid:
        lines.append(f"{ui.e('warn')} Не ссылки на поиск: <b>{invalid}</b>")
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

    rows = [[ui.btn("Мои радары", "radars", callback_data="list_searches")]]
    if over_limit:
        rows.insert(0, [ui.btn("+10 к моему лимиту", "add", callback_data="admin_radar_limit:self:10:0")])
    await message.answer("\n".join(lines), reply_markup=ui.kb(*rows), parse_mode="HTML")


@router.message(F.text.in_(ui.menu_texts("add")))
@router.callback_query(F.data == "add_search_prompt")
async def btn_add_search(event, state: FSMContext):
    await state.set_state(SearchStates.waiting_for_url)
    text = (
        f"{ui.e('radar')} <b>Новый радар</b>\n\n"
        "Пришлите ссылку на поиск Авито:\n"
        "1. Откройте <b>avito.ru</b> и найдите нужный товар.\n"
        "2. Выставьте фильтры: город, цена, состояние.\n"
        "3. Скопируйте ссылку из адресной строки и отправьте сюда.\n\n"
        "<blockquote>Пример: <code>https://www.avito.ru/moskva/telefony?q=iphone</code></blockquote>"
    )
    if config.is_admin(event.from_user.id):
        text += (
            f"\n{ui.e('admin')} Можно прислать до {int(config.BULK_RADAR_MAX)} ссылок, "
            "по одной на строку: каждая станет отдельным радаром."
        )
    markup = ui.kb(
        [ui.btn("Отмена", "cancel", callback_data="cancel_fsm"),
         ui.btn("Мои радары", "radars", callback_data="list_searches")],
    )
    await ui.show(event, text, markup)


@router.callback_query(F.data == "cancel_fsm")
async def cb_cancel_fsm(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.show(call, f"{ui.e('cancel')} <b>Отменено</b>", ui.kb(
        [ui.btn("Мои радары", "radars", callback_data="list_searches"), ui.home_btn()],
    ))


def _url_error_kb() -> InlineKeyboardMarkup:
    return ui.kb(
        [ui.btn("Отмена", "cancel", callback_data="cancel_fsm"),
         ui.btn("Инструкция", "help", callback_data="open_help_menu")],
    )


@router.message(F.text.regexp(r"(?i)avito\.ru"))
@router.message(SearchStates.waiting_for_url)
async def process_avito_url(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пришлите ссылку текстом.", reply_markup=_url_error_kb())
        return
    if await route_menu_text(message, state):
        return
    raw_url = message.text.strip()
    user_id = message.from_user.id

    if config.is_admin(user_id):
        bulk_urls = extract_avito_urls(raw_url)
        if len(bulk_urls) > 1:
            await bulk_add_radars(message, state, bulk_urls)
            return

    if "avito.ru" not in raw_url.lower():
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", f"Ошибка: ссылка не с avito.ru («{raw_url[:80]}»)", status="WARN")
        await message.answer(
            f"{ui.e('warn')} <b>Нужна ссылка с avito.ru</b>\n\n"
            "Например: <code>https://www.avito.ru/moskva/telefony?q=iphone</code>",
            reply_markup=_url_error_kb(),
            parse_mode="HTML"
        )
        return

    if "/item/" in raw_url or re.search(r"_\d{8,}", raw_url):
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", "Ошибка: ссылка на объявление (/item/), а не на поиск", status="WARN")
        await message.answer(
            f"{ui.e('warn')} <b>Это ссылка на одно объявление</b>\n\n"
            "Радару нужна ссылка на <b>страницу поиска</b>: со списком объявлений и фильтрами.",
            reply_markup=_url_error_kb(),
            parse_mode="HTML"
        )
        return

    current_count = await database.count_user_searches(user_id)
    max_radars = await database.get_user_max_radars(user_id)
    if current_count >= max_radars:
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", f"Ошибка: лимит {max_radars} исчерпан", status="WARN")
        limit_button = (
            ui.btn("+10 к моему лимиту", "add", callback_data="admin_radar_limit:self:10:0")
            if config.is_admin(user_id)
            else ui.btn("Тариф с бо́льшим лимитом", "tariffs", callback_data="open_subscription_menu", style="primary")
        )
        await message.answer(
            f"{ui.e('warn')} <b>Все радары заняты ({current_count} из {max_radars})</b>\n\n"
            "Удалите ненужный радар или перейдите на тариф с бо́льшим лимитом: "
            f"Средний — {config.plan_radars('standart')}, Полный — {config.plan_radars('pro')}.",
            reply_markup=ui.kb([limit_button], [ui.btn("Мои радары", "radars", callback_data="list_searches")]),
            parse_mode="HTML"
        )
        await state.clear()
        return

    try:
        normalized_url = AvitoParser.normalize_url(raw_url)
    except Exception as e:
        log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Добавление радара", f"Ошибка синтаксиса ссылки: {e}", status="WARN")
        await message.answer(
            f"{ui.e('warn')} Не получилось разобрать ссылку: {html.escape(str(e))}",
            reply_markup=_url_error_kb(), parse_mode="HTML",
        )
        return

    status_msg = await message.answer(f"{ui.e('wait')} <b>Запоминаю текущую выдачу…</b>", parse_mode="HTML")

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
        await status_msg.edit_text(f"{ui.e('warn')} {html.escape(str(exc))}", parse_mode="HTML")
        await state.clear()
        return

    log_user_event(user_id, message.from_user.username, message.from_user.first_name, "Радар создан", f"Радар #{search_id} «{search_title}» (лотов: {len(items)})", status="OK")
    logger.info(f"🎯 [РАДАР] Пользователь ID:{user_id} создал радар #{search_id}: «{search_title}» ({len(items)} базовых лотов проиндексировано)")

    await state.clear()

    owner = await database.get_user(user_id) or {"user_id": user_id}
    scan_interval = config.plan_interval(database.classify_user_tariff(owner)[0])
    baseline = (
        f"Уже выложенных лотов: <b>{len(items)}</b>, они не придут."
        if snapshot is not None
        else "Текущую выдачу бот запомнит при первой проверке."
    )
    text = (
        f"{ui.e('ok')} <b>Радар запущен</b>\n\n"
        f"{ui.e('radar')} <b>{html.escape(search_title)}</b>\n"
        f"{ui.e('scope')} Ищет: {_scope_label(bool(is_title_only))}\n"
        f"{ui.e('on')} Проверка раз в {scan_interval} с\n"
        f"{baseline}\n\n"
        "<i>Новый лот придёт сюда сразу после обнаружения.</i>"
    )
    markup = ui.kb(
        [_scope_button(search_id, bool(is_title_only))],
        [ui.btn("Стоп-слова", "shield", callback_data=f"manage_stops:{search_id}"),
         ui.btn("На Авито", "link", url=normalized_url)],
        [ui.btn("Мои радары", "radars", callback_data="list_searches")],
    )
    await status_msg.edit_text(text, reply_markup=markup, parse_mode="HTML")


# ── Список радаров и карточка ──────────────────────────────────────────

def format_max_age_label(max_age_minutes: Optional[int]) -> str:
    if not max_age_minutes or max_age_minutes <= 0:
        max_age_minutes = 5
    elif max_age_minutes > 30:
        max_age_minutes = 30
    return f"до {max_age_minutes} мин. ⚡️"


def _scope_label(title_only: bool) -> str:
    return "только в названиях" if title_only else "в названиях и описаниях"


def _scope_button(search_id: int, title_only: bool):
    label = "Ищет только в названиях" if title_only else "Ищет в названиях и описаниях"
    return ui.btn(label, "scope", callback_data=f"toggle_title_scope:{search_id}")


def build_radar_card(s: dict) -> Tuple[str, InlineKeyboardMarkup]:
    search_id = s["id"]
    is_active = bool(s["is_active"])
    stops_count = len(s["stop_words"])
    title_only = bool(s.get("search_in_title_only", 0))

    status = f"{ui.e('on')} Работает" if is_active else f"{ui.e('pause')} На паузе"
    card_text = (
        f"{ui.e('radar')} <b>{html.escape(s['title'])}</b>\n\n"
        f"<b>Статус:</b> {status}\n"
        f"<b>Ищет:</b> {_scope_label(title_only)}\n"
        f"<b>Стоп-слова:</b> {stops_count or 'нет'}\n"
        f"<a href=\"{html.escape(s['url'], quote=True)}\">Открыть выдачу на Авито</a>"
    )
    toggle = (
        ui.btn("Пауза", "pause", callback_data=f"toggle_search:{search_id}")
        if is_active else
        ui.btn("Запустить", "play", callback_data=f"toggle_search:{search_id}", style="success")
    )
    markup = ui.kb(
        [toggle, ui.btn(f"Стоп-слова ({stops_count})", "shield", callback_data=f"manage_stops:{search_id}")],
        [_scope_button(search_id, title_only)],
        [ui.btn("На Авито", "link", url=s["url"]),
         ui.btn("Удалить", "trash", callback_data=f"delete_search:{search_id}", style="danger")],
        [ui.btn("« Все радары", callback_data="list_searches"), ui.home_btn()],
    )
    return card_text, markup


async def _show_card(event, s: dict) -> None:
    card_text, markup = build_radar_card(s)
    await ui.show(event, card_text, markup, disable_web_page_preview=True)


@router.message(F.text.in_(ui.menu_texts("radars")))
@router.callback_query(F.data == "list_searches")
async def show_searches(event, state: Optional[FSMContext] = None):
    user_id = event.from_user.id
    if state:
        await state.clear()
    searches = await database.get_user_searches(user_id)

    if not searches:
        text = (
            f"{ui.e('radars')} <b>Радаров пока нет</b>\n\n"
            "Пришлите ссылку на поиск Авито прямо в чат или нажмите кнопку ниже."
        )
        await ui.show(event, text, ui.kb(
            [ui.btn("Добавить первый радар", "radar", callback_data="add_search_prompt", style="primary")],
            [ui.home_btn()],
        ))
        return

    from scheduler import get_last_scan_info
    max_radars = await database.get_user_max_radars(user_id)
    active_cnt = sum(1 for s in searches if s.get("is_active"))
    lines = [
        f"{ui.e('radars')} <b>Мои радары</b> · {len(searches)} из {max_radars}",
        f"Работают: {active_cnt} · последний скан {get_last_scan_info()}",
        "",
    ]
    for i, s in enumerate(searches, 1):
        dot = ui.e("on") if s.get("is_active") else ui.e("pause")
        stops_cnt = len(s["stop_words"])
        stops_str = f" · {ui.e('shield')} {stops_cnt}" if stops_cnt else ""
        lines.append(f"{dot} <b>{i}. {html.escape(s['title'])}</b>{stops_str}")
    lines.append("")
    lines.append("<i>Нажмите на радар, чтобы настроить его.</i>")

    rows = []
    for i, s in enumerate(searches, 1):
        raw_title = s["title"].strip()
        short_title = (raw_title[:27] + "…") if len(raw_title) > 28 else raw_title
        rows.append([ui.btn(f"{i}. {short_title}", "on" if s.get("is_active") else "pause",
                            callback_data=f"open_search:{s['id']}")])
    add_row = [ui.btn("Обновить", "refresh", callback_data="list_searches")]
    if len(searches) < max_radars:
        add_row.insert(0, ui.btn("Добавить", "add", callback_data="add_search_prompt"))
    rows.append(add_row)
    rows.append([ui.home_btn()])
    await ui.show(event, "\n".join(lines), ui.kb(*rows))


async def _own_search(call: CallbackQuery, search_id: int) -> Optional[dict]:
    s = await database.get_search(search_id)
    if not s or s.get("user_id") != call.from_user.id:
        await call.answer("Радар не найден", show_alert=True)
        return None
    return s


@router.callback_query(F.data.startswith("open_search:"))
async def cb_open_search(call: CallbackQuery):
    try:
        search_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректная кнопка радара", show_alert=True)
        return
    s = await _own_search(call, search_id)
    if s:
        await _show_card(call, s)


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
    await call.answer("Радар запущен" if new_status else "Радар на паузе")

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

    card_text, markup = build_radar_card(s)
    await ui.show(call, card_text, markup, answer=False, disable_web_page_preview=True)


# ── Переключение области выборки (Только в названиях / Название + описание) ──

@router.callback_query(F.data.startswith("toggle_title_scope:"))
async def cb_toggle_title_scope(call: CallbackQuery):
    user_id = call.from_user.id
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
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

    await call.answer(
        "Теперь ищет только в названиях: меньше случайных лотов." if new_val == 1
        else "Теперь ищет в названиях и описаниях: больше лотов."
    )
    s["search_in_title_only"] = new_val
    s["url"] = new_url
    card_text, markup = build_radar_card(s)
    await ui.show(call, card_text, markup, answer=False, disable_web_page_preview=True)


# ── Настройка свежести лотов (max_age_minutes) ──────────────────────────

@router.callback_query(F.data.startswith("set_age:"))
async def cb_set_age_menu(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return

    cur_age = s.get("max_age_minutes")
    if not cur_age or cur_age <= 0:
        cur_age = 5
    elif cur_age > 30:
        cur_age = 30

    text = (
        f"{ui.e('time')} <b>Свежесть лотов</b>\n\n"
        f"Радар: <b>{html.escape(s['title'])}</b>\n"
        f"Сейчас: <b>{format_max_age_label(cur_age)}</b>\n\n"
        "Лоты старше выбранного времени не придут (максимум 30 минут)."
    )
    options = [3, 5, 10, 15, 20, 30]
    rows, row = [], []
    for val in options:
        mark = " ✓" if cur_age == val else ""
        row.append(ui.btn(f"{val} мин{mark}", callback_data=f"save_age:{search_id}:{val}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([ui.btn("Ввести вручную (1–30)", "write", callback_data=f"custom_age:{search_id}")])
    rows.append([ui.btn("« К радару", callback_data=f"open_search:{search_id}")])
    await ui.show(call, text, ui.kb(*rows))


@router.callback_query(F.data.startswith("save_age:"))
async def cb_save_age(call: CallbackQuery):
    parts = call.data.split(":")
    search_id = int(parts[1])
    val = min(30, max(1, int(parts[2])))
    s = await _own_search(call, search_id)
    if not s:
        return

    await database.update_search_max_age(search_id, call.from_user.id, val)
    s["max_age_minutes"] = val

    ans_text = f"Установлен фильтр: {format_max_age_label(val)}"
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Настройка свежести", f"Радар #{search_id} -> {ans_text}", status="OK")
    await call.answer(ans_text)
    card_text, markup = build_radar_card(s)
    await ui.show(call, card_text, markup, answer=False, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("custom_age:"))
async def cb_prompt_custom_age(call: CallbackQuery, state: FSMContext):
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return

    await state.set_state(SearchStates.waiting_for_custom_age)
    await state.update_data(age_search_id=search_id)
    text = (
        f"{ui.e('write')} <b>Свежесть лотов</b>\n\n"
        f"Радар: <b>{html.escape(s['title'])}</b>\n"
        "Пришлите число минут от <b>1</b> до <b>30</b>."
    )
    await ui.show(call, text, ui.kb([ui.btn("« Отмена", callback_data=f"set_age:{search_id}")]))


@router.message(SearchStates.waiting_for_custom_age)
async def process_custom_age_input(message: Message, state: FSMContext):
    if await route_menu_text(message, state):
        return

    text = (message.text or "").strip()
    if not text.isdigit() or not 1 <= int(text) <= 30:
        await message.answer(
            f"{ui.e('warn')} Нужно целое число от 1 до 30, например <code>5</code>.",
            parse_mode="HTML"
        )
        return

    val = int(text)
    data = await state.get_data()
    search_id = data.get("age_search_id")
    await state.clear()

    if search_id:
        await database.update_search_max_age(search_id, message.from_user.id, val)
        log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name, "Настройка свежести", f"Радар #{search_id} -> свой порог: {val} мин.", status="OK")
        s = await database.get_search(search_id)
        if s:
            await _show_card(message, s)
            return

    await show_searches(message, state)


# ── Удаление поиска ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("delete_search:"))
async def cb_delete_search(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return
    text = (
        f"{ui.e('trash')} <b>Удалить радар «{html.escape(s['title'])}»?</b>\n\n"
        "Радар остановится, его стоп-слова удалятся. Отменить это нельзя."
    )
    await ui.show(call, text, ui.kb([
        ui.btn("Да, удалить", "trash", callback_data=f"confirm_delete:{search_id}", style="danger"),
        ui.btn("Отмена", callback_data=f"cancel_delete:{search_id}"),
    ]))


@router.callback_query(F.data.startswith("confirm_delete:"))
async def cb_confirm_delete(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    await database.delete_search(search_id, call.from_user.id)
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Удаление радара", f"Удален радар #{search_id}", status="OK")
    logger.info(f"🗑 [РАДАР] Пользователь ID:{call.from_user.id} удалил радар #{search_id}")
    await call.answer("Радар удалён")
    await show_searches(call)


@router.callback_query(F.data.startswith("cancel_delete:"))
async def cb_cancel_delete(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await database.get_search(search_id)
    if not s or s.get("user_id") != call.from_user.id:
        await show_searches(call)
        return
    await _show_card(call, s)


# ── Управление стоп-словами ────────────────────────────────────────────

@router.callback_query(F.data.startswith("manage_stops:"))
async def cb_manage_stops(call: CallbackQuery):
    search_id = int(call.data.split(":")[1])
    s = await _own_search(call, search_id)
    if not s:
        return

    stop_words = s["stop_words"]
    lines = [
        f"{ui.e('shield')} <b>Стоп-слова</b> · {html.escape(s['title'])}",
        "",
    ]
    if stop_words:
        lines.append("Лоты с этими словами в названии или описании не придут:")
        lines.append(", ".join(f"<code>{html.escape(w)}</code>" for w in stop_words))
        lines.append("")
        lines.append("<i>Нажмите на слово, чтобы удалить его.</i>")
    else:
        lines.append("Пока пусто. Например: «запчасти», «реплика», «дефект», «копия».")

    rows, row = [], []
    # The button carries the word's position, not the word: callback data is limited to 64 bytes.
    for idx, w in enumerate(stop_words):
        row.append(ui.btn(f"✖ {w}", callback_data=f"rm_stop_i:{search_id}:{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([ui.btn("Добавить стоп-слово", "add", callback_data=f"add_stop:{search_id}", style="primary")])
    rows.append([ui.btn("« К радару", callback_data=f"open_search:{search_id}"),
                 ui.btn("Все радары", "radars", callback_data="list_searches")])
    await ui.show(call, "\n".join(lines), ui.kb(*rows))


async def _start_stop_word_fsm(call: CallbackQuery, state: FSMContext, search_id: int) -> None:
    await state.set_state(SearchStates.waiting_for_stop_word)
    await state.update_data(search_id=search_id)
    if call.message:
        await call.message.answer(
            f"{ui.e('write')} <b>Пришлите стоп-слово или фразу</b>\n\n"
            "Несколько — через запятую: <code>на запчасти, копия, без комплекта</code>",
            reply_markup=ui.kb([ui.btn("Отмена", "cancel", callback_data="cancel_fsm")]),
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
    lines = [f"{ui.e('shield')} <b>В какой радар добавить стоп-слово?</b>\n"]
    rows: list = []
    current_row: list = []
    for idx, r in enumerate(active_radars, start=1):
        sid = int(r["search_id"])
        rtitle = html.escape(str(r.get("radar_title") or f"Радар #{sid}"))
        lines.append(f"<b>{idx}.</b> {rtitle}")
        short_label = str(r.get("radar_title") or f"#{sid}").strip()
        if len(short_label) > 16:
            short_label = short_label[:15] + "…"
        current_row.append(ui.btn(f"{idx}. {short_label}", callback_data=f"add_stop:{sid}"))
        if len(current_row) == 5:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([ui.btn("Отмена", "cancel", callback_data="cancel_fsm")])

    if call.message:
        await call.message.answer("\n".join(lines), reply_markup=ui.kb(*rows), parse_mode="HTML")
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
    if await route_menu_text(message, state):
        return

    data = await state.get_data()
    search_id = data.get("search_id")
    words = [w.strip() for w in message.text.split(",") if w.strip()]
    added = []
    for w in words:
        if await database.add_stop_word_to_search(search_id, w, message.from_user.id):
            added.append(w)

    await state.clear()
    log_user_event(message.from_user.id, message.from_user.username, message.from_user.first_name, "Добавление стоп-слов", f"Добавлены слова: {added} к радару #{search_id}", status="OK")
    logger.info(f"🛡 [СТОП-СЛОВА] Пользователь ID:{message.from_user.id} добавил к радару #{search_id}: {added}")

    if added:
        text = (
            f"{ui.e('ok')} <b>Добавлено:</b> {', '.join(f'«{html.escape(w)}»' for w in added)}\n"
            "Лоты с этими словами больше не придут."
        )
    else:
        text = f"{ui.e('warn')} Ничего не добавлено: эти слова уже есть в списке."
    await message.answer(
        text,
        reply_markup=ui.kb(
            [ui.btn("Стоп-слова радара", "shield", callback_data=f"manage_stops:{search_id}")],
            [ui.btn("Мои радары", "radars", callback_data="list_searches")],
        ),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("rm_stop_i:"))
@router.callback_query(F.data.startswith("rm_stop:"))
async def cb_remove_stop_word(call: CallbackQuery):
    by_index = call.data.startswith("rm_stop_i:")
    parts = call.data.split(":", 2)
    search_id = int(parts[1])
    word = parts[2]
    if by_index:
        s = await _own_search(call, search_id)
        if not s:
            return
        idx = int(word) if word.isdigit() else -1
        if not 0 <= idx < len(s["stop_words"]):
            await call.answer("Список изменился, откройте его заново", show_alert=True)
            return
        word = s["stop_words"][idx]

    if not await database.remove_stop_word_from_search(search_id, word, call.from_user.id):
        await call.answer("Радар или стоп-слово не найдено", show_alert=True)
        return
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Удаление стоп-слова", f"Удалено «{word}» из радара #{search_id}", status="OK")
    logger.info(f"🛡 [СТОП-СЛОВА] Пользователь ID:{call.from_user.id} удалил «{word}» из радара #{search_id}")
    await call.answer(f"«{word}» удалено")
    await cb_manage_stops(call)

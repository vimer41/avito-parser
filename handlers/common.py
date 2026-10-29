import html
import logging
import re
from typing import Optional
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.fsm.context import FSMContext

import config
import database
import ui
from logger_setup import log_user_event

logger = logging.getLogger(__name__)
router = Router()


DOCS_HUB_TEXT = (
    "<b>ДОКУМЕНТЫ И РЕГЛАМЕНТЫ СЕРВИСА AVITO RADAR</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "<i>Редакция от 26.10.2026 г. · Код верификации: mekbuda</i>\n\n"
    "Официальные условия предоставления услуг, политика обработки данных и правила расчётов.\n\n"
    "• <b>Характер услуги:</b> нематериальная цифровая услуга по автоматизированному мониторингу открытых объявлений.\n"
    "• <b>Лимит операции:</b> не более 10 000 ₽ за одну транзакцию.\n"
    "• <b>Регламент возвратов:</b> рассмотрение запроса в течение 24 часов.\n"
    "• <b>Служба поддержки:</b> bernardgofov@gmail.com\n\n"
    "<i>Выберите необходимый документ для ознакомления:</i>"
)

AGREEMENT_TEXT = (
    "<b>ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ</b>\n"
    "<i>Публичная оферта сервиса Avito Radar</i>\n"
    "<i>Редакция от 26.10.2026 г. · Код верификации: mekbuda</i>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>1. Общие положения</b>\n"
    "1.1. Настоящее Соглашение регулирует порядок использования сервиса Avito Radar (Telegram-бот).\n"
    "1.2. Сервис оказывает нематериальную цифровая услугу — автоматизированный программный мониторинг и оповещение о появлении публично доступных объявлений на платформе avito.ru по критериям пользователя.\n"
    "1.3. Сервис не является представителем или аффилированным лицом Avito, не выступает стороной сделок купли-продажи и не несёт ответственности за действия продавцов.\n\n"
    "<b>2. Тарифные планы и расчёты</b>\n"
    "2.1. Доступ к функционалу сервиса предоставляется на условиях цифровой подписки на 30 дней без автоматического продления (без автосписаний).\n"
    "2.2. Услуга признаётся оказанной в полном объёме с момента предоставления доступа к выбранному тарифному плану в интерфейсе бота.\n"
    "2.3. Максимальная сумма разовой платёжной операции составляет не более 10 000 ₽ (или эквивалент в Telegram Stars).\n\n"
    "<b>3. Порядок возврата средств</b>\n"
    "3.1. Запросы на возврат средств принимаются через встроенную тикет-систему бота (/paysupport) или по электронной почте: <b>bernardgofov@gmail.com</b>.\n"
    "3.2. Срок рассмотрения заявления на возврат денежных средств составляет не более <b>24 часов</b> с момента подачи обращения.\n"
    "3.3. При подтверждении возврата доступ к услугам сервиса аннулируется, а средства возвращаются пользователю тем же способом, которым была совершена оплата.\n\n"
    "<b>4. Контакты и верификация</b>\n"
    "• Официальный email: <b>bernardgofov@gmail.com</b>\n"
    "• Поддержка в Telegram: встроенная тикет-система бота (/support, /paysupport)\n"
    "• Проверочный код верификации: <code>mekbuda</code>"
)

PRIVACY_TEXT = (
    "<b>ПОЛИТИКА КОНФИДЕНЦИАЛЬНОСТИ</b>\n"
    "<i>Политика обработки данных сервиса Avito Radar</i>\n"
    "<i>Редакция от 26.10.2026 г. · Код верификации: mekbuda</i>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>1. Общие положения</b>\n"
    "1.1. Настоящая Политика определяет порядок обработки технических данных пользователей Telegram-бота Avito Radar.\n"
    "1.2. Сервис собирает исключительно минимально необходимый объём данных для обеспечения доставки уведомлений и работы поисковых радаров.\n\n"
    "<b>2. Состав обрабатываемых данных</b>\n"
    "2.1. В рамках работы сервиса обрабатываются исключительно идентификаторы платформы Telegram:\n"
    "• Числовой Telegram User ID (для привязки настроек радаров и подписки);\n"
    "• Общедоступное имя пользователя (username) и имя аккаунта Telegram;\n"
    "• Параметры поисковых ссылок, фильтров и стоп-слов, добавленных пользователем.\n"
    "2.2. Сервис НЕ собирает паспортные данные, номера телефонов, реквизиты банковских карт и персональные сведения, не относящиеся к Telegram-аккаунту.\n\n"
    "<b>3. Безопасность и использование данных</b>\n"
    "3.1. Данные используются исключительно для маршрутизации уведомлений и технической поддержки.\n"
    "3.2. Личные данные не передаются третьим лицам и не используются для сторонней рекламы.\n"
    "3.3. Максимальный лимит расчётных операций: 10 000 ₽ за одну транзакцию.\n\n"
    "<b>4. Контакты службы поддержки</b>\n"
    "• Официальная почта для обращений: <b>bernardgofov@gmail.com</b>\n"
    "• Поддержка в боте: команда /support\n"
    "• Код подтверждения сервиса: <code>mekbuda</code>"
)


def get_docs_keyboard() -> InlineKeyboardMarkup:
    return ui.kb(
        [ui.btn("Пользовательское соглашение", "docs", callback_data="doc_agreement")],
        [ui.btn("Политика конфиденциальности", "lock", callback_data="doc_privacy")],
        [ui.btn("Условия подписки и возврата", "card", callback_data="payment_terms")],
        [ui.home_btn()],
    )


def get_doc_nav_keyboard() -> InlineKeyboardMarkup:
    return ui.kb(
        [ui.btn("« К списку документов", callback_data="open_docs_menu")],
        [ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu"),
         ui.btn("Поддержка", "support", callback_data="support_start_ticket")],
        [ui.home_btn()],
    )


def get_main_keyboard(user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    """Нижняя клавиатура: кнопки присылают свой текст, поэтому эмодзи в них обычные."""
    label = {key: ui.MENU[key][0] for key in ui.MENU}
    kb = [
        [KeyboardButton(text=label["add"]), KeyboardButton(text=label["radars"])],
        [KeyboardButton(text=label["tariffs"]), KeyboardButton(text=label["profile"])],
        [KeyboardButton(text=label["help"]), KeyboardButton(text=label["support"])],
    ]
    if user_id and config.is_admin(user_id):
        kb.append([KeyboardButton(text=label["admin"])])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, input_field_placeholder="Пришлите ссылку на поиск Авито")


def main_menu_kb() -> InlineKeyboardMarkup:
    return ui.kb(
        [ui.btn("Добавить радар", "radar", callback_data="add_search_prompt", style="primary")],
        [ui.btn("Мои радары", "radars", callback_data="list_searches"),
         ui.btn("Профиль", "profile", callback_data="refresh_profile")],
        [ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu"),
         ui.btn("Инструкция", "help", callback_data="open_help_menu")],
        [ui.btn("Поддержка", "support", callback_data="support_start_ticket"),
         ui.btn("Документы", "docs", callback_data="open_docs_menu")],
    )


TRIAL_HOURS = 6


def radars_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} радар"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} радара"
    return f"{count} радаров"


def plan_icon(plan: str) -> str:
    return ui.e(plan if plan in ("lite", "standart", "pro") else "standart")


def plan_summary(plan: str) -> str:
    """«🚀 Средний — 449 ₽ / 30 дней» plus its limits, for tariff lists."""
    return (
        f"{plan_icon(plan)} <b>{config.plan_title(plan)}</b> — {config.rub_price(plan)} ₽ / 30 дней\n"
        f"{radars_word(config.plan_radars(plan))} · проверка раз в {config.plan_interval(plan)} с"
    )


def access_status(user: dict, user_id: int) -> str:
    """One-line access status shared by the start screen, the profile and the tariff menu."""
    if config.is_admin(user_id):
        return "Администратор"
    sub_until = user.get("subscription_until")
    is_allowed = bool(user.get("is_allowed"))
    if sub_until and bool(user.get("trial_used")) and not is_allowed:
        return f"Пробный период до {database.format_subscription_date(sub_until)}"
    plan = config.plan_title(user.get("tariff_plan"))
    if sub_until and is_allowed:
        return f"Тариф «{plan}» до {database.format_subscription_date(sub_until)}"
    if is_allowed:
        return f"Тариф «{plan}», бессрочно"
    return "Подписка не активна"


def can_take_trial(user: dict, user_id: int) -> bool:
    return not config.is_admin(user_id) and not user.get("subscription_until") and not user.get("trial_used")


@router.message(CommandStart())
@router.callback_query(F.data == "start_approved")
async def cmd_start(event, state: FSMContext):
    await state.clear()
    user = event.from_user
    first_name = user.first_name or "Пользователь"
    user_record = await database.get_or_create_user(user.id, user.username, first_name)

    text = (
        f"{ui.e('logo')} <b>Avito Radar</b>\n"
        "Новые объявления Авито в Telegram через секунды после публикации.\n\n"
        f"{ui.e('hello')} <b>{html.escape(first_name)}</b> · {access_status(user_record, user.id)}\n\n"
        "<b>Как начать</b>\n"
        "1. Откройте <code>avito.ru</code>, задайте поиск и фильтры.\n"
        "2. Скопируйте ссылку из адресной строки и пришлите сюда.\n"
        "Бот запомнит выдачу и будет присылать каждый новый лот."
    )
    markup = main_menu_kb()
    if can_take_trial(user_record, user.id):
        markup.inline_keyboard.insert(0, [ui.btn(
            f"Попробовать бесплатно — {TRIAL_HOURS} часов", "trial", callback_data="activate_trial_btn", style="success",
        )])

    if isinstance(event, CallbackQuery):
        await ui.show(event, text, markup)
        return
    # A typed /start also refreshes the bottom keyboard (new labels, admin button).
    await event.answer("Меню закреплено внизу экрана.", reply_markup=get_main_keyboard(user.id))
    await event.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(F.text.in_(ui.menu_texts("help")))
@router.callback_query(F.data == "open_help_menu")
@router.message(Command("help"))
async def cmd_help(event):
    text = (
        f"{ui.e('help')} <b>Как пользоваться</b>\n\n"
        f"{ui.e('radar')} <b>1. Добавьте радар</b>\n"
        "Откройте <code>avito.ru</code>, задайте запрос и фильтры: город, цену, состояние. "
        "Скопируйте ссылку из адресной строки и пришлите боту. "
        "Сортировку по дате бот включит сам; уже выложенные лоты не придут, только новые.\n\n"
        f"{ui.e('shield')} <b>2. Отсекайте лишнее</b>\n"
        "• <b>Стоп-слова</b>: лоты с этими словами не придут («запчасти», «реплика»).\n"
        "• <b>Чёрный список</b>: кнопка под лотом скрывает все объявления продавца.\n"
        "• <b>Область поиска</b>: только заголовок или заголовок с описанием.\n\n"
        f"{ui.e('tariffs')} <b>3. Тарифы</b>\n"
        f"Пробный период: {radars_word(config.TRIAL_RADARS)} на {TRIAL_HOURS} часов.\n"
        f"{plan_summary('lite')}\n"
        f"{plan_summary('standart')}\n"
        f"{plan_summary('pro')}\n"
        "На «Полном» после технических пауз приходят до 10 свежих лотов, найденных за паузу."
    )
    markup = ui.kb(
        [ui.btn("Добавить радар", "radar", callback_data="add_search_prompt", style="primary")],
        [ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu"),
         ui.btn("Поддержка", "support", callback_data="support_start_ticket")],
        [ui.home_btn()],
    )
    await ui.show(event, text, markup)


@router.message(F.text.in_(ui.menu_texts("profile")))
@router.callback_query(F.data == "refresh_profile")
@router.message(Command("profile"))
async def cmd_profile(event):
    user_id = event.from_user.id
    if isinstance(event, CallbackQuery):
        await event.answer("Обновлено")

    user = await database.get_or_create_user(user_id)
    searches = await database.get_user_searches(user_id)
    searches_count = len(searches)
    active_count = sum(1 for s in searches if s.get("is_active"))
    is_adm = config.is_admin(user_id)
    max_radars = await database.get_user_max_radars(user_id)
    scan_interval = config.plan_interval(database.classify_user_tariff(user)[0])

    from scheduler import get_last_scan_info
    blocked_count = len(await database.get_user_blocked_sellers_list(user_id))

    text = (
        f"{ui.e('profile')} <b>Профиль</b>\n\n"
        f"<b>Доступ:</b> {access_status(user, user_id)}\n"
        f"<b>Радары:</b> {searches_count} из {max_radars}, работают {active_count}\n"
        f"<b>Проверка:</b> раз в {scan_interval} с\n"
        f"<b>Чёрный список:</b> {blocked_count}\n"
        f"<b>Последний скан:</b> {get_last_scan_info()}\n"
        f"<b>ID:</b> <code>{user_id}</code>"
    )

    rows = []
    if can_take_trial(user, user_id):
        rows.append([ui.btn(f"Пробный период {TRIAL_HOURS} часов", "trial", callback_data="activate_trial_btn", style="success")])
    if not is_adm:
        has_plan = user.get("subscription_until") or user.get("is_allowed")
        rows.append([ui.btn("Продлить или сменить тариф" if has_plan else "Выбрать тариф", "tariffs",
                            callback_data="open_subscription_menu", style=None if has_plan else "primary")])
    rows.append([
        ui.btn(f"Радары ({searches_count})", "radars", callback_data="list_searches"),
        ui.btn(f"Чёрный список ({blocked_count})", "ban", callback_data="manage_blacklist"),
    ])
    rows.append([ui.btn("Обновить", "refresh", callback_data="refresh_profile"), ui.home_btn()])
    await ui.show(event, text, ui.kb(*rows), answer=False)


@router.callback_query(F.data == "activate_trial_btn")
async def cb_activate_trial(call: CallbackQuery):
    res = await database.activate_trial(call.from_user.id)
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Активация триала", res["message"], status="OK" if res["success"] else "WARN")
    if not res["success"]:
        await call.answer(res["message"], show_alert=True)
        return
    until = database.format_subscription_date(res.get("until"))
    text = (
        f"{ui.e('trial')} <b>Пробный период включён</b>\n\n"
        f"Действует до {until}: {radars_word(config.TRIAL_RADARS)}, все фильтры.\n"
        "Пришлите ссылку на поиск Авито, и первый радар заработает."
    )
    await ui.show(call, text, ui.kb(
        [ui.btn("Добавить радар", "radar", callback_data="add_search_prompt", style="primary")],
        [ui.btn("Инструкция", "help", callback_data="open_help_menu"), ui.home_btn()],
    ))


@router.message(F.text.in_(ui.menu_texts("tariffs")))
@router.message(Command("subscribe"))
@router.message(Command("tariffs"))
@router.callback_query(F.data == "open_subscription_menu")
async def cmd_subscription_menu(event):
    user_id = event.from_user.id
    user = await database.get_or_create_user(user_id)
    current = (user.get("tariff_plan") or "").lower() if user.get("is_allowed") else ""

    text = (
        f"{ui.e('tariffs')} <b>Тарифы</b>\n"
        f"Сейчас: <b>{access_status(user, user_id)}</b>\n\n"
        f"{plan_summary('lite')}\n\n"
        f"{plan_summary('standart')}\n\n"
        f"{plan_summary('pro')}\n"
        "+ догон свежих лотов после технических пауз\n\n"
        "<blockquote>Чем чаще проверка, тем раньше вы увидите лот и успеете написать первым.</blockquote>\n"
        "Оплата разовая на 30 дней, без автосписаний. Оплаченные дни добавляются к текущему сроку."
    )

    rows = []
    if can_take_trial(user, user_id):
        rows.append([ui.btn(f"Пробный период {TRIAL_HOURS} часов", "trial", callback_data="activate_trial_btn")])
    for plan in config.PAID_PLANS:
        mark = " · ваш" if plan == current else ""
        rows.append([ui.btn(
            f"{config.plan_title(plan)} — {config.rub_price(plan)} ₽{mark}", plan,
            callback_data=f"buy_tariff:{plan}", style="primary" if plan == "standart" and not current else None,
        )])
    rows.append([
        ui.btn("Условия", "docs", callback_data="payment_terms"),
        ui.btn("Вопрос по оплате", "support", callback_data="payment_support"),
    ])
    rows.append([ui.home_btn()])
    await ui.show(event, text, ui.kb(*rows))


PLAN_PERKS = {
    "lite": "Для личного поиска: квартира, техника, вещь для себя.",
    "standart": "Для активного поиска и перепродажи в небольших городах.",
    "pro": "Для перепродажи в больших городах и конкурентных категориях. "
           "После технических пауз присылает до 10 свежих лотов, найденных за паузу.",
}


@router.callback_query(F.data.startswith("buy_tariff:"))
async def cb_select_tariff(call: CallbackQuery):
    tariff_key = call.data.split(":")[1]
    if tariff_key not in (*config.PAID_PLANS, "test"):
        # Old buttons (removed tariffs) lead back to the current tariff list.
        await cmd_subscription_menu(call)
        return
    title = config.plan_title(tariff_key)
    stars = config.stars_price(tariff_key)
    is_test = tariff_key == "test"
    price_label = f"{stars} ⭐" if is_test else f"{config.rub_price(tariff_key)} ₽"
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Выбор тарифа", f"Тариф {title} ({price_label})", status="INFO")

    # "test" is a hidden 1-day plan for checking Stars checkout end to end; it grants the Средний tariff.
    plan = "standart" if is_test else tariff_key
    text = (
        f"{plan_icon(plan)} <b>Тариф «{title}»</b> — {price_label} за {'1 день' if is_test else '30 дней'}\n\n"
        f"• До <b>{radars_word(config.plan_radars(plan))}</b>\n"
        f"• Проверка каждого радара раз в <b>{config.plan_interval(plan)} с</b>\n"
        "• Стоп-слова, чёрный список продавцов, поиск по заголовку\n\n"
        f"<i>{PLAN_PERKS[plan]}</i>\n\n"
        "Разовая оплата без автосписаний, доступ включается сразу. Оплачивая, вы соглашаетесь с /terms.\n\n"
        "<b>Способ оплаты:</b>"
    )

    rows = []
    if not is_test:
        rows.append([ui.btn(f"СБП — {config.rub_price(tariff_key)} ₽", "card",
                            callback_data=f"pay_method:sbp:{tariff_key}", style="success")])
    if config.STARS_PAYMENTS_ENABLED:
        rows.append([ui.btn(f"Telegram Stars — {stars} ⭐", "star", callback_data=f"stars_buy:{tariff_key}:{stars}")])
    else:
        text += "\n\n⏸ Оплата Telegram Stars временно выключена."
    if not is_test:
        rows.append([ui.btn("Криптовалюта", "coin", callback_data=f"pay_method:crypto:{tariff_key}")])
    rows.append([
        ui.btn("« Все тарифы", callback_data="open_subscription_menu"),
        ui.btn("Вопрос по оплате", "support", callback_data="payment_support"),
    ])
    await ui.show(call, text, ui.kb(*rows))


PAY_METHOD_TITLES = {"sbp": "СБП", "crypto": "криптовалютой"}


@router.callback_query(F.data.startswith("pay_method:"))
async def cb_pay_method(call: CallbackQuery):
    # СБП and crypto checkouts are not connected yet: the buttons only explain that and point to Stars.
    method = call.data.split(":")[1] if call.data.count(":") >= 1 else ""
    title = PAY_METHOD_TITLES.get(method, "этим способом")
    stars_hint = " Сейчас можно оплатить через Telegram Stars." if config.STARS_PAYMENTS_ENABLED else ""
    await call.answer(f"Оплата {title} скоро появится.{stars_hint}", show_alert=True)


@router.callback_query(F.data == "other_payment_methods")
async def cb_other_payment_methods(call: CallbackQuery):
    # The separate "other payment methods" screen is gone; old buttons open the tariff list.
    await cmd_subscription_menu(call)


# ── Документы и регламенты ─────────────────────────────────────────────

@router.message(F.text.in_(ui.menu_texts("docs")))
@router.message(Command("docs"))
@router.callback_query(F.data == "open_docs_menu")
async def cmd_docs_hub(event):
    await ui.show(event, f"{ui.e('docs')} {DOCS_HUB_TEXT}", get_docs_keyboard())


@router.message(Command("agreement", "offer", "contract"))
@router.callback_query(F.data == "doc_agreement")
async def cmd_doc_agreement(event):
    await ui.show(event, f"{ui.e('docs')} {AGREEMENT_TEXT}", get_doc_nav_keyboard())


@router.message(Command("privacy", "policy"))
@router.callback_query(F.data == "doc_privacy")
async def cmd_doc_privacy(event):
    await ui.show(event, f"{ui.e('lock')} {PRIVACY_TEXT}", get_doc_nav_keyboard())


# ── Служба поддержки и контакты ───────────────────────────────────────

@router.message(Command("support", "contacts"))
@router.message(F.text.in_(ui.menu_texts("support")))
@router.callback_query(F.data == "open_support_stub")
async def cmd_support(event, state: Optional[FSMContext] = None):
    if state:
        await state.clear()
    text = (
        f"{ui.e('support')} <b>Поддержка</b>\n\n"
        "Вопросы о работе бота, оплате и возвратах.\n"
        "• Ежедневно с 09:00 до 23:00 (МСК), ответ за 5–15 минут\n"
        "• Возвраты рассматриваем в течение 24 часов\n"
        f"• {ui.e('mail')} bernardgofov@gmail.com\n\n"
        "Нажмите кнопку ниже и опишите вопрос: оператор ответит в этом чате."
    )
    await ui.show(event, text, ui.kb(
        [ui.btn("Написать в поддержку", "write", callback_data="support_start_ticket", style="primary")],
        [ui.btn("Документы", "docs", callback_data="open_docs_menu"),
         ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu")],
        [ui.home_btn()],
    ))


@router.callback_query(F.data == "support_stub_click")
async def cb_support_stub(call: CallbackQuery):
    await cmd_support(call)


# ── Чёрный список продавцов (Бан в 1 клик) ─────────────────────────────

@router.callback_query(F.data == "manage_blacklist")
async def cb_manage_blacklist(call: CallbackQuery):
    blocked = await database.get_user_blocked_sellers_list(call.from_user.id)
    back_row = [ui.btn("« Профиль", callback_data="refresh_profile"), ui.home_btn()]
    if not blocked:
        text = (
            f"{ui.e('ban')} <b>Чёрный список пуст</b>\n\n"
            "Нажмите «Скрыть продавца» под лотом: его объявления перестанут приходить по всем радарам."
        )
        await ui.show(call, text, ui.kb(back_row))
        return
    text = (
        f"{ui.e('ban')} <b>Чёрный список</b> · {len(blocked)}\n\n"
        "Объявления этих продавцов не приходят. Нажмите на продавца, чтобы вернуть его."
    )
    if len(blocked) > 12:
        text += f"\n<i>Показаны первые 12 из {len(blocked)}.</i>"
    rows = [[ui.btn(f"✖ {b['seller_name'][:30]}", callback_data=f"unban_seller:{b['id']}")] for b in blocked[:12]]
    rows.append(back_row)
    await ui.show(call, text, ui.kb(*rows))


@router.callback_query(F.data.startswith("unban_seller:"))
async def cb_unban_seller(call: CallbackQuery):
    block_id = int(call.data.split(":")[1])
    await database.remove_blocked_seller_by_id(call.from_user.id, block_id)
    await call.answer("Продавец убран из чёрного списка")
    await cb_manage_blacklist(call)


@router.callback_query(F.data.startswith("ban_seller:"))
async def cb_ban_seller(call: CallbackQuery):
    user_id = call.from_user.id
    parts = call.data.split(":", 1)
    raw_id = parts[1].strip() if len(parts) > 1 else ""

    seller_name = None
    if raw_id.isdigit() and int(raw_id) > 0:
        event_id = int(raw_id)
        event = await database.get_event_for_callback(event_id)
        if event is None or not event.get("active_radars"):
            await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
            return
        if int(event["user_id"]) != int(user_id):
            await call.answer("Уведомление устарело, откройте радар в меню", show_alert=True)
            return
        item_obj = event.get("item") or {}
        if isinstance(item_obj.get("item"), dict):
            item_obj = item_obj["item"]
        seller_name = (item_obj.get("seller_name") or "").strip()

    if not seller_name and call.message:
        # Old notifications: the seller line is "👤 Продавец: Имя" in the plain message text.
        msg_text = call.message.caption or call.message.text or ""
        m = re.search(r"👤\s*Продавец:\s*([^·\n]+)", msg_text)
        if m:
            seller_name = m.group(1).strip()

    if not seller_name or seller_name.lower() in {"продавец", "компания", "частное лицо"}:
        await call.answer("Не удалось определить имя продавца", show_alert=True)
        return

    await database.add_blocked_seller(user_id, seller_name)
    log_user_event(user_id, call.from_user.username, call.from_user.first_name, "Блокировка продавца", f"Продавец «{seller_name}» добавлен в ЧС", status="OK")
    logger.info(f"🚷 [ЧЕРНЫЙ СПИСОК] Пользователь ID:{user_id} заблокировал продавца «{seller_name}»")

    await call.answer(
        f"🚫 «{seller_name}» в чёрном списке.\nЕго лоты больше не придут. Вернуть: Профиль → Чёрный список.",
        show_alert=True
    )

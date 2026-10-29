import html
import logging
import re
from typing import Optional
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import config
import database
from logger_setup import log_user_event

logger = logging.getLogger(__name__)
router = Router()


DOCS_HUB_TEXT = (
    "📄 <b>ДОКУМЕНТЫ И РЕГЛАМЕНТЫ СЕРВИСА AVITO RADAR</b>\n"
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
    "📄 <b>ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ</b>\n"
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
    "🔒 <b>ПОЛИТИКА КОНФИДЕНЦИАЛЬНОСТИ</b>\n"
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
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Пользовательское соглашение", callback_data="doc_agreement")],
        [InlineKeyboardButton(text="🔒 Политика конфиденциальности", callback_data="doc_privacy")],
        [InlineKeyboardButton(text="💳 Условия подписки и возврата", callback_data="payment_terms")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")]
    ])


def get_doc_nav_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« К списку документов", callback_data="open_docs_menu")],
        [InlineKeyboardButton(text="💳 Тарифы", callback_data="open_subscription_menu"),
         InlineKeyboardButton(text="💬 Поддержка", callback_data="support_start_ticket")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")]
    ])


def get_main_keyboard(user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    """Главная клавиатура бота."""
    kb = [
        [KeyboardButton(text="🎯 Добавить радар"), KeyboardButton(text="📁 Мои радары")],
        [KeyboardButton(text="💳 Тарифы"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="📖 Инструкция"), KeyboardButton(text="📄 Документы")],
        [KeyboardButton(text="💬 Поддержка")]
    ]
    if user_id and config.is_admin(user_id):
        kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


PLAN_ICONS = {"lite": "🟢", "standart": "🔵", "pro": "🔥"}


def radars_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} радар"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} радара"
    return f"{count} радаров"


def plan_summary(plan: str) -> str:
    """«🔵 Средний — 449 ₽ / 30 дней» plus its limits, for tariff lists."""
    return (
        f"{PLAN_ICONS[plan]} <b>{config.plan_title(plan)}</b> — {config.rub_price(plan)} ₽ / 30 дней\n"
        f"    {radars_word(config.plan_radars(plan))}, проверка раз в {config.plan_interval(plan)} с"
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


@router.message(CommandStart())
@router.callback_query(F.data == "start_approved")
async def cmd_start(event, state: FSMContext):
    await state.clear()
    if isinstance(event, CallbackQuery):
        message = event.message
        user = event.from_user
        await event.answer()
    else:
        message = event
        user = message.from_user

    user_id = user.id
    username = user.username
    first_name = user.first_name or "Пользователь"

    user_record = await database.get_or_create_user(user_id, username, first_name)
    badge = access_status(user_record, user_id)

    text = (
        "<b>Avito Radar</b> — новые объявления Авито прямо в Telegram\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👋 <b>{html.escape(first_name)}</b> · {badge}\n\n"
        "<b>Как начать:</b>\n"
        "1. Откройте <code>avito.ru</code>, задайте поиск и фильтры.\n"
        "2. Скопируйте ссылку из адресной строки и пришлите её сюда.\n\n"
        "Бот будет проверять выдачу и присылать каждый новый лот."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt"),
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")
        ],
        [
            InlineKeyboardButton(text="💳 Тарифы", callback_data="open_subscription_menu"),
            InlineKeyboardButton(text="👤 Мой профиль", callback_data="refresh_profile")
        ],
        [
            InlineKeyboardButton(text="📖 Инструкция", callback_data="open_help_menu"),
            InlineKeyboardButton(text="📄 Документы", callback_data="open_docs_menu")
        ],
        [
            InlineKeyboardButton(text="💬 Поддержка", callback_data="support_start_ticket")
        ]
    ])

    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=get_main_keyboard(user_id), parse_mode="HTML")


@router.message(F.text.in_({"📖 Инструкция", "❓ Помощь"}))
@router.callback_query(F.data == "open_help_menu")
@router.message(Command("help"))
async def cmd_help(event):
    if isinstance(event, CallbackQuery):
        message = event.message
        await event.answer()
    else:
        message = event

    help_text = (
        "<b>Как пользоваться Avito Radar</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1. Добавьте радар</b>\n"
        "• Откройте <code>avito.ru</code>, задайте запрос и фильтры: город, цену, состояние.\n"
        "• Скопируйте ссылку из адресной строки и пришлите её боту.\n"
        "• Сортировку по дате бот включит сам. Уже выложенные лоты не придут — только новые.\n\n"
        "<b>2. Отсекайте лишнее</b>\n"
        "• <b>Стоп-слова</b> — лоты с этими словами не придут («запчасти», «реплика»).\n"
        "• <b>Чёрный список</b> — кнопка под лотом скрывает все объявления продавца.\n"
        "• <b>Область поиска</b> — только заголовок или заголовок с описанием.\n\n"
        "<b>3. Тарифы</b>\n"
        f"• Пробный период: {config.TRIAL_RADARS} радар на 6 часов.\n"
        f"{plan_summary('lite')}\n"
        f"{plan_summary('standart')}\n"
        f"{plan_summary('pro')}\n"
        "• На «Полном» после технических пауз приходят до 10 свежих лотов, найденных за время паузы.\n\n"
        "Документы: /docs. Поддержка: /support."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 Тарифы", callback_data="open_subscription_menu")
        ],
        [
            InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt"),
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")
        ],
        [
            InlineKeyboardButton(text="📄 Документы", callback_data="open_docs_menu"),
            InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")
        ]
    ])
    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(help_text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(help_text, reply_markup=kb, parse_mode="HTML")


@router.message(F.text.in_({"👤 Мой профиль", "📊 Мой статус"}))
@router.callback_query(F.data == "refresh_profile")
@router.message(Command("profile"))
async def cmd_profile(event):
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
        await event.answer("Профиль обновлён")
    else:
        message = event
        user_id = message.from_user.id

    user = await database.get_or_create_user(user_id)
    searches = await database.get_user_searches(user_id)
    searches_count = len(searches)
    active_count = sum(1 for s in searches if s.get("is_active"))

    is_adm = config.is_admin(user_id)
    sub_until = user.get("subscription_until")
    trial_used = bool(user.get("trial_used"))
    max_radars = await database.get_user_max_radars(user_id)
    scan_interval = config.plan_interval(database.classify_user_tariff(user)[0])

    from scheduler import get_last_scan_info
    scan_info = get_last_scan_info()
    blocked_sellers_list = await database.get_user_blocked_sellers_list(user_id)
    blocked_count = len(blocked_sellers_list)

    text = (
        "<b>Мой профиль</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Доступ:</b> {access_status(user, user_id)}\n"
        f"• <b>Радары:</b> {active_count} активных, всего {searches_count} из {max_radars}\n"
        f"• <b>Проверка:</b> раз в {scan_interval} с\n"
        f"• <b>Чёрный список:</b> {blocked_count} продавцов\n"
        f"• <b>Мониторинг:</b> {scan_info}\n"
        f"• <b>ID:</b> <code>{user_id}</code>\n"
    )

    buttons = []
    if not is_adm and not sub_until and not trial_used:
        buttons.append([InlineKeyboardButton(text="🎁 Пробный период 6 часов", callback_data="activate_trial_btn")])
    if not is_adm:
        sub_btn_text = "💳 Продлить или сменить тариф" if sub_until or user.get("is_allowed") else "💳 Выбрать тариф"
        buttons.append([InlineKeyboardButton(text=sub_btn_text, callback_data="open_subscription_menu")])

    buttons.append([
        InlineKeyboardButton(text=f"📁 Мои радары ({searches_count})", callback_data="list_searches"),
        InlineKeyboardButton(text=f"🚷 Чёрный список ({blocked_count})", callback_data="manage_blacklist")
    ])
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_profile"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="start_approved")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "activate_trial_btn")
async def cb_activate_trial(call: CallbackQuery):
    res = await database.activate_trial(call.from_user.id)
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Активация триала", res["message"], status="OK" if res["success"] else "WARN")
    if res["success"]:
        formatted_until = database.format_subscription_date(res.get("until"))
        await call.answer(f"Пробный период 6 часов активирован!\nДействует: {formatted_until}", show_alert=True)
        await cmd_profile(call)
    else:
        await call.answer(res["message"], show_alert=True)


@router.message(F.text.in_({"💳 Тарифы", "💳 Тарифы и подписка", "Тарифы", "Подписка", "💳 Подписка"}))
@router.message(Command("subscribe"))
@router.message(Command("tariffs"))
@router.callback_query(F.data == "open_subscription_menu")
async def cmd_subscription_menu(event):
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
        await event.answer()
    else:
        message = event
        user_id = message.from_user.id

    user = await database.get_or_create_user(user_id)
    is_adm = config.is_admin(user_id)
    current = (user.get("tariff_plan") or "").lower() if user.get("is_allowed") else ""

    text = (
        "<b>Тарифы Avito Radar</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Сейчас: <b>{access_status(user, user_id)}</b>\n\n"
        f"{plan_summary('lite')}\n\n"
        f"{plan_summary('standart')}\n\n"
        f"{plan_summary('pro')}\n"
        "    + догон свежих лотов после технических пауз\n\n"
        "Чем чаще проверка, тем раньше вы увидите лот и успеете позвонить первым.\n"
        "Оплата разовая на 30 дней, без автосписаний: СБП, Telegram Stars или криптовалюта. "
        "Оплаченные дни суммируются с текущим сроком."
    )

    buttons = []
    if not is_adm and not user.get("subscription_until") and not user.get("trial_used"):
        buttons.append([InlineKeyboardButton(text="🎁 Пробный период 6 часов", callback_data="activate_trial_btn")])
    for plan in config.PAID_PLANS:
        mark = " · ваш" if plan == current else ""
        buttons.append([InlineKeyboardButton(
            text=f"{PLAN_ICONS[plan]} {config.plan_title(plan)} — {config.rub_price(plan)} ₽{mark}",
            callback_data=f"buy_tariff:{plan}",
        )])
    buttons.append([
        InlineKeyboardButton(text="📄 Условия", callback_data="payment_terms"),
        InlineKeyboardButton(text="💬 Вопрос по оплате", callback_data="payment_support")
    ])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="start_approved")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


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
        f"{PLAN_ICONS[plan]} <b>Тариф «{title}»</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• Радаров: <b>до {config.plan_radars(plan)}</b>\n"
        f"• Проверка каждого радара: <b>раз в {config.plan_interval(plan)} с</b>\n"
        "• Стоп-слова, чёрный список продавцов, поиск по заголовку\n"
        f"• Цена: <b>{price_label}</b> за {'1 день' if is_test else '30 дней'}\n\n"
        f"{PLAN_PERKS[plan]}\n\n"
        "Разовая оплата без автосписаний, доступ включается сразу после оплаты. "
        "Оплачивая, вы соглашаетесь с /terms.\n\n"
        "<b>Выберите способ оплаты:</b>"
    )

    buttons = []
    if not is_test:
        buttons.append([InlineKeyboardButton(text=f"💳 СБП — {config.rub_price(tariff_key)} ₽",
                                             callback_data=f"pay_method:sbp:{tariff_key}")])
    if config.STARS_PAYMENTS_ENABLED:
        buttons.append([InlineKeyboardButton(text=f"⭐ Telegram Stars — {stars} ⭐",
                                             callback_data=f"stars_buy:{tariff_key}:{stars}")])
    else:
        text += "\n\n⏸ Оплата Telegram Stars временно выключена."
    if not is_test:
        buttons.append([InlineKeyboardButton(text="🪙 Криптовалюта", callback_data=f"pay_method:crypto:{tariff_key}")])
    buttons.append([
        InlineKeyboardButton(text="« Все тарифы", callback_data="open_subscription_menu"),
        InlineKeyboardButton(text="💬 Вопрос по оплате", callback_data="payment_support")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


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

@router.message(F.text.in_({"📄 Документы", "Документы"}))
@router.message(Command("docs"))
@router.callback_query(F.data == "open_docs_menu")
async def cmd_docs_hub(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
        try:
            await message.edit_text(DOCS_HUB_TEXT, reply_markup=get_docs_keyboard(), parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(DOCS_HUB_TEXT, reply_markup=get_docs_keyboard(), parse_mode="HTML")


@router.message(Command("agreement", "offer", "contract"))
@router.callback_query(F.data == "doc_agreement")
async def cmd_doc_agreement(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
        try:
            await message.edit_text(AGREEMENT_TEXT, reply_markup=get_doc_nav_keyboard(), parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(AGREEMENT_TEXT, reply_markup=get_doc_nav_keyboard(), parse_mode="HTML")


@router.message(Command("privacy", "policy"))
@router.callback_query(F.data == "doc_privacy")
async def cmd_doc_privacy(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
        try:
            await message.edit_text(PRIVACY_TEXT, reply_markup=get_doc_nav_keyboard(), parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(PRIVACY_TEXT, reply_markup=get_doc_nav_keyboard(), parse_mode="HTML")


# ── Служба поддержки и контакты ───────────────────────────────────────

@router.message(Command("support", "contacts"))
@router.message(F.text.in_({"💬 Поддержка", "Поддержка", "Контакты", "Помощь", "SOS"}))
@router.callback_query(F.data == "open_support_stub")
async def cmd_support(event, state: Optional[FSMContext] = None):
    if state:
        await state.clear()
    if isinstance(event, CallbackQuery):
        message = event.message
        await event.answer()
    else:
        message = event

    text = (
        "<b>Служба поддержки клиентов</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Сервис мониторинга объявлений: <b>Avito Radar</b>\n\n"
        "По вопросам работы сервиса, оплаты тарифов, возвратов и технической консультации:\n"
        "• <b>Email:</b> bernardgofov@gmail.com\n"
        "• <b>Поддержка в Telegram:</b> тикет-система прямо в боте (кнопка ниже)\n"
        "• <b>График работы:</b> ежедневно с 09:00 до 23:00 (МСК)\n"
        "• <b>Регламент ответа:</b> 5–15 минут (возвраты: в течение 24 часов)\n\n"
        "Нажмите кнопку ниже, чтобы задать вопрос — оператор ответит вам в этом чате."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✍️ Задать вопрос / Написать в поддержку", callback_data="support_start_ticket")
        ],
        [
            InlineKeyboardButton(text="📄 Документы", callback_data="open_docs_menu"),
            InlineKeyboardButton(text="💳 Выбрать тариф", callback_data="open_subscription_menu")
        ],
        [
            InlineKeyboardButton(text="👤 В профиль", callback_data="refresh_profile"),
            InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")
        ]
    ])
    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "support_stub_click")
async def cb_support_stub(call: CallbackQuery):
    await call.answer()
    await cmd_support(call)


# ── Чёрный список продавцов (Бан в 1 клик) ─────────────────────────────

@router.callback_query(F.data == "manage_blacklist")
async def cb_manage_blacklist(call: CallbackQuery):
    user_id = call.from_user.id
    blocked = await database.get_user_blocked_sellers_list(user_id)

    if not blocked:
        text = (
            "<b>Чёрный список продавцов</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Ваш чёрный список пока пуст.\n\n"
            "Чтобы заблокировать продавца, нажмите кнопку «Забанить продавца» под карточкой лота. "
            "Объявления от него больше не будут поступать ни по одному из ваших радаров."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="« Назад в профиль", callback_data="refresh_profile"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="start_approved")
            ]
        ])
    else:
        text = (
            f"<b>Чёрный список продавцов ({len(blocked)})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Объявления от указанных продавцов автоматически блокируются:\n\n"
            "<i>Нажмите на продавца для разблокировки:</i>"
        )
        buttons = []
        for b in blocked[:12]:
            buttons.append([
                InlineKeyboardButton(text=f"❌ {b['seller_name'][:25]}", callback_data=f"unban_seller:{b['id']}")
            ])
        buttons.append([
            InlineKeyboardButton(text="« Назад в профиль", callback_data="refresh_profile"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="start_approved")
        ])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("unban_seller:"))
async def cb_unban_seller(call: CallbackQuery):
    block_id = int(call.data.split(":")[1])
    await database.remove_blocked_seller_by_id(call.from_user.id, block_id)
    await call.answer("✅ Продавец разблокирован!", show_alert=True)
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
        msg_text = call.message.caption or call.message.text or ""
        m = re.search(r"👤\s*<b>Продавец:</b>\s*([^·\n<]+)", msg_text)
        if not m:
            m = re.search(r"👤\s*Продавец:\s*([^·\n]+)", msg_text)
        if m:
            seller_name = m.group(1).strip()

    if not seller_name or seller_name.lower() in {"продавец", "компания", "частное лицо"}:
        await call.answer("Не удалось определить имя продавца для блокировки", show_alert=True)
        return

    await database.add_blocked_seller(user_id, seller_name)
    log_user_event(user_id, call.from_user.username, call.from_user.first_name, "Блокировка продавца", f"Продавец «{seller_name}» добавлен в ЧС", status="OK")
    logger.info(f"🚷 [ЧЕРНЫЙ СПИСОК] Пользователь ID:{user_id} заблокировал продавца «{seller_name}»")

    await call.answer(
        f"🚷 Продавец «{seller_name}» добавлен в ваш Чёрный список!\nЛоты от него больше не будут приходить.",
        show_alert=True
    )

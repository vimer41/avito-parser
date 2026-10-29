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
    is_adm = config.is_admin(user_id)
    sub_until = user_record.get("subscription_until")
    trial_used = bool(user_record.get("trial_used"))
    is_allowed = bool(user_record.get("is_allowed"))

    if is_adm:
        badge = "Администратор"
    elif sub_until and trial_used and not is_allowed:
        formatted_until = database.format_subscription_date(sub_until)
        badge = f"Пробный доступ (до {formatted_until})"
    elif sub_until and is_allowed:
        plan_name = (user_record.get("tariff_plan") or "Standart").upper()
        formatted_until = database.format_subscription_date(sub_until)
        badge = f"Подписка {plan_name} (до {formatted_until})"
    elif is_allowed:
        badge = "Доступ активен"
    else:
        badge = "Подписка не активна"

    text = (
        "<b>Avito Radar</b> — автоматизированный мониторинг объявлений\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Пользователь: <b>{html.escape(first_name)}</b>\n"
        f"Статус: <b>{badge}</b>\n\n"
        "Сервис непрерывно проверяет новые объявления на Авито по заданным параметрам "
        "и отправляет подходящие лоты сразу после обнаружения.\n\n"
        "<b>Как запустить мониторинг:</b>\n"
        "1. Настройте поиск и фильтры на <code>avito.ru</code> в браузере.\n"
        "2. Скопируйте ссылку и отправьте её в этот чат.\n\n"
        "Для управления используйте кнопки меню ниже."
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
        "<b>Инструкция по работе с сервисом Avito Radar</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1. Добавление поискового радара</b>\n"
        "• Откройте <code>avito.ru</code> в любом браузере.\n"
        "• Задайте поисковый запрос и выставьте нужные фильтры (город, цену, состояние лота).\n"
        "• Скопируйте ссылку из адресной строки и отправьте её боту в чат.\n"
        "• Сортировку по дате бот применяет автоматически.\n\n"
        "<b>2. Мгновенная доставка и догон после паузы (PRO)</b>\n"
        "• Все радары опрашиваются с единым целевым интервалом 15–20 секунд.\n"
        "• На тарифе PRO уведомления отправляются мгновенно (0.0 с задержки) с наивысшим приоритетом.\n"
        "• После технических пауз пользователи PRO получают до 10 самых свежих объявлений моложе 30 минут с пометкой «🕒 найдено после паузы».\n\n"
        "<b>3. Фильтрация и защита от спама</b>\n"
        "• <b>Стоп-слова:</b> отсекают объявления с указанными словами (например, «запчасти», «реплика»).\n"
        "• <b>Чёрный список:</b> блокировка продавцов и спам-магазинов в 1 клик под карточкой лота.\n"
        "• <b>Область поиска:</b> переключение поиска строго в заголовках или в заголовке и описании.\n\n"
        "<b>4. Тарифные планы</b>\n"
        "• Всем новым пользователям доступен ознакомительный период (6 часов, 1 радар).\n"
        f"• <b>Standart ({config.STARS_PRICE_STANDART} ⭐ / 30 дней):</b> до 4 радаров, фото лотов, фильтрация стоп-слов, чёрный список.\n"
        f"• <b>PRO ({config.STARS_PRICE_PRO} ⭐ / 30 дней):</b> до 9 радаров, мгновенная доставка (0.0 с), догон свежих лотов после паузы.\n\n"
        "Официальные регламенты и правила: /docs, /agreement, /privacy. Поддержка: /paysupport."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 Тарифы и подписка", callback_data="open_subscription_menu")
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
    is_allowed = bool(user.get("is_allowed"))

    max_radars = await database.get_user_max_radars(user_id)

    if is_adm:
        status_str = "Администратор (PRO)"
    elif sub_until and trial_used and not is_allowed:
        formatted_until = database.format_subscription_date(sub_until)
        status_str = f"Пробный период (до {formatted_until})"
    elif sub_until and is_allowed:
        plan_name = (user.get("tariff_plan") or "Standart").upper()
        formatted_until = database.format_subscription_date(sub_until)
        status_str = f"Подписка {plan_name} (до {formatted_until})"
    elif is_allowed:
        status_str = "Доступ активен"
    else:
        status_str = "Подписка не активна"

    from scheduler import get_last_scan_info
    scan_info = get_last_scan_info()
    blocked_sellers_list = await database.get_user_blocked_sellers_list(user_id)
    blocked_count = len(blocked_sellers_list)

    text = (
        "<b>Личный кабинет</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>ID аккаунта:</b> <code>{user_id}</code>\n"
        f"• <b>Тарифный статус:</b> {status_str}\n"
        f"• <b>Радары:</b> {active_count} активных из {searches_count} (лимит: {max_radars})\n"
        f"• <b>Чёрный список:</b> {blocked_count} продавцов\n"
        f"• <b>Состояние мониторинга:</b> {scan_info}\n"
    )

    buttons = []
    if not is_adm and not sub_until and not trial_used:
        buttons.append([InlineKeyboardButton(text="Активировать пробный период 6 часов", callback_data="activate_trial_btn")])

    if is_adm:
        sub_btn_text = "Тарифные планы"
    elif sub_until:
        sub_btn_text = "Продлить / сменить тариф"
    else:
        sub_btn_text = "💳 Выбрать тариф"
    buttons.append([InlineKeyboardButton(text=sub_btn_text, callback_data="open_subscription_menu")])

    buttons.append([
        InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt"),
        InlineKeyboardButton(text=f"📁 Мои радары ({searches_count})", callback_data="list_searches")
    ])
    buttons.append([
        InlineKeyboardButton(text=f"Чёрный список ({blocked_count})", callback_data="manage_blacklist"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_profile")
    ])
    buttons.append([
        InlineKeyboardButton(text="📄 Документы", callback_data="open_docs_menu"),
        InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")
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
    sub_until = user.get("subscription_until")
    trial_used = bool(user.get("trial_used"))
    is_allowed = bool(user.get("is_allowed"))

    if is_adm:
        status_line = f"<b>Текущий статус:</b> Администратор (лимит: {await database.get_user_max_radars(user_id)} радаров)"
    elif sub_until and trial_used and not is_allowed:
        formatted_until = database.format_subscription_date(sub_until)
        status_line = f"<b>Текущий статус:</b> Пробный доступ (до {formatted_until})"
    elif sub_until and is_allowed:
        plan_name = (user.get("tariff_plan") or "Standart").upper()
        formatted_until = database.format_subscription_date(sub_until)
        status_line = f"<b>Текущий статус:</b> Подписка {plan_name} (до {formatted_until})"
    elif is_allowed:
        status_line = "<b>Текущий статус:</b> Доступ активен"
    else:
        status_line = "<b>Текущий статус:</b> Подписка не активна. Выберите тариф для продления:"

    text = (
        "<b>Тарифные планы · Avito Radar</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{status_line}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Standart:</b> {config.STARS_PRICE_STANDART} ⭐ / 30 дней\n"
        "  До 4 радаров, фото лотов, фильтрация стоп-слов, чёрный список продавцов.\n\n"
        f"• <b>PRO:</b> {config.STARS_PRICE_PRO} ⭐ / 30 дней\n"
        "  До 9 радаров, мгновенная доставка (0.0 с), догон свежих лотов после паузы (до 30 мин), приоритетная очередь, поиск в заголовках.\n\n"
        "Оплата производится через Telegram Stars. Разовая подписка на 30 дней без автоматических списаний.\n\n"
        f"{'Приём платежей включён.' if config.STARS_PAYMENTS_ENABLED else 'Приём платежей временно выключен.'}\n\n"
        "<i>Выберите интересующий тариф для просмотра параметров:</i>"
    )

    buttons = []
    if not is_adm and not sub_until and not trial_used:
        buttons.append([
            InlineKeyboardButton(text="Активировать пробный период 6 часов", callback_data="activate_trial_btn")
        ])

    buttons.append([
        InlineKeyboardButton(text=f"Standart — {config.STARS_PRICE_STANDART} ⭐ / 30 дней", callback_data="buy_tariff:standart")
    ])
    buttons.append([
        InlineKeyboardButton(text=f"PRO — {config.STARS_PRICE_PRO} ⭐ / 30 дней", callback_data="buy_tariff:pro")
    ])
    buttons.append([
        InlineKeyboardButton(text="💳 Другие способы оплаты", callback_data="other_payment_methods")
    ])
    buttons.append([
        InlineKeyboardButton(text="📄 Документы", callback_data="open_docs_menu"),
        InlineKeyboardButton(text="💬 Поддержка", callback_data="support_start_ticket")
    ])
    buttons.append([
        InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("buy_tariff:"))
async def cb_select_tariff(call: CallbackQuery):
    tariff_key = call.data.split(":")[1]
    if tariff_key not in ("standart", "pro", "test"):
        await call.answer("Неизвестный тариф", show_alert=True)
        return
    tariffs = {
        "test": {
            "name": "Тестовый доступ",
            "radars": 4,
            "price": f"{config.STARS_PRICE_TEST} ⭐ / 1 день",
            "price_num": f"{config.STARS_PRICE_TEST} ⭐",
            "perks": (
                "• Тестовый доступ на 1 день для проверки сервиса\n"
                "• До 4 активных радаров одновременно\n"
                "• Опрос каждые 15–20 сек., оповещения с ценой и ссылкой"
            )
        },
        "standart": {
            "name": "Standart",
            "radars": 4,
            "price": f"{config.STARS_PRICE_STANDART} ⭐ / 30 дней",
            "price_num": f"{config.STARS_PRICE_STANDART} ⭐",
            "perks": (
                "• До 4 активных радаров одновременно\n"
                f"• Целевой интервал опроса: {int(config.SCAN_INTERVAL_MIN)}–{int(config.SCAN_INTERVAL_MAX)} сек.\n"
                "• Оповещения с фото, ценой и ссылкой\n"
                "• Фильтрация по стоп-словам\n"
                "• Чёрный список продавцов"
            )
        },
        "pro": {
            "name": "PRO Мониторинг",
            "radars": 9,
            "price": f"{config.STARS_PRICE_PRO} ⭐ / 30 дней",
            "price_num": f"{config.STARS_PRICE_PRO} ⭐",
            "perks": (
                "• До 9 активных радаров одновременно\n"
                "• Мгновенная доставка уведомлений без задержки (0.0 с)\n"
                "• Авто-догон до 10 свежих лотов (моложе 30 мин) после паузы\n"
                "• Точный поиск строго в названиях\n"
                "• Оповещения с фото, фильтрация стоп-слов и чёрный список"
            )
        }
    }
    selected = tariffs.get(tariff_key, tariffs["standart"])
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Выбор тарифа", f"Тариф {selected['name']} ({selected['price']})", status="INFO")

    text = (
        f"<b>Тарифный план: {selected['name']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Лимит радаров:</b> до {selected['radars']} одновременно\n"
        f"• <b>Стоимость:</b> {selected['price']}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Параметры тарифа:</b>\n"
        f"{selected['perks']}\n\n"
        "Разовая оплата на 30 дней без автосписаний. Доступ активируется сразу после подтверждения транзакции. "
        "Условия обслуживания: /terms | Поддержка и возврат: /paysupport"
    )

    nav_buttons = [
        InlineKeyboardButton(text="« Назад к тарифам", callback_data="open_subscription_menu"),
        InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")
    ]
    buttons = []
    if config.STARS_PAYMENTS_ENABLED:
        buttons.append([InlineKeyboardButton(text=f"Согласен с условиями — оплатить {selected['price_num']}",
                                             callback_data=f"stars_buy:{tariff_key}:{config.stars_price(tariff_key)}")])
    else:
        text += "\n\nПриём платежей временно выключен."
    buttons.append([
        InlineKeyboardButton(text="💳 Другие способы оплаты", callback_data="other_payment_methods")
    ])
    buttons.append([InlineKeyboardButton(text="📄 Условия подписки", callback_data="payment_terms"),
                    InlineKeyboardButton(text="💬 Платёжная поддержка", callback_data="payment_support")])
    buttons.append(nav_buttons)
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "other_payment_methods")
async def cb_other_payment_methods(call: CallbackQuery):
    text = (
        "<b>ДРУГИЕ СПОСОБЫ ОПЛАТЫ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Основной автоматический способ оплаты — <b>Telegram Stars</b> (моментальное зачисление).\n\n"
        "Для оплаты банковскими картами, через СБП или для юридических лиц доступно выставление счёта через поддержку. "
        "Служба поддержки поможет с подбором параметров и индивидуальной настройки доступа.\n\n"
        "• Официальный email: <b>bernardgofov@gmail.com</b>\n"
        "• Написать в поддержку: кнопка ниже."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить через Telegram Stars", callback_data="open_subscription_menu")],
        [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="support_start_ticket")],
        [InlineKeyboardButton(text="« Назад к тарифам", callback_data="open_subscription_menu")]
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


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

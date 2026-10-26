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


def get_main_keyboard(user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    """Главная клавиатура бота."""
    kb = [
        [KeyboardButton(text="🎯 Добавить радар"), KeyboardButton(text="📁 Мои радары")],
        [KeyboardButton(text="💳 Тарифы"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="📖 Инструкция"), KeyboardButton(text="💬 Поддержка")]
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
        badge = "👑 <b>Администратор системы</b>"
    elif sub_until and trial_used and not is_allowed:
        formatted_until = database.format_subscription_date(sub_until)
        badge = f"🎁 <b>Бесплатный триал (5 ч · до 1 радара)</b>\n<i>└ Доступен: {formatted_until}</i>"
    elif sub_until and is_allowed:
        plan_name = (user_record.get("tariff_plan") or "Standart").upper()
        formatted_until = database.format_subscription_date(sub_until)
        badge = f"🟢 <b>{plan_name} Подписка</b>\n<i>└ Действует: {formatted_until}</i>"
    elif is_allowed:
        badge = "🟢 <b>Бессрочный доступ</b>"
    else:
        badge = "🔴 <b>Доступ истёк — выберите тариф</b>"

    text = (
        f"⚡️ <b>AVITO RADAR</b> · <i>Умный перехват новых лотов</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Привет, <b>{html.escape(first_name)}</b>! {badge}\n\n"
        f"Я проверяю новые объявления на <b>Авито</b> и присылаю подходящие лоты после обнаружения.\n\n"
        f"🚀 <b>Ключевые преимущества:</b>\n"
        f"• ⚡️ <b>Приоритетная очередь</b> для PRO\n"
        f"• 📸 <b>Фото товара</b> — при недоступности фото доставка текстом\n"
        f"• 🚷 <b>Чёрный список продавцов</b> — бан спам-магазинов в 1 клик\n"
        f"• 🛡 <b>Фильтр стоп-слов</b> — защита от реплик, брака и мусора\n\n"
        f"💡 <b>Как запустить радар:</b>\n"
        f"Скопируйте ссылку с фильтрами с <code>avito.ru</code> и отправьте её сюда в чат!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt"),
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")
        ],
        [
            InlineKeyboardButton(text="💳 Тарифы и подписка", callback_data="open_subscription_menu"),
            InlineKeyboardButton(text="📖 Инструкция", callback_data="open_help_menu")
        ],
        [
            InlineKeyboardButton(text="👤 Мой профиль", callback_data="refresh_profile"),
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
        "📖 <b>РУКОВОДСТВО ПОЛЬЗОВАТЕЛЯ AVITO RADAR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎯 <b>1. Как настроить радар:</b>\n"
        "1. Зайдите на <b>avito.ru</b> в браузере (Chrome, Safari, Яндекс).\n"
        "2. Введите название нужного товара и выставите фильтры (город, цену, состояние).\n"
        "3. Скопируйте ссылку из адресной строки и отправьте её боту в чат.\n"
        "<i>Сортировку «сначала свежие» бот применит сам автоматически!</i>\n\n"
        "📉 <b>2. Отслеживание скидок продавцов (Price Drop Alert) 💎:</b>\n"
        "Уникальная фича для байеров и перекупщиков:\n"
        "• Бот фиксирует первоначальную цену каждого лота в базе данных.\n"
        "• Если продавец <b>скинет цену</b> на указанный вами процент (например, -10% или -20%) — бот моментально пришлёт повторный алерт с расчётом вашей выгоды в рублях!\n"
        "• <b>Где включить:</b>\n"
        "  — В меню «📁 Мои радары» по кнопке <b>«📉 Настройка скидок (PRO)»</b>\n"
        "  — В карточке конкретного радара по верхней кнопке <b>«📉 Алерт на скидки»</b>\n"
        "  — Сразу при добавлении нового радара\n"
        "  — Доступны пресеты (-5%, -10%, -15%, -20%, -30%, -50%) или ввод <b>любого своего процента от 1% до 99%</b>.\n\n"
        "🔍 <b>3. Область выборки (Только в названиях / Название + описание):</b>\n"
        "По умолчанию поиск ведётся везде — в заголовках и описаниях объявлений.\n"
        "• Нажмите <b>«🔍 Выборка: Только в названиях 🏷»</b> — бот будет отбирать лоты строго с указанным товаром в заголовке, отсекая спам, аксессуары и чехлы!\n"
        "• В любой момент можно переключить обратно на <b>«Название + описание 📄»</b> в один клик.\n\n"
        "🛡 <b>4. Умные стоп-слова:</b>\n"
        "Отсекайте мусор: <i>«на запчасти», «реплика», «дефект», «копия»</i>.\n"
        "Добавлять стоп-слова можно как в настройках радара, так и по кнопке <b>«🛡 Стоп-слово»</b> прямо под уведомлением о товаре.\n\n"
        "🚷 <b>5. Чёрный список продавцов:</b>\n"
        "Нажмите <b>«🚷 Забанить продавца»</b> под карточкой любого лота, чтобы навсегда заблокировать спам от назойливых магазинов.\n\n"
        "⚡️ <b>6. Быстрый отклик продавцу:</b>\n"
        "Настройте шаблон в меню «⚡️ Быстрый отклик», и бот будет формировать ссылку для связи с продавцом в 1 клик.\n\n"
        "🎁 <b>7. Бесплатный триал и тарифы:</b>\n"
        "• Всем новым пользователям автоматически доступен <b>5-часовой бесплатный триал на 1 радар</b>.\n"
        f"• <b>Standart ({config.STARS_PRICE_STANDART} ⭐ / 30 дней):</b> до 4 радаров, фото, стоп-слова и ЧС.\n"
        f"• <b>PRO ({config.STARS_PRICE_PRO} ⭐ / 30 дней):</b> до 9 радаров, приоритетная очередь и алерты на скидки.\n"
        "Оплата только Telegram Stars, без автосписания. Условия: /terms. Поддержка платежей: /paysupport."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 Тарифы и подписка", callback_data="open_subscription_menu"),
            InlineKeyboardButton(text="📉 Настройка скидок (PRO) 💎", callback_data="all_discounts_menu")
        ],
        [
            InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt"),
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")
        ],
        [
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
        await event.answer("Профиль обновлен!")
    else:
        message = event
        user_id = message.from_user.id

    user = await database.get_or_create_user(user_id)
    searches = await database.get_user_searches(user_id)
    searches_count = len(searches)
    active_count = sum(1 for s in searches if s.get("is_active"))

    current_tmpl = user.get("default_template") or database.DEFAULT_SELLER_MESSAGE
    is_adm = config.is_admin(user_id)
    sub_until = user.get("subscription_until")
    trial_used = bool(user.get("trial_used"))
    is_allowed = bool(user.get("is_allowed"))

    max_radars = await database.get_user_max_radars(user_id)

    if is_adm:
        status_str = "👑 <b>Администратор (Вечный PRO)</b>"
    elif sub_until and trial_used and not is_allowed:
        formatted_until = database.format_subscription_date(sub_until)
        status_str = f"🎁 <b>Бесплатный триал (5 ч)</b>\n  └ <i>Доступен: {formatted_until}</i>"
    elif sub_until and is_allowed:
        plan_name = (user.get("tariff_plan") or "Standart").upper()
        formatted_until = database.format_subscription_date(sub_until)
        status_str = f"🟢 <b>{plan_name} Подписка</b>\n  └ <i>Действует: {formatted_until}</i>"
    elif is_allowed:
        status_str = "🟢 <b>VIP Доступ (Одобрен)</b>"
    else:
        status_str = "🔴 <b>Подписка истекла</b>"

    from scheduler import get_last_scan_info
    scan_info = get_last_scan_info()
    blocked_sellers_list = await database.get_user_blocked_sellers_list(user_id)
    blocked_count = len(blocked_sellers_list)

    text = (
        "┌ 👤 <b>ЛИЧНЫЙ КАБИНЕТ</b>\n"
        f"├ 🆔 <b>Ваш ID:</b> <code>{user_id}</code>\n"
        f"├ ⚡️ <b>Статус:</b> {status_str}\n"
        f"├ 🎯 <b>Радары:</b> <b>{active_count}</b> акт. / <b>{searches_count}</b> всего (макс: {max_radars})\n"
        f"├ 🚷 <b>Чёрный список:</b> <b>{blocked_count}</b> продавцов\n"
        f"├ 📡 <b>Проверка Авито:</b> <code>{scan_info}</code> 🟢\n"
        f"└ 🔔 <b>Доставка лотов:</b> ⚡️ Мгновенно (с фото HD)"
    )

    buttons = []
    # Если нет подписки и триал еще не использован — предлагаем бесплатный триал 6 часов
    if not is_adm and not sub_until and not trial_used:
        buttons.append([InlineKeyboardButton(text="🎁 Попробовать 6 часов бесплатно", callback_data="activate_trial_btn")])

    # Кнопка подписки/тарифов (видна и пользователям, и администраторам)
    if is_adm:
        sub_btn_text = "👑 Тарифные планы (PRO активен)"
    elif sub_until:
        sub_btn_text = "💎 Продлить / Сменить тариф"
    else:
        sub_btn_text = "💳 Выбрать тариф (Подписка)"
    buttons.append([InlineKeyboardButton(text=sub_btn_text, callback_data="open_subscription_menu")])

    buttons.append([
        InlineKeyboardButton(text="📉 Настройка скидок (PRO) 💎", callback_data="all_discounts_menu")
    ])

    buttons.append([
        InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt"),
        InlineKeyboardButton(text=f"📁 Мои радары ({searches_count})", callback_data="list_searches")
    ])
    buttons.append([
        InlineKeyboardButton(text=f"🚷 Чёрный список ({blocked_count})", callback_data="manage_blacklist"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_profile")
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


@router.callback_query(F.data == "activate_trial_btn")
async def cb_activate_trial(call: CallbackQuery):
    res = await database.activate_trial(call.from_user.id)
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Активация триала", res["message"], status="OK" if res["success"] else "WARN")
    if res["success"]:
        formatted_until = database.format_subscription_date(res.get("until"))
        await call.answer(f"🎉 Пробный период 6 часов активирован!\nДействует: {formatted_until}", show_alert=True)
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
        status_line = f"👑 <b>Ваш статус:</b> <i>Администратор (PRO, до {config.MAX_SEARCHES_PER_USER} радаров)</i>"
    elif sub_until and trial_used and not is_allowed:
        formatted_until = database.format_subscription_date(sub_until)
        status_line = f"🎁 <b>Ваш статус:</b> <i>Бесплатный триал (5 ч)\n  └ Доступен: {formatted_until}</i>"
    elif sub_until and is_allowed:
        plan_name = (user.get("tariff_plan") or "Standart").upper()
        formatted_until = database.format_subscription_date(sub_until)
        status_line = f"🟢 <b>Ваш статус:</b> <i>Подписка {plan_name}\n  └ Действует: {formatted_until}</i>"
    elif is_allowed:
        status_line = "🟢 <b>Ваш статус:</b> <i>VIP Доступ одобрен</i>"
    else:
        status_line = "🔴 <b>Ваш статус:</b> <i>Подписка истекла. Выберите тариф ниже для продления:</i>"

    text = (
        "💳 <b>ТАРИФНЫЕ ПЛАНЫ · AVITO RADAR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{status_line}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡️ <b>STANDART ({config.STARS_PRICE_STANDART} ⭐ / 30 дней)</b> — до 4 радаров, фото, стоп-слова, ЧС\n"
        f"🔥 <b>PRO ({config.STARS_PRICE_PRO} ⭐ / 30 дней)</b> — до 9 радаров + <b>Price Drop 💎</b>\n\n"
        "⭐ <b>Моментальная оплата Telegram Stars.</b>\n"
        "Разовая оплата за 30 дней доступа, без регулярных списаний и комиссий.\n\n"
        f"{'Приём платежей включён.' if config.STARS_PAYMENTS_ENABLED else 'Приём платежей временно выключен.'}\n\n"
        "<i>💡 Выберите тариф ниже для просмотра подробностей:</i>"
    )

    buttons = []
    # Если триал ещё не использован (для старых аккаунтов)
    if not is_adm and not sub_until and not trial_used:
        buttons.append([
            InlineKeyboardButton(text="🎁 Активировать 6 часов БЕСПЛАТНО", callback_data="activate_trial_btn")
        ])

    buttons.append([
        InlineKeyboardButton(text=f"⚡️ Standart — {config.STARS_PRICE_STANDART} ⭐ / 30 дней", callback_data="buy_tariff:standart")
    ])
    buttons.append([
        InlineKeyboardButton(text=f"🔥 PRO — {config.STARS_PRICE_PRO} ⭐ / 30 дней", callback_data="buy_tariff:pro")
    ])
    buttons.append([
        InlineKeyboardButton(text="💳 Другие способы оплаты", callback_data="other_payment_methods")
    ])
    buttons.append([
        InlineKeyboardButton(text="👤 Мой профиль", callback_data="refresh_profile"),
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
            "name": "🧪 Тестовый доступ",
            "radars": 4,
            "price": f"{config.STARS_PRICE_TEST} ⭐ / 1 день",
            "price_num": f"{config.STARS_PRICE_TEST} ⭐",
            "perks": (
                "✔️ Тестовый доступ на <b>1 день</b> для проверки оплаты\n"
                "✔️ До <b>4 активных радаров</b> одновременно\n"
                "✔️ Моментальные оповещения с фото товара в HD\n"
                "✔️ Проверка списания и возврата Telegram Stars"
            )
        },
        "standart": {
            "name": "⚡️ Standart",
            "radars": 4,
            "price": f"{config.STARS_PRICE_STANDART} ⭐ / 30 дней",
            "price_num": f"{config.STARS_PRICE_STANDART} ⭐",
            "perks": (
                "✔️ До <b>4 активных радаров</b> одновременно\n"
                f"✔️ Базовый интервал: {config.CHECK_INTERVAL} сек., с учётом нагрузки и блокировок Авито\n"
                "✔️ Моментальные оповещения с фото товара в HD\n"
                "✔️ Фильтрация по стоп-словам\n"
                "✔️ Чёрный список продавцов в 1 клик"
            )
        },
        "pro": {
            "name": "🔥 PRO Мониторинг",
            "radars": 9,
            "price": f"{config.STARS_PRICE_PRO} ⭐ / 30 дней",
            "price_num": f"{config.STARS_PRICE_PRO} ⭐",
            "perks": (
                "✔️ До <b>9 активных радаров</b> одновременно\n"
                "✔️ 📉 <b>Алерты на снижение цены (Price Drop Alert) 💎</b>\n"
                "✔️ Точный поиск строго в названиях\n"
                "✔️ Моментальные оповещения с фото товара в HD\n"
                "✔️ Фильтрация по стоп-словам и чёрный список"
            )
        }
    }
    selected = tariffs.get(tariff_key, tariffs["standart"])
    log_user_event(call.from_user.id, call.from_user.username, call.from_user.first_name, "Выбор тарифа", f"Тариф {selected['name']} ({selected['price']})", status="INFO")

    text = (
        f"📦 <b>ТАРИФНЫЙ ПЛАН: {selected['name']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Лимит:</b> до <b>{selected['radars']} радаров</b> одновременно\n"
        f"💰 <b>Стоимость:</b> <b>{selected['price']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 <b>Возможности тарифа:</b>\n"
        f"{selected['perks']}\n\n"
        "⭐ <b>Оплата через Telegram Stars активна.</b>\n"
        "Разовая подписка на 30 дней, без автосписания. Доступ активируется мгновенно после подтверждения. "
        "Доступность и скорость зависят от Авито. Условия и возврат: /terms, поддержка: /paysupport."
    )

    nav_buttons = [
        InlineKeyboardButton(text="« Назад к тарифам", callback_data="open_subscription_menu"),
        InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")
    ]
    buttons = []
    if config.STARS_PAYMENTS_ENABLED:
        buttons.append([InlineKeyboardButton(text=f"⭐ Согласен с условиями — оплатить {selected['price_num']}",
                                             callback_data=f"stars_buy:{tariff_key}:{config.stars_price(tariff_key)}")])
    else:
        text += "\n\n⚠️ Приём платежей временно выключен."
    buttons.append([
        InlineKeyboardButton(text="💳 Другие способы оплаты", callback_data="other_payment_methods")
    ])
    buttons.append([InlineKeyboardButton(text="📃 Условия оплаты", callback_data="payment_terms"),
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
        "💳 <b>ДРУГИЕ СПОСОБЫ ОПЛАТЫ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⭐ Моментальная автоматическая оплата доступна через <b>Telegram Stars</b>.\n\n"
        "💳 <b>Оплата через оператора (по запросу):</b>\n"
        "• Банковские карты (МИР, Visa, Mastercard)\n"
        "• СБП (Система быстрых платежей)\n\n"
        "Для выставления индивидуального счёта и персональной настройки обратитесь в службу поддержки:\n"
        "📧 Email: <b>bernardgofov@gmail.com</b>\n"
        "💬 Нажмите кнопку «Написать в поддержку» ниже для создания тикета."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Оплатить через Telegram Stars", callback_data="open_subscription_menu")],
        [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="support_start_ticket")],
        [InlineKeyboardButton(text="« Назад к тарифам", callback_data="open_subscription_menu")]
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ── Служба поддержки и контакты ───────────────────────────────────────

@router.message(Command("support", "contacts", "help"))
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
        "📞 <b>КОНТАКТЫ И СЛУЖБА ПОДДЕРЖКИ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "По любым вопросам работы сервиса Avito Radar, оплате тарифов, возвратам и предложениям:\n\n"
        "📧 <b>Email:</b> bernardgofov@gmail.com\n"
        "💬 <b>Telegram-поддержка:</b> тикет-система прямо в боте (кнопка ниже)\n"
        "🕒 <b>График работы:</b> Ежедневно с 09:00 до 23:00 (МСК)\n"
        "⚡️ <b>Среднее время ответа:</b> 5–15 минут\n\n"
        "Нажмите кнопку ниже, чтобы задать вопрос — оператор ответит вам прямо в этом чате!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✍️ Задать вопрос / Написать в поддержку", callback_data="support_start_ticket")
        ],
        [
            InlineKeyboardButton(text="💳 Выбрать тариф", callback_data="open_subscription_menu"),
            InlineKeyboardButton(text="👤 В профиль", callback_data="refresh_profile")
        ],
        [
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
            "🚷 <b>ЧЁРНЫЙ СПИСОК ПРОДАВЦОВ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Ваш чёрный список пока пуст.\n\n"
            "💡 <b>Как блокировать продавцов:</b>\n"
            "Когда бот присылает объявление от спамера, перекупа или назойливого магазина, "
            "просто нажмите кнопку <b>«🚷 Забанить продавца»</b> под карточкой лота. "
            "Бот больше никогда не пришлёт от него ни одного объявления ни по одному радару!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="« Назад в профиль", callback_data="refresh_profile"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="start_approved")
            ]
        ])
    else:
        text = (
            f"🚷 <b>ЧЁРНЫЙ СПИСОК ПРОДАВЦОВ ({len(blocked)})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Объявления от этих продавцов автоматически блокируются до отправки:\n\n"
            "<i>Нажмите на продавца, чтобы разблокировать его:</i>"
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
    msg_text = call.message.caption or call.message.text or ""
    seller_name = None

    m = re.search(r"👤\s*<b>Продавец:</b>\s*([^·\n<]+)", msg_text)
    if not m:
        m = re.search(r"👤\s*Продавец:\s*([^·\n]+)", msg_text)

    if m:
        seller_name = m.group(1).strip()

    if not seller_name or seller_name.lower() in {"продавец", "компания", "частное лицо"}:
        await call.answer("Не удалось определить имя продавца для блокировки", show_alert=True)
        return

    user_id = call.from_user.id
    await database.add_blocked_seller(user_id, seller_name)
    log_user_event(user_id, call.from_user.username, call.from_user.first_name, "Блокировка продавца", f"Продавец «{seller_name}» добавлен в ЧС", status="OK")
    logger.info(f"🚷 [ЧЕРНЫЙ СПИСОК] Пользователь ID:{user_id} заблокировал продавца «{seller_name}»")

    await call.answer(
        f"🚷 Продавец «{seller_name}» добавлен в ваш Чёрный список!\nЛоты от него больше не будут приходить.",
        show_alert=True
    )

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
        badge = f"🎁 <b>Бесплатный триал (24 ч · до 2 радаров)</b>\n<i>└ Доступен: {formatted_until}</i>"
    elif sub_until and is_allowed:
        plan_name = (user_record.get("tariff_plan") or "Standart").upper()
        formatted_until = database.format_subscription_date(sub_until)
        badge = f"🟢 <b>{plan_name} Подписка</b>\n<i>└ Действует: {formatted_until}</i>"
    else:
        badge = "💎 <b>PRO Мониторинг</b>"

    text = (
        f"⚡️ <b>AVITO RADAR</b> · <i>Умный перехват новых лотов</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Привет, <b>{html.escape(first_name)}</b>! {badge}\n\n"
        f"Я отслеживаю новинки на <b>Авито в реальном времени</b> и присылаю "
        f"их за секунды до того, как их увидят перекупы и конкуренты.\n\n"
        f"🚀 <b>Ключевые преимущества:</b>\n"
        f"• ⚡️ <b>Приоритетная доставка</b> — получение новых лотов раньше всех\n"
        f"• 📸 <b>Фотографии в HD</b> — 100% доставка без блокировок\n"
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
        "По умолчанию Авито ищет совпадения везде — и в заголовках, и в текстах описаний (из-за чего часто приходят чехлы, переходники и спам со списком моделей внизу текста).\n"
        "• В карточке любого радара нажмите кнопку <b>«🔍 Выборка: Только в названиях 🏷»</b> — бот включит серверный фильтр Авито (<code>bt=1</code>) и будет отбирать лоты <b>строго с указанным товаром в заголовке</b>!\n"
        "• В любой момент можно переключить обратно на <b>«Название + описание 📄»</b> в один клик.\n\n"
        "🛡 <b>4. Умные стоп-слова:</b>\n"
        "Отсекайте мусор: <i>«на запчасти», «реплика», «дефект», «копия»</i>.\n"
        "Добавлять стоп-слова можно как в настройках радара, так и по кнопке <b>«🛡 Стоп-слово»</b> прямо под уведомлением о товаре.\n\n"
        "🚷 <b>5. Чёрный список продавцов:</b>\n"
        "Нажмите <b>«🚷 Забанить продавца»</b> под карточкой любого лота, чтобы навсегда заблокировать спам от назойливых магазинов.\n\n"
        "⚡️ <b>6. Быстрый отклик продавцу:</b>\n"
        "Настройте шаблон в меню «⚡️ Быстрый отклик», и бот будет формировать ссылку для связи с продавцом в 1 клик.\n\n"
        "🎁 <b>7. Бесплатный триал и тарифы:</b>\n"
        "• Всем новым пользователям автоматически доступен <b>24-часовой бесплатный триал на 2 радара</b>.\n"
        "• <b>Тариф Standart (250 ₽ / мес):</b> до 5 радаров, моментальные HD-фото, стоп-слова и бан спамеров.\n"
        "• <b>Тариф PRO (700 ₽ / мес):</b> до 15 радаров, приоритет доставки №1, алерты на скидки продавцов (Price Drop 💎)."
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
        status_str = f"🎁 <b>Бесплатный триал (24 ч)</b>\n  └ <i>Доступен: {formatted_until}</i>"
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
    # Если нет подписки и триал еще не использован — предлагаем бесплатный триал 24 часа
    if not is_adm and not sub_until and not trial_used:
        buttons.append([InlineKeyboardButton(text="🎁 Попробовать 24 часа бесплатно", callback_data="activate_trial_btn")])

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
        await call.answer(f"🎉 Пробный период 24 часа активирован!\nДействует: {formatted_until}", show_alert=True)
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
        status_line = "👑 <b>Ваш статус:</b> <i>Администратор (Вечный PRO без ограничений)</i>"
    elif sub_until and trial_used and not is_allowed:
        formatted_until = database.format_subscription_date(sub_until)
        status_line = f"🎁 <b>Ваш статус:</b> <i>Бесплатный триал (24 ч)\n  └ Доступен: {formatted_until}</i>"
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
        "⚡️ <b>STANDART (250 ₽/мес)</b> — до 5 радаров, HD фото, стоп-слова, ЧС\n"
        "🔥 <b>PRO (700 ₽/мес)</b> — до 15 радаров + <b>Price Drop 💎</b> (скидки продавцов)\n\n"
        "<i>💡 Выберите тариф ниже для просмотра подробностей:</i>"
    )

    buttons = []
    # Если триал ещё не использован (для старых аккаунтов)
    if not is_adm and not sub_until and not trial_used:
        buttons.append([
            InlineKeyboardButton(text="🎁 Активировать 24 часа БЕСПЛАТНО", callback_data="activate_trial_btn")
        ])

    buttons.append([
        InlineKeyboardButton(text="⚡️ Standart (5 радаров) — 250 ₽ / мес", callback_data="buy_tariff:standart")
    ])
    buttons.append([
        InlineKeyboardButton(text="🔥 PRO (15 радаров + Скидки) — 700 ₽ / мес", callback_data="buy_tariff:pro")
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
    tariffs = {
        "standart": {
            "name": "⚡️ Standart",
            "radars": 5,
            "price": "250 ₽ / месяц",
            "price_num": "250 ₽",
            "perks": (
                "✔️ До <b>5 активных радаров</b> одновременно\n"
                "✔️ Молниеносный мониторинг раз в минуту\n"
                "✔️ Моментальные оповещения с фото товара в HD\n"
                "✔️ Фильтрация по стоп-словам\n"
                "✔️ Чёрный список продавцов в 1 клик"
            )
        },
        "pro": {
            "name": "🔥 PRO Мониторинг",
            "radars": 15,
            "price": "700 ₽ / месяц",
            "price_num": "700 ₽",
            "perks": (
                "✔️ До <b>15 активных радаров</b> одновременно\n"
                "✔️ ⚡️ <b>Приоритет доставки №1</b> (раньше всех)\n"
                "✔️ 📉 <b>Алерты на снижение цены (Price Drop Alert) 💎</b>\n"
                "✔️ Точный поиск строго в названиях (bt=1)\n"
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
        f"💰 <b>Стоимость:</b> <b>{selected['price']}</b> (30 дней)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 <b>Возможности тарифа:</b>\n"
        f"{selected['perks']}"
    )

    nav_buttons = [
        InlineKeyboardButton(text="« Назад к тарифам", callback_data="open_subscription_menu"),
        InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")
    ]
    demo_btn = [InlineKeyboardButton(text=f"💳 Подключить {selected['name']} — {selected['price_num']}", callback_data=f"demo_tariff:{tariff_key}")]
    kb = InlineKeyboardMarkup(inline_keyboard=[demo_btn, nav_buttons])

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("demo_tariff:"))
async def cb_demo_tariff(call: CallbackQuery):
    await call.answer(
        "💡 Автоматический приём платежей находится в процессе подключения. "
        "Сейчас для вас действует бесплатный ознакомительный доступ!",
        show_alert=True
    )



@router.callback_query(F.data.startswith("pay_request:"))
async def cb_pay_request(call: CallbackQuery):
    tariff_key = call.data.split(":")[1]
    tariff_info = {
        "standart": ("⚡️ Standart", "250 ₽"),
        "pro": ("🔥 PRO", "700 ₽")
    }
    t_name, t_price = tariff_info.get(tariff_key, ("Подписка", "250 ₽"))
    user = call.from_user
    user_id = user.id
    bot = call.bot

    log_user_event(user_id, user.username, user.full_name, "Заявка на оплату", f"Тариф {t_name} ({t_price})", status="CLICK")

    user_confirm_text = (
        f"✅ <b>Заявка на оплату тарифа {t_name} принята!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Сумма к оплате: <b>{t_price}</b>\n\n"
        "👨‍💻 Оператор поддержки уже получил вашу заявку и отправит реквизиты (СБП / карта / крипта) "
        "прямо в этот чат в течение 2-5 минут.\n\n"
        "<i>Если вам удобен конкретный банк или криптовалюта — просто напишите об этом следующим сообщением!</i>"
    )

    user_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать оператору", callback_data="support_start_ticket")],
        [
            InlineKeyboardButton(text="« Назад к тарифам", callback_data="open_subscription_menu"),
            InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")
        ]
    ])

    try:
        await call.message.edit_text(user_confirm_text, reply_markup=user_kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(user_confirm_text, reply_markup=user_kb, parse_mode="HTML")
    await call.answer("Заявка успешно отправлена операторам!")


    # Отправляем уведомление администраторам с кнопками быстрой активации в 1 клик
    admin_alert_text = (
        "🚨 <b>НОВАЯ ЗАЯВКА НА ОПЛАТУ ПОДПИСКИ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Пользователь:</b> {html.escape(user.full_name)}\n"
        f"🔗 <b>Username:</b> @{user.username or 'нет'}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"📦 <b>Тариф:</b> <b>{t_name}</b>\n"
        f"💰 <b>Сумма к оплате:</b> <b>{t_price}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "После получения перевода нажмите кнопку ниже для мгновенной выдачи доступа:"
    )

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✅ Активировать {t_name} (30 дн.)", callback_data=f"admin_grant_sub:{user_id}:{tariff_key}:30")
        ],
        [
            InlineKeyboardButton(text="✍️ Ответить / отправить реквизиты", callback_data=f"support_reply:{user_id}")
        ]
    ])

    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=admin_alert_text,
                reply_markup=admin_kb,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление о заявке админу {admin_id}: {e}")




# ── Служба поддержки и контакты ───────────────────────────────────────

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
        "💬 <b>СЛУЖБА ПОДДЕРЖКИ И ОБРАТНАЯ СВЯЗЬ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "По любым вопросам работы сервиса, настройке радаров, оплате тарифов или предложениям:\n\n"
        "🕒 <b>График работы:</b> Ежедневно с 09:00 до 23:00 (МСК)\n"
        "⚡️ <b>Среднее время ответа:</b> 5–15 минут\n\n"
        "Нажмите кнопку ниже, чтобы задать вопрос — оператор ответит вам прямо в этом боте!"
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

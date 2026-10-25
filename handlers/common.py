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

logger = logging.getLogger(__name__)
router = Router()


def get_main_keyboard(user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    """Главная клавиатура бота."""
    kb = [
        [KeyboardButton(text="🎯 Добавить радар"), KeyboardButton(text="📁 Мои радары")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="📖 Инструкция")],
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

    await database.get_or_create_user(user_id, username, first_name)
    is_adm = config.is_admin(user_id)
    badge = "👑 <b>Администратор системы</b>" if is_adm else "💎 <b>PRO Мониторинг</b>"

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
            InlineKeyboardButton(text="💳 Тарифы", callback_data="open_subscription_menu"),
            InlineKeyboardButton(text="📖 Инструкция", callback_data="open_help_menu")
        ]
    ])

    await message.answer(text, reply_markup=get_main_keyboard(user_id), parse_mode="HTML")
    await message.answer("👇 <i>Выберите действие в меню или отправьте ссылку на поиск с Авито:</i>", reply_markup=kb, parse_mode="HTML")


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
        "1. Зайдите на <b>avito.ru</b> через любой браузер (Chrome, Safari, Яндекс).\n"
        "2. Введите название товара (например: <i>iPhone 15 Pro 128</i>).\n"
        "3. Настройте нужный город/район и фильтры цен (например: <i>от 60 000 до 85 000 ₽</i>).\n"
        "4. Скопируйте адресную строку и пришлите её боту.\n"
        "<i>Сортировку «по новизне» бот выставит сам автоматически!</i>\n\n"
        "🛡 <b>2. Умные стоп-слова:</b>\n"
        "Исключайте нежелательные лоты: <i>«на запчасти», «реплика», «дефект», «копия»</i>.\n"
        "Добавлять стоп-слова можно как в настройках радара, так и нажав кнопку <b>«🚫 В стоп-слова»</b> прямо под пришедшим уведомлением.\n\n"
        "⚡️ <b>3. Быстрый отклик:</b>\n"
        "Под каждым лотом есть кнопка <b>«📋 Скопировать отклик»</b>.\n"
        "При нажатии текст готового сообщения сразу выводится в чат в формате <code>текст</code> — просто коснитесь его, чтобы скопировать и отправить продавцу за 2 секунды!\n\n"
        "⚡️ <b>4. Скорость уведомлений:</b>\n"
        "Благодаря TLS-спуфингу и обходу PoW-защиты Авито, оповещения приходят за считанные секунды после публикации лота."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt"),
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")
        ]
    ])
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

    if is_adm:
        status_str = "👑 <b>Администратор (Вечный PRO)</b>"
    elif sub_until:
        status_str = f"🟢 <b>PRO Подписка</b> (до {str(sub_until)[:16]})"
    elif user.get("is_allowed"):
        status_str = "🟢 <b>VIP Доступ (Одобрен)</b>"
    else:
        status_str = "⚪️ <b>Базовый доступ</b>"

    from scheduler import get_last_scan_info
    scan_info = get_last_scan_info()
    blocked_sellers_list = await database.get_user_blocked_sellers_list(user_id)
    blocked_count = len(blocked_sellers_list)

    text = (
        "┌ 👤 <b>ЛИЧНЫЙ КАБИНЕТ</b>\n"
        f"├ 🆔 <b>Ваш ID:</b> <code>{user_id}</code>\n"
        f"├ ⚡️ <b>Статус:</b> {status_str}\n"
        f"├ 🎯 <b>Радары:</b> <b>{active_count}</b> акт. / <b>{searches_count}</b> всего (макс: {config.MAX_SEARCHES_PER_USER})\n"
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
        InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt"),
        InlineKeyboardButton(text=f"📁 Мои радары ({searches_count})", callback_data="list_searches")
    ])
    buttons.append([
        InlineKeyboardButton(text=f"🚷 Чёрный список ({blocked_count})", callback_data="manage_blacklist"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_profile")
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
    if res["success"]:
        await call.answer("🎉 Пробный период 24 часа успешно активирован!", show_alert=True)
        await cmd_profile(call)
    else:
        await call.answer(res["message"], show_alert=True)


@router.message(Command("subscribe"))
@router.callback_query(F.data == "open_subscription_menu")
async def cmd_subscription_menu(event):
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
        await event.answer()
    else:
        message = event
        user_id = message.from_user.id

    is_adm = config.is_admin(user_id)
    admin_banner = "👑 <i>Режим администратора: у вас уже включен полный доступ ко всем функциям без ограничений.</i>\n\n" if is_adm else ""

    text = (
        "💳 <b>ТАРИФНЫЕ ПЛАНЫ ПОДПИСКИ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{admin_banner}"
        "Подписка снимает ограничения, позволяет отслеживать больше категорий и получать новые объявления с Авито мгновенно.\n\n"
        "<b>Доступные тарифы на 1 месяц:</b>\n\n"
        "⚡️ <b>Тариф «Standart»</b> — <b>250 ₽ / мес</b>\n"
        "├ 🎯 До <b>5</b> активных радаров\n"
        "├ 📡 Мониторинг раз в 60 секунд\n"
        "├ 🚷 Чёрный список продавцов (бан в 1 клик)\n"
        "└ 📸 Доставка с реальными фото товара в HD\n\n"
        "🔥 <b>Тариф «PRO»</b> — <b>700 ₽ / мес</b>\n"
        "├ 🎯 До <b>15</b> активных радаров\n"
        "├ ⚡️ <b>Приоритет №1</b> (получают пуш первыми)\n"
        "├ 🚷 Чёрный список продавцов (бан в 1 клик)\n"
        "└ 🛡 Фильтрация по стоп-словам\n\n"
        "Выберите тариф для подключения:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚡️ Standart (5 радаров) — 250 ₽", callback_data="buy_tariff:standart")
        ],
        [
            InlineKeyboardButton(text="🔥 PRO (15 радаров) — 700 ₽", callback_data="buy_tariff:pro")
        ],
        [
            InlineKeyboardButton(text="👤 Назад в профиль", callback_data="refresh_profile")
        ]
    ])

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
            "price": "250 ₽"
        },
        "pro": {
            "name": "🔥 PRO",
            "radars": 15,
            "price": "700 ₽"
        }
    }
    selected = tariffs.get(tariff_key, tariffs["standart"])
    is_adm = config.is_admin(call.from_user.id)

    if is_adm:
        info_note = "👑 <i>Вы являетесь администратором бота — для вас все функции и лимиты уже действуют бесплатно и бессрочно!</i>"
        action_buttons = []
    else:
        info_note = "<i>💡 Для оформления подписки и получения счёта на оплату свяжитесь с оператором поддержки:</i>"
        action_buttons = [[InlineKeyboardButton(text="💳 Оплатить подписку (Оператор)", url="https://t.me/vimerd")]]

    text = (
        "💳 <b>ОФОРМЛЕНИЕ ПОДПИСКИ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Тариф: <b>{selected['name']}</b>\n"
        f"Лимит радаров: <b>{selected['radars']} шт.</b>\n"
        f"Срок: <b>1 месяц (30 дней)</b>\n"
        f"Стоимость: <b>{selected['price']}</b>\n\n"
        f"{info_note}"
    )

    nav_buttons = [
        InlineKeyboardButton(text="« Назад к тарифам", callback_data="open_subscription_menu"),
        InlineKeyboardButton(text="👤 В профиль", callback_data="refresh_profile")
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=action_buttons + [nav_buttons])

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()



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
        "💬 <b>СЛУЖБА ПОДДЕРЖКИ И КОНТАКТЫ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "По вопросам работы сервиса, настройке радаров, оплате тарифов или предложениям:\n\n"
        "👤 <b>Telegram поддержки:</b> @vimerd\n"
        "🕒 <b>График работы:</b> Ежедневно с 09:00 до 23:00 (МСК)\n"
        "⚡️ <b>Среднее время ответа:</b> 5–15 минут\n\n"
        "При вопросе по оплате укажите ваш <code>ID</code> из раздела «Мой профиль»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👨‍💻 Написать в поддержку", url="https://t.me/vimerd")
        ],
        [
            InlineKeyboardButton(text="💳 Выбрать тариф", callback_data="open_subscription_menu"),
            InlineKeyboardButton(text="👤 В профиль", callback_data="refresh_profile")
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
            [InlineKeyboardButton(text="« Назад в профиль", callback_data="refresh_profile")]
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
        buttons.append([InlineKeyboardButton(text="« Назад в профиль", callback_data="refresh_profile")])
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
    logger.info(f"🚷 [ЧЕРНЫЙ СПИСОК] Пользователь ID:{user_id} заблокировал продавца «{seller_name}»")

    await call.answer(
        f"🚷 Продавец «{seller_name}» добавлен в ваш Чёрный список!\nЛоты от него больше не будут приходить.",
        show_alert=True
    )

import html
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

router = Router()


class DefaultTemplateStates(StatesGroup):
    waiting_for_template = State()


PRESET_TEMPLATES = {
    "cash": "Здравствуйте! Товар ещё продаётся? Готов забрать сегодня за наличные.",
    "delivery": "Добрый день! Товар актуален? Оформите, пожалуйста, через Авито Доставку.",
    "reserve": "Здравствуйте! Можно бронь до вечера? Точно заберу, без торга.",
}


def get_main_keyboard(user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    """Главная клавиатура бота с премиальным стилем."""
    kb = [
        [KeyboardButton(text="🎯 Добавить радар"), KeyboardButton(text="📁 Мои радары")],
        [KeyboardButton(text="⚡️ Быстрый отклик"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="📖 Инструкция")]
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
        f"• 📸 <b>Фотографии в HD</b> — 100% доставка без блокировок\n"
        f"• ⚡️ <b>Только свежие лоты</b> — отсечение старых и поднятых объявлений\n"
        f"• 🛡 <b>Фильтр стоп-слов</b> — защита от реплик, брака и мусора\n"
        f"• 📋 <b>Быстрый отклик</b> — копирование приветствия продавцу в 1 клик\n\n"
        f"💡 <b>Как запустить радар:</b>\n"
        f"Скопируйте ссылку с фильтрами с <code>avito.ru</code> и отправьте её сюда в чат!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt"),
            InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")
        ],
        [
            InlineKeyboardButton(text="⚡️ Быстрый отклик", callback_data="open_quick_response_menu"),
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
    status_str = "👑 <b>Администратор системы</b>" if is_adm else "🟢 <b>Активен (PRO доступ)</b>"

    text = (
        "┌ 👤 <b>ЛИЧНЫЙ КАБИНЕТ</b>\n"
        f"├ 🆔 <b>Ваш ID:</b> <code>{user_id}</code>\n"
        f"├ ⚡️ <b>Статус:</b> {status_str}\n"
        f"├ 🎯 <b>Радары:</b> <b>{active_count}</b> акт. / <b>{searches_count}</b> всего (лимит: {config.MAX_SEARCHES_PER_USER})\n"
        f"└ 🔔 <b>Доставка лотов:</b> ⚡️ Мгновенно (с фото HD)\n\n"
        "💬 <b>Текущий шаблон отклика:</b>\n"
        f"<code>{html.escape(current_tmpl)}</code>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt"),
            InlineKeyboardButton(text=f"📁 Мои радары ({searches_count})", callback_data="list_searches")
        ],
        [
            InlineKeyboardButton(text="✏️ Настроить отклик", callback_data="open_quick_response_menu"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_profile")
        ]
    ])

    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(F.text.in_({"⚡️ Быстрый отклик", "💬 Быстрый отклик", "⚙️ Шаблон по умолчанию"}))
@router.callback_query(F.data == "open_quick_response_menu")
async def cmd_default_template(event, state: FSMContext):
    await state.clear()
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
        await event.answer()
    else:
        message = event
        user_id = message.from_user.id

    user = await database.get_or_create_user(user_id)
    current_tmpl = user.get("default_template") or database.DEFAULT_SELLER_MESSAGE

    text = (
        "⚡️ <b>БЫСТРЫЙ ОТКЛИК ПРОДАВЦУ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Этот текст будет автоматически прикреплен к кнопке <b>«📋 Скопировать отклик»</b> под каждым новым объявлением.\n\n"
        "📌 <b>Текущее сообщение:</b>\n"
        f"<code>{html.escape(current_tmpl)}</code>\n\n"
        "Выберите готовый шаблон или задайте свой собственный:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💵 «Выкуп за наличные»", callback_data="set_preset:cash")
        ],
        [
            InlineKeyboardButton(text="🚚 «Авито Доставка»", callback_data="set_preset:delivery")
        ],
        [
            InlineKeyboardButton(text="🤝 «Бронь до вечера»", callback_data="set_preset:reserve")
        ],
        [
            InlineKeyboardButton(text="✍️ Ввести свой текст вручную", callback_data="change_default_template")
        ],
        [
            InlineKeyboardButton(text="👤 К профилю", callback_data="refresh_profile")
        ]
    ])

    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("set_preset:"))
async def cb_set_preset(call: CallbackQuery):
    preset_key = call.data.split(":")[1]
    new_text = PRESET_TEMPLATES.get(preset_key)
    if not new_text:
        await call.answer("Шаблон не найден", show_alert=True)
        return

    await database.update_user_default_template(call.from_user.id, new_text)
    await call.answer("✅ Шаблон успешно применен!")
    # Повторно открываем меню с обновленным текстом
    await cmd_default_template(call, None)


@router.callback_query(F.data == "change_default_template")
async def cb_change_default_template(call: CallbackQuery, state: FSMContext):
    await state.set_state(DefaultTemplateStates.waiting_for_template)
    await call.message.answer(
        "✍️ <b>Отправьте ваш текст сообщения продавцу:</b>\n\n"
        "<i>Пример:\n"
        "Здравствуйте! Товар ещё актуален? Готов приехать и забрать за наличные прямо сегодня.</i>",
        parse_mode="HTML"
    )
    await call.answer()


@router.message(DefaultTemplateStates.waiting_for_template)
async def process_new_default_template(message: Message, state: FSMContext):
    new_text = message.text.strip()
    if not new_text or len(new_text) > 500:
        await message.answer("⚠️ Текст шаблона должен быть от 1 до 500 символов. Попробуйте еще раз:")
        return

    await database.update_user_default_template(message.from_user.id, new_text)
    await state.clear()

    text = (
        "✅ <b>Шаблон отклика успешно сохранен!</b>\n\n"
        f"<code>{html.escape(new_text)}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡️ К настройке отклика", callback_data="open_quick_response_menu")],
        [InlineKeyboardButton(text="📁 Мои радары", callback_data="list_searches")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

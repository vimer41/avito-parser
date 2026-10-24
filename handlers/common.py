from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database

router = Router()

class DefaultTemplateStates(StatesGroup):
    waiting_for_template = State()


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура бота."""
    kb = [
        [KeyboardButton(text="➕ Добавить поиск"), KeyboardButton(text="📋 Мои поиски")],
        [KeyboardButton(text="⚙️ Шаблон по умолчанию"), KeyboardButton(text="❓ Помощь")]
    ]
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
    first_name = user.first_name

    await database.get_or_create_user(user_id, username, first_name)

    text = (
        f"🎯 <b>Добро пожаловать в радар новых лотов, {first_name or 'друг'}!</b>\n\n"
        "Я отслеживаю самые выгодные предложения на <b>Авито в реальном времени</b> "
        "и присылаю их раньше, чем их успеют выкупить конкуренты.\n\n"
        "⚡️ <b>Как начать поиск:</b>\n"
        "1. Откройте <b>avito.ru</b> в браузере и настройте поиск (город, цена, фильтры).\n"
        "2. Скопируйте ссылку из адресной строки.\n"
        "3. Отправьте её мне сюда — и радар сразу начнёт работу.\n\n"
        "<i>Под каждым объявлением будут фото, полная инфа, адрес и кнопка быстрого отклика продавцу.</i>\n\n"
        "👇 <b>Отправьте ссылку на поиск прямо в чат</b> или нажмите <b>➕ Добавить поиск</b>:"
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")


@router.message(F.text == "❓ Помощь")
@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📖 <b>Инструкция и возможности бота</b>\n\n"
        "🎯 <b>Принцип работы:</b>\n"
        "Вы передаёте боту ссылку на нужную категорию с вашими фильтрами. "
        "Бот 24/7 сканирует выдачу Авито и пересылает новые лоты моментально: "
        "фото в высоком качестве, реальная цена, геолокация, рейтинг продавца и прямая ссылка. "
        "Вы успеваете написать продавцу первым!\n\n"
        "🔗 <b>Как скопировать ссылку:</b>\n"
        "1. Откройте сайт <code>avito.ru</code> в любом браузере (Chrome, Safari, Яндекс).\n"
        "2. Вбейте название товара и выставьте фильтры (цена от/до, город, доставка).\n"
        "3. Скопируйте ссылку из адресной строки и отправьте её боту сообщением.\n"
        "<i>(Сортировку по новизне бот подключит сам автоматически).</i>\n\n"
        "🚫 <b>Умный фильтр (Стоп-слова):</b>\n"
        "Исключает хлам и мошенников. Если в заголовке или тексте лота есть стоп-слова "
        "(например: <i>«на запчасти», «реплика», «дефект», «копия»</i>) — объявление не побеспокоит вас. "
        "Добавлять стоп-слова можно в 1 клик прямо под пришедшим лотом.\n\n"
        "📋 <b>Быстрый отклик (Шаблон):</b>\n"
        "Внизу каждого уведомления есть кнопка «📋 Скопировать сообщение». "
        "Один клик — и готовый текст приветствия скопирован в буфер, остаётся лишь отправить его на Авито.\n\n"
        "⚡️ <b>Скорость уведомлений:</b>\n"
        "Благодаря встроенному обходу защиты Авито новые объявления прилетают "
        "спустя считанные секунды после их появления на площадке."
    )
    await message.answer(help_text, reply_markup=get_main_keyboard(), parse_mode="HTML")


@router.message(F.text == "⚙️ Шаблон по умолчанию")
async def cmd_default_template(message: Message, state: FSMContext):
    user = await database.get_or_create_user(message.from_user.id)
    current_tmpl = user.get("default_template") or database.DEFAULT_SELLER_MESSAGE

    text = (
        "<b>⚙️ Ваш шаблон сообщения продавцу:</b>\n\n"
        f"<code>{current_tmpl}</code>\n\n"
        "Этот текст будет копироваться по кнопке <b>«📋 Скопировать сообщение»</b> под новыми товарами.\n\n"
        "Нажмите кнопку ниже, чтобы изменить текст:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить шаблон", callback_data="change_default_template")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "change_default_template")
async def cb_change_default_template(call: CallbackQuery, state: FSMContext):
    await state.set_state(DefaultTemplateStates.waiting_for_template)
    await call.message.answer(
        "✍️ <b>Отправьте новый текст шаблона сообщением:</b>\n\n"
        "<i>Например: Здравствуйте! Объявление актуально? Где можно забрать?</i>",
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
    await message.answer(
        "✅ <b>Шаблон по умолчанию успешно обновлен!</b>\n\n"
        f"<code>{new_text}</code>",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

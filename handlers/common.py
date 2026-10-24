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
        f"👋 <b>Привет, {first_name or 'друг'}!</b>\n\n"
        "Я бот для <b>мгновенного мониторинга новых объявлений на Авито</b>.\n\n"
        "⚡️ <b>Как это работает:</b>\n"
        "1️⃣ Настройте поиск на сайте <b>avito.ru</b> в браузере (город, цена, фильтры).\n"
        "2️⃣ Скопируйте ссылку на результаты поиска и отправьте её мне.\n"
        "3️⃣ Я буду проверять поиск 24/7 и присылать новые объявления сразу, как только они выходят!\n\n"
        "👇 <b>Просто пришлите ссылку на поиск Авито</b> или нажмите <b>➕ Добавить поиск</b>:"
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")


@router.message(F.text == "❓ Помощь")
@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "<b>❓ Как работает бот?</b>\n"
        "Вы присылаете ссылку на поиск Авито со своими фильтрами: товар, город, цена, доставка. "
        "Бот круглосуточно следит за этим поиском и, как только появляется новое объявление, "
        "присылает его сюда: фото, цена, адрес, продавец и кнопка <b>«Открыть на Авито»</b>. "
        "Вы пишете продавцу раньше остальных!\n\n"
        "<b>❓ Как скопировать ссылку на поиск?</b>\n"
        "1. Откройте <code>avito.ru</code> в браузере (Safari, Chrome) на телефоне или компьютере.\n"
        "2. Настройте поиск: что ищете, город, цена, другие фильтры.\n"
        "3. Скопируйте адрес страницы из адресной строки и пришлите его сюда.\n\n"
        "<i>Ссылка выглядит так: avito.ru/moskva/...?q=...\n"
        "Нужна именно страница с результатами поиска, ссылка на одно объявление не подойдёт.</i>\n\n"
        "<b>❓ Что такое стоп-слова и шаблон?</b>\n"
        "• <b>Стоп-слова:</b> объявления, где в названии или описании есть эти слова, не придут. Например: <i>«не работает, на запчасти, копия»</i>.\n"
        "• <b>Шаблон:</b> ваш быстрый текст продавцу. Кнопка <b>«📋 Скопировать сообщение»</b> под уведомлением позволяет мгновенно скопировать готовый текст и отправить его на Авито.\n\n"
        "<b>❓ Как быстро приходят объявления?</b>\n"
        "Бот постоянно проверяет ваши поиски и присылает объявление в первые секунды после его появления в поиске."
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

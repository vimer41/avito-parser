import html
import logging
import re
from datetime import datetime
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

import config
import database
from logger_setup import log_user_event

logger = logging.getLogger(__name__)
router = Router()


class SupportStates(StatesGroup):
    waiting_for_user_message = State()
    waiting_for_admin_reply = State()


# ── 1. Пользователь: начало создания обращения ──────────────────────────

@router.callback_query(F.data == "support_start_ticket")
async def cb_support_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(SupportStates.waiting_for_user_message)
    text = (
        "💬 <b>ОБРАЩЕНИЕ В СЛУЖБУ ПОДДЕРЖКИ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Напишите ваш вопрос, проблему или пожелание одним сообщением.\n\n"
        "<i>Вы можете отправить текст или прикрепить скриншот/фото с описанием. "
        "Администраторы получат сообщение и ответят вам прямо в этом боте.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="support_cancel")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")]
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "support_cancel")
async def cb_support_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start_approved")]
    ])
    await call.message.edit_text("❌ <b>Обращение в службу поддержки отменено.</b>", reply_markup=cancel_kb, parse_mode="HTML")
    await call.answer()



# ── 2. Пользователь: отправка сообщения в поддержку ─────────────────────

@router.message(SupportStates.waiting_for_user_message)
async def handle_user_support_message(message: Message, state: FSMContext, bot: Bot):
    user = message.from_user
    user_id = user.id

    # Проверка на отмену текстом
    if message.text and message.text.strip().lower() in {"отмена", "❌ отмена", "/cancel"}:
        await state.clear()
        await message.answer("❌ <b>Обращение в службу поддержки отменено.</b>", parse_mode="HTML")
        return

    text_content = message.text or message.caption or "(без текста, вложение)"
    state_data = await state.get_data()
    clean_text = html.escape(text_content.strip())
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    username_str = f"@{user.username}" if user.username else "без username"

    admin_caption = (
        f"📩 <b>{'ОБРАЩЕНИЕ ПО ОПЛАТЕ / ВОЗВРАТУ' if state_data.get('payment_support') else 'НОВОЕ ОБРАЩЕНИЕ В ПОДДЕРЖКУ!'}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>От:</b> {html.escape(user.full_name)} ({username_str})\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"🕒 <b>Время:</b> {now_str}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Текст:</b>\n{clean_text}"
    )

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Ответить пользователю", callback_data=f"support_reply:{user_id}")]
    ])

    # Доставка сообщения всем администраторам бота
    for aid in config.ADMIN_IDS:
        try:
            if message.photo:
                photo_id = message.photo[-1].file_id
                await bot.send_photo(
                    chat_id=aid,
                    photo=photo_id,
                    caption=admin_caption,
                    reply_markup=admin_kb,
                    parse_mode="HTML"
                )
            elif message.document:
                await bot.send_document(
                    chat_id=aid,
                    document=message.document.file_id,
                    caption=admin_caption,
                    reply_markup=admin_kb,
                    parse_mode="HTML"
                )
            elif message.voice:
                await bot.send_voice(
                    chat_id=aid,
                    voice=message.voice.file_id,
                    caption=admin_caption,
                    reply_markup=admin_kb,
                    parse_mode="HTML"
                )
            elif message.video:
                await bot.send_video(
                    chat_id=aid,
                    video=message.video.file_id,
                    caption=admin_caption,
                    reply_markup=admin_kb,
                    parse_mode="HTML"
                )
            else:
                await bot.send_message(
                    chat_id=aid,
                    text=admin_caption,
                    reply_markup=admin_kb,
                    parse_mode="HTML"
                )
        except Exception as err:
            logger.error(f"Не удалось отправить обращение в поддержку администратору {aid}: {err}")

    await state.clear()

    log_user_event(
        user_id=user_id,
        username=user.username,
        first_name=user.first_name,
        action=f"Обращение в поддержку: «{text_content[:80]}»",
        status="SUPPORT"
    )

    await message.answer(
        "✅ <b>Ваше обращение принято и передано администрации!</b>\n\n"
        "Мы уже получили ваш запрос и ответим вам прямо в этом боте в ближайшее время.",
        parse_mode="HTML"
    )


# ── 3. Администратор: ответ через кнопку ────────────────────────────────

@router.callback_query(F.data.startswith("support_reply:"))
async def cb_admin_reply_start(call: CallbackQuery, state: FSMContext):
    admin_id = call.from_user.id
    if not config.is_admin(admin_id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    target_user_id = int(call.data.split(":")[1])
    target_user = await database.get_or_create_user(target_user_id)
    target_name = html.escape(target_user.get("first_name") or "Пользователь")
    username_str = f"@{target_user.get('username')}" if target_user.get("username") else "без username"

    await state.set_state(SupportStates.waiting_for_admin_reply)
    await state.update_data(target_user_id=target_user_id, target_name=target_name)

    text = (
        f"✍️ <b>Ответ пользователю:</b> <b>{target_name}</b> ({username_str})\n"
        f"🆔 <b>ID:</b> <code>{target_user_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Введите текст ответа (или отправьте фото с описанием).\n"
        "Сообщение будет доставлено пользователю прямо в чат с ботом.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_reply_cancel")]
    ])

    await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_reply_cancel")
async def cb_admin_reply_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ <b>Ответ пользователю отменён.</b>", parse_mode="HTML")
    await call.answer()


@router.message(SupportStates.waiting_for_admin_reply)
async def handle_admin_reply_message(message: Message, state: FSMContext, bot: Bot):
    admin_id = message.from_user.id
    if not config.is_admin(admin_id):
        return

    # Проверка на отмену
    if message.text and message.text.strip().lower() in {"отмена", "❌ отмена", "/cancel"}:
        await state.clear()
        await message.answer("❌ <b>Ответ пользователю отменён.</b>", parse_mode="HTML")
        return

    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    target_name = data.get("target_name") or f"ID {target_user_id}"

    if not target_user_id:
        await state.clear()
        await message.answer("❌ <b>Ошибка:</b> адресат сообщения не найден.", parse_mode="HTML")
        return

    success, err_msg = await send_reply_to_user(bot, target_user_id, message)
    await state.clear()

    if success:
        await message.answer(
            f"✅ <b>Ответ успешно доставлен пользователю {target_name}</b> (ID: <code>{target_user_id}</code>)!",
            parse_mode="HTML"
        )
        logger.info(f"👑 [АДМИН] Администратор ID:{admin_id} отправил ответ пользователю ID:{target_user_id}")
    else:
        await message.answer(
            f"⚠️ <b>Не удалось доставить ответ пользователю:</b> {err_msg}",
            parse_mode="HTML"
        )


# ── 4. Администратор: быстрый ответ через Reply (свайп/цитирование) ─────

@router.message(F.reply_to_message)
async def handle_admin_quote_reply(message: Message, state: FSMContext, bot: Bot):
    admin_id = message.from_user.id
    if not config.is_admin(admin_id):
        return

    # Если администратор уже находится в каком-то состоянии FSM, пропускаем
    current_state = await state.get_state()
    if current_state:
        return

    replied = message.reply_to_message
    replied_text = replied.text or replied.caption or ""

    # Извлекаем ID пользователя из текста оригинального сообщения
    m = (
        re.search(r"🆔\s*<b>ID:</b>\s*<code>(\d+)</code>", replied_text)
        or re.search(r"ID:\s*<code>(\d+)</code>", replied_text)
        or re.search(r"<code>(\d{6,})</code>", replied_text)
    )
    if not m:
        return

    target_user_id = int(m.group(1))
    if target_user_id == admin_id:
        return

    success, err_msg = await send_reply_to_user(bot, target_user_id, message)
    if success:
        await message.reply(
            f"✅ <b>Ответ доставлен пользователю ID: <code>{target_user_id}</code></b>",
            parse_mode="HTML"
        )
        logger.info(f"👑 [АДМИН] Быстрый ответ (Quote Reply) от ID:{admin_id} для ID:{target_user_id}")
    else:
        await message.reply(
            f"⚠️ <b>Не удалось доставить:</b> {err_msg}",
            parse_mode="HTML"
        )


# ── Вспомогательная функция отправки ответа пользователю ────────────────

async def send_reply_to_user(bot: Bot, target_user_id: int, message: Message) -> tuple[bool, str]:
    """Отправляет ответ администратора пользователю от имени бота."""
    reply_text = message.text or message.caption or ""
    clean_text = html.escape(reply_text.strip())

    user_header = (
        "💬 <b>ОТВЕТ СЛУЖБЫ ПОДДЕРЖКИ:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{clean_text}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Если у вас остались вопросы — вы можете написать нам снова через кнопку «💬 Поддержка».</i>"
    )

    try:
        if message.photo:
            photo_id = message.photo[-1].file_id
            await bot.send_photo(
                chat_id=target_user_id,
                photo=photo_id,
                caption=user_header,
                parse_mode="HTML"
            )
        elif message.document:
            await bot.send_document(
                chat_id=target_user_id,
                document=message.document.file_id,
                caption=user_header,
                parse_mode="HTML"
            )
        elif message.voice:
            await bot.send_voice(
                chat_id=target_user_id,
                voice=message.voice.file_id,
                caption=user_header,
                parse_mode="HTML"
            )
        elif message.video:
            await bot.send_video(
                chat_id=target_user_id,
                video=message.video.file_id,
                caption=user_header,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                chat_id=target_user_id,
                text=user_header,
                parse_mode="HTML"
            )
        return True, ""
    except TelegramForbiddenError:
        return False, "пользователь заблокировал бота"
    except Exception as e:
        return False, str(e)

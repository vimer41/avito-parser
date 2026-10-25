import html
import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database

from logger_setup import log_user_event

logger = logging.getLogger(__name__)


class ActionLoggingMiddleware(BaseMiddleware):
    """
    Детально логирует каждое действие пользователя в logs/user_actions.log:
    - Текстовые сообщения и команды
    - Нажатия на инлайн-кнопки
    - Текущее состояние FSM-машины (на каком этапе находился пользователь)
    - Ошибки выполнения хэндлеров для данного пользователя
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        state = data.get("state")
        current_state = None
        if state:
            try:
                current_state = await state.get_state()
            except Exception:
                pass

        if isinstance(event, Message) and event.text:
            log_user_event(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                action=f"Сообщение: «{event.text[:100]}»",
                status="MSG",
                fsm_state=current_state
            )
        elif isinstance(event, CallbackQuery) and event.data:
            log_user_event(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                action=f"Нажата кнопка: «{event.data}»",
                status="CLICK",
                fsm_state=current_state
            )

        try:
            return await handler(event, data)
        except Exception as e:
            log_user_event(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                action="СБОЙ ПРИ ОБРАБОТКЕ СОБЫТИЯ",
                details=f"Ошибка: {str(e)[:150]}",
                status="ERROR",
                fsm_state=current_state
            )
            raise


class AccessControlMiddleware(BaseMiddleware):

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        user_id = user.id
        bot: Bot = data.get("bot")

        # 1. Если это админский callback_query (одобрение/отклонение), пропускаем для обработки
        if isinstance(event, CallbackQuery) and event.data:
            if event.data.startswith("admin_approve:") or event.data.startswith("admin_reject:") or event.data.startswith("admin_revoke:"):
                return await handler(event, data)

        # 2. Проверяем наличие пользователя в БД
        user_record = await database.get_or_create_user(user_id, user.username, user.first_name)

        # 3. Проверяем, есть ли доступ
        if await database.is_user_allowed(user_id):
            # Автоматический сброс FSM-состояния при нажатии любой кнопки главного меню или команд
            if isinstance(event, Message) and event.text:
                txt = event.text.strip()
                menu_keywords = {
                    "🎯 Добавить радар", "➕ Добавить радар", "➕ Добавить поиск",
                    "📁 Мои радары", "📂 Мои радары", "📋 Мои поиски", "Мои радары",
                    "💬 Поддержка", "Поддержка", "Помощь",
                    "👤 Мой профиль", "📊 Мой статус", "Мой профиль",
                    "📖 Инструкция", "Инструкция",
                    "👑 Админ-панель", "👑 Админка",
                    "❌ Отмена", "Отмена"
                }
                if txt.startswith("/") or txt in menu_keywords:
                    fsm_state = data.get("state")
                    if fsm_state:
                        current_st = await fsm_state.get_state()
                        if current_st:
                            await fsm_state.clear()

            return await handler(event, data)

        # 4. Если доступ НЕ одобрен:
        text_for_user = (
            "🔒 <b>Доступ к боту ограничен</b>\n\n"
            "Этот бот является приватным. Ваша заявка на доступ отправлена администратору <b>@vimerd</b>.\n"
            "Как только администратор подтвердит доступ — вы получите уведомление и сможете начать поиск!"
        )

        if isinstance(event, Message):
            await event.answer(text_for_user, parse_mode="HTML")
        elif isinstance(event, CallbackQuery):
            await event.answer("Доступ ограничен. Ожидайте подтверждения от @vimerd.", show_alert=True)

        # 5. Отправляем запрос администратору
        admin_text = (
            "🔔 <b>Новый запрос на доступ к боту!</b>\n\n"
            f"👤 <b>Имя:</b> {html.escape(user.full_name)}\n"
            f"🔗 <b>Username:</b> @{user.username if user.username else 'нет'}\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>"
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить доступ", callback_data=f"admin_approve:{user_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject:{user_id}")
            ]
        ])

        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=admin_text,
                    reply_markup=admin_kb,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

        # Прерываем выполнение дальнейших обработчиков
        return

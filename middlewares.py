import html
import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database

logger = logging.getLogger(__name__)


class ActionLoggingMiddleware(BaseMiddleware):
    """Логирует каждое действие пользователя (сообщения, команды, нажатия на инлайн-кнопки)."""
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        user_info = f"ID: {user.id} (@{user.username or 'нет'}, {user.first_name or ''})" if user else "ID: Unknown"

        if isinstance(event, Message) and event.text:
            logger.info(f"👤 [USER] {user_info} ──▶ Сообщение: «{event.text}»")
        elif isinstance(event, CallbackQuery) and event.data:
            logger.info(f"🔘 [КНОПКА] {user_info} ──▶ Нажата инлайн-кнопка: «{event.data}»")

        return await handler(event, data)


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

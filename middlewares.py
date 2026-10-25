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

        # 1. Разрешаем технические callback-запросы (админка, служба поддержки, меню тарифов, оплата)
        if isinstance(event, CallbackQuery) and event.data:
            allowed_callbacks = (
                "admin_approve:", "admin_reject:", "admin_revoke:", "admin_grant_sub:",
                "support_reply:", "admin_reply_cancel",
                "open_subscription_menu", "buy_tariff:", "pay_request:", "activate_trial_btn",
                "support_start_ticket", "support_cancel"
            )
            if any(event.data.startswith(prefix) for prefix in allowed_callbacks):
                return await handler(event, data)

        # Разрешаем отправку сообщений, если пользователь пишет обращение в поддержку
        state = data.get("state")
        if state:
            current_st = await state.get_state()
            if current_st == "SupportStates:waiting_for_user_message":
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
                    "💳 Тарифы", "💳 Тарифы и подписка", "Тарифы", "Подписка", "💳 Подписка",
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

        # 4. Если доступ НЕ одобрен (пробный период истёк или доступ закрыт):
        trial_used = bool(user_record.get("trial_used"))
        if trial_used:
            text_for_user = (
                "🔒 <b>Срок действия пробного периода завершился</b>\n\n"
                "Ваш бесплатный 24-часовой доступ подошёл к концу. "
                "Чтобы продолжить отслеживать новые лоты на Авито без ограничений, выберите подходящий тариф подписки "
                "или свяжитесь со службой поддержки прямо в этом боте!"
            )
        else:
            text_for_user = (
                "🔒 <b>Доступ к боту ограничен</b>\n\n"
                "Этот бот является приватным. Ваша заявка передана администраторам сервиса.\n"
                "Как только доступ будет подтверждён — вы получите уведомление и сможете начать поиск!"
            )

        user_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Выбрать тариф", callback_data="open_subscription_menu")],
            [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="support_start_ticket")]
        ])

        if isinstance(event, Message):
            await event.answer(text_for_user, reply_markup=user_kb, parse_mode="HTML")
        elif isinstance(event, CallbackQuery):
            await event.answer("Срок действия доступа истёк. Выберите тариф или напишите в поддержку.", show_alert=True)

        # 5. Отправляем запрос администраторам
        user_msg = f"\n💬 <b>Сообщение:</b> <i>«{html.escape(event.text[:250])}»</i>" if (isinstance(event, Message) and event.text) else ""
        admin_text = (
            "🔔 <b>Пользователь с истёкшим доступом пишет в бот:</b>\n\n"
            f"👤 <b>Имя:</b> {html.escape(user.full_name)}\n"
            f"🔗 <b>Username:</b> @{user.username if user.username else 'нет'}\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>{user_msg}"
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Продлить доступ", callback_data=f"admin_approve:{user_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject:{user_id}")
            ],
            [
                InlineKeyboardButton(text="✍️ Написать пользователю", callback_data=f"support_reply:{user_id}")
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

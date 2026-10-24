import html
import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database

logger = logging.getLogger(__name__)
router = Router()


# ── Одобрение / Отклонение доступа администратором ─────────────────────

@router.callback_query(F.data.startswith("admin_approve:"))
async def cb_admin_approve(call: CallbackQuery, bot: Bot):
    if call.from_user.id != config.ADMIN_ID:
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    target_user_id = int(call.data.split(":")[1])
    await database.set_user_allowed(target_user_id, 1)

    user = await database.get_or_create_user(target_user_id)
    username_str = f"@{user['username']}" if user.get("username") else f"ID {target_user_id}"

    await call.message.edit_text(
        f"✅ <b>Доступ успешно одобрен!</b>\n\n"
        f"Пользователь: <b>{html.escape(user.get('first_name') or '')}</b> ({username_str}) теперь может полноценно пользоваться ботом.",
        parse_mode="HTML"
    )
    await call.answer("Доступ одобрен")

    # Уведомляем пользователя
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "🎉 <b>Администратор одобрил ваш доступ к боту!</b>\n\n"
                "Теперь вы можете отслеживать любые товары на Авито 24/7.\n"
                "Нажмите /start или отправьте ссылку на поиск, чтобы начать!"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Начать работу", callback_data="start_approved")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление пользователю {target_user_id}: {e}")


@router.callback_query(F.data.startswith("admin_reject:"))
async def cb_admin_reject(call: CallbackQuery, bot: Bot):
    if call.from_user.id != config.ADMIN_ID:
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    target_user_id = int(call.data.split(":")[1])
    await database.set_user_allowed(target_user_id, 0)

    await call.message.edit_text(
        f"❌ <b>Заявка пользователя {target_user_id} отклонена.</b>",
        parse_mode="HTML"
    )
    await call.answer("Доступ отклонен")

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text="❌ <b>К сожалению, доступ к боту был отклонен администратором.</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ── Панель управления пользователями для админа (/admin, /users) ───────

@router.message(Command("admin"))
@router.message(Command("users"))
async def cmd_admin_users(message: Message):
    if message.from_user.id != config.ADMIN_ID:
        return

    users = await database.get_all_users()
    if not users:
        await message.answer("В базе данных пока нет пользователей.")
        return

    lines = [f"👑 <b>Панель администратора</b> (Всего пользователей: {len(users)}):\n"]
    buttons = []

    for u in users:
        u_id = u["user_id"]
        status = "🟢 Доступ есть" if u.get("is_allowed") else "🔴 Заблокирован"
        uname = f"@{u['username']}" if u.get("username") else f"ID: {u_id}"
        fname = html.escape(u.get("first_name") or "")

        lines.append(f"• <b>{fname}</b> ({uname}) — {status}")

        # Для не-админов добавляем кнопку блокировки / разблокировки
        if u_id != config.ADMIN_ID:
            if u.get("is_allowed"):
                buttons.append([InlineKeyboardButton(text=f"🚫 Заблокировать {uname}", callback_data=f"admin_revoke:{u_id}")])
            else:
                buttons.append([InlineKeyboardButton(text=f"✅ Разрешить {uname}", callback_data=f"admin_approve:{u_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_revoke:"))
async def cb_admin_revoke(call: CallbackQuery):
    if call.from_user.id != config.ADMIN_ID:
        await call.answer("У вас нет прав", show_alert=True)
        return

    target_user_id = int(call.data.split(":")[1])
    await database.set_user_allowed(target_user_id, 0)
    await call.answer("Доступ отозван")
    await cmd_admin_users(call.message)

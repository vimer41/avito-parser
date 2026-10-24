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
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    target_user_id = int(call.data.split(":")[1])
    await database.set_user_allowed(target_user_id, 1)

    user = await database.get_or_create_user(target_user_id)
    username_str = f"@{user['username']}" if user.get("username") else f"ID {target_user_id}"

    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} одобрил доступ пользователю ID:{target_user_id} ({username_str})")

    await call.message.edit_text(
        f"✅ <b>Доступ успешно одобрен!</b>\n\n"
        f"Пользователь: <b>{html.escape(user.get('first_name') or '')}</b> ({username_str}) теперь имеет полный доступ к мониторингу.",
        parse_mode="HTML"
    )
    await call.answer("Доступ одобрен")


    # Уведомляем пользователя
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "🎉 <b>Ваш доступ к AVITO RADAR успешно одобрен!</b>\n\n"
                "Теперь вы можете отслеживать любые товары на Авито 24/7 без ограничений.\n"
                "Нажмите кнопку ниже или отправьте ссылку на поиск, чтобы запустить первый радар!"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Запустить радар", callback_data="start_approved")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление пользователю {target_user_id}: {e}")


@router.callback_query(F.data.startswith("admin_reject:"))
async def cb_admin_reject(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    target_user_id = int(call.data.split(":")[1])
    await database.set_user_allowed(target_user_id, 0)
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} отклонил доступ пользователю ID:{target_user_id}")

    await call.message.edit_text(
        f"❌ <b>Заявка пользователя {target_user_id} отклонена.</b>",
        parse_mode="HTML"
    )
    await call.answer("Доступ отклонён")


    try:
        await bot.send_message(
            chat_id=target_user_id,
            text="❌ <b>К сожалению, доступ к боту был отклонён администратором.</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ── Панель администратора (/admin, /users, кнопка в меню) ─────────────

@router.message(F.text.in_({"👑 Админ-панель", "👑 Админка"}))
@router.message(Command("admin"))
@router.message(Command("users"))
@router.callback_query(F.data == "admin_refresh")
async def cmd_admin_panel(event):
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
        await event.answer("Данные обновлены!")
    else:
        message = event
        user_id = message.from_user.id

    if not config.is_admin(user_id):
        return

    stats = await database.get_system_stats()
    users = await database.get_all_users()

    text_lines = [
        "👑 <b>ПАНЕЛЬ УПРАВЛЕНИЯ СИСТЕМОЙ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>Сводная статистика:</b>\n"
        f"• 👥 Пользователей: <b>{stats['total_users']}</b> (🟢 {stats['allowed_users']} с доступом)\n"
        f"• 🎯 Всего радаров: <b>{stats['total_searches']}</b> (🟢 {stats['active_searches']} активно)\n"
        f"• 📦 Обработано лотов: <b>{stats['total_seen']}</b>\n"
        f"• 🛡 Защита: <b>TLS Chameleon / PoW Bypass</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>Пользователи и права доступа:</b>\n"
    ]

    buttons = []
    for u in users:
        u_id = u["user_id"]
        is_adm = config.is_admin(u_id)
        is_allowed = bool(u.get("is_allowed"))
        uname = f"@{u['username']}" if u.get("username") else f"ID: {u_id}"
        fname = html.escape(u.get("first_name") or "Без имени")

        if is_adm:
            text_lines.append(f"👑 <b>{fname}</b> ({uname}) — <i>Администратор</i>")
        elif is_allowed:
            text_lines.append(f"🟢 <b>{fname}</b> ({uname}) — <i>Доступ разрешён</i>")
            buttons.append([InlineKeyboardButton(text=f"🚫 Заблокировать {uname}", callback_data=f"admin_revoke:{u_id}")])
        else:
            text_lines.append(f"🔴 <b>{fname}</b> ({uname}) — <i>Ограничен / Заблокирован</i>")
            buttons.append([InlineKeyboardButton(text=f"✅ Разрешить {uname}", callback_data=f"admin_approve:{u_id}")])

    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_refresh"),
        InlineKeyboardButton(text="💾 Скачать бэкап БД", callback_data="admin_backup")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    full_text = "\n".join(text_lines)
    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(full_text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    await message.answer(full_text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_revoke:"))
async def cb_admin_revoke(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав", show_alert=True)
        return

    target_user_id = int(call.data.split(":")[1])
    await database.set_user_allowed(target_user_id, 0)
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} отозвал доступ у пользователя ID:{target_user_id}")
    await call.answer("Доступ отозван")
    await cmd_admin_panel(call)


@router.message(Command("backup"))
@router.callback_query(F.data == "admin_backup")
async def cmd_admin_backup(event):
    user_id = event.from_user.id
    if not config.is_admin(user_id):
        return

    if isinstance(event, CallbackQuery):
        message = event.message
        await event.answer("Формирую бэкап...")
    else:
        message = event

    import os
    from aiogram.types import BufferedInputFile

    backup_path = database.create_database_backup_file()
    if not backup_path or not os.path.exists(backup_path):
        await message.answer("❌ Не удалось создать резервную копию базы данных.")
        return

    try:
        with open(backup_path, "rb") as f:
            data = f.read()

        filename = os.path.basename(backup_path)
        file_to_send = BufferedInputFile(data, filename=filename)

        await message.answer_document(
            document=file_to_send,
            caption=(
                "💾 <b>Резервная копия базы данных SQLite (WAL-безопасная)</b>\n\n"
                f"📦 Размер: <b>{len(data):,} байт</b>\n"
                "Файл полностью консистентен и готов к восстановлению."
            ),
            parse_mode="HTML"
        )
        logger.info(f"👑 [АДМИН] Администратор ID:{user_id} выгрузил бэкап базы данных ({filename})")
    finally:
        if os.path.exists(backup_path):
            os.remove(backup_path)



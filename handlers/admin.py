import html
import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data == "admin_probe")
async def cb_admin_probe(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await call.answer("✅ Запрос получен, бот отвечает", show_alert=True)
    await call.message.answer(
        "✅ <b>Проверка реального запроса Telegram пройдена.</b>\n"
        f"Stars: {'включены' if config.STARS_PAYMENTS_ENABLED else 'выключены'}, "
        f"Standart {config.STARS_PRICE_STANDART} ⭐ / PRO {config.STARS_PRICE_PRO} ⭐.\n"
        "Для проверки покупки нужен неадминистративный аккаунт: у администратора доступ бесплатный.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_refresh")],
            [InlineKeyboardButton(text="💳 Тарифы", callback_data="open_subscription_menu")],
        ]),
    )
    logger.info("Live Telegram probe completed by admin=%s", call.from_user.id)


# ── Одобрение / Отклонение доступа администратором ─────────────────────

@router.callback_query(F.data.startswith("admin_approve:"))
async def cb_admin_approve(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    parts = call.data.split(":")
    target_user_id = int(parts[1])
    is_panel = len(parts) > 2
    page = int(parts[2]) if is_panel else 0

    await database.set_user_allowed(target_user_id, 1)
    max_radars = await database.get_user_max_radars(target_user_id)

    user = await database.get_or_create_user(target_user_id)
    username_str = f"@{user['username']}" if user.get("username") else f"ID {target_user_id}"

    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} одобрил доступ пользователю ID:{target_user_id} ({username_str})")

    # Уведомляем пользователя
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "🎉 <b>Ваш доступ к AVITO RADAR успешно одобрен!</b>\n\n"
                f"Доступен мониторинг Авито 24/7: до {max_radars} радаров.\n"
                "Нажмите кнопку ниже или отправьте ссылку на поиск, чтобы запустить первый радар!"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Запустить радар", callback_data="start_approved")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление пользователю {target_user_id}: {e}")

    if is_panel:
        await call.answer(f"Доступ для {username_str} одобрен!")
        await cmd_admin_panel(call, page=page)
    else:
        await call.message.edit_text(
            f"✅ <b>Доступ успешно одобрен!</b>\n\n"
            f"Пользователь: <b>{html.escape(user.get('first_name') or '')}</b> ({username_str}) теперь имеет полный доступ к мониторингу.",
            parse_mode="HTML"
        )
        await call.answer("Доступ одобрен")


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


@router.callback_query(F.data.startswith("admin_grant_sub:"))
async def cb_admin_grant_sub(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    # Формат: admin_grant_sub:{user_id}:{plan}:{days}
    try:
        parts = call.data.split(":")
        target_user_id, plan, days = int(parts[1]), parts[2], int(parts[3])
        page = int(parts[4]) if len(parts) > 4 else 0
        token = await database.create_admin_subscription_action(call.from_user.id, target_user_id, plan, days, page)
    except (ValueError, IndexError, PermissionError) as exc:
        await call.answer(str(exc) or "Неверная кнопка подписки", show_alert=True)
        return
    await call.message.edit_text(
        f"💳 <b>ПОДТВЕРЖДЕНИЕ ВЫДАЧИ ПОДПИСКИ</b>\n\n"
        f"Пользователь: <code>{target_user_id}</code>\nТариф: <b>{plan.upper()}</b>\nДобавить: <b>{days} дн.</b>\n\n"
        "Срок добавится к текущей подписке или триалу; выбранный тариф применяется сразу. "
        "Это ручная выдача доступа, а не подтверждение оплаты Stars.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить выдачу / продление", callback_data=f"admin_confirm_sub:{token}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user:{target_user_id}:{page}")],
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_confirm_sub:"))
async def cb_admin_confirm_sub(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        result = await database.apply_admin_subscription_action(call.data.split(":")[1], call.from_user.id)
    except (ValueError, IndexError, PermissionError) as exc:
        await call.answer(str(exc), show_alert=True)
        return
    if not result["applied"]:
        await call.answer("Эта выдача уже выполнена. Повторное нажатие не добавляет срок.", show_alert=True)
        return
    target_user_id, plan, days = result["user_id"], result["tariff_plan"], result["days"]
    until = result["subscription_until"]
    max_radars = await database.get_user_max_radars(target_user_id)
    formatted_until = database.format_subscription_date(until)

    plan_title = "🔥 PRO" if plan == "pro" else "⚡️ STANDART"
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} выдал подписку {plan_title} на {days} дн. пользователю ID:{target_user_id}")

    await call.message.edit_text(
        f"✅ <b>Подписка {plan_title} успешно выдана!</b>\n\n"
        f"👤 ID пользователя: <code>{target_user_id}</code>\n"
        f"📦 Тариф: <b>{plan_title}</b> (до {max_radars} радаров)\n"
        f"📅 Срок действия: <b>{formatted_until}</b> ({days} дн.)",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Карточка пользователя", callback_data=f"admin_user:{target_user_id}:{result['page']}")],
            [InlineKeyboardButton(text="« Список пользователей", callback_data=f"admin_page:{result['page']}")],
        ]),
    )
    await call.answer(f"Подписка {plan_title} активирована!")

    # Ручная выдача не является подтверждением финансовой операции.
    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="refresh_profile")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")]
        ])
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                f"🎉 <b>Администратор активировал подписку {plan_title}!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Доступно активных радаров: <b>{max_radars} шт.</b>\n"
                f"📅 Срок действия: <b>{formatted_until}</b> ({days} дн.)\n\n"
                f"Радары доступны в пределах тарифа. Доставка новых лотов зависит от доступности Авито. "
                f"Удачных и прибыльных покупок!"
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {target_user_id} об активации подписки: {e}")


@router.callback_query(F.data.startswith(("admin_user:", "admin_sub_menu:", "admin_sub_days:")))
async def cb_admin_user(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        parts = call.data.split(":")
        target_id = int(parts[1])
        plan = parts[2] if parts[0] == "admin_sub_days" else None
        page = max(0, int(parts[3] if plan is not None else parts[2]))
        if plan is not None and plan not in ("standart", "pro"):
            raise ValueError("Неизвестный тариф")
    except (ValueError, IndexError):
        await call.answer("Некорректная кнопка пользователя", show_alert=True)
        return
    user = await database.get_user(target_id)
    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    if config.is_admin(target_id):
        await call.answer("Доступ администратора не требует подписки и не меняется здесь.", show_alert=True)
        return
    until = database.format_subscription_date(user["subscription_until"])
    has_access = await database.is_user_allowed(target_id)
    text = (f"👤 <b>{html.escape(user.get('first_name') or 'Пользователь')}</b>\n"
            f"🆔 ID: <code>{target_id}</code>\n"
            f"📦 Тариф: <b>{(user.get('tariff_plan') or 'standart').upper()}</b>\n"
            f"🔑 Доступ: {'активен' if has_access else 'истёк / отозван'}\n📅 Срок: {until}\n\n")
    if parts[0] == "admin_sub_menu":
        text += "<b>Выберите тариф для выдачи / продления:</b>"
        buttons = [
            [InlineKeyboardButton(text="⚡️ Standart · 4 радара", callback_data=f"admin_sub_days:{target_id}:standart:{page}")],
            [InlineKeyboardButton(text="🔥 PRO · 9 радаров и скидки", callback_data=f"admin_sub_days:{target_id}:pro:{page}")],
        ]
    elif parts[0] == "admin_sub_days":
        text += f"<b>Добавить срок на тарифе {plan.upper()}:</b>"
        buttons = [[InlineKeyboardButton(text=f"➕ {days} дн.", callback_data=f"admin_grant_sub:{target_id}:{plan}:{days}:{page}") for days in (1, 7, 30, 90)]]
    else:
        text += "Управление подпиской и оплатами:"
        buttons = [
            [InlineKeyboardButton(text="💳 Выдать / продлить → выбрать тариф", callback_data=f"admin_sub_menu:{target_id}:{page}")],
            [InlineKeyboardButton(text="⭐ Платежи / возврат", callback_data=f"admin_payments:{target_id}")],
            [InlineKeyboardButton(text="🚫 Отозвать доступ", callback_data=f"admin_revoke_prompt:{target_id}:{page}")],
            [InlineKeyboardButton(text="✍️ Написать пользователю", callback_data=f"support_reply:{target_id}")],
        ]
    buttons.append([InlineKeyboardButton(text="« Список пользователей", callback_data=f"admin_page:{page}")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admin_revoke_prompt:"))
async def cb_admin_revoke_prompt(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        _, user_id, page = call.data.split(":")
        user_id, page = int(user_id), int(page)
    except (ValueError, IndexError):
        await call.answer("Некорректная кнопка пользователя", show_alert=True)
        return
    if config.is_admin(user_id) or not await database.get_user(user_id):
        await call.answer("Отзыв недоступен", show_alert=True)
        return
    await call.message.edit_text(f"🚫 Отозвать подписку / триал пользователя <code>{user_id}</code>?\n"
                                 "Возврат Stars не выполняется автоматически.", parse_mode="HTML",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="✅ Отозвать доступ", callback_data=f"admin_revoke:{user_id}:{page}")],
                                     [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user:{user_id}:{page}")],
                                 ]))
    await call.answer()



# ── Панель администратора (/admin, /users, кнопка в меню) ─────────────

USERS_PER_PAGE = 5


@router.callback_query(F.data == "admin_noop")
async def cb_admin_noop(call: CallbackQuery):
    await call.answer()


@router.message(F.text.in_({"👑 Админ-панель", "👑 Админка"}))
@router.message(Command("admin"))
@router.message(Command("users"))
@router.callback_query(F.data == "admin_refresh")
@router.callback_query(F.data.startswith("admin_page:"))
async def cmd_admin_panel(event, page: int = 0):
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
        if event.data.startswith("admin_page:"):
            try:
                page = int(event.data.split(":")[1])
            except (ValueError, IndexError):
                page = 0
        await event.answer()
    else:
        message = event
        user_id = message.from_user.id

    if not config.is_admin(user_id):
        return

    stats = await database.get_system_stats()
    all_users = await database.get_all_users()
    total_users_count = len(all_users)
    total_pages = max(1, (total_users_count + USERS_PER_PAGE - 1) // USERS_PER_PAGE)

    # Защита границ страниц
    if page < 0:
        page = 0
    elif page >= total_pages:
        page = total_pages - 1

    start_idx = page * USERS_PER_PAGE
    end_idx = start_idx + USERS_PER_PAGE
    page_users = all_users[start_idx:end_idx]

    page_info = f" (Стр. {page + 1}/{total_pages})" if total_pages > 1 else ""
    text_lines = [
        "👑 <b>ПАНЕЛЬ УПРАВЛЕНИЯ СИСТЕМОЙ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>Сводная статистика:</b>\n"
        f"• 👥 Пользователей: <b>{stats['total_users']}</b> (🟢 {stats['allowed_users']} с доступом)\n"
        f"• 🎯 Всего радаров: <b>{stats['total_searches']}</b> (🟢 {stats['active_searches']} активно)\n"
        f"• 📦 Обработано лотов: <b>{stats['total_seen']}</b>\n"
        f"• 🛡 Защита: <b>TLS Chameleon / PoW Bypass</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Пользователи и права доступа{page_info}:</b>\n"
        "Нажмите «👤 Подписка» под пользователем → выдача / продление → тариф → срок → подтверждение.\n"
    ]

    buttons = []
    for idx, u in enumerate(page_users, start=start_idx + 1):
        u_id = u["user_id"]
        is_adm = config.is_admin(u_id)
        is_allowed = bool(u.get("is_allowed"))
        uname = f"@{u['username']}" if u.get("username") else f"ID: {u_id}"
        fname = html.escape(u.get("first_name") or "Без имени")

        has_access = await database.is_user_allowed(u_id)
        trial_used = bool(u.get("trial_used"))
        sub_until = u.get("subscription_until")
        sub_str = database.format_subscription_date(sub_until)

        # Компактный лейбл для кнопок, чтобы верстка не разъезжалась на экранах телефонов
        btn_tag = f"@{u['username']}" if u.get("username") else str(u_id)
        if len(btn_tag) > 13:
            btn_tag = btn_tag[:11] + ".."

        if is_adm:
            text_lines.append(f"{idx}. 👑 <b>{fname}</b> ({uname}) — <i>Администратор</i>")
        elif has_access and not is_allowed and trial_used:
            text_lines.append(f"{idx}. 🎁 <b>{fname}</b> ({uname}) — <i>[Триал 5ч · 1 радар] {sub_str}</i>")
            buttons.append([
                InlineKeyboardButton(text=f"🚫 Блок {btn_tag}", callback_data=f"admin_revoke_prompt:{u_id}:{page}"),
                InlineKeyboardButton(text=f"🗑 Удалить {btn_tag}", callback_data=f"admin_purge_user:{u_id}:{page}")
            ])
        elif has_access:
            plan = (u.get("tariff_plan") or "standart").upper()
            text_lines.append(f"{idx}. 🟢 <b>{fname}</b> ({uname}) — <i>[{plan}] {sub_str}</i>")
            buttons.append([
                InlineKeyboardButton(text=f"🚫 Блок {btn_tag}", callback_data=f"admin_revoke_prompt:{u_id}:{page}"),
                InlineKeyboardButton(text=f"🗑 Удалить {btn_tag}", callback_data=f"admin_purge_user:{u_id}:{page}")
            ])
        else:
            text_lines.append(f"{idx}. 🔴 <b>{fname}</b> ({uname}) — <i>Истёк доступ ({sub_str})</i>")
            buttons.append([
                InlineKeyboardButton(text=f"💳 Выдать {btn_tag}", callback_data=f"admin_sub_menu:{u_id}:{page}"),
                InlineKeyboardButton(text=f"🗑 Удалить {btn_tag}", callback_data=f"admin_purge_user:{u_id}:{page}")
            ])

        if not is_adm:
            buttons.append([InlineKeyboardButton(text=f"👤 Подписка / платежи {btn_tag}", callback_data=f"admin_user:{u_id}:{page}")])

    # Панель постраничной навигации (только если пользователей > 5)
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_page:{page - 1}"))
        else:
            nav_buttons.append(InlineKeyboardButton(text="⏹", callback_data="admin_noop"))

        nav_buttons.append(InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="admin_noop"))

        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin_page:{page + 1}"))
        else:
            nav_buttons.append(InlineKeyboardButton(text="⏹", callback_data="admin_noop"))

        buttons.append(nav_buttons)

    buttons.append([
        InlineKeyboardButton(text="💾 Скачать БД", callback_data="admin_backup"),
        InlineKeyboardButton(text="📋 Скачать логи", callback_data="admin_logs_menu")
    ])
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить список", callback_data=f"admin_page:{page}")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    full_text = "\n".join(text_lines)
    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(full_text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return
            pass

    await message.answer(full_text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_revoke:"))
async def cb_admin_revoke(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав", show_alert=True)
        return

    parts = call.data.split(":")
    target_user_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0

    if config.is_admin(target_user_id):
        await call.answer("Нельзя отозвать доступ администратора", show_alert=True)
        return
    await database.set_user_allowed(target_user_id, 0)
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} отозвал доступ у пользователя ID:{target_user_id}")
    await call.answer("Доступ отозван")
    await cmd_admin_panel(call, page=page)


@router.callback_query(F.data.startswith("admin_purge_user:"))
async def cb_admin_purge_user(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    parts = call.data.split(":")
    target_user_id = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0

    res = await database.purge_user_by_pattern(target_user_id)
    logger.info(f"👑 [АДМИН] Администратор ID:{call.from_user.id} стер пользователя ID:{target_user_id}: {res['message']}")
    await call.answer(res["message"][:200], show_alert=True)
    await cmd_admin_panel(call, page=page)


@router.message(Command("delete_user"))
@router.message(Command("purge_user"))
async def cmd_purge_user(message: Message):
    if not config.is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "⚠️ <b>Использование команды:</b>\n"
            "<code>/delete_user @username</code> или <code>/delete_user 12345678</code>\n\n"
            "<i>Полностью стирает пользователя, его радары, историю лотов и логи из системы.</i>",
            parse_mode="HTML"
        )
        return

    pattern = parts[1].strip()
    res = await database.purge_user_by_pattern(pattern)
    if res["success"]:
        await message.answer(f"✅ <b>Пользователь успешно удален из системы!</b>\n\n{html.escape(res['message'])}", parse_mode="HTML")
    else:
        await message.answer(f"⚠️ {html.escape(res['message'])}", parse_mode="HTML")



# ── Выгрузка базы данных SQLite ────────────────────────────────────────

@router.message(Command("backup"))
@router.message(Command("db"))
@router.callback_query(F.data == "admin_backup")
async def cmd_admin_backup(event):
    user_id = event.from_user.id
    if not config.is_admin(user_id):
        return

    if isinstance(event, CallbackQuery):
        message = event.message
        await event.answer("Формирую бэкап базы данных...")
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

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Скачать логи", callback_data="admin_logs_menu"),
                InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_refresh")
            ]
        ])

        await message.answer_document(
            document=file_to_send,
            caption=(
                "💾 <b>Резервная копия базы данных SQLite (WAL-безопасная)</b>\n\n"
                f"📦 Размер: <b>{len(data):,} байт</b> ({len(data) / 1024:.1f} KB)\n"
                "Файл полностью консистентен и готов к восстановлению или анализу."
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )
        logger.info(f"👑 [АДМИН] Администратор ID:{user_id} выгрузил бэкап базы данных ({filename})")
    finally:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except Exception:
                pass


# ── Выгрузка системных логов ──────────────────────────────────────────

@router.message(Command("logs"))
@router.message(Command("log"))
@router.callback_query(F.data == "admin_logs_menu")
async def cmd_admin_logs_menu(event):
    user_id = event.from_user.id
    if not config.is_admin(user_id):
        return

    if isinstance(event, CallbackQuery):
        message = event.message
        await event.answer()
    else:
        message = event

    menu_text = (
        "📋 <b>ВЫГРУЗКА ЛОГОВ СИСТЕМЫ AVITO RADAR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите файл для скачивания или выгрузите полный ZIP-архив:\n\n"
        "• 📦 <b>Все логи (ZIP)</b> — полный архив всех лог-файлов проекта\n"
        "• 📄 <b>bot.log</b> — общий консольный лог (все события)\n"
        "• 👥 <b>user_actions.log</b> — детальная хронология действий пользователей\n"
        "• ⚠️ <b>errors.log</b> — журнал ошибок и исключений (WARNING / ERROR)\n"
        "• 🕷 <b>parser.log</b> — сетевая активность парсера и мониторинга"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 Скачать ВСЕ логи (ZIP-архив)", callback_data="admin_log_get:all_zip")
        ],
        [
            InlineKeyboardButton(text="📄 bot.log", callback_data="admin_log_get:bot"),
            InlineKeyboardButton(text="👥 user_actions.log", callback_data="admin_log_get:user_actions")
        ],
        [
            InlineKeyboardButton(text="⚠️ errors.log", callback_data="admin_log_get:errors"),
            InlineKeyboardButton(text="🕷 parser.log", callback_data="admin_log_get:parser")
        ],
        [
            InlineKeyboardButton(text="💾 Скачать БД", callback_data="admin_backup"),
            InlineKeyboardButton(text="👑 В админку", callback_data="admin_refresh")
        ]
    ])

    if isinstance(event, CallbackQuery):
        try:
            await message.edit_text(menu_text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    await message.answer(menu_text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_log_get:"))
async def cb_admin_log_get(call: CallbackQuery):
    user_id = call.from_user.id
    if not config.is_admin(user_id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    target = call.data.split(":")[1]
    await call.answer("Формирую файл логов...")

    import io
    import os
    import zipfile
    from datetime import datetime
    from pathlib import Path
    from aiogram.types import BufferedInputFile

    base_dir = config.BASE_DIR
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1. Выгрузка ВСЕХ логов единым ZIP-архивом
    if target == "all_zip":
        zip_buffer = io.BytesIO()
        total_raw_size = 0
        files_count = 0

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            # Общий bot.log
            bot_log = base_dir / "bot.log"
            if bot_log.exists() and bot_log.is_file():
                sz = bot_log.stat().st_size
                if sz > 0:
                    zf.write(bot_log, arcname="bot.log")
                    total_raw_size += sz
                    files_count += 1

            # Файлы из папки logs/
            logs_dir = base_dir / "logs"
            if logs_dir.exists() and logs_dir.is_dir():
                for p in logs_dir.glob("*.log*"):
                    if p.is_file():
                        sz = p.stat().st_size
                        if sz > 0:
                            zf.write(p, arcname=f"logs/{p.name}")
                            total_raw_size += sz
                            files_count += 1

        if files_count == 0:
            await call.message.answer("⚠️ Файлы логов в системе пока отсутствуют или пусты.")
            return

        zip_buffer.seek(0)
        zip_bytes = zip_buffer.getvalue()
        zip_filename = f"avito_radar_logs_{timestamp}.zip"
        file_to_send = BufferedInputFile(zip_bytes, filename=zip_filename)

        compression_pct = round((1 - len(zip_bytes) / max(total_raw_size, 1)) * 100)

        await call.message.answer_document(
            document=file_to_send,
            caption=(
                "📦 <b>Полный архив системных логов AVITO RADAR</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🗂 Файлов в архиве: <b>{files_count}</b>\n"
                f"📊 Исходный размер: <b>{total_raw_size / 1024:.1f} KB</b>\n"
                f"🗜 Размер архива: <b>{len(zip_bytes) / 1024:.1f} KB</b> (сжатие {compression_pct}%)\n"
                f"📅 Дата выгрузки: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
            ),
            parse_mode="HTML"
        )
        logger.info(f"👑 [АДМИН] Администратор ID:{user_id} выгрузил полный ZIP-архив логов ({zip_filename})")
        return

    # 2. Выгрузка конкретного файла логов
    log_map = {
        "bot": (base_dir / "bot.log", "📄 bot.log (общий консольный лог)"),
        "user_actions": (base_dir / "logs" / "user_actions.log", "👥 user_actions.log (активность пользователей)"),
        "errors": (base_dir / "logs" / "errors.log", "⚠️ errors.log (журнал ошибок и сбоев)"),
        "parser": (base_dir / "logs" / "parser.log", "🕷 parser.log (лог парсера и запросов к Авито)"),
    }

    if target not in log_map:
        await call.answer("Неизвестный тип лога", show_alert=True)
        return

    log_path, desc = log_map[target]
    if not log_path.exists() or log_path.stat().st_size == 0:
        await call.message.answer(f"ℹ️ Файл <b>{log_path.name}</b> пока пуст или ещё не создан системой.", parse_mode="HTML")
        return

    raw_size = log_path.stat().st_size

    # Если файл больше 40 МБ, сжимаем его в ZIP во избежание ограничений Telegram (50 MB)
    if raw_size > 40 * 1024 * 1024:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(log_path, arcname=log_path.name)
        zip_buf.seek(0)
        file_bytes = zip_buf.getvalue()
        send_name = f"{log_path.stem}_{timestamp}.zip"
        file_to_send = BufferedInputFile(file_bytes, filename=send_name)
        caption = (
            f"{desc}\n\n"
            f"📦 <i>Файл сжат в ZIP из-за большого размера (>40 МБ)</i>\n"
            f"📊 Исходный: <b>{raw_size / (1024*1024):.1f} MB</b> | Архив: <b>{len(file_bytes) / (1024*1024):.1f} MB</b>"
        )
    else:
        with open(log_path, "rb") as f:
            file_bytes = f.read()
        send_name = f"{log_path.stem}_{timestamp}.log"
        file_to_send = BufferedInputFile(file_bytes, filename=send_name)
        caption = (
            f"{desc}\n\n"
            f"📦 Размер: <b>{raw_size / 1024:.1f} KB</b>\n"
            f"📅 Выгружен: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )

    await call.message.answer_document(
        document=file_to_send,
        caption=caption,
        parse_mode="HTML"
    )
    logger.info(f"👑 [АДМИН] Администратор ID:{user_id} выгрузил лог {send_name}")




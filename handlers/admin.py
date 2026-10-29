import html
import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database

logger = logging.getLogger(__name__)
router = Router()


class AdminStates(StatesGroup):
    waiting_for_user_query = State()


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


async def render_user_card_data(target_id: int, page: int = 0):
    user = await database.get_user(target_id)
    if not user:
        return None, None
    has_access = await database.is_user_allowed(target_id)
    until = database.format_subscription_date(user.get("subscription_until"))
    plan_name = (user.get("tariff_plan") or "standart").upper()
    trial_used = bool(user.get("trial_used"))
    is_allowed = bool(user.get("is_allowed"))

    user_radars = await database.get_user_searches(target_id)
    active_radars = sum(1 for r in user_radars if r.get("is_active", 1))
    max_radars = await database.get_user_max_radars(target_id)
    blocked_count = len(await database.get_user_blocked_sellers_list(target_id))

    if has_access and not is_allowed and trial_used:
        status_line = "🎁 <b>Бесплатный триал (5 ч)</b>"
    elif has_access and is_allowed and user.get("subscription_until"):
        status_line = f"🟢 <b>{plan_name} Подписка</b>"
    elif has_access and is_allowed:
        status_line = f"🟢 <b>Бессрочный VIP ({plan_name})</b>"
    else:
        status_line = "🔴 <b>Доступ истёк / отозван</b>"

    uname = f"@{user['username']}" if user.get("username") else "—"
    fname = html.escape(user.get("first_name") or "Без имени")

    text = (
        "👤 <b>КАРТОЧКА ПОЛЬЗОВАТЕЛЯ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• Имя: <b>{fname}</b>\n"
        f"• Username: <b>{uname}</b>\n"
        f"• Telegram ID: <code>{target_id}</code>\n"
        f"• Статус: {status_line}\n"
        f"• Тариф: <b>{plan_name}</b> (лимит: до {max_radars} радаров)\n"
        f"• Срок подписки: <i>{until}</i>\n"
        f"• Радары: <b>{active_radars}</b> акт. / <b>{len(user_radars)}</b> всего\n"
        f"• Чёрный список: <b>{blocked_count}</b> продавцов\n"
        f"• Триал использован: <b>{'Да' if trial_used else 'Нет'}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Управление пользователем:"
    )

    buttons = [
        [InlineKeyboardButton(text="💳 Выдать / продлить → выбрать тариф", callback_data=f"admin_sub_menu:{target_id}:{page}")],
        [InlineKeyboardButton(text="🎁 Выдать / обновить триал (5 ч)", callback_data=f"admin_reset_trial_prompt:{target_id}:{page}")],
        [InlineKeyboardButton(text=f"📁 Радары ({len(user_radars)})", callback_data=f"admin_user_radars:{target_id}:{page}"),
         InlineKeyboardButton(text="⭐ Платежи / возврат", callback_data=f"admin_payments:{target_id}")],
    ]
    if has_access:
        buttons.append([InlineKeyboardButton(text="🚫 Отозвать доступ", callback_data=f"admin_revoke_prompt:{target_id}:{page}")])
    buttons.append([
        InlineKeyboardButton(text="🗑 Удалить пользователя", callback_data=f"admin_purge_user:{target_id}:{page}"),
        InlineKeyboardButton(text="✍️ Написать пользователю", callback_data=f"support_reply:{target_id}")
    ])
    buttons.append([
        InlineKeyboardButton(text="🔍 Найти другого", callback_data="admin_search_user_prompt"),
        InlineKeyboardButton(text="« Список пользователей", callback_data=f"admin_page:{page}")
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


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

    if parts[0] == "admin_user":
        text, kb = await render_user_card_data(target_id, page)
        if not text:
            await call.answer("Пользователь не найден", show_alert=True)
            return
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await call.answer()
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
            [InlineKeyboardButton(text="👑 Бессрочный Standart (VIP)", callback_data=f"admin_grant_vip_prompt:{target_id}:standart:{page}")],
            [InlineKeyboardButton(text="👑 Бессрочный PRO (VIP)", callback_data=f"admin_grant_vip_prompt:{target_id}:pro:{page}")],
            [InlineKeyboardButton(text="« Назад в карточку", callback_data=f"admin_user:{target_id}:{page}")]
        ]
    elif parts[0] == "admin_sub_days":
        text += f"<b>Добавить срок на тарифе {plan.upper()}:</b>"
        buttons = [
            [InlineKeyboardButton(text=f"➕ {days} дн.", callback_data=f"admin_grant_sub:{target_id}:{plan}:{days}:{page}") for days in (1, 7, 14)],
            [InlineKeyboardButton(text=f"➕ {days} дн.", callback_data=f"admin_grant_sub:{target_id}:{plan}:{days}:{page}") for days in (30, 90, 365)],
            [InlineKeyboardButton(text="« Назад к выбору тарифа", callback_data=f"admin_sub_menu:{target_id}:{page}")]
        ]
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admin_grant_vip_prompt:"))
async def cb_admin_grant_vip_prompt(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    target_id, plan, page = int(parts[1]), parts[2], int(parts[3])
    plan_title = "🔥 PRO" if plan == "pro" else "⚡️ STANDART"
    text = (
        f"👑 <b>ВЫДАЧА БЕССРОЧНОГО VIP-ДОСТУПА</b>\n\n"
        f"Пользователь: <code>{target_id}</code>\n"
        f"Тариф: <b>{plan_title}</b>\n\n"
        "Пользователю будет выдан постоянный бессрочный доступ к боту без ограничения по времени. "
        "Пользователь получит мгновенное уведомление в чат."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить выдачу VIP", callback_data=f"admin_confirm_vip:{target_id}:{plan}:{page}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user:{target_id}:{page}")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admin_confirm_vip:"))
async def cb_admin_confirm_vip(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    target_id, plan, page = int(parts[1]), parts[2], int(parts[3])
    await database.grant_vip_access(target_id, plan)
    max_radars = await database.get_user_max_radars(target_id)
    plan_title = "🔥 PRO" if plan == "pro" else "⚡️ STANDART"

    logger.info("👑 [АДМИН] Администратор ID:%s выдал бессрочный VIP (%s) пользователю ID:%s", call.from_user.id, plan_title, target_id)

    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Добавить радар", callback_data="add_search_prompt")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="refresh_profile")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_approved")]
        ])
        await bot.send_message(
            chat_id=target_id,
            text=(
                f"👑 <b>Администратор предоставил вам постоянный VIP-доступ к AVITO RADAR!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Тариф: <b>{plan_title}</b>\n"
                f"🎯 Лимит активных радаров: <b>{max_radars} шт.</b>\n"
                f"📅 Срок действия: <b>Бессрочно</b> ♾️\n\n"
                f"Вам открыт доступ ко всем возможностям тарифа без ограничений по времени. "
                f"Удачных и выгодных находок!"
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning("Не удалось уведомить пользователя %s о выдаче VIP: %s", target_id, e)

    await call.answer(f"Бессрочный {plan_title} выдан!", show_alert=True)
    text, kb = await render_user_card_data(target_id, page)
    if text:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_reset_trial_prompt:"))
async def cb_admin_reset_trial_prompt(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    target_id, page = int(parts[1]), int(parts[2])
    text = (
        f"🎁 <b>АКТИВАЦИЯ ТРИАЛА (6 ЧАСОВ)</b>\n\n"
        f"Пользователь: <code>{target_id}</code>\n\n"
        "Пользователю будет выдан бесплатный пробный доступ на <b>6 часов</b> (1 радар, Standart). "
        "Пользователь получит мгновенное уведомление в чат."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Активировать триал 6 ч", callback_data=f"admin_confirm_trial:{target_id}:{page}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user:{target_id}:{page}")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admin_confirm_trial:"))
async def cb_admin_confirm_trial(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    target_id, page = int(parts[1]), int(parts[2])
    user = await database.reset_user_trial(target_id)
    until = database.format_subscription_date(user.get("subscription_until"))

    logger.info("👑 [АДМИН] Администратор ID:%s активировал триал 6ч для пользователя ID:%s", call.from_user.id, target_id)

    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить радар", callback_data="start_approved")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="refresh_profile")]
        ])
        await bot.send_message(
            chat_id=target_id,
            text=(
                f"🎁 <b>Администратор активировал вам бесплатный пробный период на 6 часов!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Доступен: <b>1 активный радар</b> (Standart)\n"
                f"📅 Срок действия: <b>{until}</b> (6 ч.)\n\n"
                f"Отправьте ссылку на поиск с Авито прямо в чат или нажмите кнопку ниже:"
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning("Не удалось уведомить пользователя %s об активации триала: %s", target_id, e)

    await call.answer("Триал 6 часов активирован!", show_alert=True)
    text, kb = await render_user_card_data(target_id, page)
    if text:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_user_radars:"))
async def cb_admin_user_radars(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    parts = call.data.split(":")
    target_id, page = int(parts[1]), int(parts[2])
    radars = await database.get_user_searches(target_id)

    text_lines = [
        f"📁 <b>РАДАРЫ ПОЛЬЗОВАТЕЛЯ <code>{target_id}</code></b> (всего: {len(radars)})\n"
        "━━━━━━━━━━━━━━━━━━━━"
    ]
    if not radars:
        text_lines.append("<i>У пользователя пока нет созданных радаров.</i>")
    else:
        for idx, r in enumerate(radars, start=1):
            is_active = bool(r.get("is_active", 1))
            status_emoji = "🟢" if is_active else "⏸"
            scope = "🏷 В заголовках" if r.get("search_in_title_only") else "📄 Заголовок+текст"
            title = html.escape(r.get("title") or "Без названия")
            url = html.escape(r.get("url") or "")
            text_lines.append(
                f"{idx}. {status_emoji} <b>{title}</b>\n"
                f"   ├ Выборка: <i>{scope}</i>\n"
                f"   └ <a href=\"{url}\">Ссылка на Авито</a>"
            )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Назад в карточку пользователя", callback_data=f"admin_user:{target_id}:{page}")]
    ])
    await call.message.edit_text("\n".join(text_lines), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
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

    from proxy_pool import get_proxy_pool
    pool = get_proxy_pool()
    real_proxies = [c for c in pool.clients if c.proxy_url is not None]
    if real_proxies:
        net_mode_str = f"🟢 <b>Пул прокси ({len(real_proxies)} шт.)</b>"
    elif config.ALLOW_DIRECT_AVITO:
        net_mode_str = "🟡 <b>Прямой без прокси (IP сервера, 1.0 req/s)</b>"
    else:
        net_mode_str = "⏸ <b>На паузе (прокси нет, прямой режим ВЫКЛ)</b>"

    page_info = f" (Стр. {page + 1}/{total_pages})" if total_pages > 1 else ""
    text_lines = [
        "👑 <b>ПАНЕЛЬ УПРАВЛЕНИЯ СИСТЕМОЙ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>Сводная статистика:</b>\n"
        f"• 👥 Пользователей: <b>{stats['total_users']}</b> (🟢 {stats['allowed_users']} с доступом)\n"
        f"• 🎯 Всего радаров: <b>{stats['total_searches']}</b> (🟢 {stats['active_searches']} активно)\n"
        f"• 📦 Обработано лотов: <b>{stats['total_seen']}</b>\n"
        f"• 🌐 Режим сети: {net_mode_str}\n"
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
            text_lines.append(f"{idx}. 🎁 <b>{fname}</b> ({uname}) — <i>[Триал 6ч · 1 радар] {sub_str}</i>")
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
        InlineKeyboardButton(text="🔍 Поиск по ID / @username", callback_data="admin_search_user_prompt")
    ])
    buttons.append([
        InlineKeyboardButton(text="💾 Скачать БД", callback_data="admin_backup"),
        InlineKeyboardButton(text="📋 Скачать логи", callback_data="admin_logs_menu")
    ])
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить список", callback_data=f"admin_page:{page}")
    ])
    direct_btn_label = (
        "🌐 Прямой парсинг (без прокси): ВКЛ 🟢"
        if config.ALLOW_DIRECT_AVITO
        else "🌐 Прямой парсинг (без прокси): ВЫКЛ 🔴"
    )
    buttons.append([
        InlineKeyboardButton(text=direct_btn_label, callback_data=f"admin_toggle_direct_avito:panel:{page}"),
        InlineKeyboardButton(text="📊 Статистика (/stats)", callback_data="admin_runtime_stats"),
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
async def cb_admin_revoke(call: CallbackQuery, bot: Bot):
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

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "⚠️ <b>Ваш доступ к AVITO RADAR был приостановлен администратором.</b>\n\n"
                "Если у вас есть вопросы или возникла ошибка, свяжитесь со службой поддержки:"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="support_start_ticket")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning("Не удалось уведомить пользователя %s об отзыве доступа: %s", target_user_id, e)

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


# ── Поиск пользователей администратором ───────────────────────────────

@router.callback_query(F.data == "admin_search_user_prompt")
async def cb_admin_search_user_prompt(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_user_query)
    text = (
        "🔍 <b>ПОИСК ПОЛЬЗОВАТЕЛЯ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите <b>@username</b>, <b>Telegram ID</b> или имя пользователя:\n\n"
        "<i>Примеры:</i>\n"
        "• <code>@username</code>\n"
        "• <code>986832959</code>\n"
        "• <code>Алексей</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_search_cancel")]
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_search_cancel")
async def cb_admin_search_cancel(call: CallbackQuery, state: FSMContext):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    await state.clear()
    await call.answer("Поиск отменён")
    await cmd_admin_panel(call, page=0)


@router.message(Command("find"))
@router.message(Command("user"))
@router.message(Command("search_user"))
async def cmd_admin_find_user(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await state.set_state(AdminStates.waiting_for_user_query)
        await message.answer(
            "🔍 Введите <b>@username</b>, <b>Telegram ID</b> или имя пользователя для поиска:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_search_cancel")]
            ])
        )
        return
    await do_admin_user_search(message, args[1].strip(), state)


@router.message(AdminStates.waiting_for_user_query)
async def msg_admin_user_query(message: Message, state: FSMContext):
    if not config.is_admin(message.from_user.id):
        await state.clear()
        return
    query = (message.text or "").strip()
    await do_admin_user_search(message, query, state)


async def do_admin_user_search(message: Message, query: str, state: FSMContext):
    await state.clear()
    if not query:
        await message.answer("Запрос пуст. Попробуйте снова.")
        return

    users = await database.find_users_by_query(query)
    if not users:
        await message.answer(
            f"❌ <b>Пользователь не найден</b>\n\n"
            f"По запросу <code>{html.escape(query)}</code> в базе данных бота ничего не найдено.\n\n"
            f"Убедитесь, что пользователь хотя бы раз отправлял <code>/start</code> в бот.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Искать снова", callback_data="admin_search_user_prompt")],
                [InlineKeyboardButton(text="👑 В админ-панель", callback_data="admin_refresh")]
            ])
        )
        return

    if len(users) == 1:
        u_id = users[0]["user_id"]
        text, kb = await render_user_card_data(u_id, page=0)
        if text:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

    text = (
        f"🔍 <b>РЕЗУЛЬТАТЫ ПОИСКА</b> (найдено: {len(users)})\n"
        f"Запрос: <code>{html.escape(query)}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )
    buttons = []
    for idx, u in enumerate(users[:10], start=1):
        u_id = u["user_id"]
        uname = f"@{u['username']}" if u.get("username") else f"ID: {u_id}"
        fname = html.escape(u.get("first_name") or "Без имени")
        plan = (u.get("tariff_plan") or "standart").upper()
        has_access = await database.is_user_allowed(u_id)
        status_icon = "🟢" if has_access else "🔴"
        text += f"{idx}. {status_icon} <b>{fname}</b> ({uname}) — <i>[{plan}]</i> [ID: <code>{u_id}</code>]\n"
        buttons.append([
            InlineKeyboardButton(text=f"👤 Карточка {uname}", callback_data=f"admin_user:{u_id}:0"),
            InlineKeyboardButton(text="💳 Выдать", callback_data=f"admin_sub_menu:{u_id}:0")
        ])

    buttons.append([
        InlineKeyboardButton(text="🔍 Новый поиск", callback_data="admin_search_user_prompt"),
        InlineKeyboardButton(text="👑 В админ-панель", callback_data="admin_refresh")
    ])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")





@router.message(Command("stats"))
@router.callback_query(F.data == "admin_runtime_stats")
async def cmd_admin_stats(event):
    if isinstance(event, CallbackQuery):
        message = event.message
        user_id = event.from_user.id
        if not config.is_admin(user_id):
            await event.answer("У вас нет прав администратора", show_alert=True)
            return
        await event.answer()
    else:
        message = event
        user_id = message.from_user.id
        if not config.is_admin(user_id):
            await message.answer("⛔ У вас нет прав администратора.")
            return

    from proxy_pool import get_proxy_pool
    from runtime_metrics import METRICS
    try:
        pool = get_proxy_pool()
        ob = await database.get_outbox_metrics()
        METRICS.update_outbox_state(
            pending_depth=ob.get("pending", 0),
            leased_depth=ob.get("leased", 0),
            oldest_pending_age_seconds=ob.get("oldest_seconds", 0),
        )
        METRICS.snapshot(proxy_snapshots=pool.snapshots())
    except Exception:
        pass

    direct_btn_label = (
        "🌐 Прямой парсинг (без прокси): ВКЛ 🟢"
        if config.ALLOW_DIRECT_AVITO
        else "🌐 Прямой парсинг (без прокси): ВЫКЛ 🔴"
    )
    stats_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=direct_btn_label, callback_data="admin_toggle_direct_avito:stats")],
        [
            InlineKeyboardButton(text="🔄 Обновить /stats", callback_data="admin_runtime_stats"),
            InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_refresh"),
        ],
    ])

    chunks = METRICS.format_stats_messages()
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        await message.answer(chunk, reply_markup=stats_kb if is_last else None, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_toggle_direct_avito"))
async def cb_admin_toggle_direct_avito(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return

    from proxy_pool import get_proxy_pool
    new_state = not bool(config.ALLOW_DIRECT_AVITO)
    await database.set_bot_setting("allow_direct_avito", "1" if new_state else "0")
    pool = get_proxy_pool()
    res = await pool.set_direct_mode_async(new_state)

    status_toast = (
        f"✅ Прямой парсинг без прокси ВКЛЮЧЕН (активных каналов: {res['total']})"
        if new_state
        else f"🛑 Прямой парсинг без прокси ВЫКЛЮЧЕН (активных каналов: {res['total']})"
    )
    await call.answer(status_toast, show_alert=True)

    parts = (call.data or "").split(":")
    source = parts[1] if len(parts) > 1 else "panel"
    if source == "panel":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await cmd_admin_panel(call, page=page)
    elif source == "stats":
        await cmd_admin_stats(call)
    else:
        state_badge = "🟢 <b>ВКЛЮЧЕН</b> (1.0 req/s с IP сервера)" if new_state else "🔴 <b>ВЫКЛЮЧЕН</b> (на паузе без прокси)"
        toggle_label = (
            "🌐 Прямой парсинг (без прокси): ВКЛ 🟢"
            if new_state
            else "🌐 Прямой парсинг (без прокси): ВЫКЛ 🔴"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=toggle_label, callback_data="admin_toggle_direct_avito:alert")],
            [
                InlineKeyboardButton(text="📊 Открыть /stats", callback_data="admin_runtime_stats"),
                InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_refresh"),
            ],
        ])
        try:
            await call.message.edit_text(
                f"🌐 <b>Режим прямого парсинга без прокси:</b> {state_badge}\n\n"
                f"• Активных каналов в пуле: <code>{res['total']}</code>\n"
                "Настройка сохранена в БД и действует сразу без перезапуска.",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.message(Command("direct_mode"))
@router.message(Command("direct"))
async def cmd_admin_direct_mode(message: Message):
    if not config.is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав администратора.")
        return

    from proxy_pool import get_proxy_pool
    parts = (message.text or "").strip().split()
    if len(parts) > 1 and parts[1].lower() in ("1", "on", "true", "yes", "вкл"):
        new_state = True
    elif len(parts) > 1 and parts[1].lower() in ("0", "off", "false", "no", "выкл"):
        new_state = False
    else:
        new_state = not bool(config.ALLOW_DIRECT_AVITO)

    await database.set_bot_setting("allow_direct_avito", "1" if new_state else "0")
    pool = get_proxy_pool()
    res = await pool.set_direct_mode_async(new_state)

    state_badge = "🟢 ВКЛЮЧЕН (1.0 req/s с IP сервера)" if new_state else "🔴 ВЫКЛЮЧЕН"
    toggle_label = (
        "🌐 Прямой парсинг (без прокси): ВКЛ 🟢"
        if new_state
        else "🌐 Прямой парсинг (без прокси): ВЫКЛ 🔴"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_label, callback_data="admin_toggle_direct_avito:alert")],
        [
            InlineKeyboardButton(text="📊 Открыть /stats", callback_data="admin_runtime_stats"),
            InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_refresh"),
        ],
    ])
    await message.answer(
        f"🌐 <b>Прямой парсинг Авито (без прокси): {state_badge}</b>\n\n"
        f"• Активных каналов в пуле: <code>{res['total']}</code>\n"
        "• Настройка сохранена в базе данных и применена мгновенно.",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(Command("reload_proxies"))
async def cmd_admin_reload_proxies(message: Message):
    if not config.is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав администратора.")
        return

    from proxy_pool import get_proxy_pool
    from runtime_metrics import sanitize_proxy_credentials

    try:
        pool = get_proxy_pool()
        res = await pool.reload_async()
        await message.answer(
            "✅ <b>Пул прокси успешно перезагружен:</b>\n"
            f"• Активных прокси: <code>{res['total']}</code>\n"
            f"• Добавлено: <code>{res['added']}</code>\n"
            f"• Сохранено: <code>{res['kept']}</code>\n"
            f"• Выведено в дренаж: <code>{res['removed']}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        safe_err = sanitize_proxy_credentials(str(exc))
        await message.answer(
            f"❌ <b>Ошибка перезагрузки пула прокси:</b> {html.escape(safe_err)}\n"
            "Текущий активный пул оставлен без изменений.",
            parse_mode="HTML",
        )


# ── /proxy: подключение и проверка одного прокси без перезапуска ─────────

PROXY_HELP = (
    "• <code>/proxy http://логин:пароль@IP:порт</code> — проверить и подключить\n"
    "  (подходит и формат <code>IP:порт:логин:пароль</code>, а также socks5://)\n"
    "• <code>/proxy test</code> — проверить текущий канал (прокси или IP сервера)\n"
    "• <code>/proxy off</code> — отключить прокси из бота"
)

FAILURE_LABELS = {
    "timeout": "таймаут (прокси медленный или мёртвый)",
    "network": "сетевая ошибка (прокси недоступен или неверные данные)",
    "forbidden": "Авито заблокировал IP (403)",
    "rate_limit": "Авито ограничил частоту (429)",
    "challenge": "антибот-проверка не пройдена",
    "cooldown": "канал на паузе после блокировки",
    "catalog_missing": "страница без каталога (логин/заглушка)",
}


def _format_probe(result) -> str:
    lines = [
        f"🧪 <b>Проверка канала</b> <code>{html.escape(result.masked_url)}</code>",
        f"Поиск: <code>{html.escape(result.target_url[:90])}</code>",
        "",
    ]
    for index, attempt in enumerate(result.attempts, 1):
        if attempt.ok:
            lines.append(f"{index}) ✅ {attempt.elapsed_ms} мс · лотов: {attempt.items}")
        else:
            label = FAILURE_LABELS.get(attempt.failure, attempt.failure)
            status = f" (HTTP {attempt.status})" if attempt.status else ""
            lines.append(f"{index}) ❌ {attempt.elapsed_ms} мс · {html.escape(label)}{status}")
    median = result.median_ok_ms
    lines.append("")
    lines.append(
        f"Итог: <b>{result.successes}/{len(result.attempts)}</b> успешно"
        + (f", медиана <b>{median} мс</b>" if median is not None else "")
    )
    return "\n".join(lines)


async def _probe_target_url() -> str | None:
    """Probe on a real radar link when there is one: that is what the proxy will be scanning."""
    try:
        searches = await database.get_all_active_searches()
    except Exception:
        return None
    return searches[0]["url"] if searches else None


def _current_channel_text(pool) -> str:
    if pool.pool_file_overrides_single_proxy():
        return f"📄 файл пула прокси ({len(pool.clients)} шт.)"
    if config.PROXY_URL:
        from runtime_metrics import mask_proxy_url
        return f"🟢 прокси <code>{html.escape(mask_proxy_url(config.PROXY_URL))}</code>"
    if config.ALLOW_DIRECT_AVITO:
        return "🟡 IP сервера (прямой режим)"
    return "⏸ нет канала (прокси не задан, прямой режим выключен)"


@router.message(Command("proxy"))
async def cmd_admin_proxy(message: Message):
    if not config.is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав администратора.")
        return

    from proxy_check import probe_channel
    from proxy_pool import get_proxy_pool, parse_and_validate_proxy_line
    from runtime_metrics import sanitize_proxy_credentials

    pool = get_proxy_pool()
    parts = (message.text or "").strip().split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if not arg:
        lines = [f"🌐 <b>Текущий канал:</b> {_current_channel_text(pool)}"]
        for snap in pool.snapshots():
            lines.append(
                f"• <code>{html.escape(str(snap.get('masked_url')))}</code> [{snap.get('state')}] "
                f"rate <code>{float(snap.get('effective_rate') or 0):.2f}</code> req/s"
            )
        lines += ["", PROXY_HELP]
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    if arg.lower() in ("off", "выкл", "0"):
        await database.set_bot_setting("proxy_url", "")
        await pool.set_single_proxy_async(config.parse_proxy(config.RAW_PROXY))
        await message.answer(f"🔌 Прокси из бота отключён.\n🌐 <b>Сейчас:</b> {_current_channel_text(pool)}", parse_mode="HTML")
        return

    target = await _probe_target_url()

    if arg.lower() in ("test", "тест", "check"):
        status = await message.answer("⏳ Проверяю текущий канал: 3 запроса к Авито подряд…")
        current = None if pool.pool_file_overrides_single_proxy() else config.PROXY_URL
        result = await probe_channel(current, target)
        await status.edit_text(_format_probe(result), parse_mode="HTML")
        return

    # The command text carries the proxy password: do not leave it in the chat history.
    try:
        await message.delete()
    except Exception:
        pass
    try:
        normalized = parse_and_validate_proxy_line(arg)
    except ValueError as exc:
        await message.answer(
            f"❌ {html.escape(sanitize_proxy_credentials(str(exc)))}\n\n{PROXY_HELP}", parse_mode="HTML"
        )
        return

    status = await message.answer("⏳ Проверяю прокси: 3 запроса к Авито подряд…")
    result = await probe_channel(normalized, target)
    report = _format_probe(result)
    if result.successes == 0:
        await status.edit_text(
            report + "\n\n🚫 <b>Прокси не подключён</b>: ни один запрос не прошёл. Текущий канал не изменён.",
            parse_mode="HTML",
        )
        return

    await database.set_bot_setting("proxy_url", normalized)
    await pool.set_single_proxy_async(normalized)
    note = ""
    if pool.pool_file_overrides_single_proxy():
        note = "\n⚠️ На сервере задан PROXY_POOL_FILE — он важнее, этот прокси не используется, пока файл существует."
    await status.edit_text(
        report
        + f"\n\n✅ <b>Прокси подключён</b> и сохранён: радары уже сканируют через него, перезапуск не нужен.{note}"
        + "\nСкорость и блокировки смотрите в /stats, вернуть IP сервера — <code>/proxy off</code>.",
        parse_mode="HTML",
    )
    logger.info("Admin %s switched scanning to proxy %s", message.from_user.id, result.proxy_id)

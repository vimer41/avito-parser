"""Background jobs that talk to people rather than Avito: renewal reminders and the morning admin report."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import config
import database
import ui
from runtime_metrics import METRICS

logger = logging.getLogger(__name__)

DAY = 86400.0


def _until_epoch(raw) -> Optional[float]:
    try:
        return datetime.strptime(str(raw).strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def reminder_kind(user: dict, now: float) -> Optional[str]:
    """Which reminder is due: '3d', '1d', '1h' (trial), 'end', or None."""
    until = _until_epoch(user.get("subscription_until"))
    if until is None or config.is_admin(int(user["user_id"])):
        return None
    left = until - now
    is_trial = bool(user.get("trial_used")) and not bool(user.get("is_allowed"))
    if left <= 0:
        return "end" if left > -DAY else None
    if is_trial:
        return "1h" if left <= 3600 else None
    if left <= DAY:
        return "1d"
    if left <= 3 * DAY:
        return "3d"
    return None


def reminder_text(user: dict, kind: str) -> str:
    plan = config.plan_title(user.get("tariff_plan"))
    is_trial = bool(user.get("trial_used")) and not bool(user.get("is_allowed"))
    date = database.format_subscription_date(user.get("subscription_until")).split(" · ")[0]
    if kind == "end" and is_trial:
        return (
            f"{ui.e('time')} <b>Пробный период закончился</b>\n\n"
            "Радары и настройки сохранены. Выберите тариф, и бот сразу снова начнёт присылать новые лоты."
        )
    if kind == "end":
        return (
            f"{ui.e('time')} <b>Подписка «{plan}» закончилась</b>\n\n"
            "Новые лоты больше не приходят. Радары и стоп-слова сохранены: продлите подписку, и всё заработает сразу."
        )
    if kind == "1h":
        return (
            f"{ui.e('time')} <b>Пробный период закончится через час</b>\n\n"
            "Чтобы не пропустить новые лоты, выберите тариф заранее."
        )
    when = "завтра" if kind == "1d" else "через 3 дня"
    return (
        f"{ui.e('time')} <b>Подписка «{plan}» закончится {when}</b>\n"
        f"{date}\n\n"
        "Продлите заранее: оплаченные дни добавятся к текущему сроку, без автосписаний."
    )


async def send_due_reminders(bot: Bot, now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    sent = 0
    for user in await database.get_users_with_subscription():
        kind = reminder_kind(user, now)
        if kind is None:
            continue
        key = f"{user.get('subscription_until')}|{kind}"
        if user.get("reminded_key") == key:
            continue
        user_id = int(user["user_id"])
        try:
            await bot.send_message(user_id, reminder_text(user, kind), parse_mode="HTML", reply_markup=ui.kb(
                [ui.btn("Продлить" if kind != "end" else "Выбрать тариф", "tariffs",
                        callback_data="open_subscription_menu", style="success")],
            ))
            sent += 1
        except TelegramForbiddenError:
            pass
        except Exception as exc:
            logger.warning("Напоминание о подписке не отправлено %s: %s", user_id, exc)
            continue
        await database.set_reminded_key(user_id, key)
    return sent


async def reminder_worker(bot: Bot) -> None:
    while True:
        try:
            count = await send_due_reminders(bot)
            if count:
                logger.info("Отправлено напоминаний о подписке: %s", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой рассылки напоминаний")
        await asyncio.sleep(config.REMINDER_CHECK_INTERVAL)


def _fmt_seconds(value) -> str:
    if value is None:
        return "нет данных"
    value = float(value)
    return f"{value:.0f} с" if value < 120 else f"{value / 60:.0f} мин"


async def build_daily_report(reset: bool = True) -> str:
    stats = await database.get_daily_report_stats(24)
    system = await database.get_system_stats()
    day = METRICS.pop_daily_summary(reset=reset)
    scans_total = day["scans_ok"] + day["scans_failed"]
    scan_ok = f"{day['scans_ok'] * 100 / scans_total:.0f} %" if scans_total else "нет данных"
    pay_lines = [
        f"• {config.plan_title(plan)}: {count} шт · {amount} ⭐" for plan, count, amount in stats["payments"]
    ] or ["• нет"]
    stars_total = sum(amount for _plan, _count, amount in stats["payments"])
    lat = day["pub_to_sent"]
    try:
        deficit = float(METRICS.snapshot(window_minutes=60)["capacity"].get("deficit_now") or 0.0)
    except Exception:
        deficit = 0.0
    lines = [
        f"{ui.e('stats')} <b>Отчёт за сутки</b>" + (f" ({day['hours']:.0f} ч с запуска)" if day["hours"] < 23 else ""),
        "",
        f"{ui.e('users')} Новых пользователей: <b>{stats['new_users']}</b> · всего {system['total_users']}, с доступом {system['allowed_users']}",
        f"{ui.e('time')} Подписка кончается за 3 дня: <b>{stats['expiring_3d']}</b>",
        f"{ui.e('star')} Оплаты: <b>{stars_total} ⭐</b>",
        *pay_lines,
        "",
        f"{ui.e('radar')} Радары: {system['active_searches']} активных из {system['total_searches']}",
        f"{ui.e('ok')} Удачных сканов: <b>{scan_ok}</b> ({day['scans_ok']} из {scans_total})",
        f"{ui.e('new')} Отправлено лотов: <b>{day['sent']}</b>",
        f"{ui.e('wait')} Публикация → уведомление: p50 <b>{_fmt_seconds(lat.get('p50'))}</b>, "
        f"p95 {_fmt_seconds(lat.get('p95'))} (n={lat.get('sample_count', 0)})",
    ]
    if deficit > 0:
        lines.append(f"{ui.e('warn')} Не хватает мощности: {deficit:.2f} req/s — нужны ещё прокси")
    return "\n".join(lines)


def seconds_until_report(now: Optional[float] = None) -> float:
    now = time.time() if now is None else now
    msk = now + 3 * 3600
    target = (msk // DAY) * DAY + config.DAILY_REPORT_HOUR * 3600
    if target <= msk:
        target += DAY
    return target - msk


async def daily_report_worker(bot: Bot) -> None:
    while True:
        await asyncio.sleep(seconds_until_report())
        try:
            text = await build_daily_report()
            for admin_id in config.ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=ui.kb(
                        [ui.btn("Мониторинг", "stats", callback_data="admin_runtime_stats"),
                         ui.btn("Админ-панель", "admin", callback_data="admin_refresh")],
                    ))
                except Exception as exc:
                    logger.warning("Утренний отчёт не доставлен %s: %s", admin_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой утреннего отчёта")


def start_report_services(bot: Bot) -> list[asyncio.Task]:
    return [
        asyncio.create_task(reminder_worker(bot), name="subscription-reminders"),
        asyncio.create_task(daily_report_worker(bot), name="daily-report"),
    ]

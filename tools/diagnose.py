"""Bounded production diagnostics. No getUpdates, migrations or raw user logs."""

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def scan_summary(text: str):
    rows = [(int(ok), int(ms), int(count), int(blocked)) for ok, ms, count, blocked in
            re.findall(r"Scan result: ok=(\d) elapsed_ms=(\d+) items=(\d+) blocked=(\d)", text)]
    success_ms = sorted(row[1] for row in rows if row[0])
    return {
        "scope": "page fetch including challenges; bounded log tail, not a sustained SLA",
        "attempts": len(rows), "successes": sum(row[0] for row in rows),
        "failures": sum(not row[0] for row in rows), "blocked_attempts": sum(row[3] for row in rows),
        "success_rate": round(sum(row[0] for row in rows) / len(rows), 4) if rows else None,
        "success_latency_p50_ms": success_ms[len(success_ms) // 2] if success_ms else None,
        "success_latency_p95_ms": success_ms[min(len(success_ms) - 1, int(len(success_ms) * .95))] if success_ms else None,
        "http_403_entries": len(re.findall(r"HTTP 403\b", text)),
        "http_429_entries": len(re.findall(r"HTTP 429\b", text)),
        "traceback_entries": text.count("Traceback (most recent call last)"),
        "payment_confirmation_entries": text.count("Stars payment fulfilled:"),
        "real_admin_probe_entries": text.count("Live Telegram probe completed by admin="),
        "ad_delivery_log_entries": sum(text.count(marker) for marker in ("Доставлено (текст)", "Доставлено (с фото)", "Доставлено (file_id")),
    }


def tail(path: Path, size=2_000_000):
    if not path.is_file():
        return ""
    with path.open("rb") as source:
        source.seek(max(0, path.stat().st_size - size))
        return source.read(size).decode("utf-8", errors="replace")


def print_database_summary(path: Path):
    if not path.is_file():
        print("Database not found")
        return
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as db:
        print("SQLite quick_check:", db.execute("PRAGMA quick_check").fetchone()[0])
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        print("SQLite row counts:", json.dumps({name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                                               for name in ("users", "searches", "notification_outbox", "stars_orders") if name in tables}))
        if "searches" in tables:
            print("Active radar flags:", db.execute("SELECT COUNT(*) FROM searches WHERE is_active = 1").fetchone()[0])
        if "stars_orders" in tables:
            print("Stars order states:", json.dumps(dict(db.execute("SELECT status, COUNT(*) FROM stars_orders GROUP BY status"))))
            print("Refunds requiring subscription review:", db.execute("SELECT COUNT(*) FROM stars_orders WHERE refund_needs_review = 1").fetchone()[0])


async def telegram_probe(config, notify_admin: bool):
    from aiogram import Bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    bot = Bot(config.BOT_TOKEN)
    try:
        me = await bot.get_me(request_timeout=10)
        print("Telegram API getMe:", json.dumps({"ok": True, "username": me.username}))
        info = await bot.get_webhook_info(request_timeout=10)
        print("Telegram webhook:", json.dumps({"webhook_configured": bool(info.url), "pending_updates": info.pending_update_count}))
        if notify_admin:
            message = await bot.send_message(
                config.ADMIN_ID,
                "✅ <b>Версия с Telegram Stars установлена.</b>\n"
                f"Standart — {config.STARS_PRICE_STANDART} ⭐, PRO — {config.STARS_PRICE_PRO} ⭐ за 30 дней, без автосписания.\n"
                "Нажмите кнопку ниже для проверки реального ответа бота.\n"
                "Проверка покупки проводится с обычного аккаунта: администраторам оплата не нужна.",
                parse_mode="HTML", request_timeout=10,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🧪 Проверить ответ бота", callback_data="admin_probe")],
                    [InlineKeyboardButton(text="👑 Админка / подписки", callback_data="admin_refresh")],
                    [InlineKeyboardButton(text="💳 Тарифы", callback_data="open_subscription_menu")],
                ]),
            )
            print("Telegram admin diagnostic delivery:", json.dumps({"api_acknowledged": True, "message_id": message.message_id,
                                                                      "user_click_not_yet_verified": True}))
    except Exception as exc:
        # Exception strings can contain URLs/tokens. Only the safe class name is printed.
        print("Telegram diagnostic error:", type(exc).__name__)
        raise SystemExit(1)
    finally:
        await bot.session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notify-admin", action="store_true")
    args = parser.parse_args()
    import config
    print("Safe configuration:", json.dumps({"stars_enabled": config.STARS_PAYMENTS_ENABLED,
                                             "standart": config.STARS_PRICE_STANDART, "pro": config.STARS_PRICE_PRO,
                                             "check_interval": config.CHECK_INTERVAL, "workers": config.MAX_CONCURRENT_WORKERS,
                                             "admin_radar_limit": config.MAX_SEARCHES_PER_USER,
                                             "proxy_configured": bool(config.PROXY_URL)}))
    print_database_summary(config.DB_PATH)
    print("Avito / delivery metrics:", json.dumps(scan_summary(tail(Path(os.getenv("COMBINED_LOG", str(ROOT / "bot.log")))))))
    if sys.platform == "linux":
        result = subprocess.run(["journalctl", "-u", "avito-bot", "--since", "-20 minutes", "--no-pager", "-o", "cat"],
                                capture_output=True, text=True, timeout=15)
        print("Journal summary (20 minutes):", json.dumps({"lines": len(result.stdout.splitlines()),
                                                          "tracebacks": result.stdout.count("Traceback"),
                                                          "failed_entries": result.stdout.lower().count("failed"),
                                                          "error_entries": result.stdout.count("[ERROR]")}))
    asyncio.run(telegram_probe(config, args.notify_admin))


if __name__ == "__main__":
    main()
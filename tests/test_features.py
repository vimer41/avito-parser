import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import _environment
from tests._dispatcher import DispatcherFixture

import config
import database
import reports
from runtime_metrics import RuntimeMetricsRegistry as RuntimeMetrics


def _utc(seconds_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).strftime("%Y-%m-%d %H:%M:%S")


class TestReminderRules(unittest.TestCase):
    def user(self, left, *, trial=False, user_id=555):
        return {"user_id": user_id, "subscription_until": _utc(left), "trial_used": 1, "is_allowed": 0 if trial else 1,
                "tariff_plan": "pro"}

    def test_paid_subscription_stages(self):
        now = time.time()
        self.assertIsNone(reports.reminder_kind(self.user(5 * 86400), now))
        self.assertEqual(reports.reminder_kind(self.user(2.5 * 86400), now), "3d")
        self.assertEqual(reports.reminder_kind(self.user(3600 * 20), now), "1d")
        self.assertEqual(reports.reminder_kind(self.user(-600), now), "end")
        self.assertIsNone(reports.reminder_kind(self.user(-2 * 86400), now))

    def test_trial_gets_last_hour_and_end_only(self):
        now = time.time()
        self.assertIsNone(reports.reminder_kind(self.user(3 * 3600, trial=True), now))
        self.assertEqual(reports.reminder_kind(self.user(1800, trial=True), now), "1h")
        self.assertIn("Пробный период закончился", reports.reminder_text(self.user(-60, trial=True), "end"))

    def test_admins_get_no_reminders(self):
        admin = next(iter(config.ADMIN_IDS))
        self.assertIsNone(reports.reminder_kind(self.user(3600, user_id=admin), time.time()))

    def test_report_time_is_next_morning_msk(self):
        # 2026-10-29 05:00 UTC = 08:00 MSK -> one hour to 09:00 MSK
        now = datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(reports.seconds_until_report(now), 3600)
        self.assertEqual(reports.seconds_until_report(now + 7200), 86400 - 3600)


class TestDailySummary(unittest.TestCase):
    def test_daily_summary_counts_scans_and_real_latency(self):
        metrics = RuntimeMetrics()
        metrics.record_scan_attempt(success=True)
        metrics.record_scan_attempt(success=False)
        now = time.time()
        metrics.record_delivery("pro", t_pub=now - 40, t_pub_reliable=True, t_seen=now - 30, t_queued=now - 30,
                                deliver_at=now - 30, t_sent=now)
        peek = metrics.pop_daily_summary(reset=False)
        self.assertEqual((peek["scans_ok"], peek["scans_failed"], peek["sent"]), (1, 1, 1))
        self.assertEqual(peek["pub_to_sent"]["p50"], 45.0)
        metrics.pop_daily_summary()
        self.assertEqual(metrics.pop_daily_summary()["sent"], 0)


class TestAdminRadars(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-features-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        self.user_id = next(iter(config.ADMIN_IDS))
        await database.get_or_create_user(self.user_id)
        self.setup_dispatcher()

    async def asyncTearDown(self):
        await self.close_dispatcher()
        await database.close_db()
        self.db_patch.stop()
        self.directory.cleanup()

    async def add(self, n):
        return await database.add_search(self.user_id, f"Радар {n}", f"https://www.avito.ru/moskva?q=r{n}&s=104",
                                         initial_items=[("1", 100)])

    async def test_admin_bulk_delete_removes_only_selected(self):
        ids = [await self.add(n) for n in range(3)]
        await self.dispatch_callback("bulk_del:new", user_id=self.user_id)
        await self.dispatch_callback(f"bulk_tog:{ids[0]}:0", user_id=self.user_id)
        await self.dispatch_callback(f"bulk_tog:{ids[2]}:0", user_id=self.user_id)
        self.assertIn("bulk_go", self.callback_values())
        await self.dispatch_callback("bulk_go", user_id=self.user_id)
        await self.dispatch_callback("bulk_yes", user_id=self.user_id)
        self.assertEqual([s["id"] for s in await database.get_user_searches(self.user_id)], [ids[1]])

    async def test_radar_list_is_paged(self):
        for n in range(23):
            await self.add(n)
        ids = [s["id"] for s in await database.get_user_searches(self.user_id)]  # in list order
        await self.dispatch_callback("list_searches", user_id=self.user_id)
        values = self.callback_values()
        self.assertIn(f"open_search:{ids[9]}", values)
        self.assertNotIn(f"open_search:{ids[10]}", values)
        self.assertIn("list_searches:1", values)
        await self.dispatch_callback("list_searches:2", user_id=self.user_id)
        self.assertIn(f"open_search:{ids[22]}", self.callback_values())
        self.assertNotIn(f"open_search:{ids[0]}", self.callback_values())

    async def test_bulk_delete_marks_a_whole_page(self):
        for n in range(15):
            await self.add(n)
        ids = [s["id"] for s in await database.get_user_searches(self.user_id)]  # in list order
        await self.dispatch_callback("bulk_del:new", user_id=self.user_id)
        await self.dispatch_callback("bulk_page:1", user_id=self.user_id)
        await self.dispatch_callback("bulk_go", user_id=self.user_id)
        await self.dispatch_callback("bulk_yes", user_id=self.user_id)
        self.assertEqual([s["id"] for s in await database.get_user_searches(self.user_id)], ids[:10])

    async def test_bulk_delete_is_admin_only(self):
        user_id = 777001
        await database.get_or_create_user(user_id)
        await database.set_user_allowed(user_id, 1)
        await self.dispatch_callback("bulk_yes", user_id=user_id)
        await self.dispatch_callback("bulk_del:new", user_id=user_id)
        self.assertNotIn("bulk_go", self.callback_values() if self.methods("EditMessageText") else [])

    async def test_reminder_is_sent_once_per_stage(self):
        user_id = 777002
        await database.get_or_create_user(user_id)
        await database.grant_subscription(user_id, days=2, tariff_plan="pro")
        bot = AsyncMock()
        self.assertEqual(await reports.send_due_reminders(bot), 1)
        self.assertEqual(await reports.send_due_reminders(bot), 0)
        self.assertIn("через 3 дня", bot.send_message.await_args.args[1])

    async def test_daily_report_renders(self):
        text = await reports.build_daily_report(reset=False)
        self.assertIn("Отчёт за сутки", text)
        self.assertIn("Удачных сканов", text)


if __name__ == "__main__":
    unittest.main()

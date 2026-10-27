import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests import _environment
import aiosqlite
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
import database
import scheduler
from parser import AvitoItem


class TestReliability(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-reliability-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        self.user_id = 123456
        await database.get_or_create_user(self.user_id, "fixture", "Fixture")
        self.radar_id = await database.add_search(self.user_id, "Test", "https://www.avito.ru/search")
        self.item = AvitoItem("101", "Test item", "10 000 ₽", "https://www.avito.ru/test_101", raw_price=10000, date="только что", timestamp=int(time.time()))
        self.bot = Mock()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    async def execute(self, statement, parameters=()):
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(statement, parameters)
            await db.commit()

    async def outbox_count(self):
        async with aiosqlite.connect(database.DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM notification_outbox") as cursor:
                return (await cursor.fetchone())[0]

    async def enqueue(self, *, is_price_drop=False, old_price=None):
        return await scheduler.enqueue_notification_task(scheduler.NotificationTask(
            self.user_id, self.radar_id, "Test", self.item,
            is_price_drop=is_price_drop, old_price=old_price, drop_percent=20 if is_price_drop else None,
        ))

    async def test_init_preserves_users_and_explicit_five_percent_discount(self):
        await self.execute("UPDATE users SET username = 'johnpeterson92' WHERE user_id = ?", (self.user_id,))
        await database.update_search_discount_percent(self.radar_id, self.user_id, 5)
        await database.init_db()
        await database.init_db()
        users = await database.get_all_users()
        self.assertEqual([u["user_id"] for u in users], [self.user_id])
        self.assertEqual((await database.get_search(self.radar_id))["min_discount_percent"], 5)

    async def test_paid_subscription_expires_even_with_legacy_allowed_flag(self):
        await database.grant_subscription(self.user_id, days=30, tariff_plan="pro")
        self.assertTrue(await database.is_user_allowed(self.user_id))
        await self.execute("UPDATE users SET subscription_until = datetime('now', '-1 second') WHERE user_id = ?", (self.user_id,))
        self.assertFalse(await database.is_user_allowed(self.user_id))
        self.assertEqual(await database.get_all_active_searches(), [])
        self.assertEqual((await database.get_system_stats())["allowed_users"], 0)
        with self.assertRaises(PermissionError):
            await database.add_search(self.user_id, "Forbidden", "https://www.avito.ru/forbidden")

    async def test_revoke_stops_trial_and_does_not_reactivate_it(self):
        self.assertTrue(await database.is_user_allowed(self.user_id))
        await database.set_user_allowed(self.user_id, 0)
        await database.get_or_create_user(self.user_id)
        self.assertFalse(await database.is_user_allowed(self.user_id))
        self.assertFalse((await database.activate_trial(self.user_id))["success"])
        await database.set_user_allowed(self.user_id, 1)
        self.assertTrue(await database.is_user_allowed(self.user_id))

    async def test_stop_words_require_ownership(self):
        self.assertFalse(await database.add_stop_word_to_search(self.radar_id, "foreign", self.user_id + 1))
        self.assertTrue(await database.add_stop_word_to_search(self.radar_id, "owned", self.user_id))
        self.assertFalse(await database.remove_stop_word_from_search(self.radar_id, "owned", self.user_id + 1))
        self.assertEqual((await database.get_search(self.radar_id))["stop_words"], ["owned"])

    async def test_concurrent_radar_creation_cannot_exceed_quota(self):
        await database.delete_search(self.radar_id, self.user_id)
        results = await asyncio.gather(*[
            database.add_search(self.user_id, f"Radar {i}", f"https://www.avito.ru/search?q={i}")
            for i in range(4)
        ], return_exceptions=True)
        self.assertEqual(sum(isinstance(result, int) for result in results), 1)
        self.assertEqual(sum(isinstance(result, database.RadarLimitError) for result in results), 3)
        self.assertEqual(await database.count_user_searches(self.user_id), 1)

    async def test_enqueue_is_persistent_deduplicated_and_not_seen(self):
        self.assertTrue(await self.enqueue())
        self.assertFalse(await self.enqueue())
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertEqual(await database.get_pending_new_item_ids(self.radar_id), {self.item.item_id})
        await database.init_db()
        self.assertEqual(await self.outbox_count(), 1)
        self.assertIsNotNone(await database.claim_notification())

    async def test_outbox_metrics_include_leased_and_queue_age(self):
        self.assertEqual(await database.get_outbox_metrics(), {"pending": 0, "leased": 0, "oldest_seconds": 0})
        await self.enqueue()
        await self.execute("UPDATE notification_outbox SET created_at = datetime('now', '-10 minutes')")
        await database.claim_notification()
        metrics = await database.get_outbox_metrics()
        self.assertEqual(metrics["pending"], 1)
        self.assertEqual(metrics["leased"], 1)
        self.assertGreaterEqual(metrics["oldest_seconds"], 600)

    async def test_claim_is_atomic_and_stale_lease_cannot_acknowledge(self):
        await self.enqueue()
        results = await asyncio.gather(database.claim_notification(), database.claim_notification())
        leases = [result for result in results if result is not None]
        self.assertEqual(len(leases), 1)
        stale = leases[0]
        current = await database.claim_notification(lease_seconds=0)
        self.assertNotEqual(stale["lease_token"], current["lease_token"])
        self.assertFalse(await database.acknowledge_notification(stale["id"], stale["lease_token"], 10000))
        self.assertTrue(await database.acknowledge_notification(current["id"], current["lease_token"], 10000))
        self.assertEqual(await self.outbox_count(), 0)

    async def test_delivery_failure_keeps_item_pending_and_unseen(self):
        await self.enqueue()
        row = await database.claim_notification()
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(return_value=False)):
            self.assertFalse(await scheduler.deliver_notification(self.bot, None, row))
        self.assertEqual(await self.outbox_count(), 1)
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertIsNone(await database.claim_notification())

    async def test_success_marks_seen_after_telegram_acknowledgment(self):
        await self.enqueue()
        row = await database.claim_notification()
        async def send(*args, **kwargs):
            self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
            return True
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(side_effect=send)):
            self.assertTrue(await scheduler.deliver_notification(self.bot, None, row))
        self.assertEqual(await self.outbox_count(), 0)
        self.assertEqual(await database.get_seen_items_with_prices(self.radar_id), {self.item.item_id: 10000})

    async def test_price_drop_does_not_update_price_before_delivery(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await database.update_search_discount_percent(self.radar_id, self.user_id, 5)
        await database.mark_item_seen(self.radar_id, self.item.item_id, 12500)
        self.assertTrue(await self.enqueue(is_price_drop=True, old_price=12500))
        row = await database.claim_notification()
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(return_value=False)):
            await scheduler.deliver_notification(self.bot, None, row)
        self.assertEqual(await database.get_seen_items_with_prices(self.radar_id), {self.item.item_id: 12500})

    async def test_only_one_price_drop_per_item_can_remain_pending(self):
        await database.mark_item_seen(self.radar_id, self.item.item_id, 12500)
        self.assertTrue(await self.enqueue(is_price_drop=True, old_price=12500))
        self.item.raw_price = 9000
        self.assertFalse(await self.enqueue(is_price_drop=True, old_price=12500))
        self.assertEqual(await self.outbox_count(), 1)

    async def test_price_drop_delivery_acknowledges_new_price(self):
        await database.grant_subscription(self.user_id, tariff_plan="pro")
        await database.update_search_discount_percent(self.radar_id, self.user_id, 5)
        await database.mark_item_seen(self.radar_id, self.item.item_id, 12500)
        await self.enqueue(is_price_drop=True, old_price=12500)
        row = await database.claim_notification()
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(return_value=True)):
            self.assertTrue(await scheduler.deliver_notification(self.bot, None, row))
        self.assertEqual(await database.get_seen_items_with_prices(self.radar_id), {self.item.item_id: 10000})

    async def test_purge_requires_exact_user_and_protects_admins(self):
        self.assertFalse((await database.purge_user_by_pattern("fix"))["success"])
        self.assertFalse((await database.purge_user_by_pattern("%"))["success"])
        await database.get_or_create_user(database.ADMIN_IDS[0])
        self.assertFalse((await database.purge_user_by_pattern(str(database.ADMIN_IDS[0])))["success"])
        self.assertTrue(await database.is_user_allowed(self.user_id))

    async def test_trial_activation_is_atomic(self):
        await self.execute("UPDATE users SET trial_used = 0, subscription_until = NULL WHERE user_id = ?", (self.user_id,))
        results = await asyncio.gather(*[database.activate_trial(self.user_id) for _ in range(4)])
        self.assertEqual(sum(result["success"] for result in results), 1)

    async def test_pipeline_priority_and_cancelled_waiters(self):
        urgent, pipeline = asyncio.Queue(), asyncio.Queue()
        await pipeline.put("normal")
        await urgent.put("urgent")
        self.assertEqual(await scheduler.get_next_pipeline_task(urgent, pipeline), ("urgent", True))
        self.assertEqual(await scheduler.get_next_pipeline_task(urgent, pipeline), ("normal", False))
        task = asyncio.create_task(scheduler.get_next_pipeline_task(urgent, pipeline))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await pipeline.put("not-stolen")
        await asyncio.sleep(0)
        self.assertEqual(pipeline.qsize(), 1)

    async def test_retry_after_preserves_notification(self):
        await self.enqueue()
        row = await database.claim_notification()
        error = TelegramRetryAfter(method=SendMessage(chat_id=self.user_id, text="test"), message="rate limited", retry_after=30)
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(side_effect=error)):
            self.assertFalse(await scheduler.deliver_notification(self.bot, None, row))
        self.assertEqual(await self.outbox_count(), 1)
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertIsNone(await database.claim_notification())

    async def test_cancelled_delivery_releases_lease(self):
        await self.enqueue()
        row = await database.claim_notification()
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler.deliver_notification(self.bot, None, row)
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertIsNotNone(await database.claim_notification())

    async def test_revoked_or_deleted_radar_cannot_receive_pending_notifications(self):
        await self.enqueue()
        row = await database.claim_notification()
        await database.set_user_allowed(self.user_id, 0)
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock()) as send:
            self.assertFalse(await scheduler.deliver_notification(self.bot, None, row))
            send.assert_not_called()
        self.assertEqual(await self.outbox_count(), 0)
        await database.set_user_allowed(self.user_id, 1)
        await self.enqueue()
        await database.delete_search(self.radar_id, self.user_id)
        self.assertEqual(await self.outbox_count(), 0)

    async def test_pipeline_enqueues_once_without_premarking_deliverable_items(self):
        radar = await database.get_search(self.radar_id)
        parser = Mock()
        parser.fetch_page_async = AsyncMock(return_value="<html>fixture</html>")
        parser.parse_items.return_value = [self.item]
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertEqual(await self.outbox_count(), 1)

    async def test_deleted_radar_cannot_send_after_rate_wait(self):
        self.item.image_url = "https://c80.img.avito.st/offline-fixture"
        self.bot.send_photo = AsyncMock()
        self.bot.send_message = AsyncMock()
        async def rate_wait():
            await database.delete_search(self.radar_id, self.user_id)
        with patch.object(scheduler, "get_cached_photo_file_id", return_value=None), patch.object(scheduler, "limit_notification_send_rate", new=rate_wait):
            self.assertFalse(await scheduler.send_ad_to_user(self.bot, None, self.user_id, self.radar_id, "Test", self.item))
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_uncached_photo_never_delays_text_notification(self):
        self.item.image_url = "https://c80.img.avito.st/test.jpg"
        self.bot.send_photo = AsyncMock()
        self.bot.send_message = AsyncMock()
        with patch.object(scheduler, "get_cached_photo_file_id", return_value=None), patch.object(scheduler, "download_image_bytes", new=AsyncMock()) as download, patch.object(scheduler, "limit_notification_send_rate", new=AsyncMock()):
            self.assertTrue(await scheduler.send_ad_to_user(self.bot, None, self.user_id, self.radar_id, "Test", self.item))
        download.assert_not_awaited()
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_awaited_once()

    async def test_verified_notification_survives_queue_age_and_changed_age_limit(self):
        await self.enqueue()
        await database.update_search_max_age(self.radar_id, self.user_id, 1)
        row = await database.claim_notification()
        with patch.object(scheduler.time, "time", return_value=time.time() + 600), patch.object(scheduler, "send_ad_to_user", new=AsyncMock(return_value=True)) as send:
            self.assertTrue(await scheduler.deliver_notification(self.bot, None, row))
        send.assert_awaited_once()
        self.assertTrue(await database.is_item_seen(self.radar_id, self.item.item_id))

    async def test_legacy_notification_is_validated_at_enqueue_time(self):
        await self.enqueue()
        row = await database.claim_notification()
        payload = json.loads(row["payload"])
        payload.pop("freshness_verified")
        payload.pop("discovered_at")
        row["payload"] = json.dumps(payload)
        with patch.object(scheduler.time, "time", return_value=time.time() + 600), patch.object(scheduler, "send_ad_to_user", new=AsyncMock(return_value=True)):
            self.assertTrue(await scheduler.deliver_notification(self.bot, None, row))

    async def test_unknown_age_is_not_marked_seen_and_can_be_delivered_later(self):
        radar = await database.get_search(self.radar_id)
        self.item.date, self.item.timestamp = "Дата не указана", None
        parser = Mock()
        parser.fetch_page_async = AsyncMock(return_value="<html></html>")
        parser.parse_items.return_value = [self.item]
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertEqual(await self.outbox_count(), 0)
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.item.date = "2 мин. назад"
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertEqual(await self.outbox_count(), 1)

    async def test_atomic_initial_snapshot_preserves_unknown_price(self):
        await database.delete_search(self.radar_id, self.user_id)
        radar_id = await database.add_search(self.user_id, "Atomic", "https://www.avito.ru/search", initial_items=[("old", None), ("free", 0)])
        radar = await database.get_search(radar_id)
        self.assertIsNotNone(radar["initialized_at"])
        async with aiosqlite.connect(database.DB_PATH) as db:
            async with db.execute("SELECT item_id, last_price FROM seen_items WHERE search_id = ?", (radar_id,)) as cursor:
                self.assertEqual(dict(await cursor.fetchall()), {"old": None, "free": 0})

    async def test_empty_deferred_baseline_initializes_only_once(self):
        await self.execute("UPDATE searches SET initialized_at = NULL WHERE id = ?", (self.radar_id,))
        radar = await database.get_search(self.radar_id)
        parser = Mock()
        parser.fetch_page_async = AsyncMock(return_value="<html></html>")
        parser.parse_items.return_value = []
        parser.has_catalog.return_value = True
        parser.next_page_url.return_value = None
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        radar = await database.get_search(self.radar_id)
        self.assertIsNotNone(radar["initialized_at"])
        parser.parse_items.return_value = [self.item]
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertEqual(await self.outbox_count(), 1)

    async def test_pending_baseline_survives_repeated_migration(self):
        await self.execute("UPDATE searches SET initialized_at = NULL WHERE id = ?", (self.radar_id,))
        await database.init_db()
        self.assertIsNone((await database.get_search(self.radar_id))["initialized_at"])

    async def test_legacy_unknown_age_returns_to_discovery_without_seen_state(self):
        self.item.timestamp = None
        self.item.date = "Дата не указана"
        await self.enqueue()
        row = await database.claim_notification()
        with patch.object(scheduler, "send_ad_to_user", new=AsyncMock()) as send:
            self.assertFalse(await scheduler.deliver_notification(self.bot, None, row))
        send.assert_not_called()
        self.assertEqual(await self.outbox_count(), 0)
        self.assertFalse(await database.is_item_seen(self.radar_id, self.item.item_id))
        self.item.date = "только что"
        radar = await database.get_search(self.radar_id)
        parser = Mock()
        parser.fetch_page_async = AsyncMock(return_value="<html></html>")
        parser.parse_items.return_value = [self.item]
        self.assertTrue(await scheduler.process_url_group(self.bot, parser, radar["url"], [radar]))
        self.assertEqual(await self.outbox_count(), 1)

    async def test_send_message_floodwait_reaches_persistent_outbox(self):
        self.bot.send_message = AsyncMock(side_effect=TelegramRetryAfter(method=SendMessage(chat_id=self.user_id, text="test"), message="rate limited", retry_after=10))
        with patch.object(scheduler, "get_cached_photo_file_id", return_value=None), patch.object(scheduler, "limit_notification_send_rate", new=AsyncMock()):
            with self.assertRaises(TelegramRetryAfter):
                await scheduler.send_ad_to_user(self.bot, None, self.user_id, self.radar_id, "Test", self.item)
        self.bot.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
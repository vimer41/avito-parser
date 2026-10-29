"""Broad radars: flood pages are read further, bursts go out as one list message, one chat never blocks others."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests import _environment
import config
import database
import scheduler
from collector import collect_source
from parser import AvitoItem, AvitoParser, classify_proxy_error
from seen_cache import InMemorySeenStore


def catalog_page(ids, page):
    cards = [{"id": str(i), "title": f"Lot {i}", "urlPath": f"/lot_{i}", "sortTimeStamp": int(time.time() * 1000)} for i in ids]
    nxt = f'<a data-marker="pagination-button/nextPage" href="?q=x&amp;s=104&amp;p={page + 1}">next</a>'
    return "<html>" + nxt + "<script>" + json.dumps({"loaderData": {"data": {"catalog": {"items": cards}}}}) + "</script></html>"


def fake_parser(pages):
    parser = Mock(spec=AvitoParser)
    parser.fetch_page_async = AsyncMock(side_effect=pages)
    parser.parse_items = AvitoParser.parse_items
    parser.has_catalog = AvitoParser.has_catalog
    parser.next_page_url = AvitoParser.next_page_url
    parser.extract_search_title = lambda *_: "Fixture"
    return parser


class TestFloodPages(unittest.IsolatedAsyncioTestCase):
    URL = "https://www.avito.ru/moskva?q=x&s=104"

    async def test_reads_next_page_only_while_the_page_is_all_new(self):
        store = InMemorySeenStore(ttl_seconds=600, max_per_source=1000)
        await store.remember("src", "rev", ["old-1", "old-2"])

        async def all_new(items):
            return await store.none_known("src", "rev", [i.item_id for i in items])

        parser = fake_parser([catalog_page(range(1, 51), 1), catalog_page([*range(51, 60), "old-1"], 2), catalog_page(range(90, 99), 3)])
        collection = await collect_source(parser, self.URL, 30, max_pages=3, read_next_page=all_new)
        self.assertEqual(collection.pages, 2, "page 2 reaches a known lot, so page 3 is not fetched")
        self.assertEqual(len(collection.items), 60)
        self.assertFalse(collection.coverage_gap)

    async def test_first_scan_without_history_reads_one_page(self):
        store = InMemorySeenStore(ttl_seconds=600, max_per_source=1000)

        async def all_new(items):
            return await store.none_known("src", "rev", [i.item_id for i in items])

        parser = fake_parser([catalog_page(range(1, 51), 1), catalog_page(range(51, 60), 2)])
        collection = await collect_source(parser, self.URL, 30, max_pages=3, read_next_page=all_new)
        self.assertEqual(collection.pages, 1)
        self.assertEqual(parser.fetch_page_async.await_count, 1)


class TestDigestAndFairness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "digest.db")
        self.db_patch.start()
        await database.init_db()
        self.users = {}
        for uid in (601, 602):
            await database.get_or_create_user(uid, f"u{uid}", "U")
            await database.grant_subscription(uid, 30, "pro")
            self.users[uid] = await database.add_search(uid, "Одежда", f"https://www.avito.ru/spb/odezhda?u={uid}")
        scheduler.reset_notification_rate_limiter()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def enqueue(self, uid, count, start=0):
        now = time.time()
        events = []
        for index in range(start, start + count):
            item = AvitoItem(f"{uid}-{index}", f"Куртка {index}", f"{1000 + index} ₽", f"https://www.avito.ru/lot_{uid}_{index}",
                             timestamp=int(now), t_pub_reliable=True, raw_price=1000 + index)
            events.append({
                "user_id": uid, "radar_id": self.users[uid], "radar_title": "Одежда", "item_id": item.item_id,
                "raw_price": item.raw_price, "tariff": "pro", "priority": 10, "delay_seconds": 0.0, "t_seen": now,
                "t_pub": item.timestamp, "t_pub_reliable": True, "is_catchup": False,
                "payload_dict": {"user_id": uid, "radar_id": self.users[uid], "search_title": "Одежда",
                                 "item": item.__dict__, "freshness_verified": True},
            })
        await database.enqueue_group_events(events, [])

    async def test_claim_skips_busy_chats_and_can_target_one_chat(self):
        await self.enqueue(601, 3)
        await self.enqueue(602, 1)
        row = await database.claim_notification_event(exclude_user_ids=[601])
        self.assertEqual(row["user_id"], 602)
        same_chat = await database.claim_notification_event(user_id=601)
        self.assertEqual(same_chat["user_id"], 601)

    async def test_burst_goes_out_as_one_list_message_and_all_events_are_acknowledged(self):
        await self.enqueue(601, 7)
        rows = [await database.claim_notification_event(user_id=601) for _ in range(7)]
        bot = Mock(send_message=AsyncMock(), send_photo=AsyncMock())
        with patch.object(scheduler, "_await_rate_limit", new=AsyncMock()):
            delivered = await scheduler.deliver_digest(bot, None, rows)
        self.assertEqual(delivered, 7)
        bot.send_message.assert_awaited_once()
        bot.send_photo.assert_not_awaited()
        text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("7 новых лотов", text)
        self.assertIn("Куртка 6", text)
        self.assertIsNone(await database.claim_notification_event(user_id=601), "every event of the burst is resolved")

    async def test_small_group_is_delivered_one_by_one(self):
        await self.enqueue(601, 2)
        rows = [await database.claim_notification_event(user_id=601) for _ in range(2)]
        bot = Mock(send_message=AsyncMock(), send_photo=AsyncMock())
        with patch.object(scheduler, "_await_rate_limit", new=AsyncMock()):
            delivered = await scheduler.deliver_digest(bot, None, rows)
        self.assertEqual(delivered, 2)
        self.assertEqual(bot.send_message.await_count + bot.send_photo.await_count, 2)

    async def test_digest_still_applies_seller_blacklist(self):
        await self.enqueue(601, 5)
        rows = [await database.claim_notification_event(user_id=601) for _ in range(5)]
        payload = json.loads(rows[0]["payload_json"])
        payload["item"]["seller_name"] = "spammer"
        rows[0]["payload_json"] = json.dumps(payload)
        bot = Mock(send_message=AsyncMock(), send_photo=AsyncMock())
        with patch.object(database, "get_blocked_sellers", new=AsyncMock(return_value={"spammer"})), \
                patch.object(scheduler, "_await_rate_limit", new=AsyncMock()):
            delivered = await scheduler.deliver_digest(bot, None, rows)
        self.assertEqual(delivered, 4)
        self.assertIn("4 новых лота", bot.send_message.await_args.kwargs["text"])


class TestProxyErrorClassification(unittest.TestCase):
    def test_libcurl_messages_map_to_readable_reasons(self):
        cases = {
            "Failed to perform, curl: (56) CONNECT tunnel failed, response 407.": ("proxy_auth", 407),
            "Failed to perform, curl: (56) CONNECT tunnel failed, response 403.": ("proxy_forbidden", 403),
            "curl: (56) Received HTTP code 502 from proxy after CONNECT": ("proxy_error", 502),
            "curl: (97) SOCKS5 authentication failed": ("proxy_auth", None),
            "curl: (5) Could not resolve proxy: bad.host": ("proxy_dns", None),
            "curl: (7) Failed to connect to 1.2.3.4 port 3000: Connection refused": ("proxy_refused", None),
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_proxy_error(Exception(message)), expected)
        self.assertIsNone(classify_proxy_error(TimeoutError("Operation timed out after 8000 milliseconds")))


if __name__ == "__main__":
    unittest.main()

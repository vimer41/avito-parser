"""Hot ingestion path: in-memory dedup, token-bucket pacing and batched radar eligibility."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment
import config
import database
from collector import SourceCollection, settled_item_ids
from parser import AvitoItem
from proxy_pool import ProxyClient
from seen_cache import InMemorySeenStore


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestInMemorySeenStore(unittest.IsolatedAsyncioTestCase):
    async def test_known_only_after_remember_with_same_revision(self):
        store = InMemorySeenStore(ttl_seconds=60, max_per_source=100)
        self.assertFalse(await store.all_known("src", "r1", ["1", "2"]))
        await store.remember("src", "r1", ["1", "2", "3"])
        self.assertTrue(await store.all_known("src", "r1", ["1", "2"]))
        self.assertFalse(await store.all_known("src", "r1", ["1", "4"]))
        # New radar joined the group -> its fanout must run even for old IDs.
        self.assertFalse(await store.all_known("src", "r2", ["1", "2"]))
        self.assertFalse(await store.all_known("other", "r1", ["1"]))

    async def test_empty_page_is_never_known(self):
        store = InMemorySeenStore(ttl_seconds=60, max_per_source=100)
        await store.remember("src", "r1", ["1"])
        self.assertFalse(await store.all_known("src", "r1", []))

    async def test_revision_change_resets_source(self):
        store = InMemorySeenStore(ttl_seconds=60, max_per_source=100)
        await store.remember("src", "r1", ["1"])
        await store.remember("src", "r2", ["2"])
        self.assertTrue(await store.all_known("src", "r2", ["2"]))
        self.assertFalse(await store.all_known("src", "r2", ["1"]))

    async def test_ttl_and_lru_bounds(self):
        clock = FakeClock()
        store = InMemorySeenStore(ttl_seconds=10, max_per_source=3, monotonic_fn=clock)
        await store.remember("src", "r", ["1", "2", "3"])
        await store.remember("src", "r", ["4"])
        self.assertEqual(len(store), 3)
        self.assertFalse(await store.all_known("src", "r", ["1"]))
        self.assertTrue(await store.all_known("src", "r", ["2", "3", "4"]))
        clock.now += 11
        self.assertFalse(await store.all_known("src", "r", ["4"]))

    async def test_prune_and_forget(self):
        store = InMemorySeenStore(ttl_seconds=60, max_per_source=10)
        await store.remember("a", "r", ["1"])
        await store.remember("b", "r", ["2"])
        store.prune({"a": []})
        self.assertFalse(await store.all_known("b", "r", ["2"]))
        await store.forget("a")
        self.assertEqual(len(store), 0)


class TestFanoutSkipDecision(unittest.IsolatedAsyncioTestCase):
    def make(self, ids, *, fast_skipped=False):
        now = time.time()
        items = tuple(AvitoItem(item_id=i, title="t", price="1", url="u", timestamp=int(now - 10)) for i in ids)
        return SourceCollection(title="x", items=items, captured_at=now, pages=1, coverage_gap=False,
                                elapsed_ms=1, fast_skipped=fast_skipped)

    async def test_reshuffled_known_page_skips_but_any_new_id_or_guard_does_not(self):
        import monitoring
        from scan_planner import ScheduledScan

        store = InMemorySeenStore(ttl_seconds=60, max_per_source=100)
        task = ScheduledScan(key="src", radars=(), scheduled_at=0, started_at=0, retry_attempt=0,
                             generation=1, can_fast_skip=True, group_revision="rev")
        first = self.make(["1", "2", "3", "4"])
        self.assertFalse(await monitoring.nothing_new_for_fanout(store, task, first, False))
        await store.remember(task.key, task.group_revision, settled_item_ids(first))

        reshuffled = self.make(["4", "3", "1", "2"])
        self.assertTrue(await monitoring.nothing_new_for_fanout(store, task, reshuffled, False))
        self.assertFalse(await monitoring.nothing_new_for_fanout(store, task, self.make(["5", "1"]), False))
        # Planner guards win: catch-up, a required full scan, or an already fast-skipped page.
        self.assertFalse(await monitoring.nothing_new_for_fanout(store, task, reshuffled, True))
        full_scan_due = ScheduledScan(**{**task.__dict__, "can_fast_skip": False})
        self.assertFalse(await monitoring.nothing_new_for_fanout(store, full_scan_due, reshuffled, False))
        self.assertFalse(await monitoring.nothing_new_for_fanout(store, task, self.make(["1"], fast_skipped=True), False))


class TestSettledItems(unittest.TestCase):
    def test_items_without_reliable_age_stay_for_fanout_recheck(self):
        now = time.time()
        items = (
            AvitoItem(item_id="dated", title="t", price="1", url="u", timestamp=int(now - 30)),
            AvitoItem(item_id="relative", title="t", price="1", url="u", date="5 минут назад"),
            AvitoItem(item_id="unknown", title="t", price="1", url="u", date="Дата не указана"),
        )
        collection = SourceCollection(title="x", items=items, captured_at=now, pages=1, coverage_gap=False, elapsed_ms=1)
        self.assertEqual(settled_item_ids(collection), ["dated", "relative"])


class TestTokenBucketPacing(unittest.TestCase):
    def make_client(self, clock, rate=2.0):
        with patch.object(config, "PROXY_RATE_BURST", 3.0):
            client = ProxyClient("http://user:pass@127.0.0.1:8080", configured_rate=rate, monotonic_fn=clock)
        return client

    def test_burst_is_free_then_sustained_rate_is_enforced(self):
        clock = FakeClock()
        client = self.make_client(clock)
        with patch.object(config, "PROXY_RATE_BURST", 3.0):
            self.assertEqual([client._reserve_pacing_delay() for _ in range(3)], [0.0, 0.0, 0.0])
            # Bucket empty: next requests are spaced at 1/rate = 0.5 s.
            self.assertAlmostEqual(client._reserve_pacing_delay(), 0.5)
            self.assertAlmostEqual(client._reserve_pacing_delay(), 1.0)
            self.assertGreater(client.estimated_ready_at(clock.now), clock.now)

    def test_idle_client_refills_up_to_burst(self):
        clock = FakeClock()
        client = self.make_client(clock)
        with patch.object(config, "PROXY_RATE_BURST", 3.0):
            for _ in range(3):
                client._reserve_pacing_delay()
            clock.now += 60  # long idle: refill is capped at the burst size
            self.assertEqual([client._reserve_pacing_delay() for _ in range(3)], [0.0, 0.0, 0.0])
            self.assertAlmostEqual(client._reserve_pacing_delay(), 0.5)

    def test_rate_halving_after_block_slows_pacing(self):
        clock = FakeClock()
        client = self.make_client(clock, rate=2.0)
        with patch.object(config, "PROXY_RATE_BURST", 1.0):
            client._tokens = 1.0
            client.effective_rate = 1.0
            self.assertEqual(client._reserve_pacing_delay(), 0.0)
            self.assertAlmostEqual(client._reserve_pacing_delay(), 1.0)


class TestBatchedRadarEligibility(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "hot.db")
        self.db_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def test_matches_per_radar_checks(self):
        allowed, blocked, admin = 501, 502, config.ADMIN_ID
        for uid in (allowed, blocked, admin):
            await database.get_or_create_user(uid, f"u{uid}", "U")
        await database.set_user_allowed(allowed, 1)
        await database.set_user_allowed(blocked, 1)
        ids = {
            "allowed": await database.add_search(allowed, "a", "https://www.avito.ru/moskva?q=a"),
            "blocked": await database.add_search(blocked, "b", "https://www.avito.ru/moskva?q=b"),
            "admin": await database.add_search(admin, "c", "https://www.avito.ru/moskva?q=c"),
        }
        paused = await database.add_search(allowed, "p", "https://www.avito.ru/moskva?q=p")
        await database.toggle_search_active(paused, allowed)
        await database.set_user_allowed(blocked, 0)

        batched = await database.get_deliverable_search_ids([*ids.values(), paused, 999999])
        expected = set()
        for sid, uid in ((ids["allowed"], allowed), (ids["blocked"], blocked), (ids["admin"], admin), (paused, allowed)):
            if await database.is_search_active(sid) and await database.is_user_allowed(uid):
                expected.add(sid)
        self.assertEqual(batched, expected)
        self.assertEqual(batched, {ids["allowed"], ids["admin"]})
        self.assertEqual(await database.get_deliverable_search_ids([]), set())


if __name__ == "__main__":
    unittest.main()

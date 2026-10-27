import asyncio
import datetime
import json
import time
import unittest
from threading import Event
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from tests import _environment
from freshness import freshness_status, publication_age_seconds
from parser import AvitoParser
from curl_cffi import requests as curl_requests
from request_budget import NetworkBlockState, RequestDeferred, RequestBudgetExceeded, parse_retry_after
from scan_planner import DeadlineScheduler, calculate_interval_plan
from tools.simulate_scheduler import simulate
from collector import collect_source
from source_cache import SourceSnapshotCache, SourceSnapshot
from tools.diagnose import scan_summary
from tests.test_anti_bot import FakeResponse
import config
import monitoring
import parser as parser_module
import scheduler


class TestDeadlineScheduling(unittest.TestCase):
    def make_planner(self, count=3):
        planner = DeadlineScheduler(retry_base_delay=5, max_retries=2)
        groups = {str(index): [{"id": index}] for index in range(count)}
        planner.sync(groups, {key: 15 for key in groups}, 0)
        return planner

    def test_no_duplicate_reservations_and_start_anchored_interval(self):
        planner = self.make_planner(1)
        task = planner.take_due(0)
        self.assertIsNone(planner.take_due(100))
        planner.complete(task, 2, True)
        self.assertIsNone(planner.take_due(14.99))
        self.assertIsNotNone(planner.take_due(15))

    def test_slow_scans_do_not_replay_missed_ticks(self):
        planner = self.make_planner(1)
        task = planner.take_due(0)
        planner.complete(task, 40, True)
        next_task = planner.take_due(40)
        self.assertIsNotNone(next_task)
        planner.complete(next_task, 41, True)
        self.assertIsNone(planner.take_due(41))

    def test_retries_are_delayed_bounded_and_fair(self):
        planner = self.make_planner()
        first, second = planner.take_due(0), planner.take_due(0)
        planner.complete(first, 1, False)
        planner.complete(second, 1, False)
        regular = planner.take_due(6)
        self.assertEqual(regular.retry_attempt, 0)
        retry = planner.take_due(6)
        self.assertEqual(retry.retry_attempt, 1)
        planner.complete(retry, 7, False, retry_after=30)
        self.assertGreaterEqual(planner.sources[retry.key].due_at, 37)

    def test_retry_burst_cannot_starve_later_regular_deadline(self):
        planner = self.make_planner(3)
        tasks = [planner.take_due(0) for _ in range(3)]
        for task in tasks[:2]:
            planner.complete(task, 0, False)
        planner.complete(tasks[2], 0, True)
        self.assertEqual(planner.take_due(15).retry_attempt, 1)
        self.assertEqual(planner.take_due(15).retry_attempt, 0)

    def test_retry_exhaustion_and_nonretryable_failures(self):
        planner = self.make_planner(1)
        for now in (0, 5, 15):
            task = planner.take_due(now)
            planner.complete(task, now, False)
        self.assertEqual(planner.sources["0"].retry_attempt, 0)
        self.assertEqual(planner.sources["0"].due_at, 30)
        task = planner.take_due(30)
        planner.complete(task, 30, False, retryable=False)
        self.assertEqual(planner.sources["0"].due_at, 45)

    def test_new_sources_immediate_existing_sources_staggered(self):
        planner = DeadlineScheduler()
        groups = {str(index): [] for index in range(10)}
        planner.sync(groups, {key: 25 for key in groups}, 100, startup=True)
        self.assertEqual(planner.sources["0"].due_at, 100)
        self.assertGreater(planner.sources["9"].due_at, 110)
        groups["new"] = []
        planner.sync(groups, {key: 25 for key in groups}, 101)
        self.assertEqual(planner.sources["new"].due_at, 101)

    def test_removed_generation_cannot_overwrite_readded_source(self):
        planner = self.make_planner(1)
        task = planner.take_due(0)
        planner.sync({}, {}, 1)
        planner.sync({"0": [{"id": 99}]}, {"0": 25}, 2)
        self.assertIsNone(planner.take_due(2))
        planner.complete(task, 3, False)
        new_task = planner.take_due(3)
        self.assertEqual(new_task.radars[0]["id"], 99)
        self.assertEqual(new_task.retry_attempt, 0)

    def test_interval_budget_reports_unachievable_maximum(self):
        plan = calculate_interval_plan({str(i): 25 for i in range(350)}, 0.5)
        self.assertTrue(plan.overloaded)
        self.assertTrue(all(interval <= 120 for interval in plan.intervals.values()))
        small = calculate_interval_plan({"pro": 15, "regular": 25}, 0.5)
        self.assertFalse(small.overloaded)
        self.assertEqual(small.intervals, {"pro": 15, "regular": 25})

    def test_model_350_subscriptions_uses_unique_source_count(self):
        shared = simulate(sources=10, seconds=1800)
        distinct = simulate(sources=350, seconds=1800)
        self.assertEqual(shared["sources_visited"], 10)
        self.assertEqual(distinct["sources_visited"], 350)
        self.assertGreater(distinct["revisit_p95_seconds"], shared["revisit_p95_seconds"])
        self.assertLessEqual(distinct["requests_started"], 901)


class TestPublicationAges(unittest.TestCase):
    def test_abbreviated_relative_dates(self):
        for text, seconds in (("12 сек. назад", 12), ("2 мин. назад", 120), ("7 мин назад", 420),
                              ("1 ч. назад", 3600), ("3 дня назад", 259200), ("минуту назад", 60)):
            with self.subTest(text=text):
                self.assertEqual(publication_age_seconds(text), seconds)
        self.assertTrue(freshness_status("2 мин. назад", max_age_minutes=5))
        self.assertFalse(freshness_status("7 мин. назад", max_age_minutes=5))

    def test_unknown_invalid_and_future_dates_are_not_old(self):
        for text in (None, "Дата не указана", "минуты", "сегодня", "сегодня в 29:90"):
            self.assertIsNone(freshness_status(text))
        self.assertIsNone(freshness_status(None, time.time() + 3600))
        self.assertIsNone(freshness_status(None, float("nan")))

    def test_timestamp_seconds_milliseconds_and_moscow_clock(self):
        now = datetime.datetime(2026, 10, 27, 12, 10, tzinfo=datetime.timezone(datetime.timedelta(hours=3))).timestamp()
        self.assertTrue(freshness_status(None, (now - 120) * 1000, now=now))
        self.assertFalse(freshness_status(None, now - 600, now=now))
        self.assertEqual(publication_age_seconds("сегодня в 12:08", now=now), 120)
        self.assertIsNone(publication_age_seconds("сегодня в 22:00", now=now))

    def test_nonpositive_age_limit_preserves_default_and_metadata_types(self):
        self.assertTrue(freshness_status("2 мин. назад", max_age_minutes=-1))
        self.assertIsNone(freshness_status({"unexpected": "date"}))


class TestSharedRequestBudget(unittest.IsolatedAsyncioTestCase):
    async def test_spacing_concurrency_and_fifo(self):
        state = NetworkBlockState(requests_per_second=50, max_in_flight=1)
        started = []
        running = peak = 0
        async def perform(index):
            nonlocal running, peak
            async with state.request_slot_async() as granted_at:
                started.append((index, granted_at))
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.025)
                running -= 1
        await asyncio.gather(*(perform(index) for index in range(5)))
        self.assertEqual(peak, 1)
        self.assertEqual([index for index, _ in started], list(range(5)))
        self.assertTrue(all(started[i + 1][1] - started[i][1] >= 0.019 for i in range(4)))
        self.assertEqual(state.snapshot()["requests"], 5)

    async def test_cooldown_cancellation_and_deadline_do_not_leak_slots(self):
        state = NetworkBlockState(requests_per_second=100, max_in_flight=1)
        state.acquire()
        async def waiting():
            async with state.request_slot_async(timeout=0.05):
                self.fail("occupied slot was acquired")
        with self.assertRaises(RequestBudgetExceeded):
            await waiting()
        task = asyncio.create_task(waiting())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state.release()
        state.register_block(120)
        with self.assertRaises(RequestDeferred):
            state.acquire()
        self.assertEqual(state.snapshot()["waiting"], 0)
        self.assertEqual(state.snapshot()["in_flight"], 0)

    def test_retry_after_is_case_insensitive_and_finite(self):
        self.assertEqual(parse_retry_after({"rEtRy-AfTeR": "120"}), 120)
        self.assertEqual(parse_retry_after({"Retry-After": "nan"}), 0)
        self.assertEqual(parse_retry_after({"Retry-After": "inf"}), 0)

    def test_success_cannot_clear_other_worker_cooldown(self):
        state = NetworkBlockState()
        state.register_block(120)
        before = state.blocked_until
        for _ in range(20):
            state.register_success()
        self.assertEqual(state.blocked_until, before)
        self.assertGreater(state.cooldown_remaining(), 119)

    def test_response_backoff_and_cautious_recovery(self):
        state = NetworkBlockState()
        with patch("request_budget.time.time", return_value=1000):
            state.register_throttle_response(429, {"Retry-After": "120"})
            self.assertEqual(state.blocked_until, 1120)
            state.register_success()
            self.assertEqual(state.snapshot()["current_rps"], 0.25)
        with patch("request_budget.time.time", return_value=1121):
            for _ in range(10):
                state.register_success()
            self.assertGreater(state.snapshot()["current_rps"], 0.25)
            self.assertLessEqual(state.snapshot()["current_rps"], 0.5)
            self.assertEqual(state.block_streak, 0)

    def test_scan_log_remains_compatible_with_bounded_diagnostics(self):
        result = scan_summary('Scan result: ok=1 elapsed_ms=100 items=60 blocked=0 pages=2 coverage_gap=1\nScan result: ok=0 elapsed_ms=20 items=0 blocked=1 failure=cooldown')
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["successes"], 1)
        self.assertEqual(result["blocked_attempts"], 1)


class TestCatalogCoverage(unittest.TestCase):
    def test_ssr_and_html_are_not_truncated_at_25_cards(self):
        cards = [{"id": str(index), "title": "Fixture", "urlPath": f"/fixture_{index}"} for index in range(60)]
        ssr = '<html><script>' + json.dumps({"loaderData": {"data": {"catalog": {"items": cards}}}}) + '</script></html>'
        self.assertEqual(len(AvitoParser.parse_items(ssr)), 60)
        html = '<html>' + ''.join(f'<div data-marker="item" data-item-id="{index}"><a data-marker="item-title" href="/fixture_{index}">Fixture</a></div>' for index in range(60)) + '</html>'
        self.assertEqual(len(AvitoParser.parse_items(html)), 60)

    def test_empty_catalog_is_valid_but_login_page_is_not(self):
        html = '<html><script>' + json.dumps({"loaderData": {"data": {"catalog": {"items": []}}}}) + '</script></html>'
        self.assertTrue(AvitoParser.has_catalog(html))
        self.assertEqual(AvitoParser.parse_items(html), [])
        self.assertFalse(AvitoParser.has_catalog('<html><body>Sign in</body></html>'))

    def test_canonical_key_resets_page_but_preserves_search_filters(self):
        first = AvitoParser.normalize_url('https://m.avito.ru/moskva?q=phone&p=9&bt=1&searchRadius=10&priceMax=20000&utm_source=test')
        second = AvitoParser.normalize_url('https://www.avito.ru/moskva?priceMax=20000&searchRadius=10&bt=1&q=phone')
        self.assertEqual(first, second)
        self.assertNotEqual(first, AvitoParser.normalize_url('https://www.avito.ru/moskva?q=phone&bt=1'))

    def test_pagination_only_accepts_exact_next_page_same_search(self):
        current = 'https://www.avito.ru/moskva?q=phone&s=104'
        valid = '<a data-marker="pagination-button/nextPage" href="?q=phone&amp;s=104&amp;p=2">Next</a>'
        self.assertIn('p=2', AvitoParser.next_page_url(valid, current))
        for href in ('https://evil.test/?p=2', '?q=other&p=2', '?q=phone&p=20'):
            self.assertIsNone(AvitoParser.next_page_url(f'<a rel="next" href="{href}">Next</a>', current))


class TestBudgetedCurlPath(unittest.IsolatedAsyncioTestCase):
    def create_parser(self, state):
        with patch.object(AvitoParser, "_load_cookies"):
            parser = AvitoParser(block_state=state)
        parser._save_cookies = Mock()
        self.addCleanup(parser.close)
        return parser

    async def test_real_session_counts_nested_and_redirect_requests_off_loop(self):
        state = NetworkBlockState(requests_per_second=1000, max_in_flight=1)
        responses = [FakeResponse(439, payload={"pow_challenge": "test"}), FakeResponse(200, payload={"accepted": True}),
                     FakeResponse(302, headers={"Location": "/canonical"}), FakeResponse(200, "<html>catalog</html>")]
        session_seen = []
        def solve(session, response, url, **kwargs):
            session_seen.append(session)
            session.post("https://www.avito.ru/fixture-check", timeout=5)
            return True
        with patch.object(curl_requests.Session, "request", side_effect=responses) as request, patch.object(parser_module, "handle_anti_bot_challenge", side_effect=solve):
            parser = self.create_parser(state)
            original_session = parser.session
            self.assertIn("catalog", await parser.fetch_page_async("https://www.avito.ru/search"))
        self.assertIs(parser.session, original_session)
        self.assertEqual(session_seen, [original_session])
        self.assertEqual(request.call_count, 4)
        self.assertEqual(state.snapshot()["requests"], 4)
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in request.call_args_list))
        self.assertEqual(state.snapshot()["in_flight"], 0)

    async def test_429_never_failovers_or_calls_challenge_solver(self):
        state = NetworkBlockState(requests_per_second=1000)
        with patch.object(curl_requests.Session, "request", return_value=FakeResponse(429, headers={"Retry-After": "120"})) as request, patch.object(parser_module, "handle_anti_bot_challenge") as solve:
            first, second = self.create_parser(state), self.create_parser(state)
            self.assertIsNone(await first.fetch_page_async("https://www.avito.ru/search"))
            self.assertIsNone(await second.fetch_page_async("https://www.avito.ru/search"))
        self.assertEqual(request.call_count, 1)
        solve.assert_not_called()
        self.assertEqual(state.block_streak, 1)
        self.assertEqual(first.last_failure.kind, "rate_limit")
        self.assertGreaterEqual(state.cooldown_remaining(), 119)

    async def test_network_failure_has_one_attempt_and_preserves_event_loop(self):
        state = NetworkBlockState(requests_per_second=1000)
        started = Event()
        def slow_failure(*args, **kwargs):
            started.set()
            time.sleep(0.1)
            raise TimeoutError("fixture timeout")
        with patch.object(curl_requests.Session, "request", side_effect=slow_failure) as request:
            parser = self.create_parser(state)
            task = asyncio.create_task(parser.fetch_page_async("https://www.avito.ru/search"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            self.assertIsNone(await task)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(parser.last_failure.kind, "timeout")
        self.assertEqual(state.snapshot()["in_flight"], 0)

    async def test_request_and_time_budgets_prevent_unbounded_fetch(self):
        state = NetworkBlockState(requests_per_second=1000)
        response = FakeResponse(302, headers={"Location": "/again"})
        with patch.object(curl_requests.Session, "request", return_value=response) as request, patch.object(config, "AVITO_MAX_REQUESTS_PER_FETCH", 2):
            parser = self.create_parser(state)
            self.assertIsNone(await parser.fetch_page_async("https://www.avito.ru/search"))
            self.assertEqual(request.call_count, 2)
            self.assertEqual(parser.last_failure.kind, "request_budget")
        with patch.object(curl_requests.Session, "request") as request:
            parser = self.create_parser(NetworkBlockState())
            self.assertIsNone(await parser.fetch_page_async("https://www.avito.ru/search", deadline=time.monotonic() - 1))
            request.assert_not_called()


class TestBoundedCollection(unittest.IsolatedAsyncioTestCase):
    def catalog(self, cards, next_page=None):
        payload = {"loaderData": {"data": {"catalog": {"items": cards}}}}
        link = f'<a rel="next" href="?s=104&amp;p={next_page}">Next</a>' if next_page else ''
        return '<html><script>' + json.dumps(payload) + '</script>' + link + '</html>'

    def fake_parser(self, pages):
        parser = Mock(spec=AvitoParser)
        parser.fetch_page_async = AsyncMock(side_effect=pages)
        parser.parse_items = AvitoParser.parse_items
        parser.has_catalog = AvitoParser.has_catalog
        parser.next_page_url = AvitoParser.next_page_url
        parser.extract_search_title = lambda *_: "Fixture"
        return parser

    async def test_bounded_pages_deduplicate_and_report_gap(self):
        cards = [{"id": "1", "title": "Fixture"}, {"id": "2", "title": "Fixture"}]
        parser = self.fake_parser([self.catalog(cards, 2), self.catalog(cards + [{"id": "3", "title": "Fixture"}], 3)])
        with patch.object(config, "AVITO_MAX_PAGES_PER_SCAN", 2):
            collection = await collect_source(parser, "https://www.avito.ru/search?s=104")
        self.assertEqual(len(collection.items), 3)
        self.assertEqual(collection.pages, 2)
        self.assertTrue(collection.coverage_gap)
        self.assertEqual(parser.fetch_page_async.await_count, 2)
        self.assertEqual(len({call.kwargs["deadline"] for call in parser.fetch_page_async.call_args_list}), 1)

    async def test_pagination_failure_retains_successful_first_page(self):
        parser = self.fake_parser([self.catalog([{"id": "1", "title": "Fixture"}], 2), None])
        collection = await collect_source(parser, "https://www.avito.ru/search?s=104")
        self.assertEqual(len(collection.items), 1)
        self.assertTrue(collection.coverage_gap)

    async def test_sorted_old_boundary_stops_without_extra_requests(self):
        now = int(time.time())
        cards = [{"id": "1", "title": "Fixture", "sortTimeStamp": (now - 120) * 1000},
                 {"id": "2", "title": "Fixture", "sortTimeStamp": (now - 900) * 1000}]
        parser = self.fake_parser([self.catalog(cards, 2)])
        collection = await collect_source(parser, "https://www.avito.ru/search?s=104")
        self.assertEqual(collection.pages, 1)
        self.assertFalse(collection.coverage_gap)

    async def test_login_page_is_retryable_not_empty_baseline(self):
        parser = self.fake_parser(['<html><body>Login</body></html>'])
        self.assertIsNone(await collect_source(parser, "https://www.avito.ru/search?s=104"))
        self.assertEqual(parser.last_failure.kind, "catalog_missing")


class TestSharedCalibration(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_cache_single_flight_and_expiry(self):
        cache = SourceSnapshotCache(ttl=0.01, maxsize=1)
        calls = 0
        async def fetch(key):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            result = SourceSnapshot("Fixture", (), time.time())
            cache.put(key, result)
            return result
        with patch.object(cache, "_fetch", side_effect=fetch):
            first, second = await asyncio.gather(cache.load("same"), cache.load("same"))
            self.assertIs(first, second)
            self.assertEqual(calls, 1)
        cache.put("other", SourceSnapshot("Other", (), time.time()))
        self.assertIsNone(cache.get("same"))
        await asyncio.sleep(0.02)
        self.assertIsNone(cache.get("other"))

    async def test_scheduler_runtime_groups_sources_and_closes_owned_parsers(self):
        radar = {"id": 1, "user_id": 123, "url": "https://www.avito.ru/search", "tariff_plan": "standart"}
        parser = Mock()
        parser.last_failure = None
        parser.last_coverage_gap = False
        called = asyncio.Event()
        async def process(*args, **kwargs):
            called.set()
            await asyncio.sleep(0.01)
            return True
        with patch.object(config, "MAX_CONCURRENT_WORKERS", 1), patch.object(monitoring, "AvitoParser", wraps=AvitoParser) as constructor, patch.object(monitoring.database, "get_all_active_searches", new=AsyncMock(return_value=[radar, dict(radar, id=2)])), patch.object(scheduler, "process_url_group", new=process):
            constructor.return_value = parser
            task = asyncio.create_task(monitoring.run_monitoring(Mock(send_message=AsyncMock())))
            await asyncio.wait_for(called.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(monitoring.MONITORING_STATS["sources"], 1)
        self.assertEqual(monitoring.MONITORING_STATS["subscriptions"], 2)
        parser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
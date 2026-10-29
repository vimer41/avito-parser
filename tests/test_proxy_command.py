"""/proxy: connect one proxy from the bot, probe it, persist it, switch the pool without restart."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests import _environment
import config
import database
import proxy_check
import proxy_pool
from collector import SourceCollection
from handlers import admin
from parser import AvitoItem, FetchFailure
from proxy_check import ProbeAttempt, ProbeResult

PROXY_RAW = "10.1.2.3:8080:user:secret"
PROXY_URL = "http://user:secret@10.1.2.3:8080"


def make_message(text: str):
    status = Mock(edit_text=AsyncMock())
    message = Mock(text=text, from_user=Mock(id=config.ADMIN_ID))
    message.answer = AsyncMock(return_value=status)
    message.delete = AsyncMock()
    return message, status


class TestProxyCommand(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "proxy.db"),
            patch.object(config, "PROXY_URL", None),
            patch.object(config, "PROXY_POOL_FILE", ""),
            patch.object(config, "ALLOW_DIRECT_AVITO", True),
        ]
        for p in self.patches:
            p.start()
        await database.init_db()
        self.pool = proxy_pool.ProxyPool(proxy_pool_file="", fallback_proxy_url="", allow_direct=True)
        self.pool_patch = patch.object(proxy_pool, "get_proxy_pool", return_value=self.pool)
        self.pool_patch.start()

    async def asyncTearDown(self):
        self.pool_patch.stop()
        for client in self.pool.clients:
            await client.close()
        for p in reversed(self.patches):
            p.stop()
        self.temp_dir.cleanup()

    def probe(self, *oks):
        attempts = [ProbeAttempt(True, 400, items=50) if ok else ProbeAttempt(False, 3000, failure="forbidden", status=403) for ok in oks]
        return ProbeResult(proxy_id="p-test", masked_url="http://***@10.1.2.3:8080", target_url="https://www.avito.ru/x", attempts=attempts)

    async def test_successful_probe_connects_persists_and_hides_password(self):
        message, status = make_message(f"/proxy {PROXY_RAW}")
        with patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=self.probe(True, True, False))) as probe:
            await admin.cmd_admin_proxy(message)
        probe.assert_awaited_once()
        self.assertEqual(probe.await_args.args[0], PROXY_URL)
        message.delete.assert_awaited_once()
        self.assertEqual(await database.get_bot_setting("proxy_url"), PROXY_URL)
        self.assertEqual([c.proxy_url for c in self.pool.clients], [PROXY_URL])
        report = status.edit_text.await_args.args[0]
        self.assertIn("Прокси подключён", report)
        self.assertIn("2/3", report)
        self.assertNotIn("secret", report)

    async def test_failed_probe_keeps_current_channel(self):
        message, status = make_message(f"/proxy {PROXY_RAW}")
        with patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=self.probe(False, False, False))):
            await admin.cmd_admin_proxy(message)
        self.assertIn("не подключён", status.edit_text.await_args.args[0])
        self.assertIsNone(await database.get_bot_setting("proxy_url"))
        self.assertEqual([c.proxy_url for c in self.pool.clients], [None])

    async def test_invalid_proxy_is_rejected_without_probe(self):
        message, _ = make_message("/proxy ftp://10.1.2.3:21")
        with patch.object(proxy_check, "probe_channel", new=AsyncMock()) as probe:
            await admin.cmd_admin_proxy(message)
        probe.assert_not_awaited()
        self.assertIn("неподдерживаемая схема", message.answer.await_args.args[0])

    async def test_off_returns_to_direct_and_startup_restores_saved_proxy(self):
        await database.set_bot_setting("proxy_url", PROXY_URL)
        await database.init_db()
        self.assertEqual(config.PROXY_URL, PROXY_URL)
        await self.pool.reload_async()
        self.assertEqual([c.proxy_url for c in self.pool.clients], [PROXY_URL])

        message, _ = make_message("/proxy off")
        await admin.cmd_admin_proxy(message)
        self.assertEqual(await database.get_bot_setting("proxy_url"), "")
        self.assertIsNone(config.PROXY_URL)
        self.assertEqual([c.proxy_url for c in self.pool.clients], [None])

    async def test_non_admin_is_refused(self):
        message, _ = make_message(f"/proxy {PROXY_RAW}")
        message.from_user.id = 1
        with patch.object(proxy_check, "probe_channel", new=AsyncMock()) as probe:
            await admin.cmd_admin_proxy(message)
        probe.assert_not_awaited()


class TestProbeChannel(unittest.IsolatedAsyncioTestCase):
    async def test_probe_reports_successes_failures_and_median(self):
        now = time.time()
        ok = SourceCollection(title="t", items=(AvitoItem(item_id="1", title="t", price="1", url="u"),),
                              captured_at=now, pages=1, coverage_gap=False, elapsed_ms=1)
        outcomes = iter([ok, None, ok])

        async def fake_collect(parser, url, *args, **kwargs):
            result = next(outcomes)
            if result is None:
                parser.last_failure = FetchFailure("forbidden", status=403)
            return result

        with patch.object(proxy_check, "collect_source", new=fake_collect):
            result = await proxy_check.probe_channel(PROXY_URL, "https://www.avito.ru/moskva?q=phone")
        self.assertEqual(result.successes, 2)
        self.assertEqual([a.failure for a in result.attempts], ["", "forbidden", ""])
        self.assertEqual(result.attempts[1].status, 403)
        self.assertIsNotNone(result.median_ok_ms)
        self.assertNotIn("secret", result.masked_url)


if __name__ == "__main__":
    unittest.main()

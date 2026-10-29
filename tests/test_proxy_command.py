"""/proxy: connect one proxy from the bot, probe it, persist it, switch the pool without restart."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests import _environment
from tests._dispatcher import DispatcherFixture
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


class TestNetworkScreen(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "net.db"),
            patch.object(config, "PROXY_URL", None),
            patch.object(config, "PROXY_POOL_FILE", ""),
            patch.object(config, "ALLOW_DIRECT_AVITO", True),
            patch.object(config, "NET_MODE", "auto"),
        ]
        for p in self.patches:
            p.start()
        await database.init_db()
        self.pool = proxy_pool.ProxyPool(proxy_pool_file="", fallback_proxy_url="", allow_direct=True)
        self.pool_patch = patch.object(proxy_pool, "get_proxy_pool", return_value=self.pool)
        self.pool_patch.start()
        self.setup_dispatcher(names=("admin",))

    async def asyncTearDown(self):
        await self.close_dispatcher()
        self.pool_patch.stop()
        for client in self.pool.clients:
            await client.close()
        for p in reversed(self.patches):
            p.stop()
        self.temp_dir.cleanup()

    def channels(self):
        return sorted((c.proxy_url or "direct") for c in self.pool.clients)

    async def test_screen_lists_modes_and_refuses_proxy_mode_without_proxy(self):
        await self.dispatch_callback("admin_net", config.ADMIN_ID)
        self.assertTrue({"admin_net_mode:direct", "admin_net_mode:proxy", "admin_net_mode:both",
                         "admin_net_probe", "admin_net_add"} <= set(self.callback_values()))
        await self.dispatch_callback("admin_net_mode:proxy", config.ADMIN_ID)
        self.assertTrue(self.methods("AnswerCallbackQuery")[-1].show_alert)
        self.assertEqual(config.NET_MODE, "auto")
        self.assertEqual(self.channels(), ["direct"])

    async def test_button_flow_connects_proxy_then_both_mode_uses_two_channels(self):
        await self.dispatch_callback("admin_net_add", config.ADMIN_ID)
        probe = ProbeResult(proxy_id="p", masked_url="http://***@10.1.2.3:8080", target_url="u",
                            attempts=[ProbeAttempt(True, 300, items=50)])
        with patch.object(proxy_check, "probe_channel", new=AsyncMock(return_value=probe)):
            await self.dispatch_message(text=PROXY_RAW, user_id=config.ADMIN_ID)
        self.assertTrue(self.methods("DeleteMessage"), "the message with the password must be deleted")
        self.assertEqual(self.channels(), [PROXY_URL])
        self.assertIn("admin_net_proxy_off", self.callback_values())

        await self.dispatch_callback("admin_net_mode:both", config.ADMIN_ID)
        self.assertEqual(config.NET_MODE, "both")
        self.assertEqual(await database.get_bot_setting("net_mode"), "both")
        self.assertEqual(self.channels(), ["direct", PROXY_URL])

        await self.dispatch_callback("admin_net_mode:proxy", config.ADMIN_ID)
        self.assertEqual(self.channels(), [PROXY_URL])

        # Removing the only proxy in proxy-only mode must not silently stop scanning.
        await self.dispatch_callback("admin_net_proxy_off", config.ADMIN_ID)
        self.assertEqual(config.NET_MODE, "direct")
        self.assertEqual(self.channels(), ["direct"])

    async def test_saved_mode_is_restored_on_startup(self):
        await database.set_bot_setting("net_mode", "both")
        await database.init_db()
        self.assertEqual(config.NET_MODE, "both")

    async def test_non_admin_cannot_switch_modes(self):
        await self.dispatch_callback("admin_net_mode:direct", 123456)
        self.assertEqual(config.NET_MODE, "auto")


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

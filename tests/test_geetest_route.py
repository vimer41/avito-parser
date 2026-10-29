"""GeeTest is solved directly from the server; only Avito's verify goes through the challenged channel."""

import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests import _environment
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, Message, User

import anti_bot.manager as manager
from middlewares import ActionLoggingMiddleware


def jsonp_response(callback_holder):
    def get(url, params=None, **kwargs):
        callback_holder["callback"] = params["callback"]
        body = {"status": "success", "data": {"lot_number": "lot-1"}}
        return Mock(status_code=200, text=f"{params['callback']}({json.dumps(body)})")
    return get


class TestGeeTestRoute(unittest.TestCase):
    def run_solver(self, direct_enabled: bool):
        holder = {}
        direct = Mock(name="direct_session")
        direct.get.side_effect = jsonp_response(holder)
        avito = Mock(name="avito_session", headers={"User-Agent": "UA/1.0"})
        avito.get.side_effect = jsonp_response(holder)
        avito.post.return_value = Mock(status_code=200, json=lambda: {"success": {"result": {"verified": True}}})

        created = {}

        class FakeGeeked:
            def __init__(self, captcha_id, **kwargs):
                created["session"] = kwargs.get("session")

            def submit_captcha(self, task_data):
                return {"lot_number": "lot-1", "pass_token": "t", "gen_time": "1", "captcha_output": "o"}

        fake_module = types.ModuleType("geeked")
        fake_module.Geeked = FakeGeeked
        fake_module.CaptchaSolveRejected = type("CaptchaSolveRejected", (Exception,), {})
        page = Mock(text="const captchaId = 'abc';")
        with patch.dict(sys.modules, {"geeked": fake_module}), \
                patch.object(manager, "GEETEST_DIRECT", direct_enabled), \
                patch.object(manager.requests, "Session", return_value=direct) as session_factory:
            ok = manager.solve_geetest_if_available(avito, page, "https://www.avito.ru/x")
        return ok, direct, avito, created, session_factory

    def test_geetest_traffic_goes_direct_and_verify_through_the_channel(self):
        ok, direct, avito, created, session_factory = self.run_solver(direct_enabled=True)
        self.assertTrue(ok)
        session_factory.assert_called_once()
        self.assertNotIn("proxies", session_factory.call_args.kwargs, "the GeeTest session must not use the proxy")
        direct.get.assert_called_once()
        avito.get.assert_not_called()
        self.assertIs(created["session"], direct)
        avito.post.assert_called_once()
        self.assertEqual(avito.post.call_args.args[0], manager.FIREWALL_CAPTCHA_VERIFY_URL)
        direct.close.assert_called_once()

    def test_switch_keeps_old_route(self):
        ok, direct, avito, created, session_factory = self.run_solver(direct_enabled=False)
        self.assertTrue(ok)
        session_factory.assert_not_called()
        avito.get.assert_called_once()
        self.assertIs(created["session"], avito)


class TestExpiredCallback(unittest.IsolatedAsyncioTestCase):
    async def test_expired_button_press_is_ignored_not_raised(self):
        user = User(id=1, is_bot=False, first_name="U")
        message = Message(message_id=1, date=0, chat=Chat(id=1, type="private"), text="x")
        call = CallbackQuery(id="c", from_user=user, chat_instance="i", message=message, data="admin_net")
        error = TelegramBadRequest(method=Mock(), message="Bad Request: query is too old and response timeout expired or query ID is invalid")
        handler = AsyncMock(side_effect=error)
        result = await ActionLoggingMiddleware()(handler, call, {"event_from_user": user})
        self.assertIsNone(result)

    async def test_other_bad_requests_still_raise(self):
        user = User(id=1, is_bot=False, first_name="U")
        message = Message(message_id=1, date=0, chat=Chat(id=1, type="private"), text="x")
        call = CallbackQuery(id="c", from_user=user, chat_instance="i", message=message, data="admin_net")
        handler = AsyncMock(side_effect=TelegramBadRequest(method=Mock(), message="Bad Request: message is not modified"))
        with self.assertRaises(TelegramBadRequest):
            await ActionLoggingMiddleware()(handler, call, {"event_from_user": user})


if __name__ == "__main__":
    unittest.main()

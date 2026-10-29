import asyncio
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests import _environment
from aiogram.types import Message, CallbackQuery, User, Chat
import config
import database
from middlewares import AccessControlMiddleware
from handlers.searches import cb_add_stop_word_prompt
from scheduler import ParserPool


class TestAccessMiddleware(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = User(id=123456, is_bot=False, first_name="Fixture")
        self.handler = AsyncMock(return_value="handled")
        self.middleware = AccessControlMiddleware()
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.state = SimpleNamespace(get_state=AsyncMock(return_value=None), clear=AsyncMock())
        self.data = {"event_from_user": self.user, "bot": self.bot, "state": self.state}

    def message(self, text):
        return Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc), chat=Chat(id=self.user.id, type="private"), from_user=self.user, text=text)

    def callback(self, value):
        return CallbackQuery(id="test", from_user=self.user, chat_instance="test", data=value)

    async def test_expired_users_can_open_public_menu_messages(self):
        for text in ("/start", "/help@fixture_bot", "💳 Тарифы", "💬 Поддержка", "👤 Мой профиль"):
            with self.subTest(text=text), patch.object(database, "is_user_allowed", new=AsyncMock(return_value=False)), patch.object(database, "get_or_create_user", new=AsyncMock(return_value={"trial_used": 1})):
                self.assertEqual(await self.middleware(self.handler, self.message(text), self.data), "handled")

    async def test_expired_users_can_open_public_menu_callbacks(self):
        for value in ("open_subscription_menu", "start_approved", "refresh_profile", "buy_tariff:pro", "demo_tariff:pro", "support_start_ticket", "other_payment_methods", "pay_method:sbp:pro"):
            with self.subTest(value=value):
                self.assertEqual(await self.middleware(self.handler, self.callback(value), self.data), "handled")

    async def test_callback_whitelist_is_exact_and_blocks_admin_actions(self):
        for value in ("admin_purge_user:986832959", "support_reply:455386428", "open_subscription_menu_fake", "add_search_prompt"):
            self.handler.reset_mock()
            with self.subTest(value=value), patch.object(CallbackQuery, "answer", new=AsyncMock()) as answer, patch.object(database, "is_user_allowed", new=AsyncMock(return_value=False)), patch.object(database, "get_or_create_user", new=AsyncMock(return_value={"trial_used": 1})):
                await self.middleware(self.handler, self.callback(value), self.data)
                self.handler.assert_not_called()
                answer.assert_awaited_once()

    async def test_whitespace_message_does_not_crash_access_control(self):
        with patch.object(Message, "answer", new=AsyncMock()), patch.object(database, "is_user_allowed", new=AsyncMock(return_value=False)), patch.object(database, "get_or_create_user", new=AsyncMock(return_value={"trial_used": 1})):
            await self.middleware(self.handler, self.message("   "), self.data)
        self.handler.assert_not_called()

    async def test_foreign_stop_word_prompt_does_not_change_fsm(self):
        with patch.object(database, "get_search", new=AsyncMock(return_value={"user_id": self.user.id + 1})), patch.object(CallbackQuery, "answer", new=AsyncMock()):
            state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
            await cb_add_stop_word_prompt(self.callback("add_stop:123"), state)
            state.set_state.assert_not_called()

    def test_parser_pool_passes_proxy_to_every_session(self):
        with patch("scheduler.AvitoParser") as constructor:
            pool = ParserPool(3, proxy_url="http://127.0.0.1:9999")
            self.assertEqual(len(pool.parsers), 3)
            self.assertEqual(constructor.call_count, 3)
            self.assertTrue(all(call.kwargs == {"proxy": "http://127.0.0.1:9999"} for call in constructor.call_args_list))

    def test_config_uses_admin_ids_and_isolated_paths(self):
        self.assertEqual(config.ADMIN_IDS, [986832959, 455386428])
        self.assertTrue(config.TESTING)
        self.assertNotEqual(config.DATA_DIR, config.BASE_DIR / "data")


if __name__ == "__main__":
    unittest.main()
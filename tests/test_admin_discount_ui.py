import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment
from tests._dispatcher import DispatcherFixture

import config
import database


class TestAdminDiscountUI(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-ui-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        self.user_id = 123456
        await database.get_or_create_user(self.user_id, username="fixture_user", first_name="Fixture")
        await database.grant_subscription(self.user_id, 30, "pro")
        self.radar_id = await database.add_search(self.user_id, "Fixture radar", "https://www.avito.ru/search")
        self.setup_dispatcher()

    async def asyncTearDown(self):
        await self.close_dispatcher()
        self.db_patch.stop()
        self.directory.cleanup()

    async def test_single_radar_shortcut_preserves_immutable_callback(self):
        call = await self.dispatch_callback("all_discounts_menu")
        self.assertEqual(call.data, "all_discounts_menu")
        self.assertIn(f"save_discount:{self.radar_id}:10", self.callback_values())
        self.assertIn("ОТСЛЕЖИВАНИЕ СНИЖЕНИЯ ЦЕНЫ", self.methods("EditMessageText")[-1].text)
        await self.dispatch_callback(f"save_discount:{self.radar_id}:10")
        self.assertEqual((await database.get_search(self.radar_id))["min_discount_percent"], 10)

    async def test_multi_radar_selection_and_individual_card(self):
        second = await database.add_search(self.user_id, "Second", "https://www.avito.ru/second")
        await self.dispatch_callback("all_discounts_menu")
        self.assertIn(f"set_discount:{self.radar_id}", self.callback_values())
        self.assertIn(f"set_discount:{second}", self.callback_values())
        await self.dispatch_callback(f"set_discount:{second}")
        self.assertIn(f"custom_discount:{second}", self.callback_values())

    async def test_foreign_radar_cannot_be_viewed_saved_or_used_in_fsm(self):
        foreign_user = 654321
        await database.get_or_create_user(foreign_user)
        await database.grant_subscription(foreign_user, 30, "pro")
        for value in (f"set_discount:{self.radar_id}", f"save_discount:{self.radar_id}:30", f"custom_discount:{self.radar_id}"):
            self.session.calls.clear()
            await self.dispatch_callback(value, foreign_user)
            self.assertEqual(self.methods("EditMessageText"), [])
            self.assertTrue(self.methods("AnswerCallbackQuery")[-1].show_alert)
        state = self.dp.fsm.get_context(bot=self.bot, chat_id=foreign_user, user_id=foreign_user)
        self.assertIsNone(await state.get_state())
        self.assertEqual((await database.get_search(self.radar_id))["min_discount_percent"], 0)

    async def test_standart_cannot_use_discount_shortcut_or_forged_save(self):
        await database.grant_subscription(self.user_id, 1, "standart")
        for value in ("all_discounts_menu", f"set_discount:{self.radar_id}", f"save_discount:{self.radar_id}:20", f"custom_discount:{self.radar_id}"):
            await self.dispatch_callback(value)
            self.assertIn("PRO", self.methods("AnswerCallbackQuery")[-1].text)
        self.assertEqual(self.methods("EditMessageText"), [])
        self.assertEqual((await database.get_search(self.radar_id))["min_discount_percent"], 0)

    async def test_revoked_pro_access_cannot_change_discounts(self):
        await database.set_user_allowed(self.user_id, 0)
        for value in ("all_discounts_menu", f"set_discount:{self.radar_id}", f"save_discount:{self.radar_id}:20"):
            await self.dispatch_callback(value)
            self.assertTrue(self.methods("AnswerCallbackQuery")[-1].show_alert)
        self.assertEqual(self.methods("EditMessageText"), [])
        self.assertEqual((await database.get_search(self.radar_id))["min_discount_percent"], 0)

    async def test_custom_discount_rechecks_tariff_after_prompt(self):
        await self.dispatch_callback(f"custom_discount:{self.radar_id}")
        await database.grant_subscription(self.user_id, 1, "standart")
        await self.dispatch_message(text="17")
        self.assertEqual((await database.get_search(self.radar_id))["min_discount_percent"], 0)
        self.assertIn("PRO", self.methods("SendMessage")[-1].text)

    async def test_invalid_discount_buttons_do_not_crash_or_write(self):
        for value in ("set_discount:fake", "save_discount:1", f"save_discount:{self.radar_id}:100", "custom_discount:fake"):
            await self.dispatch_callback(value)
            self.assertTrue(self.methods("AnswerCallbackQuery")[-1].show_alert)
        self.assertEqual((await database.get_search(self.radar_id))["min_discount_percent"], 0)

    async def test_admin_list_exposes_tariff_duration_confirmation_and_replay_safety(self):
        await self.dispatch_message(text="/admin", user_id=config.ADMIN_ID)
        self.assertIn(f"admin_user:{self.user_id}:0", self.callback_values())
        await self.dispatch_callback(f"admin_user:{self.user_id}:0", config.ADMIN_ID)
        self.assertIn(f"admin_sub_menu:{self.user_id}:0", self.callback_values())
        self.assertIn(f"admin_payments:{self.user_id}", self.callback_values())
        await self.dispatch_callback(f"admin_sub_menu:{self.user_id}:0", config.ADMIN_ID)
        self.assertIn(f"admin_sub_days:{self.user_id}:standart:0", self.callback_values())
        self.assertIn(f"admin_sub_days:{self.user_id}:pro:0", self.callback_values())
        await self.dispatch_callback(f"admin_sub_days:{self.user_id}:standart:0", config.ADMIN_ID)
        for days in (1, 7, 30, 90):
            self.assertIn(f"admin_grant_sub:{self.user_id}:standart:{days}:0", self.callback_values())
        before = await database.get_user(self.user_id)
        await self.dispatch_callback(f"admin_grant_sub:{self.user_id}:standart:30:0", config.ADMIN_ID)
        self.assertEqual(await database.get_user(self.user_id), before)
        confirm = next(value for value in self.callback_values() if value.startswith("admin_confirm_sub:"))
        await self.dispatch_callback(confirm, config.ADMIN_ID)
        after = await database.get_user(self.user_id)
        self.assertEqual(after["tariff_plan"], "standart")
        self.assertEqual(after["subscription_revision"], before["subscription_revision"] + 1)
        await self.dispatch_callback(confirm, config.ADMIN_ID)
        self.assertEqual(await database.get_user(self.user_id), after)
        notices = [m.text for m in self.methods("SendMessage") if m.chat_id == self.user_id]
        self.assertEqual(len(notices), 1)
        self.assertNotIn("Оплата подтверждена", notices[0])

    async def test_admin_token_cannot_be_applied_by_another_admin_or_user(self):
        token = await database.create_admin_subscription_action(config.ADMIN_ID, self.user_id, "pro", 7)
        before = await database.get_user(self.user_id)
        await self.dispatch_callback(f"admin_confirm_sub:{token}", config.ADMIN_IDS[1])
        await self.dispatch_callback(f"admin_confirm_sub:{token}", self.user_id)
        self.assertEqual(await database.get_user(self.user_id), before)

    async def test_non_admin_cannot_use_any_subscription_or_refund_controls(self):
        before = await database.get_user(self.user_id)
        for value in (f"admin_user:{self.user_id}:0", f"admin_sub_menu:{self.user_id}:0",
                      f"admin_grant_sub:{self.user_id}:pro:30:0", f"admin_revoke:{self.user_id}:0",
                      f"admin_payments:{self.user_id}", "admin_refund:1"):
            await self.dispatch_callback(value)
            self.assertIn("прав", self.methods("AnswerCallbackQuery")[-1].text)
        self.assertEqual(await database.get_user(self.user_id), before)

    async def test_admin_card_does_not_register_unknown_targets_or_revoke_admin(self):
        await self.dispatch_callback("admin_user:999999:0", config.ADMIN_ID)
        self.assertIsNone(await database.get_user(999999))
        await self.dispatch_callback(f"admin_grant_sub:{config.ADMIN_ID}:standart:30", config.ADMIN_ID)
        await self.dispatch_callback(f"admin_revoke:{config.ADMIN_ID}:0", config.ADMIN_ID)
        self.assertTrue(await database.is_user_allowed(config.ADMIN_ID))

    async def test_admin_revoke_requires_visible_confirmation_path(self):
        await self.dispatch_callback(f"admin_revoke_prompt:{self.user_id}:0", config.ADMIN_ID)
        self.assertTrue(await database.is_user_allowed(self.user_id))
        self.assertIn(f"admin_revoke:{self.user_id}:0", self.callback_values())
        await self.dispatch_callback(f"admin_revoke:{self.user_id}:0", config.ADMIN_ID)
        self.assertFalse(await database.is_user_allowed(self.user_id))

    async def test_real_diagnostic_button_requires_admin_and_replies(self):
        await self.dispatch_callback("admin_probe")
        self.assertIn("прав", self.methods("AnswerCallbackQuery")[-1].text)
        await self.dispatch_callback("admin_probe", config.ADMIN_ID)
        self.assertIn("Запрос получен", self.methods("AnswerCallbackQuery")[-1].text)
        self.assertIn("реального запроса Telegram", self.methods("SendMessage")[-1].text)

    async def test_admin_search_user_by_id_and_username(self):
        # 1. Поиск по ID через команду /find
        await self.dispatch_message(text=f"/find {self.user_id}", user_id=config.ADMIN_ID)
        self.assertIn("КАРТОЧКА ПОЛЬЗОВАТЕЛЯ", self.methods("SendMessage")[-1].text)
        self.assertIn(str(self.user_id), self.methods("SendMessage")[-1].text)

        # 2. Поиск по @username через интерактивную кнопку
        await self.dispatch_callback("admin_search_user_prompt", config.ADMIN_ID)
        self.assertIn("ПОИСК ПОЛЬЗОВАТЕЛЯ", self.methods("EditMessageText")[-1].text)
        await self.dispatch_message(text="@fixture_user", user_id=config.ADMIN_ID)
        self.assertIn("КАРТОЧКА ПОЛЬЗОВАТЕЛЯ", self.methods("SendMessage")[-1].text)

        # 3. Поиск несуществующего пользователя
        await self.dispatch_message(text="/find non_existent_user_99999", user_id=config.ADMIN_ID)
        self.assertIn("не найден", self.methods("SendMessage")[-1].text)

    async def test_admin_grant_vip_and_reset_trial_notifications(self):
        # 1. Выдача бессрочного VIP
        await self.dispatch_callback(f"admin_grant_vip_prompt:{self.user_id}:pro:0", config.ADMIN_ID)
        self.assertIn(f"admin_confirm_vip:{self.user_id}:pro:0", self.callback_values())
        await self.dispatch_callback(f"admin_confirm_vip:{self.user_id}:pro:0", config.ADMIN_ID)
        self.assertTrue(await database.is_user_allowed(self.user_id))
        user = await database.get_user(self.user_id)
        self.assertIsNone(user["subscription_until"])
        self.assertEqual(user["tariff_plan"], "pro")
        notices = [m for m in self.methods("SendMessage") if m.chat_id == self.user_id]
        self.assertTrue(any("VIP-доступ" in m.text for m in notices))

        # 2. Активация 5-часового триала
        await self.dispatch_callback(f"admin_reset_trial_prompt:{self.user_id}:0", config.ADMIN_ID)
        self.assertIn(f"admin_confirm_trial:{self.user_id}:0", self.callback_values())
        await self.dispatch_callback(f"admin_confirm_trial:{self.user_id}:0", config.ADMIN_ID)
        user_trial = await database.get_user(self.user_id)
        self.assertIsNotNone(user_trial["subscription_until"])
        notices2 = [m for m in self.methods("SendMessage") if m.chat_id == self.user_id]
        self.assertTrue(any("5 часов" in m.text for m in notices2))

    async def test_admin_view_user_radars(self):
        await self.dispatch_callback(f"admin_user_radars:{self.user_id}:0", config.ADMIN_ID)
        text = self.methods("EditMessageText")[-1].text
        self.assertIn("РАДАРЫ ПОЛЬЗОВАТЕЛЯ", text)
        self.assertIn("Fixture radar", text)
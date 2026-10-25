import asyncio
import os
import unittest
import tempfile
from pathlib import Path

# Устанавливаем тестовое окружение до импорта модулей
os.environ["BOT_TOKEN"] = "1234567890:TEST_TOKEN_FOR_CI_CD_MOCK"
os.environ["ADMIN_ID"] = "986832959"
os.environ["ADMIN_IDS"] = "986832959,455386428"

import config
import database
from handlers.common import get_main_keyboard
from scheduler import TTLCache, get_item_lock


class TestAvitoRadarSystem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Используем отдельную изолированную тестовую базу в tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_bot.db"
        database.DB_PATH = self.test_db_path
        await database.init_db()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_admin_and_trial_limits(self):
        """Проверка выдачи 24h триала новым юзерам и лимитов радаров."""
        admin_id = 986832959
        trial_user_id = 11223344

        # 1. Администратор
        admin_user = await database.get_or_create_user(admin_id, "admin_user", "Admin")
        self.assertEqual(admin_user["is_allowed"], 1)
        self.assertEqual(admin_user["tariff_plan"], "pro")
        admin_radars = await database.get_user_max_radars(admin_id)
        self.assertEqual(admin_radars, config.MAX_SEARCHES_PER_USER)

        # 2. Новый пользователь (автоматический 24ч триал)
        new_user = await database.get_or_create_user(trial_user_id, "new_buyer", "Ivan")
        self.assertEqual(new_user["trial_used"], 1)
        self.assertIsNotNone(new_user["subscription_until"])
        
        # Доступ активен в течение 24 часов
        is_allowed = await database.is_user_allowed(trial_user_id)
        self.assertTrue(is_allowed)

        # Лимит для триала — ровно 2 радара
        trial_radars = await database.get_user_max_radars(trial_user_id)
        self.assertEqual(trial_radars, 2)

    async def test_subscription_upgrade(self):
        """Проверка апгрейда до Standart (5 радаров) и PRO (15 радаров)."""
        user_id = 55667788
        await database.get_or_create_user(user_id, "upgrader", "Alex")

        # Апгрейд до Standart
        await database.grant_subscription(user_id, days=30, tariff_plan="standart")
        standart_radars = await database.get_user_max_radars(user_id)
        self.assertEqual(standart_radars, 5)

        # Апгрейд до PRO
        await database.grant_subscription(user_id, days=30, tariff_plan="pro")
        pro_radars = await database.get_user_max_radars(user_id)
        self.assertEqual(pro_radars, 15)

    def test_main_keyboard_tariffs(self):
        """Проверка наличия кнопки тарифов в главном меню."""
        kb = get_main_keyboard()
        buttons = [btn.text for row in kb.keyboard for btn in row]
        self.assertIn("💳 Тарифы", buttons)
        self.assertIn("🎯 Добавить радар", buttons)
        self.assertIn("📁 Мои радары", buttons)

    def test_ttl_cache_logic(self):
        """Проверка кэша file_id Telegram."""
        cache = TTLCache(ttl_seconds=2)
        cache.set("key1", "val1")
        self.assertEqual(cache.get("key1"), "val1")
        self.assertIsNone(cache.get("nonexistent"))

    async def test_item_lock(self):
        """Проверка блокировок против гонки состояний (Cache Stampede)."""
        lock1 = await get_item_lock("item_100")
        lock2 = await get_item_lock("item_100")
        self.assertIs(lock1, lock2)
        lock3 = await get_item_lock("item_200")
        self.assertIsNot(lock1, lock3)


if __name__ == "__main__":
    unittest.main()

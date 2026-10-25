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

    def test_format_subscription_date_msk(self):
        """Проверка корректной конвертации даты подписки из UTC в MSK (UTC+3) и расчета остатка."""
        from datetime import datetime, timezone, timedelta
        # Допустим, подписка действует до момента через 24 часа
        target_utc = datetime.now(timezone.utc) + timedelta(hours=24)
        raw_str = target_utc.strftime("%Y-%m-%d %H:%M:%S")
        formatted = database.format_subscription_date(raw_str)
        self.assertIn("(МСК)", formatted)
        self.assertIn("осталось", formatted)

    async def test_purge_user_by_pattern(self):
        """Проверка полного удаления пользователя, радаров и данных из БД."""
        u_id = 999888777
        await database.get_or_create_user(u_id, "johnpeterson92", "John")
        await database.add_search(u_id, "iPhone 15", "https://avito.ru/search", 0)
        searches_before = await database.get_user_searches(u_id)
        self.assertEqual(len(searches_before), 1)

        # Вызываем удаление по username
        res = await database.purge_user_by_pattern("johnpeterson92")
        self.assertTrue(res["success"])
        self.assertGreaterEqual(res["deleted"], 1)

        # Проверяем, что пользователь и его радары полностью исчезли
        user_check = await database.get_all_users()
        self.assertFalse(any(u["user_id"] == u_id for u in user_check))
        searches_after = await database.get_user_searches(u_id)
        self.assertEqual(len(searches_after), 0)


    async def test_radar_deletion_atomicity_and_active_check(self):
        """Проверка атомарности delete_search и метода is_search_active."""
        user1 = 1001
        user2 = 1002
        await database.get_or_create_user(user1, "u1", "User One")
        await database.get_or_create_user(user2, "u2", "User Two")

        search1_id = await database.add_search(user1, "PlayStation 5", "https://avito.ru/ps5", 0)
        self.assertTrue(await database.is_search_active(search1_id))

        # Добавляем лоты в seen_items для search1
        await database.mark_items_seen_bulk(search1_id, [("item_1", 30000), ("item_2", 35000)])
        seen_before = await database.get_seen_items_with_prices(search1_id)
        self.assertEqual(len(seen_before), 2)

        # Попытка чужого пользователя (user2) удалить радар user1: должно вернуть False и НЕ трогать seen_items
        deleted_by_other = await database.delete_search(search1_id, user2)
        self.assertFalse(deleted_by_other)
        seen_after_attempt = await database.get_seen_items_with_prices(search1_id)
        self.assertEqual(len(seen_after_attempt), 2)
        self.assertTrue(await database.is_search_active(search1_id))

        # Удаление владельцем (user1): должно вернуть True, стереть радар и его seen_items
        deleted_by_owner = await database.delete_search(search1_id, user1)
        self.assertTrue(deleted_by_owner)
        self.assertFalse(await database.is_search_active(search1_id))
        seen_after_delete = await database.get_seen_items_with_prices(search1_id)
        self.assertEqual(len(seen_after_delete), 0)

    async def test_cleanup_orphaned_seen_items(self):
        """Проверка очистки сиротских записей seen_items при отсутствии радара."""
        # Вставляем искусственные сиротские записи для несуществующего search_id = 99999
        await database.mark_items_seen_bulk(99999, [("orphan_1", 1000), ("orphan_2", 2000)])
        orphans_before = await database.get_seen_items_with_prices(99999)
        self.assertEqual(len(orphans_before), 2)

        # Вызываем регулярную очистку БД
        deleted_total = await database.cleanup_old_seen_items(days=5)
        self.assertGreaterEqual(deleted_total, 2)

        # Сироты должны быть удалены
        orphans_after = await database.get_seen_items_with_prices(99999)
        self.assertEqual(len(orphans_after), 0)

    async def test_item_lock_rotation_preserves_locked(self):
        """Проверка, что ротация ITEM_LOCKS не удаляет активные (захваченные) мьютексы."""
        from scheduler import ITEM_LOCKS, get_item_lock

        # Создаем лок и захватываем его
        held_lock = await get_item_lock("active_item_in_progress")
        await held_lock.acquire()
        self.assertTrue(held_lock.locked())

        try:
            # Заполняем словарь фейковыми локами до лимита 3000
            for i in range(3005):
                await get_item_lock(f"idle_item_{i}")

            # Проверяем, что активный лок остался на месте и не был перезаписан
            same_held_lock = await get_item_lock("active_item_in_progress")
            self.assertIs(same_held_lock, held_lock)
            self.assertTrue(same_held_lock.locked())
        finally:
            held_lock.release()


if __name__ == "__main__":
    unittest.main()


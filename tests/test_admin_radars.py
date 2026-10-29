"""Admins add many radars from one message and raise radar limits with a button."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment
from tests._dispatcher import DispatcherFixture
import config
import database
from handlers.searches import extract_avito_urls

USER_ID = 55501


def links(count, start=0):
    return "\n".join(f"https://www.avito.ru/moskva?q=lot{i}" for i in range(start, start + count))


class TestExtractUrls(unittest.TestCase):
    def test_finds_every_link_once_in_order(self):
        text = "avito.ru/spb/odezhda?q=a\nhttps://m.avito.ru/moskva?q=b  https://www.avito.ru/spb/odezhda?q=a\nмусор"
        self.assertEqual(
            extract_avito_urls(text),
            ["https://avito.ru/spb/odezhda?q=a", "https://m.avito.ru/moskva?q=b", "https://www.avito.ru/spb/odezhda?q=a"],
        )


class TestAdminRadars(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "admin_radars.db")
        self.db_patch.start()
        await database.init_db()
        await database.get_or_create_user(config.ADMIN_ID, "admin", "Admin")
        await database.get_or_create_user(USER_ID, "user", "User")
        await database.grant_subscription(USER_ID, 30, "pro")
        self.setup_dispatcher(names=("admin", "searches"))

    async def asyncTearDown(self):
        await self.close_dispatcher()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def send_links(self, user_id, text):
        await self.dispatch_callback("add_search_prompt", user_id)
        await self.dispatch_message(user_id=user_id, text=text)

    def last_text(self):
        return [m for m in self.session.calls if type(m).__name__ in ("SendMessage", "EditMessageText")][-1].text

    async def test_admin_adds_many_radars_in_one_message(self):
        await self.send_links(config.ADMIN_ID, links(30) + "\n" + links(2))
        searches = await database.get_user_searches(config.ADMIN_ID)
        self.assertEqual(len(searches), 30, "duplicates inside the message are skipped")
        self.assertIn("Добавлено: <b>30</b>", self.last_text())

        await self.send_links(config.ADMIN_ID, links(3, start=28))
        self.assertEqual(len(await database.get_user_searches(config.ADMIN_ID)), 31)
        self.assertIn("Уже были: <b>2</b>", self.last_text())

    async def test_bulk_add_stops_at_the_limit(self):
        with patch.object(database, "MAX_SEARCHES_PER_USER", 5):
            await self.send_links(config.ADMIN_ID, links(8))
            self.assertEqual(len(await database.get_user_searches(config.ADMIN_ID)), 5)
            self.assertIn("Не влезли в лимит (5): <b>3</b>", self.last_text())

            await self.dispatch_callback("admin_radar_limit:self:10:0", config.ADMIN_ID)
            self.assertEqual(await database.get_user_max_radars(config.ADMIN_ID), 15)

    async def test_regular_user_cannot_bulk_add(self):
        await self.send_links(USER_ID, links(3))
        self.assertLessEqual(len(await database.get_user_searches(USER_ID)), 1)

    async def test_admin_raises_and_lowers_user_limit(self):
        self.assertEqual(await database.get_user_max_radars(USER_ID), 9)
        await self.dispatch_callback(f"admin_radar_limit:{USER_ID}:20:0", config.ADMIN_ID)
        self.assertEqual(await database.get_user_max_radars(USER_ID), 29)
        self.assertIn("из них +20 от админа", self.last_text())
        for _ in range(6):
            await self.dispatch_callback(f"admin_radar_limit:{USER_ID}:-5:0", config.ADMIN_ID)
        self.assertEqual(await database.get_user_max_radars(USER_ID), 9, "bonus never goes below zero")

        # The limit applies when adding radars, too.
        await database.change_user_extra_radars(USER_ID, 1)
        for i in range(10):
            await database.add_search(USER_ID, f"R{i}", f"https://www.avito.ru/moskva?q=u{i}")
        with self.assertRaises(database.RadarLimitError):
            await database.add_search(USER_ID, "R10", "https://www.avito.ru/moskva?q=u10")

    async def test_non_admin_cannot_change_limits(self):
        await self.dispatch_callback(f"admin_radar_limit:{USER_ID}:20:0", USER_ID)
        self.assertEqual(await database.get_user_max_radars(USER_ID), 9)


if __name__ == "__main__":
    unittest.main()

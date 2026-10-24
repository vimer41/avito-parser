import json
import logging
from typing import List, Optional, Dict, Any
import aiosqlite
from config import DB_PATH, ADMIN_ID

logger = logging.getLogger(__name__)

DEFAULT_SELLER_MESSAGE = "Здравствуйте! Товар ещё продаётся? Готов забрать сегодня."

async def init_db():
    """Инициализация базы данных и создание таблиц."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                default_template TEXT DEFAULT 'Здравствуйте! Товар ещё продаётся? Готов забрать сегодня.',
                is_allowed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Миграция: добавляем is_allowed если таблица уже создана
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_allowed INTEGER DEFAULT 0;")
        except Exception:
            pass

        # Администратор всегда имеет доступ
        await db.execute("UPDATE users SET is_allowed = 1 WHERE user_id = ?", (ADMIN_ID,))
        # Разрешаем доступ пользователю 455386428, который уже тестировал
        await db.execute("UPDATE users SET is_allowed = 1 WHERE user_id = 455386428")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                stop_words TEXT DEFAULT '[]',
                template TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                search_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (search_id, item_id),
                FOREIGN KEY (search_id) REFERENCES searches (id) ON DELETE CASCADE
            );
        """)

        await db.commit()
        logger.info("База данных успешно инициализирована")


async def get_or_create_user(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)

        is_allowed = 1 if user_id == ADMIN_ID else 0
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, default_template, is_allowed) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, first_name, DEFAULT_SELLER_MESSAGE, is_allowed)
        )
        await db.commit()

        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row)


async def is_user_allowed(user_id: int) -> bool:
    """Проверяет, одобрен ли доступ пользователю администратором."""
    if user_id == ADMIN_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_allowed FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            return bool(row[0])


async def set_user_allowed(user_id: int, is_allowed: int = 1):
    """Устанавливает статус разрешения доступа (1 = одобрен, 0 = заблокирован)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_allowed = ? WHERE user_id = ?", (is_allowed, user_id))
        await db.commit()


async def get_all_users() -> List[Dict[str, Any]]:
    """Возвращает список всех пользователей бота."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY created_at DESC") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def update_user_default_template(user_id: int, template: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET default_template = ? WHERE user_id = ?",
            (template.strip(), user_id)
        )
        await db.commit()


async def add_search(user_id: int, title: str, url: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO searches (user_id, title, url, stop_words) VALUES (?, ?, ?, ?)",
            (user_id, title, url, json.dumps([], ensure_ascii=False))
        )
        search_id = cursor.lastrowid
        await db.commit()
        return search_id


async def get_search(search_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM searches WHERE id = ?", (search_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            data = dict(row)
            data["stop_words"] = json.loads(data["stop_words"]) if data["stop_words"] else []
            return data


async def get_user_searches(user_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM searches WHERE user_id = ? ORDER BY id DESC", (user_id,)) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                data = dict(row)
                data["stop_words"] = json.loads(data["stop_words"]) if data["stop_words"] else []
                results.append(data)
            return results


async def count_user_searches(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM searches WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def toggle_search_active(search_id: int, user_id: int) -> Optional[bool]:
    """Переключает статус пауза/активен. Возвращает новое состояние is_active."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_active FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row[0] == 1 else 1

        await db.execute(
            "UPDATE searches SET is_active = ? WHERE id = ? AND user_id = ?",
            (new_status, search_id, user_id)
        )
        await db.commit()
        return bool(new_status)


async def delete_search(search_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id))
        await db.execute("DELETE FROM seen_items WHERE search_id = ?", (search_id,))
        await db.commit()
        return cursor.rowcount > 0


async def add_stop_word_to_search(search_id: int, word: str) -> bool:
    word = word.strip().lower()
    if not word:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT stop_words FROM searches WHERE id = ?", (search_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            stop_words = json.loads(row[0]) if row[0] else []

        if word not in stop_words:
            stop_words.append(word)
            await db.execute(
                "UPDATE searches SET stop_words = ? WHERE id = ?",
                (json.dumps(stop_words, ensure_ascii=False), search_id)
            )
            await db.commit()
        return True


async def remove_stop_word_from_search(search_id: int, word: str) -> bool:
    word = word.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT stop_words FROM searches WHERE id = ?", (search_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            stop_words = json.loads(row[0]) if row[0] else []

        if word in stop_words:
            stop_words.remove(word)
            await db.execute(
                "UPDATE searches SET stop_words = ? WHERE id = ?",
                (json.dumps(stop_words, ensure_ascii=False), search_id)
            )
            await db.commit()
            return True
        return False


async def update_search_template(search_id: int, user_id: int, template: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE searches SET template = ? WHERE id = ? AND user_id = ?",
            (template.strip() if template else None, search_id, user_id)
        )
        await db.commit()


async def get_all_active_searches() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*, u.default_template 
            FROM searches s 
            JOIN users u ON s.user_id = u.user_id 
            WHERE s.is_active = 1
        """) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                data = dict(row)
                data["stop_words"] = json.loads(data["stop_words"]) if data["stop_words"] else []
                results.append(data)
            return results


async def is_item_seen(search_id: int, item_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_items WHERE search_id = ? AND item_id = ?",
            (search_id, str(item_id))
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark_item_seen(search_id: int, item_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_items (search_id, item_id) VALUES (?, ?)",
            (search_id, str(item_id))
        )
        await db.commit()


async def mark_items_seen_bulk(search_id: int, item_ids: List[str]):
    if not item_ids:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        records = [(search_id, str(iid)) for iid in item_ids]
        await db.executemany(
            "INSERT OR IGNORE INTO seen_items (search_id, item_id) VALUES (?, ?)",
            records
        )
        await db.commit()

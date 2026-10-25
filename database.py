import json
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
import aiosqlite
from config import DB_PATH, ADMIN_ID, ADMIN_IDS, MAX_SEARCHES_PER_USER

logger = logging.getLogger(__name__)

MSK_TZ = timezone(timedelta(hours=3))

DEFAULT_SELLER_MESSAGE = "Здравствуйте! Товар ещё продаётся? Готов забрать сегодня."


def format_subscription_date(dt_val) -> str:
    """
    Форматирует дату окончания подписки в Московском часовом поясе (UTC+3, МСК)
    с указанием оставшегося времени (например: '26.10.2026 в 19:53 (МСК) · осталось 23 ч. 58 мин.').
    """
    if not dt_val:
        return "не активна"
    try:
        raw_str = str(dt_val).strip()
        if len(raw_str) >= 19:
            utc_dt = datetime.strptime(raw_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        elif len(raw_str) >= 16:
            utc_dt = datetime.strptime(raw_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        else:
            return raw_str

        msk_dt = utc_dt.astimezone(MSK_TZ)
        now_utc = datetime.now(timezone.utc)
        diff = utc_dt - now_utc

        formatted_date = msk_dt.strftime("%d.%m.%Y в %H:%M (МСК)")
        if diff.total_seconds() > 0:
            total_minutes = int(diff.total_seconds() // 60)
            hours = total_minutes // 60
            minutes = total_minutes % 60
            if hours >= 24:
                days = hours // 24
                rem_hours = hours % 24
                time_left = f"{days} дн. {rem_hours} ч."
            elif hours > 0:
                time_left = f"{hours} ч. {minutes} мин."
            else:
                time_left = f"{minutes} мин."
            return f"{formatted_date} (осталось {time_left})"
        else:
            return f"{formatted_date} (истекла)"
    except Exception as e:
        logger.warning(f"Ошибка форматирования даты подписки '{dt_val}': {e}")
        return str(dt_val)[:16]


async def init_db():
    """Инициализация базы данных и создание таблиц."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous = NORMAL;")
        await db.execute("PRAGMA busy_timeout = 5000;")
        await db.execute("PRAGMA cache_size = -2000;")  # Ограничение кэша в памяти 2 МБ (для 512 МБ RAM)
        await db.execute("PRAGMA temp_store = MEMORY;")
        await db.execute("PRAGMA auto_vacuum = INCREMENTAL;")
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

        # Миграция: добавляем is_allowed, subscription_until, trial_used если таблица уже создана
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_allowed INTEGER DEFAULT 0;")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN subscription_until TIMESTAMP;")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN trial_used INTEGER DEFAULT 0;")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN tariff_plan TEXT DEFAULT 'standart';")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                stop_words TEXT DEFAULT '[]',
                template TEXT,
                is_active INTEGER DEFAULT 1,
                min_discount_percent INTEGER DEFAULT 0,
                search_in_title_only INTEGER DEFAULT 0,
                max_age_minutes INTEGER DEFAULT 5,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            );
        """)

        try:
            await db.execute("ALTER TABLE searches ADD COLUMN min_discount_percent INTEGER DEFAULT 0;")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE searches ADD COLUMN search_in_title_only INTEGER DEFAULT 0;")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE searches ADD COLUMN max_age_minutes INTEGER DEFAULT 5;")
        except Exception:
            pass

        # Отключаем отслеживание скидок по дефолту на всех радарах (0% = выключено),
        # скидки работают только если пользователь сам явно включает их в меню радара.
        await db.execute("UPDATE searches SET min_discount_percent = 0 WHERE min_discount_percent IS NULL OR min_discount_percent = 5;")

        # По дефолту фильтр свежести на всех радарах до 5 минут (максимум 30 минут, без ограничений убрано)
        await db.execute("UPDATE searches SET max_age_minutes = 5 WHERE max_age_minutes IS NULL OR max_age_minutes <= 0 OR max_age_minutes > 30;")

        # Администраторы всегда имеют полный доступ
        for aid in ADMIN_IDS:
            await db.execute("UPDATE users SET is_allowed = 1, tariff_plan = 'pro' WHERE user_id = ?", (aid,))


        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                search_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                last_price INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (search_id, item_id),
                FOREIGN KEY (search_id) REFERENCES searches (id) ON DELETE CASCADE
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocked_sellers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                seller_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, seller_name) ON CONFLICT IGNORE,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            );
        """)

        await db.commit()
        logger.info("База данных успешно инициализирована")

    # Автоматическая очистка по запросу: удаляем johnpeterson92
    try:
        await purge_user_by_pattern("johnpeterson92")
    except Exception as e:
        logger.debug(f"Автоочистка johnpeterson92: {e}")



async def get_or_create_user(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                # Если пользователь уже был в БД, но еще не получил триал и не одобрен
                if not row["trial_used"] and row["subscription_until"] is None and not row["is_allowed"] and user_id not in ADMIN_IDS:
                    await db.execute("""
                        UPDATE users 
                        SET trial_used = 1,
                            subscription_until = datetime('now', '+1 day'),
                            tariff_plan = 'standart'
                        WHERE user_id = ?
                    """, (user_id,))
                    await db.commit()
                    async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as c2:
                        return dict(await c2.fetchone())
                return dict(row)


        is_adm = user_id in ADMIN_IDS
        if is_adm:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, default_template, is_allowed, tariff_plan) VALUES (?, ?, ?, ?, 1, 'pro')",
                (user_id, username, first_name, DEFAULT_SELLER_MESSAGE)
            )
        else:
            # Новым пользователям автоматически активируем 24-часовой бесплатный пробный период (тариф Standart, 1 радар)
            await db.execute("""
                INSERT OR IGNORE INTO users (user_id, username, first_name, default_template, is_allowed, tariff_plan, trial_used, subscription_until)
                VALUES (?, ?, ?, ?, 0, 'standart', 1, datetime('now', '+1 day'))
            """, (user_id, username, first_name, DEFAULT_SELLER_MESSAGE))
        await db.commit()

        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row)


async def get_user_max_radars(user_id: int) -> int:
    """
    Возвращает максимальное количество радаров для пользователя в зависимости от тарифа:
    - Администратор: MAX_SEARCHES_PER_USER
    - PRO (оплаченный или вечный): 9
    - Standart (оплаченный): 4
    - Бесплатный триал (новый пользователь): 1
    """
    if user_id in ADMIN_IDS:
        return MAX_SEARCHES_PER_USER

    user = await get_or_create_user(user_id)
    plan = (user.get("tariff_plan") or "standart").lower()
    is_allowed = bool(user.get("is_allowed"))
    trial_used = bool(user.get("trial_used"))

    if is_allowed or plan == "pro":
        return 9 if plan == "pro" else 4

    # Если бесплатный пробный период (новый пользователь без постоянного VIP)
    if trial_used and not is_allowed:
        return 1

    return 4 if plan == "standart" else 1


async def is_user_allowed(user_id: int) -> bool:
    """
    Проверяет, есть ли у пользователя активный доступ к функциям бота:
    1. Администратор (всегда полный доступ)
    2. Ручное одобрение (is_allowed = 1 для тестов/друзей)
    3. Действующая подписка или пробный период (subscription_until > datetime('now'))
    """
    if user_id in ADMIN_IDS:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT 1 FROM users 
            WHERE user_id = ? 
              AND (is_allowed = 1 OR subscription_until > datetime('now'))
        """, (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row)



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


async def add_search(user_id: int, title: str, url: str, search_in_title_only: int = 0, max_age_minutes: int = 5) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO searches (user_id, title, url, stop_words, search_in_title_only, min_discount_percent, max_age_minutes) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (user_id, title, url, json.dumps([], ensure_ascii=False), search_in_title_only, max_age_minutes)
        )
        search_id = cursor.lastrowid
        await db.commit()
        return search_id


async def update_search_max_age(search_id: int, user_id: int, max_age_minutes: int) -> bool:
    """Обновляет максимальный возраст объявлений (в минутах) для радара. Ограничение от 1 до 30 минут."""
    clamped_val = min(30, max(1, int(max_age_minutes)))
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE searches SET max_age_minutes = ? WHERE id = ? AND user_id = ?",
            (clamped_val, search_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_search_title_scope(search_id: int, user_id: int, search_in_title_only: int, new_url: str) -> bool:
    """Обновляет область выборки (только название / название+описание) и URL радара."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE searches SET search_in_title_only = ?, url = ? WHERE id = ? AND user_id = ?",
            (search_in_title_only, new_url, search_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


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


async def is_search_active(search_id: int) -> bool:
    """Проверяет, существует ли радар и активен ли он в базе данных."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM searches WHERE id = ? AND is_active = 1", (search_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def delete_search(search_id: int, user_id: int) -> bool:
    """
    Удаляет радар пользователя и связанные просмотренные лоты.
    Удаление seen_items выполняется ТОЛЬКО если радар действительно принадлежал пользователю.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id))
        if cursor.rowcount > 0:
            await db.execute("DELETE FROM seen_items WHERE search_id = ?", (search_id,))
            await db.commit()
            return True
        return False


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
    """
    Возвращает список активных радаров пользователей, у которых:
    - активен статус администратора, ИЛИ
    - выдан ручной доступ (is_allowed = 1), ИЛИ
    - действует подписка / триал (subscription_until > datetime('now'))
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        admin_placeholders = ",".join("?" for _ in ADMIN_IDS)
        query = f"""
            SELECT s.*, u.username, u.first_name, u.tariff_plan, u.subscription_until 
            FROM searches s 
            JOIN users u ON s.user_id = u.user_id 
            WHERE s.is_active = 1
              AND (u.user_id IN ({admin_placeholders}) 
                   OR u.is_allowed = 1 
                   OR u.subscription_until > datetime('now'))
        """
        async with db.execute(query, ADMIN_IDS) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                data = dict(row)
                data["stop_words"] = json.loads(data["stop_words"]) if data["stop_words"] else []
                results.append(data)
            return results


async def add_blocked_seller(user_id: int, seller_name: str) -> bool:
    name = seller_name.strip()
    if not name or name.lower() in {"продавец", "пользователь", "авито", "компания", "частное лицо"}:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO blocked_sellers (user_id, seller_name) VALUES (?, ?)",
            (user_id, name)
        )
        await db.commit()
        return True


async def get_blocked_sellers(user_id: int) -> set:
    """Возвращает set имен продавцов в нижнем регистре для быстрого отсечения O(1)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT seller_name FROM blocked_sellers WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return {r[0].lower().strip() for r in rows}


async def get_user_blocked_sellers_list(user_id: int) -> List[Dict[str, Any]]:
    """Возвращает список заблокированных продавцов с ID для управления в профиле."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, seller_name, created_at FROM blocked_sellers WHERE user_id = ? ORDER BY id DESC",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def remove_blocked_seller_by_id(user_id: int, block_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM blocked_sellers WHERE id = ? AND user_id = ?",
            (block_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0



async def get_seen_items_with_prices(search_id: int) -> Dict[str, int]:
    """Возвращает словарь {item_id: last_price} всех просмотренных объявлений в данном радаре."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT item_id, COALESCE(last_price, 0) FROM seen_items WHERE search_id = ?",
            (search_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return {str(r[0]): int(r[1] or 0) for r in rows}


async def get_seen_item_ids_for_search(search_id: int) -> set:
    """Возвращает множество всех ID объявлений, уже просмотренных в данном радаре."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT item_id FROM seen_items WHERE search_id = ?",
            (search_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return {r[0] for r in rows}


async def is_item_seen(search_id: int, item_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_items WHERE search_id = ? AND item_id = ?",
            (search_id, str(item_id))
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark_item_seen(search_id: int, item_id: str, price: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)
            ON CONFLICT(search_id, item_id) DO UPDATE SET last_price = excluded.last_price
            """,
            (search_id, str(item_id), int(price or 0))
        )
        await db.commit()


async def mark_items_seen_bulk(search_id: int, items_data: list):
    """
    Массовая запись просмотренных объявлений.
    Принимает либо список item_id (str), либо список кортежей (item_id, price).
    """
    if not items_data:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        records = []
        for entry in items_data:
            if isinstance(entry, (tuple, list)):
                iid, price = entry[0], entry[1]
            else:
                iid, price = entry, 0
            records.append((search_id, str(iid), int(price or 0)))

        await db.executemany(
            """
            INSERT INTO seen_items (search_id, item_id, last_price) VALUES (?, ?, ?)
            ON CONFLICT(search_id, item_id) DO UPDATE SET last_price = excluded.last_price
            """,
            records
        )
        await db.commit()


async def update_seen_item_price(search_id: int, item_id: str, new_price: int):
    """Обновляет зафиксированную цену объявления после срабатывания алерта о скидке."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE seen_items SET last_price = ? WHERE search_id = ? AND item_id = ?",
            (int(new_price or 0), search_id, str(item_id))
        )
        await db.commit()


async def update_search_discount_percent(search_id: int, user_id: int, percent: int) -> bool:
    """Обновляет порог скидки (в процентах) для отслеживания снижения цены в радаре."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE searches SET min_discount_percent = ? WHERE id = ? AND user_id = ?",
            (percent, search_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def cleanup_old_seen_items(days: int = 5) -> int:
    """
    Очистка старых объявлений из БД для оптимизации памяти и размера базы.
    Удаляет записи старше указанного количества дней и сиротские записи удаленных радаров, сжимает базу.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM seen_items WHERE created_at < datetime('now', '-' || ? || ' days')",
                (days,)
            )
            deleted_count = cursor.rowcount

            # Очищаем возможные сиротские записи от удаленных радаров
            orphan_cursor = await db.execute(
                "DELETE FROM seen_items WHERE search_id NOT IN (SELECT id FROM searches)"
            )
            orphans_deleted = orphan_cursor.rowcount
            deleted_count += orphans_deleted

            await db.commit()
            if deleted_count > 0:
                await db.execute("PRAGMA incremental_vacuum;")
                logger.info(f"Очистка БД: удалено {deleted_count} записей seen_items (из них сирот: {orphans_deleted})")
            return deleted_count
    except Exception as e:
        logger.error(f"Ошибка при очистке устаревших записей БД: {e}")
        return 0


async def get_system_stats() -> Dict[str, int]:
    """Возвращает общую статистику по пользователям, радарам и объявлениям."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            total_users = row[0] if row else 0
        admin_placeholders = ",".join("?" for _ in ADMIN_IDS)
        query_allowed = f"""
            SELECT COUNT(*) FROM users 
            WHERE user_id IN ({admin_placeholders}) 
               OR is_allowed = 1 
               OR subscription_until > datetime('now')
        """
        async with db.execute(query_allowed, ADMIN_IDS) as cur:
            row = await cur.fetchone()
            allowed_users = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM searches") as cur:
            row = await cur.fetchone()
            total_searches = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM searches WHERE is_active = 1") as cur:
            row = await cur.fetchone()
            active_searches = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM seen_items") as cur:
            row = await cur.fetchone()
            total_seen = row[0] if row else 0

        return {
            "total_users": total_users,
            "allowed_users": allowed_users,
            "total_searches": total_searches,
            "active_searches": active_searches,
            "total_seen": total_seen,
        }


async def activate_trial(user_id: int) -> Dict[str, Any]:
    """Активирует пробный период 24 часа. Строго 1 раз на пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT trial_used, subscription_until FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return {"success": False, "message": "Пользователь не найден."}
            if row["trial_used"]:
                return {"success": False, "message": "Пробный период уже был использован ранее."}

        await db.execute("""
            UPDATE users 
            SET trial_used = 1,
                subscription_until = datetime('now', '+1 day')
            WHERE user_id = ?
        """, (user_id,))
        await db.commit()

        async with db.execute("SELECT subscription_until FROM users WHERE user_id = ?", (user_id,)) as cur:
            r = await cur.fetchone()
            until = r[0] if r else ""

        return {"success": True, "message": "Пробный период 24 часа успешно активирован!", "until": until}


async def grant_subscription(user_id: int, days: int = 30, tariff_plan: Optional[str] = None) -> str:
    """Выдаёт или продлевает подписку на N дней и опционально обновляет тариф."""
    async with aiosqlite.connect(DB_PATH) as db:
        if tariff_plan:
            await db.execute("""
                UPDATE users 
                SET subscription_until = CASE 
                    WHEN subscription_until > datetime('now') 
                    THEN datetime(subscription_until, '+' || ? || ' days')
                    ELSE datetime('now', '+' || ? || ' days')
                END,
                tariff_plan = ?,
                is_allowed = 1
                WHERE user_id = ?
            """, (days, days, tariff_plan.lower(), user_id))
        else:
            await db.execute("""
                UPDATE users 
                SET subscription_until = CASE 
                    WHEN subscription_until > datetime('now') 
                    THEN datetime(subscription_until, '+' || ? || ' days')
                    ELSE datetime('now', '+' || ? || ' days')
                END,
                is_allowed = 1
                WHERE user_id = ?
            """, (days, days, user_id))
        await db.commit()

        async with db.execute("SELECT subscription_until FROM users WHERE user_id = ?", (user_id,)) as cur:
            r = await cur.fetchone()
            return r[0] if r else ""


async def deactivate_user_searches(user_id: int):
    """Ставит все радары пользователя на паузу (например, если бот заблокирован)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE searches SET is_active = 0 WHERE user_id = ?", (user_id,))
        await db.commit()


def create_database_backup_file() -> Optional[str]:
    """
    Создает консистентный бинарный бэкап базы данных SQLite через встроенный API backup().
    Возвращает путь к созданному файлу бэкапа.
    """
    import os
    import sqlite3
    import tempfile
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"avito_radar_backup_{timestamp}.db"
    backup_path = os.path.join(tempfile.gettempdir(), backup_filename)

    try:
        src = sqlite3.connect(DB_PATH)
        dest = sqlite3.connect(backup_path)
        src.backup(dest)
        dest.close()
        src.close()
        return backup_path
    except Exception as e:
        logger.error(f"Ошибка при создании бэкапа SQLite: {e}")
        return None


async def purge_user_by_pattern(pattern: str) -> Dict[str, Any]:
    """
    Полностью и безвозвратно удаляет пользователя и все его данные из БД
    (аккаунт, радары, историю лотов, стоп-слова, черный список продавцов).
    Поиск ведется по user_id, username (@...) или first_name.
    """
    clean = pattern.strip().lstrip("@").lower()
    if not clean:
        return {"success": False, "deleted": 0, "message": "Паттерн не может быть пустым"}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if clean.isdigit():
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (int(clean),)) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute("""
                SELECT * FROM users 
                WHERE LOWER(COALESCE(username, '')) = ? 
                   OR LOWER(COALESCE(username, '')) LIKE ? 
                   OR LOWER(COALESCE(first_name, '')) LIKE ?
            """, (clean, f"%{clean}%", f"%{clean}%")) as cur:
                rows = await cur.fetchall()

        if not rows:
            return {"success": False, "deleted": 0, "message": f"Пользователь '{pattern}' не найден в базе данных."}

        deleted_users = []
        for r in rows:
            uid = r["user_id"]
            uname = r["username"] or "без username"
            fname = r["first_name"] or ""
            deleted_users.append(f"{fname} (@{uname}, ID:{uid})")

            # Удаляем историю лотов по радарам пользователя
            await db.execute("""
                DELETE FROM seen_items 
                WHERE search_id IN (SELECT id FROM searches WHERE user_id = ?)
            """, (uid,))
            # Удаляем радары пользователя
            await db.execute("DELETE FROM searches WHERE user_id = ?", (uid,))
            # Удаляем заблокированных продавцов
            await db.execute("DELETE FROM blocked_sellers WHERE user_id = ?", (uid,))
            # Удаляем самого пользователя
            await db.execute("DELETE FROM users WHERE user_id = ?", (uid,))

        await db.commit()

        # Также подчищаем логи от упоминаний удаленного пользователя
        try:
            from logger_setup import sanitize_logs_for_user
            sanitize_logs_for_user(clean)
        except Exception as e:
            logger.warning(f"Ошибка очистки логов при удалении пользователя {pattern}: {e}")

        logger.info(f"🗑 Полностью удалены данные пользователей: {', '.join(deleted_users)}")
        return {
            "success": True,
            "deleted": len(deleted_users),
            "users": deleted_users,
            "message": f"Успешно удалено: {', '.join(deleted_users)}"
        }





import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any
import aiosqlite
from config import DB_PATH, ADMIN_ID, ADMIN_IDS, MAX_SEARCHES_PER_USER

logger = logging.getLogger(__name__)

MSK_TZ = timezone(timedelta(hours=3))

DEFAULT_SELLER_MESSAGE = "Здравствуйте! Товар ещё продаётся? Готов забрать сегодня."


class RadarLimitError(ValueError):
    """The user's radar quota was reached during an atomic insert."""


async def ensure_columns(db, table: str, columns: dict[str, str]):
    """Idempotent additive migrations; real SQLite errors must fail startup."""
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        existing = {row[1] for row in await cursor.fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
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

        await ensure_columns(db, "users", {
            "is_allowed": "INTEGER DEFAULT 0", "subscription_until": "TIMESTAMP",
            "trial_used": "INTEGER DEFAULT 0", "tariff_plan": "TEXT DEFAULT 'standart'",
            "subscription_revision": "INTEGER NOT NULL DEFAULT 0",
        })

        # Финансовые записи не удаляются вместе с пользователем: они нужны для
        # идемпотентности событий Telegram, поддержки и возвратов.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stars_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                tariff_plan TEXT NOT NULL CHECK(tariff_plan IN ('standart', 'pro')),
                total_amount INTEGER NOT NULL CHECK(total_amount > 0),
                days INTEGER NOT NULL CHECK(days > 0),
                status TEXT NOT NULL DEFAULT 'pending',
                pre_checkout_query_id TEXT,
                approved_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                telegram_payment_charge_id TEXT UNIQUE,
                provider_payment_charge_id TEXT,
                paid_at TIMESTAMP,
                subscription_before TIMESTAMP,
                tariff_before TEXT,
                allowed_before INTEGER,
                trial_before INTEGER,
                subscription_after TIMESTAMP,
                tariff_after TEXT,
                revision_after INTEGER,
                refund_requested_at TIMESTAMP,
                refund_admin_id INTEGER,
                refunded_at TIMESTAMP,
                refund_needs_review INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS stars_orders_user ON stars_orders(user_id, id)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_subscription_actions (
                token TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                tariff_plan TEXT NOT NULL,
                days INTEGER NOT NULL,
                page INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                applied_at TIMESTAMP,
                subscription_until TIMESTAMP
            )
        """)

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

        await ensure_columns(db, "searches", {
            "min_discount_percent": "INTEGER DEFAULT 0", "search_in_title_only": "INTEGER DEFAULT 0",
            "max_age_minutes": "INTEGER DEFAULT 5",
        })

        # Отключаем отслеживание скидок по дефолту на всех радарах (0% = выключено),
        # скидки работают только если пользователь сам явно включает их в меню радара.
        await db.execute("UPDATE searches SET min_discount_percent = 0 WHERE min_discount_percent IS NULL;")

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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                event_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claimed_at TIMESTAMP,
                lease_token TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(search_id, item_id, event_key),
                FOREIGN KEY (search_id) REFERENCES searches (id) ON DELETE CASCADE
            );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS outbox_ready ON notification_outbox(available_at, claimed_at);")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS outbox_one_pending_item ON notification_outbox(search_id, item_id);")

        await db.commit()
        logger.info("База данных успешно инициализирована")

async def get_or_create_user(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                # Если пользователь уже был в БД, но еще не получил триал и не одобрен
                if not row["trial_used"] and row["subscription_until"] is None and not row["is_allowed"] and user_id not in ADMIN_IDS:
                    await db.execute("""
                        UPDATE users 
                        SET trial_used = 1,
                            subscription_until = datetime('now', '+5 hours'),
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
            # Новым пользователям автоматически активируем 5-часовой бесплатный пробный период (тариф Standart, 1 радар)
            await db.execute("""
                INSERT OR IGNORE INTO users (user_id, username, first_name, default_template, is_allowed, tariff_plan, trial_used, subscription_until)
                VALUES (?, ?, ?, ?, 0, 'standart', 1, datetime('now', '+5 hours'))
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
    2. Ручное бессрочное одобрение (is_allowed = 1, subscription_until IS NULL)
    3. Действующая подписка или пробный период (subscription_until > datetime('now'))
    """
    if user_id in ADMIN_IDS:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT 1 FROM users 
            WHERE user_id = ? 
              AND ((is_allowed = 1 AND subscription_until IS NULL) OR subscription_until > datetime('now'))
        """, (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row)



async def set_user_allowed(user_id: int, is_allowed: int = 1):
    """Устанавливает статус разрешения доступа (1 = одобрен, 0 = заблокирован)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_allowed = ?, subscription_until = NULL, trial_used = 1, subscription_revision = subscription_revision + 1 WHERE user_id = ?",
            (int(bool(is_allowed)), user_id),
        )
        await db.commit()


async def get_all_users() -> List[Dict[str, Any]]:
    """Возвращает список всех пользователей бота."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY created_at DESC") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Read without registering an unknown user or activating a trial."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def find_users_by_query(query: str) -> List[Dict[str, Any]]:
    """Поиск пользователей по ID, @username или имени (регистронезависимо)."""
    clean_q = query.strip().lstrip("@")
    if not clean_q:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        results = []
        if clean_q.isdigit():
            target_id = int(clean_q)
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (target_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    results.append(dict(row))

        like_str = f"%{clean_q}%"
        async with db.execute("""
            SELECT * FROM users 
            WHERE username LIKE ? OR first_name LIKE ? OR CAST(user_id AS TEXT) LIKE ?
            ORDER BY user_id DESC
            LIMIT 20
        """, (like_str, like_str, like_str)) as cur:
            rows = await cur.fetchall()
            existing_ids = {u["user_id"] for u in results}
            for r in rows:
                if r["user_id"] not in existing_ids:
                    results.append(dict(r))
        return results


async def grant_vip_access(user_id: int, tariff_plan: str = "pro") -> Dict[str, Any]:
    """Предоставляет бессрочный доступ (VIP) с указанным тарифом."""
    plan = tariff_plan.lower() if tariff_plan in ("standart", "pro") else "pro"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users 
            SET is_allowed = 1,
                subscription_until = NULL,
                tariff_plan = ?,
                trial_used = 1,
                subscription_revision = subscription_revision + 1
            WHERE user_id = ?
        """, (plan, user_id))
        await db.commit()
    user = await get_user(user_id)
    return user or {}


async def reset_user_trial(user_id: int) -> Dict[str, Any]:
    """Сбрасывает и активирует 5-часовой пробный период для пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users 
            SET trial_used = 1,
                subscription_until = datetime('now', '+5 hours'),
                tariff_plan = 'standart',
                is_allowed = 0,
                subscription_revision = subscription_revision + 1
            WHERE user_id = ?
        """, (user_id,))
        await db.commit()
    user = await get_user(user_id)
    return user or {}


async def update_user_default_template(user_id: int, template: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET default_template = ? WHERE user_id = ?",
            (template.strip(), user_id)
        )
        await db.commit()


async def add_search(user_id: int, title: str, url: str, search_in_title_only: int = 0, max_age_minutes: int = 5) -> int:
    max_radars = await get_user_max_radars(user_id)
    max_age_minutes = min(30, max(1, int(max_age_minutes)))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        db.row_factory = aiosqlite.Row
        if user_id not in ADMIN_IDS:
            async with db.execute("""
                SELECT * FROM users WHERE user_id = ?
                  AND ((is_allowed = 1 AND subscription_until IS NULL) OR subscription_until > datetime('now'))
            """, (user_id,)) as cursor:
                user = await cursor.fetchone()
                if user is None:
                    raise PermissionError("Доступ к мониторингу истёк или отозван")
                plan = (user["tariff_plan"] or "standart").lower()
                max_radars = 9 if plan == "pro" else 4 if user["is_allowed"] else 1 if user["trial_used"] else 4
        async with db.execute("SELECT COUNT(*) FROM searches WHERE user_id = ?", (user_id,)) as cursor:
            if (await cursor.fetchone())[0] >= max_radars:
                raise RadarLimitError(f"Достигнут лимит радаров ({max_radars})")
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


async def can_deliver_to_search(search_id: int, user_id: int) -> bool:
    placeholders = ",".join("?" for _ in ADMIN_IDS)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"""
            SELECT 1 FROM searches s JOIN users u ON u.user_id = s.user_id
            WHERE s.id = ? AND s.user_id = ? AND s.is_active = 1
              AND (u.user_id IN ({placeholders})
                   OR (u.is_allowed = 1 AND u.subscription_until IS NULL)
                   OR u.subscription_until > datetime('now'))
        """, (search_id, user_id, *ADMIN_IDS)) as cursor:
            return await cursor.fetchone() is not None


async def delete_search(search_id: int, user_id: int) -> bool:
    """
    Удаляет радар пользователя и связанные просмотренные лоты.
    Удаление seen_items выполняется ТОЛЬКО если радар действительно принадлежал пользователю.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id))
        if cursor.rowcount > 0:
            await db.execute("DELETE FROM seen_items WHERE search_id = ?", (search_id,))
            await db.execute("DELETE FROM notification_outbox WHERE search_id = ?", (search_id,))
            await db.commit()
            return True
        return False


async def add_stop_word_to_search(search_id: int, word: str, user_id: int) -> bool:
    word = word.strip().lower()
    if not word:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT stop_words FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            stop_words = json.loads(row[0]) if row[0] else []

        if word not in stop_words:
            stop_words.append(word)
            await db.execute(
                "UPDATE searches SET stop_words = ? WHERE id = ? AND user_id = ?",
                (json.dumps(stop_words, ensure_ascii=False), search_id, user_id)
            )
            await db.commit()
        return True


async def remove_stop_word_from_search(search_id: int, word: str, user_id: int) -> bool:
    word = word.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT stop_words FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            stop_words = json.loads(row[0]) if row[0] else []

        if word in stop_words:
            stop_words.remove(word)
            await db.execute(
                "UPDATE searches SET stop_words = ? WHERE id = ? AND user_id = ?",
                (json.dumps(stop_words, ensure_ascii=False), search_id, user_id)
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
                    OR (u.is_allowed = 1 AND u.subscription_until IS NULL)
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
               OR (is_allowed = 1 AND subscription_until IS NULL)
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
    """Активирует пробный период 5 часов. Строго 1 раз на пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
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
                subscription_until = datetime('now', '+5 hours')
            WHERE user_id = ?
        """, (user_id,))
        await db.commit()

        async with db.execute("SELECT subscription_until FROM users WHERE user_id = ?", (user_id,)) as cur:
            r = await cur.fetchone()
            until = r[0] if r else ""

        return {"success": True, "message": "Пробный период 5 часов успешно активирован!", "until": until}


async def grant_subscription_in_transaction(db, user_id: int, days: int, tariff_plan: Optional[str] = None) -> str:
    """Shared atomic update; the caller owns the transaction and commit."""
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError("days must be a positive integer")
    if tariff_plan is not None and tariff_plan.lower() not in ("standart", "pro"):
        raise ValueError("Unknown tariff plan")
    cursor = await db.execute("""
        UPDATE users
        SET subscription_until = datetime(
                CASE WHEN subscription_until > datetime('now') THEN subscription_until ELSE datetime('now') END,
                '+' || ? || ' days'),
            tariff_plan = COALESCE(?, tariff_plan),
            is_allowed = 1, trial_used = 1,
            subscription_revision = subscription_revision + 1
        WHERE user_id = ?
    """, (days, tariff_plan.lower() if tariff_plan else None, user_id))
    if cursor.rowcount != 1:
        raise ValueError("Пользователь не найден")
    async with db.execute("SELECT subscription_until FROM users WHERE user_id = ?", (user_id,)) as cursor:
        return (await cursor.fetchone())[0]


async def grant_subscription(user_id: int, days: int = 30, tariff_plan: Optional[str] = None) -> str:
    """Выдаёт или продлевает подписку на N дней и опционально обновляет тариф."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        until = await grant_subscription_in_transaction(db, user_id, days, tariff_plan)
        await db.commit()
        return until


async def create_admin_subscription_action(admin_id: int, user_id: int, plan: str, days: int, page: int = 0) -> str:
    if admin_id not in ADMIN_IDS or user_id in ADMIN_IDS:
        raise PermissionError("Нельзя изменять подписку администратора")
    if plan not in ("standart", "pro") or days not in (1, 7, 14, 30, 90, 180, 365):
        raise ValueError("Неверный тариф или срок")
    token = secrets.token_hex(16)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO admin_subscription_actions(token, admin_id, user_id, tariff_plan, days, page)
            SELECT ?, ?, user_id, ?, ?, ? FROM users WHERE user_id = ?
        """, (token, admin_id, plan, days, max(0, page), user_id))
        if cursor.rowcount != 1:
            raise ValueError("Пользователь не найден")
        await db.commit()
    return token


async def apply_admin_subscription_action(token: str, admin_id: int) -> Dict[str, Any]:
    if admin_id not in ADMIN_IDS:
        raise PermissionError("У вас нет прав администратора")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("""
            SELECT * FROM admin_subscription_actions WHERE token = ? AND admin_id = ?
              AND (applied_at IS NOT NULL OR created_at > datetime('now', '-1 day'))
        """, (token, admin_id)) as cursor:
            row = await cursor.fetchone()
        if not row or row["user_id"] in ADMIN_IDS:
            raise ValueError("Подтверждение истекло или недоступно")
        result = dict(row)
        result["applied"] = row["applied_at"] is None
        if result["applied"]:
            result["subscription_until"] = await grant_subscription_in_transaction(
                db, row["user_id"], row["days"], row["tariff_plan"],
            )
            await db.execute("""
                UPDATE admin_subscription_actions SET applied_at = datetime('now'), subscription_until = ?
                WHERE token = ?
            """, (result["subscription_until"], token))
            await db.commit()
        return result


async def deactivate_user_searches(user_id: int):
    """Ставит все радары пользователя на паузу (например, если бот заблокирован)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE searches SET is_active = 0 WHERE user_id = ?", (user_id,))
        await db.commit()


async def enqueue_notification(search_id: int, user_id: int, item_id: str, event_key: str, payload: dict) -> bool:
    """Persist a notification before delivery; never mark it seen on enqueue."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        admin_placeholders = ",".join("?" for _ in ADMIN_IDS)
        async with db.execute(f"""
            SELECT 1 FROM searches s JOIN users u ON u.user_id = s.user_id
            WHERE s.id = ? AND s.user_id = ? AND s.is_active = 1
              AND (u.user_id IN ({admin_placeholders})
                   OR (u.is_allowed = 1 AND u.subscription_until IS NULL)
                   OR u.subscription_until > datetime('now'))
        """, (search_id, user_id, *ADMIN_IDS)) as cursor:
            if await cursor.fetchone() is None:
                return False
        if event_key == "new":
            async with db.execute("SELECT 1 FROM seen_items WHERE search_id = ? AND item_id = ?", (search_id, str(item_id))) as cursor:
                if await cursor.fetchone() is not None:
                    return False
        else:
            async with db.execute("SELECT last_price FROM seen_items WHERE search_id = ? AND item_id = ?", (search_id, str(item_id))) as cursor:
                seen = await cursor.fetchone()
            if seen is None or seen[0] != payload.get("old_price"):
                return False
        async with db.execute("SELECT COUNT(*) FROM notification_outbox") as cursor:
            if (await cursor.fetchone())[0] >= 3000:
                logger.warning("Outbox is full; the item remains unseen for a later scan")
                return False
        cursor = await db.execute("""
            INSERT OR IGNORE INTO notification_outbox (search_id, user_id, item_id, event_key, payload)
            VALUES (?, ?, ?, ?, ?)
        """, (search_id, user_id, str(item_id), event_key, json.dumps(payload, ensure_ascii=False)))
        await db.commit()
        return cursor.rowcount > 0


async def get_pending_new_item_ids(search_id: int) -> set[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT item_id FROM notification_outbox WHERE search_id = ? AND event_key = 'new'", (search_id,)) as cursor:
            return {row[0] for row in await cursor.fetchall()}


async def claim_notification(lease_seconds: int = 300) -> Optional[Dict[str, Any]]:
    """Atomically lease one ready row, recovering abandoned deliveries after a crash."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("""
            SELECT * FROM notification_outbox
            WHERE available_at <= datetime('now')
              AND (claimed_at IS NULL OR claimed_at <= datetime('now', '-' || ? || ' seconds'))
            ORDER BY id LIMIT 1
        """, (lease_seconds,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        token = secrets.token_hex(16)
        await db.execute("""
            UPDATE notification_outbox
            SET claimed_at = datetime('now'), lease_token = ?, attempts = attempts + 1 WHERE id = ?
        """, (token, row["id"]))
        await db.commit()
        result = dict(row)
        result.update(lease_token=token, attempts=row["attempts"] + 1)
        return result


async def acknowledge_notification(notification_id: int, lease_token: str, price: int) -> bool:
    """Commit the seen/price state only after Telegram acknowledged delivery."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT search_id, user_id, item_id FROM notification_outbox WHERE id = ? AND lease_token = ?", (notification_id, lease_token)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return False
        await db.execute("""
            INSERT INTO seen_items (search_id, item_id, last_price)
            SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM searches WHERE id = ? AND user_id = ?)
            ON CONFLICT(search_id, item_id) DO UPDATE SET last_price = excluded.last_price
        """, (row[0], row[2], int(price or 0), row[0], row[1]))
        await db.execute("DELETE FROM notification_outbox WHERE id = ? AND lease_token = ?", (notification_id, lease_token))
        await db.commit()
        return True


async def release_notification(notification_id: int, lease_token: str, delay_seconds: float = 0):
    delay = min(86400, max(0, int(delay_seconds) + (delay_seconds % 1 > 0)))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE notification_outbox SET claimed_at = NULL, lease_token = NULL,
                available_at = datetime('now', '+' || ? || ' seconds')
            WHERE id = ? AND lease_token = ?
        """, (delay, notification_id, lease_token))
        await db.commit()


async def discard_notification(notification_id: int, lease_token: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM notification_outbox WHERE id = ? AND lease_token = ?", (notification_id, lease_token))
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
    Удаление только по точному user_id или username, без LIKE и имени человека.
    """
    clean = pattern.strip().lstrip("@").lower()
    if not clean:
        return {"success": False, "deleted": 0, "message": "Паттерн не может быть пустым"}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        db.row_factory = aiosqlite.Row
        if clean.isdigit():
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (int(clean),)) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute("""
                SELECT * FROM users 
                WHERE LOWER(COALESCE(username, '')) = ?
            """, (clean,)) as cur:
                rows = await cur.fetchall()

        if not rows:
            return {"success": False, "deleted": 0, "message": f"Пользователь '{pattern}' не найден в базе данных."}
        if len(rows) != 1:
            return {"success": False, "deleted": 0, "message": "Несколько пользователей с этим username. Укажите точный ID."}
        if rows[0]["user_id"] in ADMIN_IDS:
            return {"success": False, "deleted": 0, "message": "Удаление администратора запрещено."}

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
            await db.execute("DELETE FROM notification_outbox WHERE user_id = ?", (uid,))
            await db.execute("DELETE FROM searches WHERE user_id = ?", (uid,))
            # Удаляем заблокированных продавцов
            await db.execute("DELETE FROM blocked_sellers WHERE user_id = ?", (uid,))
            # Удаляем самого пользователя
            await db.execute("DELETE FROM admin_subscription_actions WHERE user_id = ? AND applied_at IS NULL", (uid,))
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





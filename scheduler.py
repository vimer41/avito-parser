import asyncio
import datetime
import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, parse_qs
import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import config
import database
from freshness import freshness_status
from request_budget import get_network_block_state, RequestDeferred, RequestBudgetExceeded
from parser import AvitoParser, AvitoItem
from collector import collect_source
from source_cache import SOURCE_SNAPSHOTS, SourceSnapshot

logger = logging.getLogger(__name__)

LAST_SCAN_TIMESTAMP = 0

# ── 1. Задача для очереди доставки (Outbox Task) ──────────────────────────
@dataclass
class NotificationTask:
    user_id: int
    radar_id: int
    search_title: str
    item: AvitoItem
    user_label: str = ""
    is_price_drop: bool = False
    old_price: Optional[int] = None
    drop_percent: Optional[int] = None
    discovered_at: float = 0.0
    freshness_verified: bool = False


async def enqueue_notification_task(task: NotificationTask) -> bool:
    task.discovered_at = task.discovered_at or time.time()
    if not task.is_price_drop:
        radar = await database.get_search(task.radar_id)
        task.freshness_verified = task.freshness_verified or freshness_status(
            task.item.date, task.item.timestamp, radar.get("max_age_minutes") if radar else 5,
            now=task.discovered_at,
        ) is True
    event_key = f"drop:{task.old_price}:{task.item.raw_price}" if task.is_price_drop else "new"
    return await database.enqueue_notification(task.radar_id, task.user_id, task.item.item_id, event_key, asdict(task))


# ── 2. Высокопроизводительный TTL/LRU кэш Telegram file_id (RAM) ─────────
class TTLCache:
    """
    Легковесный in-memory TTL/LRU кэш для Telegram file_id (в RAM).
    Экономит 100% повторных загрузок одного и того же лота для нескольких пользователей
    и предотвращает утечки памяти.
    """
    def __init__(self, maxsize: int = 5000, ttl_seconds: float = 86400.0):
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self._data: Dict[str, Tuple[str, float]] = {}

    def get(self, key: str) -> Optional[str]:
        now = time.monotonic()
        item = self._data.get(key)
        if item is None:
            return None
        val, expires_at = item
        if now < expires_at:
            return val
        self._data.pop(key, None)
        return None

    def set(self, key: str, value: str):
        now = time.monotonic()
        if len(self._data) >= self.maxsize:
            expired = [k for k, (_, exp) in self._data.items() if now >= exp]
            for k in expired:
                self._data.pop(k, None)
            if len(self._data) >= self.maxsize:
                for k in list(self._data.keys())[:500]:
                    self._data.pop(k, None)
        self._data[key] = (value, now + self.ttl)

    def __len__(self) -> int:
        return len(self._data)


PHOTO_FILE_ID_CACHE = TTLCache(maxsize=5000, ttl_seconds=86400.0)

def get_cached_photo_file_id(item_id: str) -> Optional[str]:
    return PHOTO_FILE_ID_CACHE.get(item_id)

def set_cached_photo_file_id(item_id: str, file_id: str):
    PHOTO_FILE_ID_CACHE.set(item_id, file_id)


# ── 3. Блокировки по item_id (Cache Stampede Protection) ─────────────────
ITEM_LOCKS: Dict[str, asyncio.Lock] = {}
ITEM_LOCKS_MUTEX = asyncio.Lock()

async def get_item_lock(item_id: str) -> asyncio.Lock:
    """Гарантирует атомарную блокировку на уровне item_id для устранения Cache Stampede."""
    async with ITEM_LOCKS_MUTEX:
        lock = ITEM_LOCKS.get(item_id)
        if lock is None:
            if len(ITEM_LOCKS) >= 3000:
                # Безопасная ротация: удаляем только неактивные (разблокированные) локи
                unlocked_keys = [k for k, l in ITEM_LOCKS.items() if not l.locked() and not getattr(l, "_waiters", None)]
                for k in unlocked_keys[:1000]:
                    ITEM_LOCKS.pop(k, None)
            lock = asyncio.Lock()
            ITEM_LOCKS[item_id] = lock
        return lock


# ── 4. Асинхронный клиент загрузки статики с Anycast CDN c80.img.avito.st ─
IMAGE_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(12)
_IMAGE_SESSION: Optional[aiohttp.ClientSession] = None
_IMAGE_SESSION_LOCK = asyncio.Lock()

async def get_image_session() -> aiohttp.ClientSession:
    """Возвращает общий пул HTTP-соединений aiohttp для скачивания картинок лотов."""
    global _IMAGE_SESSION
    if _IMAGE_SESSION is None or _IMAGE_SESSION.closed:
        async with _IMAGE_SESSION_LOCK:
            if _IMAGE_SESSION is None or _IMAGE_SESSION.closed:
                connector = aiohttp.TCPConnector(
                    limit=50,
                    limit_per_host=25,
                    ttl_dns_cache=300,
                    use_dns_cache=True,
                    enable_cleanup_closed=True
                )
                timeout = aiohttp.ClientTimeout(total=4.0, connect=1.5, sock_read=3.0)
                _IMAGE_SESSION = aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                        "Accept": "image/jpeg, image/png;q=0.9, */*;q=0.1",
                        "Accept-Encoding": "gzip, deflate",
                        "Referer": "https://www.avito.ru/",
                    }
                )
    return _IMAGE_SESSION

async def close_image_session():
    """Корректно закрывает aiohttp сессию при остановке приложения."""
    global _IMAGE_SESSION
    if _IMAGE_SESSION and not _IMAGE_SESSION.closed:
        await _IMAGE_SESSION.close()
        _IMAGE_SESSION = None
    await SOURCE_SNAPSHOTS.close()

async def download_image_bytes(raw_url: str) -> Optional[bytes]:
    """
    Асинхронная загрузка изображения через dedicated aiohttp session:
    - Принудительно направляет запрос в глобальный Anycast CDN c80.img.avito.st;
    - Запрашивает 'Accept: image/jpeg, image/png';
    - Без блокирующих потоков curl_cffi;
    - При сбое пробует оригинальный URL.
    """
    if not raw_url:
        return None

    session = await get_image_session()

    urls_to_try = []
    c80_url = re.sub(r"https?://[^.]+\.img\.avito\.st/", "https://c80.img.avito.st/", raw_url)
    urls_to_try.append(c80_url)
    if raw_url != c80_url:
        urls_to_try.append(raw_url)

    for u in urls_to_try:
        try:
            parsed = urlparse(u)
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".img.avito.st") or parsed.username or parsed.password:
                continue
            budget = get_network_block_state(config.PROXY_URL)
            async with IMAGE_DOWNLOAD_SEMAPHORE, budget.request_slot_async():
                async with session.get(u, proxy=config.PROXY_URL, allow_redirects=False) as resp:
                    budget.record_response(resp.status)
                    budget.register_throttle_response(resp.status, resp.headers)
                    if resp.status == 200:
                        content = await resp.read()
                        if len(content) > 500:
                            return content
        except (RequestDeferred, RequestBudgetExceeded):
            return None
        except Exception as dl_err:
            logger.debug(f"Сбой загрузки фото по {u[:45]}: {dl_err}")

    return None

# ── 5. Доставка из постоянной SQLite Outbox, ограничение 20 отправок/сек ──
NOTIFICATION_SEND_LOCK = asyncio.Lock()
NEXT_NOTIFICATION_SEND = 0.0


async def limit_notification_send_rate():
    global NEXT_NOTIFICATION_SEND
    async with NOTIFICATION_SEND_LOCK:
        wait = NEXT_NOTIFICATION_SEND - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        NEXT_NOTIFICATION_SEND = time.monotonic() + 0.05


def get_last_scan_info() -> str:
    """Возвращает читаемый статус времени последней проверки для UI."""
    global LAST_SCAN_TIMESTAMP
    if not LAST_SCAN_TIMESTAMP:
        return "менее 1 мин. назад"
    diff = int(time.time() - LAST_SCAN_TIMESTAMP)
    if diff < 10:
        return "только что"
    if diff < 60:
        return f"{diff} сек. назад"
    return f"{diff // 60} мин. назад"



def build_notification_text(
    item: AvitoItem,
    search_title: str,
    is_price_drop: bool = False,
    old_price: Optional[int] = None,
    drop_percent: Optional[int] = None
) -> str:
    """Форматирует стильный и чистый текст карточки товара с безопасным лимитом длины (до 1000 символов)."""
    clean_title = html.escape(item.title)
    clean_price = html.escape(item.price)
    clean_search = search_title.replace("\xa0", " ").strip()

    # Бейджи статуса объявления (резерв / продвижение)
    badges = []
    if getattr(item, "is_reserved", False):
        badges.append("🔒 <b>В РЕЗЕРВЕ</b>")
    if getattr(item, "is_promotion", False):
        badges.append("⚡️ <b>ПРОДВИНУТО</b>")
    badge_header = f" [{ ' · '.join(badges) }]" if badges else ""

    if is_price_drop:
        diff_rub = (old_price - item.raw_price) if (old_price and item.raw_price) else 0
        diff_str = f" · выгода {diff_rub:,} ₽".replace(",", " ") if diff_rub > 0 else ""
        old_price_str = f"{old_price:,} ₽".replace(",", " ") if old_price else ""
        header_lines = [
            f"📉 <b>СНИЖЕНИЕ ЦЕНЫ · AVITO RADAR [-{drop_percent}%]</b>{badge_header}",
            f"🏷 <b>{clean_price}</b> (было <s>{old_price_str}</s>{diff_str})",
            f"📦 <b>{clean_title}</b>",
            "────────────────────"
        ]
    else:
        discount_badge = ""
        if getattr(item, "was_lowered", False) and getattr(item, "discount_percent", None):
            discount_badge = f" 🏷 <i>(-{item.discount_percent}%)</i>"
        header_lines = [
            f"⚡️ <b>НОВЫЙ ЛОТ · AVITO RADAR</b>{badge_header}",
            f"🏷 <b>{clean_price}</b>{discount_badge} · <b>{clean_title}</b>",
            "────────────────────"
        ]

    detail_lines = []
    # Локация / Адрес
    address = item.address
    if not address:
        m = re.search(r"в\s+([А-Яа-яЁё\s\-]+)$", clean_search)
        if m:
            address = m.group(1).strip()
    if address:
        detail_lines.append(f"📍 <b>Адрес:</b> {html.escape(address)}")

    # Состояние и доставка
    details = []
    if item.condition:
        details.append(f"✨ {html.escape(item.condition)}")
    if item.delivery:
        details.append(f"🚚 {html.escape(item.delivery)}")
    if details:
        detail_lines.append(f"📦 <b>Детали:</b> {' · '.join(details)}")

    # Преимущества и бейджи (Скидка на доставку, 14 дней на возврат, Торг и т.д.)
    if getattr(item, "badges", None):
        clean_badges = [html.escape(b) for b in item.badges[:3]]
        detail_lines.append(f"🛡 <b>Преимущества:</b> {' · '.join(clean_badges)}")

    # Просмотры (если подгружены)
    if getattr(item, "views_total", None) is not None:
        today_str = f" (+{item.views_today} сегодня)" if getattr(item, "views_today", None) is not None else ""
        detail_lines.append(f"👁 <b>Просмотры:</b> {item.views_total}{today_str}")

    # Продавец
    if item.seller:
        detail_lines.append(f"👤 <b>Продавец:</b> {html.escape(item.seller)}")

    # Время
    if item.date:
        detail_lines.append(f"🕒 <b>Опубликовано:</b> {html.escape(item.date)}")

    footer_lines = [
        "────────────────────",
        f"🎯 <b>Радар:</b> <i>{html.escape(clean_search)}</i>"
    ]

    # Безопасный динамический расчет длины для Telegram caption (жесткий лимит Telegram = 1024 символа для фото)
    base_chars = sum(len(l) + 1 for l in (header_lines + detail_lines + footer_lines))
    max_desc_budget = max(0, 960 - base_chars)

    desc_lines = []
    if item.description and max_desc_budget >= 40:
        raw_desc = item.description.strip()
        allowed_len = min(280, max_desc_budget)
        if len(raw_desc) > allowed_len:
            raw_desc = raw_desc[: allowed_len - 3].rstrip() + "..."
        desc_lines = ["", f"📝 <i>«{html.escape(raw_desc)}»</i>"]

    all_lines = header_lines + detail_lines + desc_lines + footer_lines
    return "\n".join(all_lines)


def build_notification_keyboard(item_url: str, search_id: int, has_seller: bool = True) -> InlineKeyboardMarkup:
    """Инлайн-кнопки под карточкой товара."""
    row1 = [InlineKeyboardButton(text="↗️ Открыть на Авито", url=item_url)]
    row2 = [InlineKeyboardButton(text="🛡 Стоп-слово", callback_data=f"add_stop_from_item:{search_id}")]
    if has_seller:
        row2.append(InlineKeyboardButton(text="🚷 Забанить продавца", callback_data=f"ban_seller:{search_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])



def plural_form(n: int, form1: str, form2: str, form5: str) -> str:
    """Склонение существительных с числительными (1 лот, 2 лота, 5 лотов)."""
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 10 < n_abs < 20:
        return f"{n} {form5}"
    if 1 < n1 < 5:
        return f"{n} {form2}"
    if n1 == 1:
        return f"{n} {form1}"
    return f"{n} {form5}"


def format_item_age(date_str: Optional[str], timestamp: Optional[int] = None) -> str:
    """Возвращает наглядное описание возраста объявления (например: '42 мин. назад', '2 ч. назад')."""
    if timestamp:
        age_seconds = max(0, int(time.time() - timestamp))
        if age_seconds < 60:
            return f"{age_seconds} сек. назад"
        elif age_seconds < 3600:
            return f"{age_seconds // 60} мин. назад"
        elif age_seconds < 86400:
            return f"{age_seconds // 3600} ч. назад"
        else:
            return f"{age_seconds // 86400} дн. назад"
    return date_str or "возраст не указан"


def is_ad_fresh_enough(date_str: Optional[str], timestamp: Optional[int] = None,
                      max_age_minutes: Optional[int] = 5) -> bool:
    """Compatibility boolean; collection distinguishes unknown publication ages."""
    return freshness_status(date_str, timestamp, max_age_minutes) is True


def is_stop_word_matched(item: AvitoItem, stop_words: list) -> Optional[str]:
    """Проверяет, содержит ли объявление стоп-слово в названии или описании."""
    if not stop_words:
        return None

    text_to_check = f"{item.title} {item.description or ''}".lower()
    for word in stop_words:
        w = word.strip().lower()
        if w and w in text_to_check:
            return w
    return None


def get_user_display_name(radar: Dict) -> str:
    """Форматирует читаемое имя владельца радара: @username, Имя или ID."""
    username = radar.get("username")
    first_name = radar.get("first_name")
    user_id = radar.get("user_id")

    parts = []
    if username:
        parts.append(f"@{username}")
    if first_name:
        parts.append(first_name)

    if parts:
        return ", ".join(parts)
    return f"ID:{user_id}"


async def get_next_pipeline_task(urgent_queue: asyncio.Queue, pipeline_queue: asyncio.Queue):
    """Legacy helper only; active monitoring uses DeadlineScheduler, not this queue."""
    try:
        return urgent_queue.get_nowait(), True
    except asyncio.QueueEmpty:
        pass
    urgent_get = asyncio.create_task(urgent_queue.get())
    pipeline_get = asyncio.create_task(pipeline_queue.get())
    tasks = (urgent_get, pipeline_get)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if not urgent_get.cancelled() and urgent_get.exception() is None:
        if not pipeline_get.cancelled() and pipeline_get.exception() is None:
            pipeline_queue.put_nowait(pipeline_get.result())
        return urgent_get.result(), True
    return pipeline_get.result(), False


async def send_ad_to_user(
    bot: Bot,
    parser: AvitoParser,
    user_id: int,
    search_id: int,
    search_title: str,
    it: AvitoItem,
    user_label: str = "",
    is_price_drop: bool = False,
    old_price: Optional[int] = None,
    drop_percent: Optional[int] = None
) -> bool:
    """Cached Telegram photo or immediate text; no image HTTP on the critical path."""
    caption = build_notification_text(
        it, search_title,
        is_price_drop=is_price_drop,
        old_price=old_price,
        drop_percent=drop_percent
    )
    keyboard = build_notification_keyboard(it.url, search_id, has_seller=bool(it.seller_name))
    owner_str = f" [{user_label}]" if user_label else f" ID:{user_id}"
    delivery_tag = f"СНИЖЕНИЕ ЦЕНЫ (-{drop_percent}%)" if is_price_drop else "Доставлено"

    async def prepare_send() -> bool:
        await limit_notification_send_rate()
        # A Telegram rate-limit wait may outlive the radar or access permission.
        return await database.can_deliver_to_search(search_id, user_id)

    # 1. Быстрая попытка через кэш telegram file_id (без блокировки)
    cached_fid = get_cached_photo_file_id(it.item_id)
    if cached_fid:
        for attempt in range(2):
            try:
                if not await prepare_send():
                    return False
                await bot.send_photo(
                    chat_id=user_id,
                    photo=cached_fid,
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                logger.info(f"     ⚡️ [Радар #{search_id}{owner_str}] {delivery_tag} (file_id кэш): {it.price} · «{it.title[:35]}»")
                return True
            except TelegramForbiddenError:
                raise
            except TelegramRetryAfter as retry_err:
                raise
            except Exception as cache_err:
                logger.debug(f"file_id кэш сбой ({cache_err}), отправляем текст...")
                break

    # 3. Фолбэк: отправляем текстом (если фото недоступно или Telegram отклонил фото)
    for attempt in range(2):
        try:
            if not await prepare_send():
                return False
            await bot.send_message(
                chat_id=user_id,
                text=caption,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
            logger.info(f"     🚀 [Радар #{search_id}{owner_str}] Доставлено (текст): {it.price} · «{it.title[:35]}»")
            return True
        except TelegramForbiddenError:
            raise
        except TelegramRetryAfter as retry_err:
            raise
        except Exception as text_err:
            logger.error(f"❌ [РАДАР #{search_id}] Не удалось доставить лот #{it.item_id}: {text_err}")
            return False

    return False


class ParserPool:
    """Пул независимых сессий парсера для параллельного выполнения запросов без коллизий."""
    def __init__(self, size: int, proxy_url: Optional[str] = None):
        self.parsers: List[AvitoParser] = [
            AvitoParser(proxy=proxy_url) for _ in range(max(1, size))
        ]

    def get_parser(self, index: int) -> AvitoParser:
        return self.parsers[index % len(self.parsers)]


async def process_url_group(
    bot: Bot,
    parser: AvitoParser,
    clean_url: str,
    radars_for_url: List[dict],
    worker_id: int = 1
):
    """Collect an exact source once, then apply each subscriber's filters.

    All pages share a finite deadline; accepted items enter the persistent outbox
    before they can become seen. Unknown publication ages remain unseen.
    """
    try:
        fetch_t0 = time.monotonic()
        parser.last_coverage_gap = False
        max_age = max((min(30, max(1, int(radar.get("max_age_minutes") or 5))) for radar in radars_for_url), default=5)
        collection = await collect_source(parser, clean_url, max_age)
        if collection is None:
            logger.info("Scan result: ok=0 elapsed_ms=%d items=0 blocked=%d failure=%s",
                        int((time.monotonic() - fetch_t0) * 1000), int(bool(parser.is_blocked())), getattr(parser, "last_failure", None))
            return False
        items = list(collection.items)
        fetch_ms = collection.elapsed_ms
        SOURCE_SNAPSHOTS.put(clean_url, SourceSnapshot(collection.title, collection.items, collection.captured_at))
        logger.info("Scan result: ok=1 elapsed_ms=%d items=%d blocked=0 pages=%d coverage_gap=%d", fetch_ms, len(items), collection.pages, int(collection.coverage_gap))
        w_tag = f"[W{worker_id}] " if config.MAX_CONCURRENT_WORKERS > 1 else ""

        if not items:
            for radar in radars_for_url:
                if radar.get("initialized_at") is None:
                    await database.initialize_search_snapshot(radar["id"], [])
            global LAST_SCAN_TIMESTAMP
            LAST_SCAN_TIMESTAMP = time.time()
            logger.info(f"📡 {w_tag}[ПАРСЕР] По URL «{clean_url[:60]}...»: страница получена за {fetch_ms} мс, лотов в каталоге не найдено")
            return True

        # ⚡️ ПРИОРИТЕТНАЯ ДОСТАВКА ЛОТОВ:
        # 0: Админы и PRO-пользователи (получают уведомления мгновенно первыми)
        # 1: Standart тариф
        # 2: Остальные
        def get_radar_priority(r):
            uid = r.get("user_id")
            plan = (r.get("tariff_plan") or "").lower()
            if uid in config.ADMIN_IDS or plan == "pro":
                return 0
            if plan == "standart":
                return 1
            return 2

        radars_for_url.sort(key=get_radar_priority)

        # Раздаем полученные объявления каждому радару, подписанному на этот URL
        for idx, radar in enumerate(radars_for_url):
            radar_id = radar["id"]
            user_id = radar["user_id"]
            search_title = radar["title"]

            # 🛡 Защита от фантомного спама: проверяем, что радар не был удален во время скачивания
            if not await database.is_search_active(radar_id) or not await database.is_user_allowed(user_id):
                continue

            if radar.get("initialized_at") is None and await database.initialize_search_snapshot(
                radar_id, [(item.item_id, item.raw_price) for item in items],
            ):
                continue

            stop_words = radar.get("stop_words", [])
            user_str = get_user_display_name(radar)
            source_tag = f"{fetch_ms} мс" if idx == 0 else "⚡ кэш запроса"

            # Получаем актуальный черный список продавцов пользователя
            blocked_sellers = await database.get_blocked_sellers(user_id)

            current_item_ids = [it.item_id for it in items]
            seen_dict = await database.get_seen_items_with_prices(radar_id, item_ids=current_item_ids)
            pending_ids = await database.get_pending_new_item_ids(radar_id, item_ids=current_item_ids)
            new_items = [it for it in items if it.item_id not in seen_dict and it.item_id not in pending_ids]

            # 📉 Проверка снижения цены (Price Drop Alert) для PRO-тарифа и администраторов
            user_plan = (radar.get("tariff_plan") or "").lower()
            is_pro = (user_id in config.ADMIN_IDS or user_plan == "pro")
            min_drop = int(radar.get("min_discount_percent") or 0)

            if is_pro and min_drop > 0:
                for it in items:
                    if it.item_id in seen_dict and it.raw_price:
                        old_p = seen_dict[it.item_id]
                        new_p = it.raw_price
                        # Если цена действительно снизилась
                        if old_p > 0 and new_p < old_p:
                            drop_pct = round(((old_p - new_p) / old_p) * 100)
                            if drop_pct >= min_drop:
                                # Проверяем стоп-слова
                                if is_stop_word_matched(it, stop_words):
                                    continue
                                # Проверяем ЧС продавцов
                                if it.seller_name and it.seller_name.lower().strip() in blocked_sellers:
                                    continue

                                logger.info(
                                    f"  {w_tag}📉 [СНИЖЕНИЕ ЦЕНЫ] Радар #{radar_id} [{user_str}] «{search_title}»: "
                                    f"«{it.title[:30]}» {old_p:,} -> {new_p:,} ₽ (-{drop_pct}%, порог {min_drop}%)"
                                )
                                # Цена фиксируется только после подтверждённой доставки.
                                await enqueue_notification_task(NotificationTask(
                                    user_id=user_id,
                                    radar_id=radar_id,
                                    search_title=search_title,
                                    item=it,
                                    user_label=user_str,
                                    is_price_drop=True,
                                    old_price=old_p,
                                    drop_percent=drop_pct
                                ))

            if not new_items:
                logger.info(f"  {w_tag}🔹 Радар #{radar_id} [{user_str}] «{search_title}»: проверено {len(items)} лотов [{source_tag}] (новых нет)")
                continue

            # Rejected items may be marked seen; deliverable items remain pending in SQLite.
            candidates = {it.item_id: it.raw_price or 0 for it in new_items}

            # 🏷 Проверка области выборки (Только в названиях)
            title_only = bool(radar.get("search_in_title_only", 0))
            if title_only:
                parsed_u = urlparse(radar.get("url", ""))
                q_val = parse_qs(parsed_u.query).get("q", [""])[0].strip()
                if q_val:
                    keywords = [w.lower() for w in re.findall(r"[\w\d]+", q_val) if len(w) >= 2]
                    if keywords:
                        matching_title_items = []
                        for it in new_items:
                            t_lower = it.title.lower()
                            if any(kw in t_lower for kw in keywords):
                                matching_title_items.append(it)
                            else:
                                logger.info(
                                    f"  {w_tag}🏷 [ТОЛЬКО В НАЗВАНИИ] Отсеян лот «{it.title[:35]}» (нет '{q_val}' в заголовке) [{user_str}]"
                                )
                        new_items = matching_title_items

            if not new_items:
                await database.mark_items_seen_bulk(radar_id, list(candidates.items()))
                continue

            # 1. Отбираем только действительно свежие объявления с учетом настройки max_age_minutes радара (1..30 мин, дефолт 5)
            max_age_raw = radar.get("max_age_minutes")
            if not max_age_raw or int(max_age_raw) <= 0:
                max_age_min = 5
            else:
                max_age_min = min(30, int(max_age_raw))

            ages = {item.item_id: freshness_status(item.date, item.timestamp, max_age_min,
                                                 now=collection.discovery_times.get(item.item_id, collection.captured_at)) for item in new_items}
            unknown_ids = {item_id for item_id, status in ages.items() if status is None}
            fresh_items = [item for item in new_items if ages[item.item_id] is True]
            deliverable_items = [
                it for it in fresh_items
                if not is_stop_word_matched(it, stop_words)
                and not (it.seller_name and it.seller_name.lower().strip() in blocked_sellers)
            ]
            deliverable_ids = {it.item_id for it in deliverable_items}
            rejected = [(item_id, price) for item_id, price in candidates.items()
                        if item_id not in deliverable_ids and item_id not in unknown_ids]
            if rejected:
                await database.mark_items_seen_bulk(radar_id, rejected)
            if unknown_ids:
                logger.info("Радар #%s: %s объявлений без достоверного возраста оставлены для повторной проверки", radar_id, len(unknown_ids))
            fresh_items = deliverable_items

            if not fresh_items:
                ages = [format_item_age(it.date, getattr(it, "timestamp", None)) for it in new_items[:2]]
                age_summary = ", ".join(ages)
                if len(new_items) > 2:
                    age_summary += f", и ещё {len(new_items) - 2}"
                items_label = plural_form(len(new_items), "лот", "лота", "лотов")
                limit_label = f"порог {max_age_min} мин." if max_age_min > 0 else "без ограничений"
                logger.info(
                    f"  {w_tag}🔹 Радар #{radar_id} [{user_str}] «{search_title}»: {items_label} в ротации [{source_tag}] "
                    f"(отсеян по возрасту: {age_summary}; {limit_label})"
                )
                continue

            # Логируем большую пачку свежих лотов (отправляем все подходящие лоты)
            if len(fresh_items) > 15:
                fresh_label = plural_form(len(fresh_items), "свежий лот", "свежих лота", "свежих лотов")
                logger.info(
                    f"  {w_tag}📦 Радар #{radar_id} [{user_str}] «{search_title}»: пачка из {fresh_label}"
                )

            fresh_label = plural_form(len(fresh_items), "СВЕЖИЙ ЛОТ", "СВЕЖИХ ЛОТА", "СВЕЖИХ ЛОТОВ")
            logger.info(f"  {w_tag}🔥 Радар #{radar_id} [{user_str}] «{search_title}»: НАЙДЕНО {fresh_label}!")

            # Отправляем в хронологическом порядке (снизу вверх)
            for it in reversed(fresh_items):
                # 2. Проверяем стоп-слова
                matched_stop = is_stop_word_matched(it, stop_words)
                if matched_stop:
                    logger.info(f"     🛡 Отсеяно по стоп-слову «{matched_stop}» [{user_str}]: «{it.title[:30]}»")
                    continue

                # 3. Проверяем черный список продавцов
                if it.seller_name and it.seller_name.lower().strip() in blocked_sellers:
                    logger.info(f"     🚷 Отсеян продавец «{it.seller_name}» из ЧС [{user_str}]")
                    continue

                # Помещаем задачу в асинхронную очередь Outbox (мониторинг не ждёт отправки!)
                await enqueue_notification_task(NotificationTask(
                    user_id=user_id,
                    radar_id=radar_id,
                    search_title=search_title,
                    item=it,
                    user_label=user_str,
                    discovered_at=collection.discovery_times.get(it.item_id, collection.captured_at),
                    freshness_verified=True,
                ))

        LAST_SCAN_TIMESTAMP = time.time()
        return True

    except Exception as e:
        logger.error(f"❌ [ГРУППА URL] Ошибка при обработке URL «{clean_url[:60]}...»: {e}", exc_info=True)
        return False


async def monitoring_worker(bot: Bot, parser: Optional[AvitoParser] = None):
    from monitoring import run_monitoring
    await run_monitoring(bot, parser)


async def deliver_notification(bot: Bot, parser: AvitoParser, row: dict) -> bool:
    """Deliver a leased row once; failed deliveries stay in the persistent queue."""
    notification_id, token = row["id"], row["lease_token"]
    try:
        payload = json.loads(row["payload"])
        payload["item"] = AvitoItem(**payload["item"])
        task = NotificationTask(**payload)
    except (ValueError, KeyError, TypeError):
        logger.error("Invalid outbox payload #%s", notification_id)
        await database.discard_notification(notification_id, token)
        return False

    try:
        search = await database.get_search(task.radar_id)
        if not search or search["user_id"] != task.user_id or not await database.is_user_allowed(task.user_id):
            await database.discard_notification(notification_id, token)
            return False
        if not search["is_active"]:
            await database.release_notification(notification_id, token, 30)
            return False
        if task.is_price_drop:
            user = await database.get_or_create_user(task.user_id)
            if not config.is_admin(task.user_id) and (user.get("tariff_plan") or "").lower() != "pro":
                await database.discard_notification(notification_id, token)
                return False
            threshold = int(search.get("min_discount_percent") or 0)
            if threshold <= 0 or (task.drop_percent or 0) < threshold:
                await database.discard_notification(notification_id, token)
                return False
        if not task.is_price_drop and not task.freshness_verified:
            queued_at = task.discovered_at or row.get("created_at") or time.time()
            if isinstance(queued_at, str):
                queued_at = datetime.datetime.fromisoformat(queued_at).replace(tzinfo=datetime.timezone.utc).timestamp()
            status = freshness_status(task.item.date, task.item.timestamp, search.get("max_age_minutes"), now=queued_at)
            if status is False:
                await database.discard_notification(notification_id, token)
                await database.mark_item_seen(task.radar_id, task.item.item_id, task.item.raw_price or 0)
                return False
            if status is None:
                # Legacy unknown-age tasks were never verified. Return them to
                # discovery without seen state so later metadata can qualify.
                await database.discard_notification(notification_id, token)
                return False
        blocked = await database.get_blocked_sellers(task.user_id)
        if is_stop_word_matched(task.item, search.get("stop_words", [])) or (task.item.seller_name and task.item.seller_name.lower().strip() in blocked):
            await database.discard_notification(notification_id, token)
            return False
        delivered = await send_ad_to_user(
            bot, parser, task.user_id, task.radar_id, task.search_title, task.item,
            user_label=task.user_label, is_price_drop=task.is_price_drop,
            old_price=task.old_price, drop_percent=task.drop_percent,
        )
        if delivered:
            acknowledged = await database.acknowledge_notification(notification_id, token, task.item.raw_price or 0)
            if acknowledged:
                logger.info("Delivery result: notification=%s discovery_to_send_ms=%d attempts=%s", notification_id,
                            int((time.time() - (task.discovered_at or time.time())) * 1000), row["attempts"])
            return acknowledged
        await database.release_notification(notification_id, token, min(300, 2 ** min(row["attempts"], 8)))
        return False
    except asyncio.CancelledError:
        await asyncio.shield(database.release_notification(notification_id, token))
        raise
    except TelegramForbiddenError:
        await database.deactivate_user_searches(task.user_id)
        await database.discard_notification(notification_id, token)
        return False
    except TelegramRetryAfter as exc:
        await database.release_notification(notification_id, token, exc.retry_after + 1)
        return False
    except Exception:
        logger.exception("Outbox delivery failed #%s", notification_id)
        await database.release_notification(notification_id, token, 10)
        return False


async def notification_worker(bot: Bot, parser: AvitoParser, worker_id: int = 1):
    logger.info("📬 Persistent Outbox #%s started", worker_id)
    while True:
        try:
            row = await database.claim_notification()
            if row is None:
                await asyncio.sleep(0.5)
                continue
            await deliver_notification(bot, parser, row)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox worker #%s failed", worker_id)
            await asyncio.sleep(1)


async def supervised_monitoring_worker(bot: Bot, parser: AvitoParser):
    """
    Супервизор фонового мониторинга:
    - Запускает бесконечный мониторинг;
    - При любом непредвиденном падении отправляет алерт администраторам и перезапускает процесс.
    """
    try:
        while True:
            try:
                await monitoring_worker(bot, parser)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.critical(f"💥 [СУПЕРВИЗОР] Фатальное падение сервиса мониторинга: {e}", exc_info=True)
                alert_text = (
                    f"🚨 <b>Критический сбой сервиса мониторинга!</b>\n\n"
                    f"<code>{html.escape(str(e)[:300])}</code>\n\n"
                    f"<i>Автоматический перезапуск мониторинга через 15 секунд...</i>"
                )
                for aid in config.ADMIN_IDS:
                    try:
                        await bot.send_message(chat_id=aid, text=alert_text, parse_mode="HTML")
                    except Exception:
                        pass
                await asyncio.sleep(15)
    finally:
        parser.close()


def start_background_services(bot: Bot) -> List[asyncio.Task]:
    """
    Запускает все фоновые сервисы:
    1. Пул воркеров очереди отправки уведомлений (notification_worker x 6)
    2. Мониторинг Авито под супервизором (supervised_monitoring_worker)
    Возвращает список фоновых задач для управления их жизненным циклом.
    """
    shared_parser = AvitoParser(proxy=config.PROXY_URL)
    NUM_NOTIF_WORKERS = 6
    notif_tasks = [
        asyncio.create_task(notification_worker(bot, shared_parser, worker_id=i + 1))
        for i in range(NUM_NOTIF_WORKERS)
    ]
    monitor_task = asyncio.create_task(supervised_monitoring_worker(bot, shared_parser))
    return [*notif_tasks, monitor_task]




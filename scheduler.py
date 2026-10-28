import asyncio
import datetime
import html
import inspect
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, parse_qs
import aiohttp
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database
from freshness import freshness_status, publication_age_seconds
from request_budget import get_network_block_state, RequestDeferred, RequestBudgetExceeded
from runtime_metrics import METRICS
from parser import AvitoParser, AvitoItem
from collector import SourceCollection, collect_source
from source_cache import SOURCE_SNAPSHOTS, SourceSnapshot

logger = logging.getLogger(__name__)

LAST_SCAN_TIMESTAMP = 0
NOTIFICATION_WAKEUP_EVENT = asyncio.Event()


def notify_outbox_wakeup() -> None:
    try:
        NOTIFICATION_WAKEUP_EVENT.set()
    except Exception:
        pass


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
    is_catchup: bool = False
    radar_titles: List[str] = field(default_factory=list)


async def enqueue_notification_task(task: NotificationTask) -> bool:
    """Compatibility helper for single-task enqueueing; price drops are disabled."""
    if task.is_price_drop:
        return False
    task.discovered_at = task.discovered_at or time.time()
    radar = await database.get_search(task.radar_id)
    task.freshness_verified = task.freshness_verified or (
        freshness_status(
            task.item.date,
            task.item.timestamp,
            radar.get("max_age_minutes") if radar else 5,
            now=task.discovered_at,
        )
        is True
    )
    enqueued = await database.enqueue_notification(
        task.radar_id,
        task.user_id,
        task.item.item_id,
        "new",
        asdict(task),
    )
    if enqueued:
        notify_outbox_wakeup()
    return enqueued


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
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB


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
                    enable_cleanup_closed=True,
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
                    },
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
    - Проверяет Content-Type image/* и размер <= 5 МБ;
    - При сбое пробует оригинальный URL.
    """
    if not raw_url:
        return None

    session = await get_image_session()

    urls_to_try = []
    c80_url = re.sub(r"https?://[^.]+\.img\.avito.st/", "https://c80.img.avito.st/", raw_url)
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
                        ctype = (resp.headers.get("Content-Type") or "").lower()
                        if ctype and not ctype.startswith("image/") and "octet-stream" not in ctype:
                            continue
                        clen = resp.headers.get("Content-Length")
                        if clen and clen.isdigit() and int(clen) > MAX_IMAGE_BYTES:
                            continue
                        content = await resp.content.read(MAX_IMAGE_BYTES + 1)
                        if 500 < len(content) <= MAX_IMAGE_BYTES:
                            return content
        except (RequestDeferred, RequestBudgetExceeded):
            return None
        except Exception as dl_err:
            logger.debug(f"Сбой загрузки фото по {u[:45]}: {dl_err}")

    return None


# ── 5. Доставка из постоянной SQLite Outbox, leaky-bucket 25/с + 1/с на чат ─
NOTIFICATION_SEND_LOCK = asyncio.Lock()
NEXT_NOTIFICATION_SEND = 0.0
CHAT_NEXT_SEND: Dict[int, float] = {}


def reset_notification_rate_limiter() -> None:
    global NEXT_NOTIFICATION_SEND
    NEXT_NOTIFICATION_SEND = 0.0
    CHAT_NEXT_SEND.clear()


async def limit_notification_send_rate(chat_id: Optional[int] = None):
    """Leaky-bucket без burst: глобальный лимит TELEGRAM_GLOBAL_RATE (25/с)
    плюс интервал TELEGRAM_CHAT_INTERVAL (1.0 с) для одного чата.
    """
    global NEXT_NOTIFICATION_SEND
    global_interval = 1.0 / max(1.0, float(config.TELEGRAM_GLOBAL_RATE))
    chat_interval = max(0.0, float(config.TELEGRAM_CHAT_INTERVAL))

    async with NOTIFICATION_SEND_LOCK:
        now = time.monotonic()
        if len(CHAT_NEXT_SEND) > 2000:
            cutoff = now - 600.0
            stale = [cid for cid, ts in CHAT_NEXT_SEND.items() if ts < cutoff]
            for cid in stale:
                CHAT_NEXT_SEND.pop(cid, None)

        target_at = max(now, NEXT_NOTIFICATION_SEND)
        if chat_id is not None and chat_interval > 0:
            chat_due = CHAT_NEXT_SEND.get(chat_id, 0.0)
            target_at = max(target_at, chat_due)

        NEXT_NOTIFICATION_SEND = target_at + global_interval
        if chat_id is not None and chat_interval > 0:
            CHAT_NEXT_SEND[chat_id] = target_at + chat_interval

        wait = target_at - now

    if wait > 0:
        await asyncio.sleep(wait)


async def _await_rate_limit(user_id: int) -> None:
    """Invoke limit_notification_send_rate while remaining compatible with 0-arg test patches."""
    try:
        sig = inspect.signature(limit_notification_send_rate)
        if len(sig.parameters) == 0:
            await limit_notification_send_rate()
            return
    except Exception:
        pass
    try:
        await limit_notification_send_rate(user_id)
    except TypeError:
        await limit_notification_send_rate()


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


def format_radar_footer(search_title: str = "", radar_titles: Optional[List[str]] = None) -> str:
    titles = [t.replace("\xa0", " ").strip() for t in (radar_titles or []) if t and t.strip()]
    if not titles and search_title:
        titles = [search_title.replace("\xa0", " ").strip()]
    if not titles:
        titles = ["Радар"]
    # Deduplicate preserving order
    unique_titles: List[str] = []
    for t in titles:
        if t not in unique_titles:
            unique_titles.append(t)

    if len(unique_titles) == 1:
        return f"🎯 Радар: <b>{html.escape(unique_titles[0])}</b>"
    if len(unique_titles) == 2:
        return f"🎯 Радары: <b>{html.escape(unique_titles[0])}</b>, <b>{html.escape(unique_titles[1])}</b>"
    extra = len(unique_titles) - 2
    return f"🎯 Радары: <b>{html.escape(unique_titles[0])}</b>, <b>{html.escape(unique_titles[1])}</b> (+{extra})"


def build_notification_text(
    item: AvitoItem,
    search_title: str = "",
    is_price_drop: bool = False,
    old_price: Optional[int] = None,
    drop_percent: Optional[int] = None,
    *,
    radar_titles: Optional[List[str]] = None,
    is_catchup: bool = False,
    possible_duplicate_after_crash: bool = False,
) -> str:
    """Форматирует стильный и чистый текст карточки товара с безопасным лимитом длины (до 1000 символов)."""
    clean_title = html.escape(item.title)
    clean_price = html.escape(item.price)
    clean_search = (search_title or (radar_titles[0] if radar_titles else "")).replace("\xa0", " ").strip()

    # Бейджи статуса объявления (резерв / продвижение)
    badges = []
    if getattr(item, "is_reserved", False):
        badges.append("🔒 <b>В РЕЗЕРВЕ</b>")
    if getattr(item, "is_promotion", False):
        badges.append("⚡️ <b>ПРОДВИНУТО</b>")
    badge_header = f" [{ ' · '.join(badges) }]" if badges else ""

    discount_badge = ""
    if getattr(item, "was_lowered", False) and getattr(item, "discount_percent", None):
        discount_badge = f" 🏷 <i>(-{item.discount_percent}%)</i>"

    header_lines = [
        f"⚡️ <b>НОВЫЙ ЛОТ · AVITO RADAR</b>{badge_header}",
        f"🏷 <b>{clean_price}</b>{discount_badge} · <b>{clean_title}</b>",
    ]
    if is_catchup:
        header_lines.append("🕒 найдено после паузы")
    if possible_duplicate_after_crash:
        header_lines.append("🔁 повторное уведомление после перезапуска")
    header_lines.append("────────────────────")

    detail_lines = []
    # Локация / Адрес
    address = item.address
    if not address and clean_search:
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
        format_radar_footer(clean_search, radar_titles),
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


def build_notification_keyboard(
    item_url: str,
    target_id: int,
    has_seller: bool = True,
    *,
    use_event_callbacks: bool = True,
) -> InlineKeyboardMarkup:
    """Инлайн-кнопки под карточкой товара (привязаны к event_id для групповых событий)."""
    row1 = [InlineKeyboardButton(text="↗️ Открыть на Авито", url=item_url)]
    stop_cb = f"event_stop:{target_id}" if use_event_callbacks else f"add_stop_from_item:{target_id}"
    row2 = [InlineKeyboardButton(text="🛡 Стоп-слово", callback_data=stop_cb)]
    if has_seller:
        row2.append(InlineKeyboardButton(text="🚷 Забанить продавца", callback_data=f"ban_seller:{target_id}"))
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


def is_ad_fresh_enough(
    date_str: Optional[str],
    timestamp: Optional[int] = None,
    max_age_minutes: Optional[int] = 5,
) -> bool:
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
        parts.append(f"{first_name}")

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


def _extract_photo_file_id(message_obj) -> Optional[str]:
    photos = getattr(message_obj, "photo", None)
    if photos and isinstance(photos, (list, tuple)):
        last = photos[-1]
        fid = getattr(last, "file_id", None)
        if isinstance(fid, str) and fid:
            return fid
    return None


async def send_ad_to_user(
    bot: Bot,
    parser: Optional[AvitoParser],
    user_id: int,
    search_id: int,
    search_title: str,
    it: AvitoItem,
    user_label: str = "",
    is_price_drop: bool = False,
    old_price: Optional[int] = None,
    drop_percent: Optional[int] = None,
    *,
    event_id: Optional[int] = None,
    radar_titles: Optional[List[str]] = None,
    search_ids: Optional[List[int]] = None,
    is_catchup: bool = False,
    possible_duplicate_after_crash: bool = False,
    allow_uncached_photo: bool = False,
    preverified_delivery: bool = False,
) -> bool:
    """Deliver notification to Telegram user with tariff-aware photo pipeline."""
    caption = build_notification_text(
        it,
        search_title,
        radar_titles=radar_titles,
        is_catchup=is_catchup,
        possible_duplicate_after_crash=possible_duplicate_after_crash,
    )
    target_btn_id = event_id if event_id is not None else search_id
    keyboard = build_notification_keyboard(
        it.url,
        target_btn_id,
        has_seller=bool(it.seller_name),
        use_event_callbacks=(event_id is not None),
    )
    owner_str = f" [{user_label}]" if user_label else f" ID:{user_id}"
    check_ids = list(search_ids) if search_ids else ([search_id] if search_id else [])

    async def prepare_send() -> bool:
        await _await_rate_limit(user_id)
        if preverified_delivery:
            return True
        if not check_ids:
            return await database.is_user_allowed(user_id)
        for sid in check_ids:
            if await database.can_deliver_to_search(sid, user_id):
                return True
        return False

    # 1. Быстрая попытка через кэш telegram file_id
    cached_fid = get_cached_photo_file_id(it.item_id)
    if cached_fid:
        try:
            if not await prepare_send():
                return False
            await bot.send_photo(
                chat_id=user_id,
                photo=cached_fid,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            logger.info(
                f"     ⚡️ [Радар #{search_id}{owner_str}] Доставлено (file_id кэш): {it.price} · «{it.title[:35]}»"
            )
            return True
        except (TelegramForbiddenError, TelegramRetryAfter):
            raise
        except Exception as cache_err:
            logger.debug(f"file_id кэш сбой ({cache_err}), пробуем следующий путь...")

    # 2. Для Standart/PRO/admin: прямая отправка по image_url -> single-flight BufferedInputFile
    if allow_uncached_photo and it.image_url:
        try:
            if not await prepare_send():
                return False
            msg = await bot.send_photo(
                chat_id=user_id,
                photo=it.image_url,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            fid = _extract_photo_file_id(msg)
            if fid:
                set_cached_photo_file_id(it.item_id, fid)
            logger.info(
                f"     🖼 [Радар #{search_id}{owner_str}] Доставлено (photo URL): {it.price} · «{it.title[:35]}»"
            )
            return True
        except (TelegramForbiddenError, TelegramRetryAfter):
            raise
        except (TelegramBadRequest, TelegramNetworkError, Exception) as url_err:
            logger.debug(f"Прямая отправка image_url не удалась ({url_err}), пробуем single-flight скачивание...")
            item_lock = await get_item_lock(it.item_id)
            async with item_lock:
                fid_after_lock = get_cached_photo_file_id(it.item_id)
                if fid_after_lock:
                    try:
                        if not await prepare_send():
                            return False
                        await bot.send_photo(
                            chat_id=user_id,
                            photo=fid_after_lock,
                            caption=caption,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )
                        return True
                    except (TelegramForbiddenError, TelegramRetryAfter):
                        raise
                    except Exception:
                        pass
                else:
                    img_bytes = await download_image_bytes(it.image_url)
                    if img_bytes:
                        try:
                            if not await prepare_send():
                                return False
                            input_file = BufferedInputFile(img_bytes, filename=f"{it.item_id}.jpg")
                            msg = await bot.send_photo(
                                chat_id=user_id,
                                photo=input_file,
                                caption=caption,
                                reply_markup=keyboard,
                                parse_mode="HTML",
                            )
                            fid = _extract_photo_file_id(msg)
                            if fid:
                                set_cached_photo_file_id(it.item_id, fid)
                            logger.info(
                                f"     🖼 [Радар #{search_id}{owner_str}] Доставлено (BufferedInputFile): {it.price} · «{it.title[:35]}»"
                            )
                            return True
                        except (TelegramForbiddenError, TelegramRetryAfter):
                            raise
                        except Exception as upload_err:
                            logger.debug(f"BufferedInputFile сбой ({upload_err}), отправляем текст...")

    # 3. Фолбэк (и базовый путь для тарифа base): отправляем текстом с превью ссылки
    for _ in range(2):
        try:
            if not await prepare_send():
                return False
            await bot.send_message(
                chat_id=user_id,
                text=caption,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
            logger.info(
                f"     🚀 [Радар #{search_id}{owner_str}] Доставлено (текст): {it.price} · «{it.title[:35]}»"
            )
            return True
        except (TelegramForbiddenError, TelegramRetryAfter):
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


def parse_created_epoch(radar: dict) -> Optional[float]:
    """Parse radar creation timestamp into unix epoch seconds."""
    epoch = radar.get("created_epoch")
    if epoch is not None:
        try:
            return float(epoch)
        except (ValueError, TypeError):
            pass
    raw = radar.get("created_at")
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        dt = datetime.datetime.strptime(str(raw).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    try:
        dt = datetime.datetime.fromisoformat(str(raw).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


async def process_collected_group_fanout(
    clean_url: str,
    radars_for_url: List[dict],
    collection: SourceCollection,
    *,
    worker_id: int = 1,
    previous_processed_at: float = 0.0,
) -> bool:
    """Batch-evaluate a collected source across all subscribed radars with 1 batch read + 1 batch write."""
    global LAST_SCAN_TIMESTAMP
    items = list(collection.items)
    fetch_ms = collection.elapsed_ms
    SOURCE_SNAPSHOTS.put(clean_url, SourceSnapshot(collection.title, collection.items, collection.captured_at))
    w_tag = f"[W{worker_id}] " if config.MAX_CONCURRENT_WORKERS > 1 else ""

    if not items:
        for radar in radars_for_url:
            if radar.get("initialized_at") is None:
                await database.initialize_search_snapshot(radar["id"], [])
        LAST_SCAN_TIMESTAMP = time.time()
        logger.info(
            f"📡 {w_tag}[ПАРСЕР] По URL «{clean_url[:60]}...»: страница получена за {fetch_ms} мс, лотов в каталоге не найдено"
        )
        return True

    def get_radar_priority(r: dict) -> int:
        _, prio, _ = database.classify_user_tariff(r)
        return prio

    sorted_radars = sorted(radars_for_url, key=get_radar_priority)
    calibrated_radars: List[dict] = []

    for radar in sorted_radars:
        radar_id = int(radar["id"])
        user_id = int(radar["user_id"])
        if not await database.is_search_active(radar_id) or not await database.is_user_allowed(user_id):
            continue
        if radar.get("initialized_at") is None:
            if await database.initialize_search_snapshot(
                radar_id,
                [(item.item_id, item.raw_price) for item in items],
            ):
                continue
        calibrated_radars.append(radar)

    if not calibrated_radars:
        LAST_SCAN_TIMESTAMP = time.time()
        return True

    search_ids = [int(r["id"]) for r in calibrated_radars]
    user_ids = list({int(r["user_id"]) for r in calibrated_radars})
    current_item_ids = [it.item_id for it in items]

    seen_by_search = await database.get_group_seen_state(
        search_ids, current_item_ids
    )
    blacklists_by_user = await database.get_blocked_sellers_for_users(user_ids)

    seen_batch: List[Tuple[int, str, Optional[int]]] = []
    candidates_to_enqueue: List[dict] = []
    now_epoch = collection.captured_at

    for idx, radar in enumerate(calibrated_radars):
        radar_id = int(radar["id"])
        user_id = int(radar["user_id"])
        search_title = radar.get("title") or "Радар"
        stop_words = radar.get("stop_words") or []
        if isinstance(stop_words, str):
            try:
                stop_words = json.loads(stop_words)
            except Exception:
                stop_words = []
        user_str = get_user_display_name(radar)
        source_tag = f"{fetch_ms} мс" if idx == 0 else "⚡ кэш запроса"
        blocked_sellers = blacklists_by_user.get(user_id, set())
        seen_set = seen_by_search.get(radar_id, set())

        # Determine if an item is already seen by this radar OR already pending for this user+radar
        new_items = [
            it for it in items
            if it.item_id not in seen_set
        ]
        if not new_items:
            logger.info(
                f"  {w_tag}🔹 Радар #{radar_id} [{user_str}] «{search_title}»: проверено {len(items)} лотов [{source_tag}] (новых нет)"
            )
            continue

        all_new_candidates = {it.item_id: it.raw_price for it in new_items}

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
                        if any(kw in it.title.lower() for kw in keywords):
                            matching_title_items.append(it)
                        else:
                            logger.info(
                                f"  {w_tag}🏷 [ТОЛЬКО В НАЗВАНИИ] Отсеян лот «{it.title[:35]}» (нет '{q_val}' в заголовке) [{user_str}]"
                            )
                    new_items = matching_title_items

        if not new_items:
            for item_id, price in all_new_candidates.items():
                seen_batch.append((radar_id, item_id, price or 0))
            continue

        from handlers.searches import consume_resumed_radar_id
        is_resumed_radar = consume_resumed_radar_id(radar_id)

        max_age_raw = radar.get("max_age_minutes")
        max_age_min = 5 if (not max_age_raw or int(max_age_raw) <= 0) else min(30, int(max_age_raw))

        tariff, priority, delay_sec = database.classify_user_tariff(radar)
        is_pro_or_admin = (tariff == "pro")
        is_downtime_catchup = (
            is_pro_or_admin
            and previous_processed_at > 0
            and (now_epoch - previous_processed_at) > (max_age_min * 60)
        )
        radar_created_epoch = parse_created_epoch(radar)
        if is_resumed_radar:
            effective_age_min = 10
        elif is_downtime_catchup:
            effective_age_min = config.CATCHUP_MAX_AGE_MINUTES
        else:
            effective_age_min = max_age_min

        ages = {}
        normal_fresh_flags = {}
        for item in new_items:
            disc_at = collection.discovery_times.get(item.item_id, now_epoch)
            ages[item.item_id] = freshness_status(item.date, item.timestamp, effective_age_min, now=disc_at)
            normal_fresh_flags[item.item_id] = (
                freshness_status(item.date, item.timestamp, max_age_min, now=disc_at) is True
            )

        unknown_ids = {item_id for item_id, status in ages.items() if status is None}
        fresh_items = [item for item in new_items if ages[item.item_id] is True]

        deliverable_items = []
        for it in fresh_items:
            matched_stop = is_stop_word_matched(it, stop_words)
            if matched_stop:
                logger.info(f"     🛡 Отсеяно по стоп-слову «{matched_stop}» [{user_str}]: «{it.title[:30]}»")
                continue
            if it.seller_name and it.seller_name.lower().strip() in blocked_sellers:
                logger.info(f"     🚷 Отсеян продавец «{it.seller_name}» из ЧС [{user_str}]")
                continue
            deliverable_items.append(it)

        # In downtime catch-up mode for PRO/admin, cap to CATCHUP_MAX_ITEMS (10) newest items
        omitted_catchup_ids: set[str] = set()
        if is_downtime_catchup and len(deliverable_items) > config.CATCHUP_MAX_ITEMS:
            kept = deliverable_items[: config.CATCHUP_MAX_ITEMS]
            omitted = deliverable_items[config.CATCHUP_MAX_ITEMS :]
            omitted_catchup_ids = {it.item_id for it in omitted}
            deliverable_items = kept
            METRICS.record_catchup_omitted(len(omitted))
            logger.info(
                "🕒 [ДОГОН ПОСЛЕ ПАУЗЫ] Радар #%s [%s]: выбрано %s самых свежих лотов, пропущено %s старых (отмечены seen)",
                radar_id,
                user_str,
                len(kept),
                len(omitted),
            )

        deliverable_ids = {it.item_id for it in deliverable_items}
        for item_id, price in all_new_candidates.items():
            if item_id in omitted_catchup_ids or (item_id not in deliverable_ids and item_id not in unknown_ids):
                seen_batch.append((radar_id, item_id, price or 0))

        if unknown_ids:
            logger.info(
                "Радар #%s: %s объявлений без достоверного возраста оставлены для повторной проверки",
                radar_id,
                len(unknown_ids),
            )

        if not deliverable_items:
            age_samples = [format_item_age(it.date, getattr(it, "timestamp", None)) for it in new_items[:2]]
            age_summary = ", ".join(age_samples)
            if len(new_items) > 2:
                age_summary += f", и ещё {len(new_items) - 2}"
            items_label = plural_form(len(new_items), "лот", "лота", "лотов")
            limit_label = f"порог {effective_age_min} мин."
            logger.info(
                f"  {w_tag}🔹 Радар #{radar_id} [{user_str}] «{search_title}»: {items_label} в ротации [{source_tag}] "
                f"(отсеян по возрасту: {age_summary}; {limit_label})"
            )
            continue

        fresh_label = plural_form(len(deliverable_items), "СВЕЖИЙ ЛОТ", "СВЕЖИХ ЛОТА", "СВЕЖИХ ЛОТОВ")
        logger.info(f"  {w_tag}🔥 Радар #{radar_id} [{user_str}] «{search_title}»: НАЙДЕНО {fresh_label}!")

        for it in reversed(deliverable_items):
            disc_at = collection.discovery_times.get(it.item_id, now_epoch)
            item_is_catchup = bool(is_resumed_radar or (is_downtime_catchup and not normal_fresh_flags.get(it.item_id, True)))
            task_dict = asdict(
                NotificationTask(
                    user_id=user_id,
                    radar_id=radar_id,
                    search_title=search_title,
                    item=it,
                    user_label=user_str,
                    discovered_at=disc_at,
                    freshness_verified=True,
                    is_catchup=item_is_catchup,
                    radar_titles=[search_title],
                )
            )
            candidates_to_enqueue.append(
                {
                    "user_id": user_id,
                    "radar_id": radar_id,
                    "radar_title": search_title,
                    "item_id": it.item_id,
                    "raw_price": it.raw_price or 0,
                    "tariff": tariff,
                    "priority": priority,
                    "delay_seconds": delay_sec,
                    "t_seen": disc_at,
                    "t_pub": it.timestamp,
                    "t_pub_reliable": bool(getattr(it, "t_pub_reliable", False)),
                    "is_catchup": item_is_catchup,
                    "payload_dict": task_dict,
                }
            )

    if candidates_to_enqueue or seen_batch:
        enqueued = await database.enqueue_group_events(candidates_to_enqueue, seen_batch)
        if enqueued:
            notify_outbox_wakeup()

    LAST_SCAN_TIMESTAMP = time.time()
    return True


async def process_url_group(
    bot: Bot,
    parser: AvitoParser,
    clean_url: str,
    radars_for_url: List[dict],
    worker_id: int = 1,
    *,
    previous_processed_at: float = 0.0,
    can_fast_skip: bool = False,
    last_top_ids: tuple[str, ...] = (),
):
    """Collect an exact source once, then apply each subscriber's filters."""
    try:
        fetch_t0 = time.monotonic()
        parser.last_coverage_gap = False
        max_age = max((min(30, max(1, int(radar.get("max_age_minutes") or 5))) for radar in radars_for_url), default=5)
        has_pro_catchup = (
            previous_processed_at > 0
            and (time.time() - previous_processed_at) > (max_age * 60)
            and any(database.classify_user_tariff(r)[0] == "pro" for r in radars_for_url)
        )
        collection_max_age = config.CATCHUP_MAX_AGE_MINUTES if has_pro_catchup else max_age
        collection_max_pages = config.CATCHUP_MAX_PAGES if has_pro_catchup else config.AVITO_MAX_PAGES_PER_SCAN

        collection = await collect_source(
            parser,
            clean_url,
            collection_max_age,
            max_pages=collection_max_pages,
            can_fast_skip=(can_fast_skip and not has_pro_catchup),
            last_top_ids=last_top_ids,
        )
        if collection is None:
            logger.info(
                "Scan result: ok=0 elapsed_ms=%d items=0 blocked=%d failure=%s",
                int((time.monotonic() - fetch_t0) * 1000),
                int(bool(parser.is_blocked())),
                getattr(parser, "last_failure", None),
            )
            return False

        logger.info(
            "Scan result: ok=1 elapsed_ms=%d items=%d blocked=0 pages=%d coverage_gap=%d",
            collection.elapsed_ms,
            len(collection.items),
            collection.pages,
            int(collection.coverage_gap),
        )
        return await process_collected_group_fanout(
            clean_url,
            radars_for_url,
            collection,
            worker_id=worker_id,
            previous_processed_at=previous_processed_at,
        )
    except Exception as e:
        logger.error(f"❌ [ГРУППА URL] Ошибка при обработке URL «{clean_url[:60]}...»: {e}", exc_info=True)
        return False


async def monitoring_worker(bot: Bot, parser: Optional[AvitoParser] = None):
    from monitoring import run_monitoring
    await run_monitoring(bot, parser)


async def deliver_notification(bot: Bot, parser: Optional[AvitoParser], row: dict) -> bool:
    """Deliver a leased notification event once; revalidates active radars, stop-words, and blacklists."""
    notification_id, token = int(row["id"]), str(row["lease_token"])
    raw_payload_str = row.get("payload_json") or row.get("payload") or ""
    try:
        payload = json.loads(raw_payload_str)
        item_data = dict(payload["item"]) if isinstance(payload.get("item"), dict) else dict(payload)
        allowed_item_keys = set(AvitoItem.__dataclass_fields__.keys())
        filtered_item_data = {k: v for k, v in item_data.items() if k in allowed_item_keys}
        item = AvitoItem(**filtered_item_data)
        user_id = int(row.get("user_id") or payload.get("user_id"))
        primary_radar_id = int(row.get("radar_id") or payload.get("radar_id") or 0)
        search_title = str(payload.get("search_title") or "Радар")
        user_label = str(payload.get("user_label") or "")
        discovered_at = float(payload.get("discovered_at") or row.get("t_seen") or row.get("queued_at") or 0.0)
        freshness_verified = bool(payload.get("freshness_verified", False))
        is_price_drop = bool(payload.get("is_price_drop", False))
    except (ValueError, KeyError, TypeError):
        logger.error("Invalid outbox payload #%s", notification_id)
        await database.discard_notification(notification_id, token)
        return False

    if is_price_drop:
        # Price drop notifications are disabled in the 300-radar architecture
        await database.discard_notification(notification_id, token)
        return False

    try:
        now = time.time()
        queued_at_raw = row.get("queued_at") or row.get("created_at") or discovered_at or now
        if isinstance(queued_at_raw, str):
            try:
                queued_at = datetime.datetime.fromisoformat(queued_at_raw).replace(tzinfo=datetime.timezone.utc).timestamp()
            except Exception:
                queued_at = now
        else:
            queued_at = float(queued_at_raw)

        # Expiration check (> 30m in outbox)
        if queued_at > 0 and (now - queued_at) > float(config.NOTIFICATION_MAX_AGE) and freshness_verified and (now - queued_at) > 3600:
            await database.mark_event_expired(notification_id, token, mark_seen=True)
            METRICS.record_outbox_expired(1)
            return False

        if row.get("_fast_claimed"):
            if not row.get("_user_allowed", True):
                await database.discard_notification(notification_id, token)
                return False
            existing_searches = [
                {
                    "id": int(r["search_id"]),
                    "user_id": user_id,
                    "title": str(r["radar_title"]),
                    "is_active": bool(r["is_active"]),
                    "stop_words": list(r.get("stop_words") or []),
                    "max_age_minutes": int(r.get("max_age_minutes") or 5),
                }
                for r in (row.get("radars") or [])
                if r.get("exists")
            ]
        else:
            if not await database.is_user_allowed(user_id):
                await database.discard_notification(notification_id, token)
                return False

            raw_search_ids = list(row.get("search_ids") or [])
            if not raw_search_ids and primary_radar_id:
                raw_search_ids = [primary_radar_id]

            existing_searches = []
            for sid in raw_search_ids:
                s = await database.get_search(int(sid))
                if s and int(s["user_id"]) == user_id:
                    existing_searches.append(s)

        if not existing_searches:
            await database.discard_notification(notification_id, token)
            return False

        active_searches = [s for s in existing_searches if s.get("is_active")]
        if not active_searches:
            if (now - queued_at) >= float(config.NOTIFICATION_MAX_AGE):
                await database.mark_event_expired(notification_id, token, mark_seen=False)
                METRICS.record_outbox_expired(1)
            else:
                await database.release_notification(notification_id, token, 30)
            return False

        # Legacy unverified freshness check
        if not freshness_verified:
            first_search = active_searches[0]
            ref_time = discovered_at if discovered_at > 0 else queued_at
            status = freshness_status(item.date, item.timestamp, first_search.get("max_age_minutes"), now=ref_time)
            if status is False:
                await database.discard_notification(notification_id, token)
                for s in existing_searches:
                    await database.mark_item_seen(int(s["id"]), item.item_id, item.raw_price or 0)
                return False
            if status is None:
                await database.discard_notification(notification_id, token)
                return False

        blocked = await database.get_blocked_sellers(user_id)
        if item.seller_name and item.seller_name.lower().strip() in blocked:
            await database.discard_notification(notification_id, token)
            return False

        passing_searches = [
            s for s in active_searches
            if not is_stop_word_matched(item, s.get("stop_words", []))
        ]
        if not passing_searches:
            await database.discard_notification(notification_id, token)
            return False

        active_radar_ids = [int(s["id"]) for s in passing_searches]
        active_radar_titles = [str(s.get("title") or search_title) for s in passing_searches]
        primary_active_id = active_radar_ids[0]
        primary_active_title = active_radar_titles[0]

        tariff = str(row.get("tariff") or payload.get("tariff") or "base").lower()
        is_catchup = bool(row.get("is_catchup") or payload.get("is_catchup"))
        possible_dup = bool(row.get("possible_duplicate_after_crash"))

        if not row.get("send_started_at"):
            await database.mark_event_send_started(notification_id, token)
        delivered = await send_ad_to_user(
            bot,
            parser,
            user_id,
            primary_active_id,
            primary_active_title,
            item,
            user_label=user_label,
            event_id=notification_id,
            radar_titles=active_radar_titles,
            search_ids=active_radar_ids,
            is_catchup=is_catchup,
            possible_duplicate_after_crash=possible_dup,
            allow_uncached_photo=(tariff in ("pro", "standart")),
            preverified_delivery=bool(row.get("_fast_claimed")),
        )
        if delivered:
            acknowledged = await database.acknowledge_notification(
                notification_id, token, item.raw_price or 0
            )
            if acknowledged:
                t_sent = time.time()
                t_seen_val = float(row.get("t_seen") or discovered_at or queued_at or t_sent)
                deliver_at_val = float(row.get("deliver_at") or queued_at or t_sent)
                METRICS.record_delivery(
                    tariff=tariff,
                    t_pub=row.get("t_pub") or item.timestamp,
                    t_pub_reliable=bool(row.get("t_pub_reliable") or getattr(item, "t_pub_reliable", False)),
                    t_seen=t_seen_val,
                    t_queued=queued_at,
                    deliver_at=deliver_at_val,
                    t_sent=t_sent,
                    is_catchup=is_catchup,
                )
                logger.info(
                    "Delivery result: notification=%s discovery_to_send_ms=%d deliver_lag_ms=%d attempts=%s",
                    notification_id,
                    int(max(0.0, t_sent - t_seen_val) * 1000),
                    int(max(0.0, t_sent - deliver_at_val) * 1000),
                    row.get("attempts", 1),
                )
            return acknowledged

        attempts = int(row.get("attempts") or 1)
        if attempts >= max(8, int(config.NOTIFICATION_GENERIC_ATTEMPTS)):
            await database.mark_event_failed(notification_id, token, error="delivery_returned_false")
            METRICS.record_outbox_failed(1)
            return False

        delay = min(int(config.NOTIFICATION_RETRY_MAX), 2 ** min(attempts, 6))
        await database.release_notification(notification_id, token, delay)
        return False
    except asyncio.CancelledError:
        await asyncio.shield(database.release_notification(notification_id, token, 0))
        raise
    except TelegramForbiddenError:
        await database.deactivate_user_searches(user_id)
        await database.discard_notification(notification_id, token)
        return False
    except TelegramRetryAfter as exc:
        retry_delay = float(exc.retry_after) + random.uniform(0.5, 1.5)
        await database.release_notification(notification_id, token, retry_delay)
        return False
    except Exception as exc:
        logger.exception("Outbox delivery failed #%s", notification_id)
        attempts = int(row.get("attempts") or 1)
        if attempts >= max(8, int(config.NOTIFICATION_GENERIC_ATTEMPTS)):
            await database.mark_event_failed(notification_id, token, error=str(exc))
            METRICS.record_outbox_failed(1)
        else:
            delay = min(int(config.NOTIFICATION_RETRY_MAX), 2 ** min(attempts, 6))
            await database.release_notification(notification_id, token, delay)
        return False


async def notification_worker(bot: Bot, parser: Optional[AvitoParser] = None, worker_id: int = 1):
    logger.info("📬 Persistent Outbox #%s started", worker_id)
    while True:
        try:
            row = await database.claim_notification_event()
            if row is None:
                NOTIFICATION_WAKEUP_EVENT.clear()
                try:
                    await asyncio.wait_for(NOTIFICATION_WAKEUP_EVENT.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
                continue
            await deliver_notification(bot, parser, row)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox worker #%s failed", worker_id)
            await asyncio.sleep(0.5)


async def supervised_monitoring_worker(bot: Bot, parser: Optional[AvitoParser] = None):
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
        if parser is not None:
            parser.close()


def start_background_services(bot: Bot) -> List[asyncio.Task]:
    """
    Запускает все фоновые сервисы:
    1. Пул воркеров очереди отправки уведомлений (notification_worker x config.NOTIFICATION_WORKERS)
    2. Мониторинг Авито под супервизором (supervised_monitoring_worker)
    Возвращает список фоновых задач для управления их жизненным циклом.
    """
    num_notif_workers = max(1, int(config.NOTIFICATION_WORKERS))
    notif_tasks = [
        asyncio.create_task(notification_worker(bot, None, worker_id=i + 1), name=f"outbox-worker-{i+1}")
        for i in range(num_notif_workers)
    ]
    monitor_task = asyncio.create_task(supervised_monitoring_worker(bot, None), name="monitoring-supervisor")
    return [*notif_tasks, monitor_task]

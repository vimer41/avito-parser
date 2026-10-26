import asyncio
import datetime
import gc
import html
import json
import logging
import random
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, parse_qs
import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

import config
import database
from config import CHECK_INTERVAL
from parser import AvitoParser, AvitoItem

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


async def enqueue_notification_task(task: NotificationTask) -> bool:
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
            async with IMAGE_DOWNLOAD_SEMAPHORE:
                async with session.get(u) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        if len(content) > 500:
                            return content
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
    if date_str:
        return date_str.strip()
    return "возраст не указан"


def is_ad_fresh_enough(
    date_str: Optional[str],
    timestamp: Optional[int] = None,
    max_age_minutes: Optional[int] = 5
) -> bool:
    """
    Проверяет, действительно ли объявление актуальное/свежее согласно настройке радара (max_age_minutes),
    отсекая старый спам из глубины каталога. Лимит строго от 1 до 30 минут (по дефолту 5 мин).

    1. Если есть точный timestamp из SSR JSON — вычисляет возраст лота с точностью до секунды.
    2. Если timestamp нет — парсит относительные фразы и время 'сегодня в HH:MM'.
    """
    if not max_age_minutes or max_age_minutes <= 0:
        eff_max_age = 5
    else:
        eff_max_age = min(30, int(max_age_minutes))

    threshold_seconds = eff_max_age * 60

    if timestamp:
        age_seconds = max(0, int(time.time() - timestamp))
        return age_seconds <= threshold_seconds

    if not date_str:
        return True

    d = date_str.lower().strip()

    # Секунды и "только что" (всегда свежие, менее минуты)
    if any(k in d for k in ["секунд", "только что"]):
        return True

    # Минуты
    if "минут" in d:
        m = re.search(r"(\d+)\s+минут", d)
        if m:
            mins = int(m.group(1))
            return mins <= eff_max_age
        return True

    # При лимите до 30 минут "час назад", "2 часа назад", "вчера" и т.д. ГАРАНТИРОВАННО отсекаются!
    if any(k in d for k in ["час назад", "часа назад", "часов назад"]):
        return False

    # "сегодня в 18:06" / "сегодня, 18:06"
    if "сегодня" in d:
        m = re.search(r"(\d{1,2}):(\d{2})", d)
        if m:
            ad_h, ad_m = int(m.group(1)), int(m.group(2))
            now = datetime.datetime.now()
            diff_min = (now.hour * 60 + now.minute) - (ad_h * 60 + ad_m)
            # Учитываем возможный рассинхрон часов / смену суток
            if diff_min < -60:
                return True
            return diff_min <= eff_max_age
        return True

    # Все остальное: "вчера", "24 октября", "3 дня назад" и т.д. считаем старыми
    return False


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
    """Priority dequeue without leaving orphaned queue.get tasks on cancellation."""
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
    """
    Отправляет уведомление пользователю (фото или текст).
    1. Синхронизируется через per-item lock (защита от Cache Stampede при параллельной отправке).
    2. Сначала проверяет кэш telegram file_id для мгновенной отправки без скачивания.
    3. Скачивает фото через high-performance aiohttp session (Anycast CDN c80.img.avito.st).
    4. Сохраняет полученный telegram file_id для остальных пользователей и будущих отправок.
    5. Автоматически выдерживает паузу и повторяет отправку при TelegramRetryAfter.
    """
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
        # Downloading an image or waiting for its lock may outlive the radar/access.
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
                logger.debug(f"file_id кэш сбой ({cache_err}), пробуем скачать фото...")
                break

    # 2. Если в кэше нет — берем атомарный item_lock (исключает повторное скачивание одной картинки параллельными воркерами)
    item_lock = await get_item_lock(it.item_id)
    async with item_lock:
        # Проверяем кэш повторно внутри лока (если другой воркер скачал и залил в Telegram мгновением ранее)
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
                    logger.info(f"     ⚡️ [Радар #{search_id}{owner_str}] {delivery_tag} (file_id кэш/lock): {it.price} · «{it.title[:35]}»")
                    return True
                except TelegramForbiddenError:
                    raise
                except TelegramRetryAfter as retry_err:
                    raise
                except Exception:
                    break

        # Скачиваем изображение через aiohttp
        photo_bytes: Optional[bytes] = None
        if it.image_url:
            photo_bytes = await download_image_bytes(it.image_url)

        # Отправляем фото и сохраняем telegram file_id в кэш
        if photo_bytes:
            for attempt in range(2):
                try:
                    photo_file = BufferedInputFile(photo_bytes, filename=f"{it.item_id}.jpg")
                    if not await prepare_send():
                        return False
                    sent_msg = await bot.send_photo(
                        chat_id=user_id,
                        photo=photo_file,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                    if sent_msg.photo:
                        set_cached_photo_file_id(it.item_id, sent_msg.photo[-1].file_id)
                    logger.info(f"     🚀 [Радар #{search_id}{owner_str}] {delivery_tag} (с фото): {it.price} · «{it.title[:35]}»")
                    return True
                except TelegramForbiddenError:
                    raise
                except TelegramRetryAfter as retry_err:
                    raise
                except Exception as photo_send_err:
                    logger.warning(f"⚠️ [РАДАР #{search_id}] Сбой send_photo: {photo_send_err}, отправляем текстом...")
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
    """
    Обрабатывает один уникальный URL выдачи Авито:
    - Делает ровно 1 HTTP-запрос к Авито
    - Парсит органическую выдачу (топ-25)
    - Сортирует пользователей по приоритету тарифов (PRO и админы получают первыми)
    - Раздает результаты с фильтрацией по стоп-словам и ЧС продавцов
    - Сразу запоминает новые ID в базе для каждого радара
    """
    try:
        # Выполняем 1 сетевой запрос к Авито на всю группу радаров с этим URL
        fetch_t0 = time.monotonic()
        html_content = await parser.fetch_page_async(clean_url)
        fetch_ms = int((time.monotonic() - fetch_t0) * 1000)

        if not html_content:
            logger.info("Scan result: ok=0 elapsed_ms=%d items=0 blocked=%d", fetch_ms, int(bool(parser.is_blocked())))
            return False

        items = parser.parse_items(html_content)
        logger.info("Scan result: ok=1 elapsed_ms=%d items=%d blocked=0", fetch_ms, len(items))
        w_tag = f"[W{worker_id}] " if config.MAX_CONCURRENT_WORKERS > 1 else ""

        if not items:
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

            stop_words = radar.get("stop_words", [])
            user_str = get_user_display_name(radar)
            source_tag = f"{fetch_ms} мс" if idx == 0 else "⚡ кэш запроса"

            # Получаем актуальный черный список продавцов пользователя
            blocked_sellers = await database.get_blocked_sellers(user_id)

            seen_dict = await database.get_seen_items_with_prices(radar_id)
            pending_ids = await database.get_pending_new_item_ids(radar_id)
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

            fresh_items = [
                it for it in new_items 
                if is_ad_fresh_enough(it.date, getattr(it, "timestamp", None), max_age_minutes=max_age_min)
            ]
            deliverable_items = [
                it for it in fresh_items
                if not is_stop_word_matched(it, stop_words)
                and not (it.seller_name and it.seller_name.lower().strip() in blocked_sellers)
            ]
            deliverable_ids = {it.item_id for it in deliverable_items}
            rejected = [(item_id, price) for item_id, price in candidates.items() if item_id not in deliverable_ids]
            if rejected:
                await database.mark_items_seen_bulk(radar_id, rejected)
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

            # Защита от аномалий: если свежих лотов аномально много (> 15 штук одновременно)
            if len(fresh_items) > 15:
                fresh_label = plural_form(len(fresh_items), "свежий лот", "свежих лота", "свежих лотов")
                logger.info(
                    f"  {w_tag}📦 Радар #{radar_id} [{user_str}] «{search_title}»: аномальная пачка из {fresh_label} (отправляем топ-10 самых свежих)"
                )
                fresh_items = fresh_items[:10]

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
                    user_label=user_str
                ))

        global LAST_SCAN_TIMESTAMP
        LAST_SCAN_TIMESTAMP = time.time()
        return True

    except Exception as e:
        logger.error(f"❌ [ГРУППА URL] Ошибка при обработке URL «{clean_url[:60]}...»: {e}", exc_info=True)
        return False


async def monitoring_worker(bot: Bot, parser: Optional[AvitoParser] = None):
    """
    Непрерывный асинхронный конвейер мониторинга поисков (Continuous Pipeline Queue)
    с приоритетным перехватом сбоев (Immediate Priority Failover).
    
    1. Непрерывная очередь (next_check_at):
       Каждая ссылка опрашивается по своему индивидуальному таймлайну без жестких пауз между кругами.
       Новые радары подхватываются мгновенно (в течение 1 секунды).
       PRO-радары и администраторы опрашиваются с повышенным приоритетом и сокращенным интервалом.
       
    2. Приоритетный перехват сбоев (Immediate Priority Failover):
       При ошибке парсинга (403, 429, таймаут сети) задача не отбрасывается в конец очереди,
       а мгновенно помещается в стек urgent_failover_queue.
       Первый освободившийся воркер немедленно забирает её со своим чистым сетевым контекстом (до 3 попыток).
    """
    pool_size = max(1, config.MAX_CONCURRENT_WORKERS)
    parser_pool = ParserPool(pool_size, proxy_url=config.PROXY_URL)
    if parser is not None and parser_pool.parsers:
        parser_pool.parsers[0].close()
        parser_pool.parsers[0] = parser

    logger.info(f"🚀 Запуск непрерывного конвейера мониторинга Авито (Worker Pool: {pool_size} воркеров, Continuous Queue)...")

    # Очереди задач
    urgent_failover_queue: asyncio.LifoQueue = asyncio.LifoQueue()
    pipeline_queue: asyncio.Queue = asyncio.Queue()

    # Состояние планировщика
    url_next_check: Dict[str, float] = {}
    url_intervals: Dict[str, float] = {}
    url_radars_map: Dict[str, List[dict]] = {}
    in_flight_urls: set = set()

    # Статистика и здоровье
    stats = {
        "processed": 0,
        "success": 0,
        "failover_retries": 0,
        "exhausted_fails": 0,
        "consecutive_failures": 0,
    }
    alert_sent = False
    FAILURE_THRESHOLD = 5

    async def get_next_task() -> Tuple[Tuple[str, List[dict], int], bool]:
        """
        Забирает задачу с абсолютным приоритетом для urgent_failover_queue.
        Если задач нет, ждет появления либо срочной, либо конвейерной задачи.
        Возвращает: ((clean_url, radars, retry_attempt), is_urgent)
        """
        return await get_next_pipeline_task(urgent_failover_queue, pipeline_queue)

    async def worker_loop(worker_id: int):
        """Индивидуальный параллельный воркер-обработчик конвейера."""
        w_parser = parser_pool.get_parser(worker_id - 1)
        # Небольшой начальный сдвиг старта воркеров
        if worker_id > 1:
            await asyncio.sleep((worker_id - 1) * 1.2)

        while True:
            try:
                task, is_urgent = await get_next_task()
            except asyncio.CancelledError:
                break
            except Exception as task_err:
                logger.error(f"[W{worker_id}] Ошибка извлечения задачи: {task_err}")
                await asyncio.sleep(1)
                continue

            clean_url, radars_for_url, retry_attempt = task
            human_name = AvitoParser._human_url(clean_url)

            if is_urgent:
                logger.info(
                    f"  [W{worker_id}] 🎯 [FAILOVER ПЕРЕХВАТ #{retry_attempt}/3] Забрал задачу «{human_name}» из приоритетного стека!"
                )

            stats["processed"] += 1
            ok = await process_url_group(bot, w_parser, clean_url, radars_for_url, worker_id=worker_id)

            if ok:
                stats["success"] += 1
                stats["consecutive_failures"] = 0
                nonlocal alert_sent
                if alert_sent:
                    alert_sent = False
                    recovery_msg = (
                        f"✅ <b>Связь с Авито восстановлена!</b>\n\n"
                        f"Конвейер мониторинга успешно получает данные. Бот работает в штатном режиме."
                    )
                    for aid in config.ADMIN_IDS:
                        try:
                            await bot.send_message(chat_id=aid, text=recovery_msg, parse_mode="HTML")
                        except Exception:
                            pass

                # Вычисляем время следующей проверки
                base_interval = url_intervals.get(clean_url, config.CHECK_INTERVAL)
                jitter = random.uniform(-1.5, 1.5)
                url_next_check[clean_url] = time.time() + max(10.0, base_interval + jitter)
                in_flight_urls.discard(clean_url)

                # Безопасная пауза между плановыми запросами конкретного воркера
                if urgent_failover_queue.empty():
                    await asyncio.sleep(random.uniform(1.8, 3.0))
                else:
                    await asyncio.sleep(0.3)
            else:
                # ОШИБКА: Мгновенная передача задачи другому воркеру без ожидания и кулдаунов!
                if retry_attempt < 3:
                    next_attempt = retry_attempt + 1
                    stats["failover_retries"] += 1
                    logger.warning(
                        f"  [W{worker_id}] ⚡️ [PRIORITY FAILOVER #{next_attempt}/3] Ошибка на «{human_name}» -> мгновенно передано в приоритетный стек другому воркеру!"
                    )
                    # Задача остается в in_flight_urls, отдается в LIFO стек первому свободному воркеру
                    await urgent_failover_queue.put((clean_url, radars_for_url, next_attempt))
                    await asyncio.sleep(0.2)
                else:
                    # 3 попытки исчерпаны
                    stats["exhausted_fails"] += 1
                    stats["consecutive_failures"] += 1
                    logger.error(
                        f"  [W{worker_id}] ❌ [FAILOVER EXHAUSTED] «{human_name}» не ответил после 3 попыток. Отложено на следующий плановый интервал."
                    )
                    base_interval = url_intervals.get(clean_url, config.CHECK_INTERVAL)
                    url_next_check[clean_url] = time.time() + base_interval
                    in_flight_urls.discard(clean_url)

                    if stats["consecutive_failures"] >= FAILURE_THRESHOLD and not alert_sent:
                        alert_sent = True
                        current_p = "Настроенный прокси (реквизиты скрыты)" if config.PROXY_URL else "Прямое соединение (без прокси)"
                        alert_msg = (
                            f"🚨 <b>ВНИМАНИЕ: Сбой мониторинга Авито!</b>\n\n"
                            f"Конвейер зафиксировал <b>{stats['consecutive_failures']} отказов подряд</b>.\n"
                            f"❌ Запросы к Авито завершаются ошибкой (403, 429 или таймаут).\n\n"
                            f"🌐 <i>Текущий прокси:</i> <code>{html.escape(current_p)}</code>\n"
                            f"🛠 <i>Рекомендация:</i> Проверьте соединение или перезапустите бота."
                        )
                        for aid in config.ADMIN_IDS:
                            try:
                                await bot.send_message(chat_id=aid, text=alert_msg, parse_mode="HTML")
                            except Exception:
                                pass

                    await asyncio.sleep(2.0)

    async def dispatcher_loop():
        """
        Диспетчер конвейера:
        - Периодически обновляет список радаров из базы данных;
        - Назначает индивидуальный интервал опроса (PRO/Standart);
        - Добавляет подошедшие по времени ссылки (next_check_at <= now) в рабочий конвейер;
        - Выводит регулярную статистику работы конвейера.
        """
        last_db_refresh = 0.0
        last_stats_log = time.time()
        last_maintenance = time.time()

        while True:
            now = time.time()

            # 1. Периодическое обновление активных радаров из БД (раз в 5 секунд)
            if now - last_db_refresh >= 5.0:
                last_db_refresh = now
                try:
                    active_searches = await database.get_all_active_searches()
                    new_groups: Dict[str, List[dict]] = defaultdict(list)
                    for search in active_searches:
                        clean_url = AvitoParser.normalize_url(search["url"])
                        new_groups[clean_url].append(search)

                    # Вычисляем интервалы для каждого URL
                    for clean_url, radars in new_groups.items():
                        # Если хотя бы один радар принадлежит PRO или Admin -> ускоренный интервал
                        has_pro = any(
                            r.get("user_id") in config.ADMIN_IDS or (r.get("tariff_plan") or "").lower() == "pro"
                            for r in radars
                        )
                        if has_pro:
                            # PRO-интервал: на 40% быстрее стандартного, но не менее 12 сек
                            url_intervals[clean_url] = max(12.0, config.CHECK_INTERVAL * 0.6)
                        else:
                            url_intervals[clean_url] = float(config.CHECK_INTERVAL)

                        # Если URL новый — ставим время проверки "прямо сейчас", чтобы проверить мгновенно!
                        if clean_url not in url_next_check:
                            url_next_check[clean_url] = now
                            logger.info(
                                f"✨ [КОНВЕЙЕР] Обнаружен новый радар «{AvitoParser._human_url(clean_url)}» "
                                f"(интервал: {url_intervals[clean_url]:.0f}с) -> мгновенная постановка в очередь!"
                            )

                    # Удаляем удаленные пользователями радары из отслеживания
                    existing_keys = set(new_groups.keys())
                    for old_url in list(url_next_check.keys()):
                        if old_url not in existing_keys:
                            url_next_check.pop(old_url, None)
                            url_intervals.pop(old_url, None)
                            # Примечание: in_flight_urls.discard(old_url) НЕ вызывается здесь,
                            # чтобы избежать race condition при повторном добавлении того же URL,
                            # пока воркер еще выполняет сетевой запрос. Флаг снимает только сам воркер!

                    url_radars_map.clear()
                    url_radars_map.update(new_groups)

                except Exception as db_err:
                    logger.error(f"[ДИСПЕТЧЕР] Ошибка синхронизации с БД: {db_err}")

            # 2. Постановка созревших задач в рабочий конвейер pipeline_queue
            for clean_url, radars in list(url_radars_map.items()):
                if clean_url not in in_flight_urls:
                    due_time = url_next_check.get(clean_url, 0.0)
                    if due_time <= now:
                        in_flight_urls.add(clean_url)
                        await pipeline_queue.put((clean_url, radars, 0))

            # 3. Периодический лог состояния конвейера (раз в 60 сек)
            if now - last_stats_log >= 60.0:
                last_stats_log = now
                total_radars = sum(len(r) for r in url_radars_map.values())
                if total_radars > 0:
                    logger.info(
                        f"🔄 [КОНВЕЙЕР СТАТУС] Активно: {total_radars} радаров ({len(url_radars_map)} URL) | "
                        f"В конвейере: {pipeline_queue.qsize()} | В приоритетном стеке: {urgent_failover_queue.qsize()} | "
                        f"За минуту: обработано {stats['processed']} (успешно {stats['success']}, failover {stats['failover_retries']})"
                    )
                else:
                    logger.info("ℹ️ [КОНВЕЙЕР] Нет активных радаров для сканирования.")
                stats["processed"] = 0
                stats["success"] = 0
                stats["failover_retries"] = 0

            # 4. Техническое обслуживание раз в 45 минут (очистка БД, старых логов и RAM)
            if now - last_maintenance >= 2700.0:
                last_maintenance = now
                try:
                    await database.cleanup_old_seen_items(days=5)
                    from logger_setup import cleanup_logs
                    cleaned_logs = cleanup_logs(max_total_mb=40, keep_days=3)
                    if cleaned_logs > 0:
                        logger.info(f"🧹 [КОНВЕЙЕР] Удалено {cleaned_logs} старых архивных файлов логов")
                    gc.collect()
                    logger.info("🧹 [КОНВЕЙЕР] Очистка старых объявлений и сборка мусора (gc.collect) выполнены")
                except Exception as maint_err:
                    logger.error(f"Ошибка техобслуживания: {maint_err}")

            # Шаг квантования диспетчера — 0.5 секунды
            await asyncio.sleep(0.5)

    # Запускаем диспетчер и пул воркеров
    worker_tasks = [asyncio.create_task(worker_loop(w_id)) for w_id in range(1, pool_size + 1)]
    dispatcher_task = asyncio.create_task(dispatcher_loop())

    try:
        await asyncio.gather(dispatcher_task, *worker_tasks)
    finally:
        dispatcher_task.cancel()
        for wt in worker_tasks:
            wt.cancel()
        await asyncio.gather(dispatcher_task, *worker_tasks, return_exceptions=True)
        for owned_parser in parser_pool.parsers:
            if owned_parser is not parser:
                owned_parser.close()


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
        if not task.is_price_drop and not is_ad_fresh_enough(task.item.date, task.item.timestamp, search.get("max_age_minutes")):
            await database.discard_notification(notification_id, token)
            await database.mark_item_seen(task.radar_id, task.item.item_id, task.item.raw_price or 0)
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
            return await database.acknowledge_notification(notification_id, token, task.item.raw_price or 0)
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




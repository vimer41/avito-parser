import asyncio
import gc
import html
import logging
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
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

# ── 2. Кэш Telegram file_id для отправленных фото (экономит 100% повторных скачиваний) ──
PHOTO_FILE_ID_CACHE: Dict[str, str] = {}
MAX_FILE_ID_CACHE = 3000

def get_cached_photo_file_id(item_id: str) -> Optional[str]:
    return PHOTO_FILE_ID_CACHE.get(item_id)

def set_cached_photo_file_id(item_id: str, file_id: str):
    if len(PHOTO_FILE_ID_CACHE) >= MAX_FILE_ID_CACHE:
        for k in list(PHOTO_FILE_ID_CACHE.keys())[:500]:
            PHOTO_FILE_ID_CACHE.pop(k, None)
    PHOTO_FILE_ID_CACHE[item_id] = file_id

# ── 3. Семафор ограничения параллельных сетевых загрузок фото (лимит RAM) ──
IMAGE_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(4)

# ── 4. Асинхронная очередь доставки уведомлений (Outbox Queue) ───────────
NOTIFICATION_QUEUE: asyncio.Queue = asyncio.Queue(maxsize=3000)


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



def build_notification_text(item: AvitoItem, search_title: str) -> str:
    """Форматирует стильный и чистый текст карточки товара."""
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

    lines = [
        f"⚡️ <b>НОВЫЙ ЛОТ НА АВИТО</b>{badge_header}",
        f"🏷 <b>{clean_price}</b> · <b>{clean_title}</b>",
        "────────────────────"
    ]

    # Локация
    address = item.address
    if not address:
        m = re.search(r"в\s+([А-Яа-яЁё\s\-]+)$", clean_search)
        if m:
            address = m.group(1).strip()
    if address:
        lines.append(f"📍 <b>Локация:</b> {html.escape(address)}")

    # Состояние и доставка
    details = []
    if item.condition:
        details.append(f"✨ {html.escape(item.condition)}")
    if item.delivery:
        details.append(f"🚚 {html.escape(item.delivery)}")
    if details:
        lines.append(f"📦 <b>Детали:</b> {' · '.join(details)}")

    # Просмотры (если подгружены)
    if getattr(item, "views_total", None) is not None:
        today_str = f" (+{item.views_today} сегодня)" if getattr(item, "views_today", None) is not None else ""
        lines.append(f"👁 <b>Просмотры:</b> {item.views_total}{today_str}")

    # Продавец
    if item.seller:
        lines.append(f"👤 <b>Продавец:</b> {html.escape(item.seller)}")

    # Время
    if item.date:
        lines.append(f"🕒 <b>Опубликовано:</b> {html.escape(item.date)}")


    # Описание
    if item.description:
        desc = item.description.strip()
        if len(desc) > 280:
            desc = desc[:277] + "..."
        lines.append("")
        lines.append(f"📝 <i>«{html.escape(desc)}»</i>")

    lines.append("────────────────────")
    lines.append(f"🎯 <b>Радар:</b> <i>{html.escape(clean_search)}</i>")

    return "\n".join(lines)


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


def is_ad_fresh_enough(date_str: Optional[str], timestamp: Optional[int] = None) -> bool:
    """
    Проверяет, действительно ли объявление свежее (опубликовано до 10 минут назад).
    1. Если есть точный timestamp из SSR JSON — вычисляет возраст лота с точностью до секунды.
    2. Если timestamp нет — использует текстовые маркеры (фолбэк).
    """
    if timestamp:
        age_seconds = time.time() - timestamp
        # До 10 минут (600 сек) считаем свежим
        return age_seconds <= 10 * 60

    if not date_str:
        return True
    d = date_str.lower().strip()
    if any(k in d for k in ["секунд", "только что"]):
        return True
    if "минут" in d:
        m = re.search(r"(\d+)\s+минут", d)
        if m:
            mins = int(m.group(1))
            return mins <= 10  # максимум 10 минут назад
        return True
    # Все остальное: "1 час назад", "2 часа назад", "вчера", "24 октября" и т.д. считаем старыми
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


async def send_ad_to_user(
    bot: Bot,
    parser: AvitoParser,
    user_id: int,
    search_id: int,
    search_title: str,
    it: AvitoItem,
    user_label: str = ""
) -> bool:
    """
    Отправляет уведомление пользователю (фото или текст).
    1. Сначала проверяет кэш telegram file_id для мгновенной отправки без скачивания.
    2. Скачивает фото через семафор (ограничение параллельных загрузок в RAM).
    3. Сохраняет полученный telegram file_id для остальных пользователей.
    """
    caption = build_notification_text(it, search_title)
    keyboard = build_notification_keyboard(it.url, search_id, has_seller=bool(it.seller_name))
    owner_str = f" [{user_label}]" if user_label else f" ID:{user_id}"

    # 1. Проверяем кэш telegram file_id (мгновенная доставка без повторного скачивания)
    cached_fid = get_cached_photo_file_id(it.item_id)
    if cached_fid:
        try:
            await bot.send_photo(
                chat_id=user_id,
                photo=cached_fid,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            logger.info(f"     ⚡️ [Радар #{search_id}{owner_str}] Доставлено (file_id кэш): {it.price} · «{it.title[:35]}»")
            return True
        except (TelegramForbiddenError, TelegramRetryAfter):
            raise
        except Exception as cache_err:
            logger.debug(f"file_id кэш сбой ({cache_err}), скачиваем картинку заново...")

    try:
        photo_file: Optional[BufferedInputFile] = None
        if it.image_url:
            try:
                async with IMAGE_DOWNLOAD_SEMAPHORE:
                    img_resp = await asyncio.to_thread(parser.session.get, it.image_url, timeout=8)
                    if img_resp.status_code == 200 and len(img_resp.content) > 500:
                        photo_file = BufferedInputFile(img_resp.content, filename=f"{it.item_id}.jpg")
            except Exception as dl_err:
                logger.debug(f"Не удалось скачать фото #{it.item_id}: {dl_err}")

        # Отправляем фото и сохраняем telegram file_id в кэш
        if photo_file:
            try:
                sent_msg = await bot.send_photo(
                    chat_id=user_id,
                    photo=photo_file,
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                if sent_msg.photo:
                    set_cached_photo_file_id(it.item_id, sent_msg.photo[-1].file_id)
                logger.info(f"     🚀 [Радар #{search_id}{owner_str}] Доставлено (с фото): {it.price} · «{it.title[:35]}»")
                return True
            except (TelegramForbiddenError, TelegramRetryAfter):
                raise
            except Exception as photo_send_err:
                logger.warning(f"⚠️ [РАДАР #{search_id}] Сбой send_photo: {photo_send_err}, отправляем текстом...")

        # Фолбэк: если фото нет или байты не скачались
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
        logger.warning(f"🚫 [БЛОКИРОВКА] Пользователь ID:{user_id} заблокировал бота. Отключаем его радары.")
        await database.deactivate_user_searches(user_id)
        return False
    except TelegramRetryAfter as retry_err:
        logger.warning(f"⏳ [FLOODWAIT] Telegram лимит отправки: пауза {retry_err.retry_after} сек.")
        await asyncio.sleep(retry_err.retry_after + 1)
        return True
    except Exception as send_err:
        logger.error(f"❌ [РАДАР #{search_id}] Не удалось доставить лот #{it.item_id}: {send_err}")
        return True


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
        html_content = await asyncio.to_thread(parser.fetch_page, clean_url)
        fetch_ms = int((time.monotonic() - fetch_t0) * 1000)

        if not html_content:
            return False

        items = parser.parse_items(html_content)
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
            stop_words = radar.get("stop_words", [])
            user_str = get_user_display_name(radar)
            source_tag = f"{fetch_ms} мс" if idx == 0 else "⚡ кэш запроса"

            # Получаем актуальный черный список продавцов пользователя
            blocked_sellers = await database.get_blocked_sellers(user_id)

            seen_ids = await database.get_seen_item_ids_for_search(radar_id)
            new_items = [it for it in items if it.item_id not in seen_ids]

            if not new_items:
                logger.info(f"  {w_tag}🔹 Радар #{radar_id} [{user_str}] «{search_title}»: проверено {len(items)} лотов [{source_tag}] (новых нет)")
                continue

            # ВАЖНО: Записываем ВСЕ новые ID в базу СРАЗУ ЖЕ, чтобы они не всплывали повторно
            await database.mark_items_seen_bulk(radar_id, [it.item_id for it in new_items])

            # 1. Отбираем только действительно свежие объявления (до 10 минут)
            fresh_items = [it for it in new_items if is_ad_fresh_enough(it.date, getattr(it, "timestamp", None))]

            if not fresh_items:
                ages = [format_item_age(it.date, getattr(it, "timestamp", None)) for it in new_items[:2]]
                age_summary = ", ".join(ages)
                if len(new_items) > 2:
                    age_summary += f", и ещё {len(new_items) - 2}"
                items_label = plural_form(len(new_items), "лот", "лота", "лотов")
                logger.info(
                    f"  {w_tag}🔹 Радар #{radar_id} [{user_str}] «{search_title}»: {items_label} в ротации [{source_tag}] "
                    f"(отсеян как старый: {age_summary}; порог 10 мин)"
                )
                continue

            # Защита от пачки: если ДАЖЕ среди свежих объявлений их больше 5 штук одновременно
            if len(fresh_items) > 5:
                fresh_label = plural_form(len(fresh_items), "свежий лот", "свежих лота", "свежих лотов")
                logger.info(
                    f"  {w_tag}📦 Радар #{radar_id} [{user_str}] «{search_title}»: пачка из {fresh_label} (сохранено в базу без спама)"
                )
                continue

            fresh_label = plural_form(len(fresh_items), "СВЕЖИЙ ЛОТ", "СВЕЖИХ ЛОТА", "СВЕЖИХ ЛОТОВ")
            logger.info(f"  {w_tag}🔥 Радар #{radar_id} [{user_str}] «{search_title}»: НАЙДЕНО {len(fresh_items)} {fresh_label}!")

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
                await NOTIFICATION_QUEUE.put(NotificationTask(
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
    Главный бесконечный цикл фонового мониторинга поисков.
    Оптимизирован для 512 МБ RAM и минимального числа HTTP-запросов к Авито (дедупликация URL).
    Использует пул параллельных асинхронных воркеров для масштабирования на сотни и тысячи ссылок.
    """
    pool_size = max(1, config.MAX_CONCURRENT_WORKERS)
    # По требованию пользователя работаем напрямую (без прокси)
    parser_pool = ParserPool(pool_size, proxy_url=None)
    if parser is not None and parser_pool.parsers:
        parser_pool.parsers[0] = parser

    logger.info(f"🚀 Запуск фонового сервиса мониторинга Авито (Worker Pool: {pool_size} воркеров)...")
    cycle_count = 0
    consecutive_failures = 0
    alert_sent = False
    FAILURE_THRESHOLD = 5

    while True:
        try:
            active_searches = await database.get_all_active_searches()

            if active_searches:
                cycle_count += 1
                start_time = time.time()
                # 1. Группируем радары по нормализованному URL (без context=..., localPriority=... и т.д.)
                url_groups = defaultdict(list)
                for search in active_searches:
                    clean_url = AvitoParser.normalize_url(search["url"])
                    url_groups[clean_url].append(search)

                radars_label = plural_form(len(active_searches), "радар", "радара", "радаров")
                urls_label = plural_form(len(url_groups), "ссылка Авито", "ссылки Авито", "ссылок Авито")
                num_workers = min(pool_size, len(url_groups))
                workers_label = plural_form(num_workers, "воркер", "воркера", "воркеров")

                logger.info(
                    f"🔄 [КРУГ #{cycle_count}] Запуск проверки: {radars_label} ({urls_label}) | "
                    f"Параллельных потоков: {workers_label}..."
                )

                # 2. Очередь задач для параллельных воркеров с защитой от пропуска при сбоях (Round Retry)
                task_queue: asyncio.Queue = asyncio.Queue()
                for clean_url, radars_for_url in url_groups.items():
                    await task_queue.put((clean_url, radars_for_url, 0))

                async def worker_task(worker_id: int) -> Tuple[int, int]:
                    w_parser = parser_pool.get_parser(worker_id - 1)
                    w_success = 0
                    w_fail = 0

                    # Начальный сдвиг старта между воркерами для исключения одновременных сетевых пиков
                    if worker_id > 1:
                        await asyncio.sleep((worker_id - 1) * 1.5)

                    while True:
                        # Если данный парсер находится в кулдауне из-за 429/403, ждем его завершения
                        while w_parser.is_blocked():
                            wait_s = min(w_parser.block_remaining(), 5)
                            if wait_s <= 0:
                                break
                            await asyncio.sleep(wait_s)

                        try:
                            # Ожидаем задачу из очереди (до 2.5 сек, пока другие воркеры могут передать задачу на повтор)
                            clean_url, radars_for_url, retry_attempt = await asyncio.wait_for(task_queue.get(), timeout=2.5)
                        except (asyncio.TimeoutError, asyncio.QueueEmpty):
                            break

                        ok = await process_url_group(bot, w_parser, clean_url, radars_for_url, worker_id=worker_id)
                        if ok:
                            w_success += 1
                        else:
                            # Если опрос ссылки не удался (например, временный 403) — не теряем радар, а передаем другому воркеру!
                            if retry_attempt < 1:
                                human_name = AvitoParser._human_url(clean_url)
                                logger.info(
                                    f"  [W{worker_id}] 🔄 {human_name}: сбой проверки, задача возвращена в очередь на мгновенный повтор..."
                                )
                                await asyncio.sleep(1.0)
                                await task_queue.put((clean_url, radars_for_url, retry_attempt + 1))
                            else:
                                w_fail += 1

                        task_queue.task_done()
                        # Безопасная пауза между запросами конкретного воркера
                        await asyncio.sleep(random.uniform(2.0, 3.5))

                    return w_success, w_fail

                # Запуск параллельных воркеров
                worker_coroutines = [worker_task(w_id) for w_id in range(1, num_workers + 1)]
                worker_results = await asyncio.gather(*worker_coroutines)

                success_count = sum(r[0] for r in worker_results)
                fail_count = sum(r[1] for r in worker_results)

                # 🛡 Анализ здоровья мониторинга и защита от сбоев
                if success_count == 0 and fail_count > 0:
                    consecutive_failures += 1
                    logger.warning(
                        f"⚠️ [СБОЙ МОНИТОРИНГА] Ни один радар не смог обновиться (сбой #{consecutive_failures}/{FAILURE_THRESHOLD} подряд)..."
                    )
                    if consecutive_failures >= FAILURE_THRESHOLD and not alert_sent:
                        alert_sent = True
                        current_p = config.PROXY_URL or "Прямое соединение (без прокси)"
                        alert_msg = (
                            f"🚨 <b>ВНИМАНИЕ: Сбой мониторинга Авито!</b>\n\n"
                            f"Мониторинг не может получить данные уже <b>{consecutive_failures} кругов подряд</b> (~{consecutive_failures} мин).\n"
                            f"❌ Все запросы к Авито завершились ошибкой (403, 429 или таймаут).\n\n"
                            f"🌐 <i>Текущий прокси:</i> <code>{html.escape(current_p)}</code>\n"
                            f"🛠 <i>Рекомендация:</i> Проверьте прокси-сервер или перезапустите бота."
                        )
                        for aid in config.ADMIN_IDS:
                            try:
                                await bot.send_message(chat_id=aid, text=alert_msg, parse_mode="HTML")
                            except Exception as alert_err:
                                logger.error(f"Не удалось отправить алерт админу {aid}: {alert_err}")
                elif success_count > 0:
                    if alert_sent:
                        alert_sent = False
                        recovery_msg = (
                            f"✅ <b>Связь с Авито восстановлена!</b>\n\n"
                            f"Мониторинг снова успешно получает данные (успешных запросов: {success_count}). "
                            f"Бот работает в штатном режиме."
                        )
                        for aid in config.ADMIN_IDS:
                            try:
                                await bot.send_message(chat_id=aid, text=recovery_msg, parse_mode="HTML")
                            except Exception as rec_err:
                                logger.error(f"Не удалось отправить уведомление о восстановлении {aid}: {rec_err}")
                    consecutive_failures = 0

                duration = time.time() - start_time
                logger.info(
                    f"💤 [КРУГ #{cycle_count}] Завершён за {duration:.1f} сек. | "
                    f"Проверено: {radars_label} | Запросов к Авито: {success_count}/{len(url_groups)} | "
                    f"Пауза {CHECK_INTERVAL} сек.\n"
                )
            else:
                logger.info("ℹ️ [МОНИТОРИНГ] Нет активных радаров для сканирования.")

            cycle_count += 1
            # Каждые 50 циклов (~50 минут) очищаем базу от старых объявлений и чистим RAM
            if cycle_count >= 50:
                cycle_count = 0
                await database.cleanup_old_seen_items(days=5)
                gc.collect()
                logger.info("🧹 [ОПТИМИЗАЦИЯ] Очистка памяти (gc.collect) и БД успешно завершена")

            await asyncio.sleep(CHECK_INTERVAL)

        except asyncio.CancelledError:
            logger.info("🛑 Фоновый сервис мониторинга остановлен.")
            break
        except Exception as e:
            logger.error(f"❌ Фатальная ошибка в цикле мониторинга: {e}", exc_info=True)
            alert_text = (
                f"🚨 <b>Алерт системы мониторинга!</b>\n\n"
                f"Произошёл сбой в основном цикле планировщика:\n"
                f"<code>{html.escape(str(e)[:300])}</code>\n\n"
                f"<i>Автоматический перезапуск цикла через 10 секунд...</i>"
            )
            for aid in config.ADMIN_IDS:
                try:
                    await bot.send_message(chat_id=aid, text=alert_text, parse_mode="HTML")
                except Exception:
                    pass
            gc.collect()
            await asyncio.sleep(10)


async def notification_worker(bot: Bot, parser: AvitoParser):
    """
    Фоновый воркер асинхронной очереди уведомлений (Outbox Worker).
    Отправляет уведомления в Telegram независимо от цикла сканирования радаров.
    Соблюдает безопасный лимит скорости (~20 сообщений/сек) и обрабатывает ошибки Telegram.
    """
    logger.info("📬 Фоновый сервис очереди уведомлений Telegram запущен")
    while True:
        try:
            task: NotificationTask = await NOTIFICATION_QUEUE.get()
            try:
                await send_ad_to_user(
                    bot=bot,
                    parser=parser,
                    user_id=task.user_id,
                    search_id=task.radar_id,
                    search_title=task.search_title,
                    it=task.item,
                    user_label=task.user_label
                )
            except TelegramForbiddenError:
                logger.warning(f"🚫 [БЛОКИРОВКА] Пользователь ID:{task.user_id} заблокировал бота. Отключаем его радары.")
                await database.deactivate_user_searches(task.user_id)
            except TelegramRetryAfter as retry_err:
                logger.warning(f"⏳ [FLOODWAIT] Telegram лимит: пауза {retry_err.retry_after} сек.")
                await asyncio.sleep(retry_err.retry_after + 1)
            except Exception as e:
                logger.error(f"❌ Ошибка отправки уведомления для ID:{task.user_id}: {e}")
            finally:
                NOTIFICATION_QUEUE.task_done()
                # Мягкое ограничение скорости: ~20 сообщений в секунду суммарно (50 мс)
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            logger.info("🛑 Сервис очереди уведомлений остановлен.")
            break
        except Exception as loop_err:
            logger.error(f"Неожиданный сбой в notification_worker: {loop_err}")
            await asyncio.sleep(1)


async def supervised_monitoring_worker(bot: Bot, parser: AvitoParser):
    """
    Супервизор фонового мониторинга:
    - Запускает бесконечный мониторинг;
    - При любом непредвиденном падении отправляет алерт администраторам и перезапускает процесс.
    """
    while True:
        try:
            await monitoring_worker(bot, parser)
        except asyncio.CancelledError:
            break
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


def start_background_services(bot: Bot) -> List[asyncio.Task]:
    """
    Запускает все фоновые сервисы:
    1. Очередь отправки уведомлений (notification_worker)
    2. Мониторинг Авито под супервизором (supervised_monitoring_worker)
    Возвращает список фоновых задач для управления их жизненным циклом.
    """
    shared_parser = AvitoParser()
    notif_task = asyncio.create_task(notification_worker(bot, shared_parser))
    monitor_task = asyncio.create_task(supervised_monitoring_worker(bot, shared_parser))
    return [notif_task, monitor_task]




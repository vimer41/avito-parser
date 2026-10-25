import asyncio
import gc
import html
import logging
import random
import re
import time
from collections import defaultdict
from typing import Optional, List, Dict
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

import config
import database
from config import CHECK_INTERVAL
from parser import AvitoParser, AvitoItem

logger = logging.getLogger(__name__)

LAST_SCAN_TIMESTAMP = 0


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

    lines = [
        "⚡️ <b>НОВЫЙ ЛОТ НА АВИТО</b>",
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



def is_ad_fresh_enough(date_str: Optional[str]) -> bool:
    """
    Проверяет, действительно ли объявление свежее (опубликовано только что или считанные минуты назад).
    Отсекает старые объявления ('1 час назад', '2 часа назад', 'вчера', календарные даты и т.д.).
    """
    if not date_str:
        return True
    d = date_str.lower().strip()
    # Свежие маркеры публикации
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


async def send_ad_to_user(
    bot: Bot,
    parser: AvitoParser,
    user_id: int,
    search_id: int,
    search_title: str,
    it: AvitoItem
) -> bool:
    """
    Отправляет уведомление пользователю (фото или текст).
    Возвращает False, если пользователь заблокировал бота (для прерывания цикла отправки).
    """
    caption = build_notification_text(it, search_title)
    keyboard = build_notification_keyboard(it.url, search_id, has_seller=bool(it.seller_name))

    try:
        photo_file: Optional[BufferedInputFile] = None
        if it.image_url:
            try:
                img_resp = await asyncio.to_thread(parser.session.get, it.image_url, timeout=8)
                if img_resp.status_code == 200 and len(img_resp.content) > 500:
                    photo_file = BufferedInputFile(img_resp.content, filename=f"{it.item_id}.jpg")
            except Exception as dl_err:
                logger.debug(f"Не удалось скачать фото #{it.item_id}: {dl_err}")

        # Отправляем фото ТОЛЬКО если успешно скачали реальные байты в BufferedInputFile.
        # Никогда не передаем сырой URL в Telegram, так как Telegram-сервер получит 403 от Avito CDN!
        if photo_file:
            try:
                await bot.send_photo(
                    chat_id=user_id,
                    photo=photo_file,
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                logger.info(f"🚀 [РАДАР #{search_id}] УСПЕШНО ДОСТАВЛЕНО С ФОТО: #{it.item_id} «{it.title[:30]}...» ({it.price}) пользователю ID: {user_id}")
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
        logger.info(f"🚀 [РАДАР #{search_id}] УСПЕШНО ДОСТАВЛЕНО ТЕКСТОМ: #{it.item_id} «{it.title[:30]}...» ({it.price}) пользователю ID: {user_id}")
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


async def process_url_group(bot: Bot, parser: AvitoParser, clean_url: str, radars_for_url: List[dict]):
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
        html_content = await asyncio.to_thread(parser.fetch_page, clean_url)
        if not html_content:
            return

        items = parser.parse_items(html_content)
        if not items:
            logger.info(f"📡 [ПАРСЕР] По URL «{clean_url[:60]}...»: страница получена, лотов в каталоге не найдено")
            return

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
        for radar in radars_for_url:
            radar_id = radar["id"]
            user_id = radar["user_id"]
            search_title = radar["title"]
            stop_words = radar.get("stop_words", [])

            # Получаем актуальный черный список продавцов пользователя
            blocked_sellers = await database.get_blocked_sellers(user_id)

            seen_ids = await database.get_seen_item_ids_for_search(radar_id)
            new_items = [it for it in items if it.item_id not in seen_ids]

            logger.info(
                f"📡 [РАДАР #{radar_id}] «{search_title}» (User: {user_id}, Тариф: {radar.get('tariff_plan', 'standart')}): "
                f"в выдаче {len(items)} лотов, новых для отправки: {len(new_items)}"
            )

            if not new_items:
                continue

            # ВАЖНО: Записываем ВСЕ новые ID в базу СРАЗУ ЖЕ до фильтра свежести!
            # Тогда даже если лоту '1 час назад', на следующем круге он уже будет в seen_ids
            # и никогда повторно не всплывет в логах.
            await database.mark_items_seen_bulk(radar_id, [it.item_id for it in new_items])

            # Защита от пачки при первом запуске или сдвиге выдачи
            if len(new_items) > 5:
                logger.info(
                    f"📦 [РАДАР #{radar_id}] Обнаружена пачка из {len(new_items)} лотов. "
                    f"Запоминаем без спама пользователю."
                )
                continue

            # Отправляем в хронологическом порядке (снизу вверх)
            for it in reversed(new_items):
                # 1. Проверяем свежесть
                if not is_ad_fresh_enough(it.date):
                    logger.info(
                        f"⏳ [РАДАР #{radar_id}] Объявление #{it.item_id} пропущено как старое (дата: '{it.date}')"
                    )
                    continue

                # 2. Проверяем стоп-слова
                matched_stop = is_stop_word_matched(it, stop_words)
                if matched_stop:
                    logger.info(
                        f"🛡 [РАДАР #{radar_id}] Объявление #{it.item_id} отсеяно по стоп-слову «{matched_stop}»"
                    )
                    continue

                # 3. Проверяем черный список продавцов
                if it.seller_name and it.seller_name.lower().strip() in blocked_sellers:
                    logger.info(
                        f"🚷 [РАДАР #{radar_id}] Объявление #{it.item_id} отсеяно: "
                        f"продавец «{it.seller_name}» в черном списке пользователя ID:{user_id}"
                    )
                    continue

                ok = await send_ad_to_user(bot, parser, user_id, radar_id, search_title, it)
                if not ok:
                    break
                await asyncio.sleep(0.5)

        global LAST_SCAN_TIMESTAMP
        LAST_SCAN_TIMESTAMP = time.time()

    except Exception as e:
        logger.error(f"❌ [ГРУППА URL] Ошибка при обработке URL «{clean_url[:60]}...»: {e}", exc_info=True)


async def monitoring_worker(bot: Bot):
    """
    Главный бесконечный цикл фонового мониторинга поисков.
    Оптимизирован для 512 МБ RAM и минимального числа HTTP-запросов к Авито (дедупликация URL).
    """
    logger.info("🚀 Запуск фонового сервиса мониторинга Авито...")
    parser = AvitoParser()
    cycle_count = 0

    while True:
        try:
            active_searches = await database.get_all_active_searches()

            if active_searches:
                logger.info(f"🔄 [МОНИТОРИНГ] Начинаем круг сканирования ({len(active_searches)} активных радаров)...")

                # 1. Группируем радары по нормализованному URL (без context=..., localPriority=... и т.д.)
                url_groups = defaultdict(list)
                for search in active_searches:
                    clean_url = AvitoParser.normalize_url(search["url"])
                    url_groups[clean_url].append(search)

                logger.info(f"🌐 [МОНИТОРИНГ] Уникальных URL-запросов к Авито: {len(url_groups)} (радаров в базе: {len(active_searches)})")

                # 2. Делаем ровно 1 запрос к Авито на каждый уникальный URL
                for clean_url, radars_for_url in url_groups.items():
                    await process_url_group(bot, parser, clean_url, radars_for_url)
                    # Пауза между запросами к разным ссылкам для распределения нагрузки
                    await asyncio.sleep(random.uniform(3.0, 5.0))
            else:
                logger.info("ℹ️ [МОНИТОРИНГ] Нет активных радаров для сканирования.")

            cycle_count += 1
            # Каждые 50 циклов (~50 минут) очищаем базу от старых объявлений и чистим RAM
            if cycle_count >= 50:
                cycle_count = 0
                await database.cleanup_old_seen_items(days=5)
                gc.collect()
                logger.info("🧹 [ОПТИМИЗАЦИЯ] Очистка памяти (gc.collect) и БД успешно завершена")

            # Ждем интервал до следующего круга проверок
            logger.info(f"💤 [МОНИТОРИНГ] Круг завершён. Ожидание {CHECK_INTERVAL} сек. до следующей проверки...")
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




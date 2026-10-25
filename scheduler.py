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



def is_ad_fresh_enough(date_str: Optional[str], timestamp: Optional[int] = None) -> bool:
    """
    Проверяет, действительно ли объявление свежее (опубликовано до 15 минут назад).
    1. Если есть точный timestamp из SSR JSON — вычисляет возраст лота с точностью до секунды.
    2. Если timestamp нет — использует текстовые маркеры (фолбэк).
    """
    if timestamp:
        age_seconds = time.time() - timestamp
        # До 15 минут (900 сек) считаем свежим
        return age_seconds <= 15 * 60

    if not date_str:
        return True
    d = date_str.lower().strip()
    if any(k in d for k in ["секунд", "только что"]):
        return True
    if "минут" in d:
        m = re.search(r"(\d+)\s+минут", d)
        if m:
            mins = int(m.group(1))
            return mins <= 15  # максимум 15 минут назад
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
    Возвращает False, если пользователь заблокировал бота (для прерывания цикла отправки).
    """
    caption = build_notification_text(it, search_title)
    keyboard = build_notification_keyboard(it.url, search_id, has_seller=bool(it.seller_name))
    owner_str = f" [{user_label}]" if user_label else f" ID:{user_id}"

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
            return False

        items = parser.parse_items(html_content)
        if not items:
            logger.info(f"📡 [ПАРСЕР] По URL «{clean_url[:60]}...»: страница получена, лотов в каталоге не найдено")
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
        for radar in radars_for_url:
            radar_id = radar["id"]
            user_id = radar["user_id"]
            search_title = radar["title"]
            stop_words = radar.get("stop_words", [])
            user_str = get_user_display_name(radar)

            # Получаем актуальный черный список продавцов пользователя
            blocked_sellers = await database.get_blocked_sellers(user_id)

            seen_ids = await database.get_seen_item_ids_for_search(radar_id)
            new_items = [it for it in items if it.item_id not in seen_ids]

            if not new_items:
                logger.info(f"  🔹 Радар #{radar_id} [{user_str}] «{search_title}»: проверено {len(items)} лотов (новых нет)")
                continue

            # ВАЖНО: Записываем ВСЕ новые ID в базу СРАЗУ ЖЕ, чтобы они не всплывали повторно
            await database.mark_items_seen_bulk(radar_id, [it.item_id for it in new_items])

            # 1. Отбираем только действительно свежие объявления (до 15 минут)
            fresh_items = [it for it in new_items if is_ad_fresh_enough(it.date, getattr(it, "timestamp", None))]

            if not fresh_items:
                logger.info(f"  🔹 Радар #{radar_id} [{user_str}] «{search_title}»: {len(new_items)} лотов в ротации Авито (старые, пропущены)")
                continue

            # Защита от пачки: если ДАЖЕ среди свежих объявлений их больше 5 штук одновременно
            if len(fresh_items) > 5:
                logger.info(
                    f"  📦 Радар #{radar_id} [{user_str}] «{search_title}»: пачка из {len(fresh_items)} свежих лотов (запомнено без спама)"
                )
                continue

            logger.info(f"  🔥 Радар #{radar_id} [{user_str}] «{search_title}»: НАЙДЕНО {len(fresh_items)} СВЕЖИХ ЛОТОВ!")

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

                ok = await send_ad_to_user(bot, parser, user_id, radar_id, search_title, it, user_str)
                if not ok:
                    break
                await asyncio.sleep(0.5)

        global LAST_SCAN_TIMESTAMP
        LAST_SCAN_TIMESTAMP = time.time()
        return True

    except Exception as e:
        logger.error(f"❌ [ГРУППА URL] Ошибка при обработке URL «{clean_url[:60]}...»: {e}", exc_info=True)
        return False


async def monitoring_worker(bot: Bot):
    """
    Главный бесконечный цикл фонового мониторинга поисков.
    Оптимизирован для 512 МБ RAM и минимального числа HTTP-запросов к Авито (дедупликация URL).
    """
    logger.info("🚀 Запуск фонового сервиса мониторинга Авито...")
    parser = AvitoParser()
    cycle_count = 0
    consecutive_failures = 0
    alert_sent = False
    FAILURE_THRESHOLD = 5

    while True:
        try:
            active_searches = await database.get_all_active_searches()

            if active_searches:
                start_time = time.time()
                # 1. Группируем радары по нормализованному URL (без context=..., localPriority=... и т.д.)
                url_groups = defaultdict(list)
                for search in active_searches:
                    clean_url = AvitoParser.normalize_url(search["url"])
                    url_groups[clean_url].append(search)

                logger.info(f"🔄 [КРУГ МОНИТОРИНГА] Запуск проверки: {len(active_searches)} радаров ({len(url_groups)} ссылок Авито)...")

                # 2. Делаем ровно 1 запрос к Авито на каждый уникальный URL
                success_count = 0
                fail_count = 0
                for clean_url, radars_for_url in url_groups.items():
                    ok = await process_url_group(bot, parser, clean_url, radars_for_url)
                    if ok:
                        success_count += 1
                    else:
                        fail_count += 1
                    # Пауза между запросами к разным ссылкам для распределения нагрузки
                    await asyncio.sleep(random.uniform(2.5, 4.0))

                # 🛡 Анализ здоровья мониторинга и защита от сбоев
                if success_count == 0 and fail_count > 0:
                    consecutive_failures += 1
                    logger.warning(
                        f"⚠️ [СБОЙ МОНИТОРИНГА] Ни один радар не смог обновиться (сбой #{consecutive_failures}/{FAILURE_THRESHOLD} подряд)..."
                    )
                    if consecutive_failures >= FAILURE_THRESHOLD and not alert_sent:
                        alert_sent = True
                        current_p = getattr(parser, "current_proxy", None) or "Прямое соединение (без прокси)"
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
                logger.info(f"💤 Круг завершён за {duration:.1f} сек. Пауза {CHECK_INTERVAL} сек.\n")
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




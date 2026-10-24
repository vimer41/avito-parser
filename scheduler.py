import asyncio
import gc
import html
import logging
import random
import re
from typing import Optional
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

import database
from config import CHECK_INTERVAL
from parser import AvitoParser, AvitoItem

logger = logging.getLogger(__name__)


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


def build_notification_keyboard(item_url: str, search_id: int) -> InlineKeyboardMarkup:
    """Стильные инлайн-кнопки под карточкой товара."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="↗️ Открыть на Авито", url=item_url),
            InlineKeyboardButton(text="📋 Скопировать отклик", callback_data=f"copy_msg:{search_id}")
        ],
        [
            InlineKeyboardButton(text="🛡 В стоп-слова", callback_data=f"add_stop_from_item:{search_id}")
        ]
    ])
    return kb



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


async def process_search(bot: Bot, parser: AvitoParser, search: dict):
    search_id = search["id"]
    user_id = search["user_id"]
    search_title = search["title"]
    url = search["url"]
    stop_words = search.get("stop_words", [])

    try:
        # Выполняем запрос в отдельном потоке (т.к. curl_cffi синхронный в Session)
        html_content = await asyncio.to_thread(parser.fetch_page, url)
        if not html_content:
            return

        items = parser.parse_items(html_content)
        if not items:
            return

        # Новые объявления идут сверху вниз. Чтобы присылать в хронологическом порядке,
        # разворачиваем список новых элементов
        new_items = []
        for it in items:
            if not await database.is_item_seen(search_id, it.item_id):
                new_items.append(it)

        # Защита от спама старыми товарами: если "новых" сразу больше 5 штук
        # (первичная подгрузка или сдвиг выдачи Авито), просто запоминаем их в базу без спама
        if len(new_items) > 5:
            logger.info(f"Обнаружена пачка из {len(new_items)} лотов для #{search_id}. Запоминаем без спама пользователю.")
            for it in new_items:
                await database.mark_item_seen(search_id, it.item_id)
            return

        # Отправляем в правильном порядке (от менее свежих к самым свежим)
        for it in reversed(new_items):
            # 1. Проверяем свежесть: отправляем ТОЛЬКО лоты, выложенные после добавления поиска
            if not is_ad_fresh_enough(it.date):
                logger.info(f"Объявление {it.item_id} пропущено как старое (дата: '{it.date}')")
                await database.mark_item_seen(search_id, it.item_id)
                continue

            # 2. Проверяем стоп-слова
            matched_stop = is_stop_word_matched(it, stop_words)
            if matched_stop:
                logger.info(f"Объявление {it.item_id} пропущено по стоп-слову '{matched_stop}'")
                await database.mark_item_seen(search_id, it.item_id)
                continue

            caption = build_notification_text(it, search_title)
            keyboard = build_notification_keyboard(it.url, search_id)

            try:
                if it.image_url:
                    # Скачиваем изображение через сессию парсера, чтобы Telegram-сервер не блокировался CDN Авито
                    photo_to_send = it.image_url
                    try:
                        img_resp = parser.session.get(it.image_url, timeout=10)
                        if img_resp.status_code == 200 and len(img_resp.content) > 200:
                            photo_to_send = BufferedInputFile(img_resp.content, filename=f"{it.item_id}.jpg")
                    except Exception as dl_err:
                        logger.debug(f"Не удалось предварительно скачать фото {it.image_url}: {dl_err}")

                    await bot.send_photo(
                        chat_id=user_id,
                        photo=photo_to_send,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                else:
                    await bot.send_message(
                        chat_id=user_id,
                        text=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        disable_web_page_preview=False
                    )

                logger.info(f"Отправлено новое объявление {it.item_id} пользователю {user_id}")

            except Exception as send_err:
                # Если отправка фото не удалась (например, неверный URL картинки или Telegram заблокировал),
                # отправляем текстом
                logger.warning(f"Не удалось отправить фото {it.image_url}: {send_err}, отправляем текстом")
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                except Exception as msg_err:
                    logger.error(f"Не удалось отправить сообщение {it.item_id}: {msg_err}")

            # Запоминаем ID объявления
            await database.mark_item_seen(search_id, it.item_id)
            await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"Ошибка при обработке поиска #{search_id} ({search_title}): {e}")


async def monitoring_worker(bot: Bot):
    """
    Главный бесконечный цикл фонового мониторинга поисков.
    Оптимизирован для 512 МБ RAM: выполняет регулярную очистку памяти и старых записей БД.
    """
    logger.info("Запуск фонового сервиса мониторинга Авито (low-memory режим)...")
    parser = AvitoParser()
    cycle_count = 0

    while True:
        try:
            active_searches = await database.get_all_active_searches()

            if active_searches:
                for search in active_searches:
                    await process_search(bot, parser, search)
                    # Небольшая пауза между запросами к разным ссылкам, чтобы распределять нагрузку
                    await asyncio.sleep(random.uniform(2.0, 4.0))

            cycle_count += 1
            # Каждые 50 циклов (~50 минут) очищаем базу от старых объявлений и чистим RAM
            if cycle_count >= 50:
                cycle_count = 0
                await database.cleanup_old_seen_items(days=5)
                gc.collect()
                logger.info("Оптимизация памяти (gc.collect) и БД успешно завершена")

            # Ждем интервал до следующего круга проверок
            await asyncio.sleep(CHECK_INTERVAL)

        except asyncio.CancelledError:
            logger.info("Фоновый сервис мониторинга остановлен.")
            break
        except Exception as e:
            logger.error(f"Непредвиденная ошибка в цикле мониторинга: {e}", exc_info=True)
            gc.collect()
            await asyncio.sleep(10)


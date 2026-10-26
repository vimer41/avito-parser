import os
import asyncio
import logging
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiogram.fsm.storage.memory import MemoryStorage

import config
import database
from logging.handlers import RotatingFileHandler
from handlers import main_router
from middlewares import AccessControlMiddleware, ActionLoggingMiddleware
from scheduler import monitoring_worker, start_background_services, close_image_session

# Принудительная поддержка UTF-8 в консоли Windows (для эмодзи)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Настройка многофайлового логирования (user_actions.log, parser.log, errors.log, bot.log)
from logger_setup import setup_logging
setup_logging()
logger = logging.getLogger("bot")





async def start_health_server():
    """
    Легковесный HTTP-сервер для хостингов вроде Render (Web Service).
    Render требует привязки к порту $PORT, иначе считает сервис упавшим.
    Потребляет < 1 МБ RAM.
    """
    port = os.getenv("PORT")
    if not port:
        return None
    try:
        app = web.Application()
        async def handle_ping(request):
            return web.Response(text="Avito Bot OK (512MB RAM Mode)")
        app.router.add_get("/", handle_ping)
        app.router.add_get("/health", handle_ping)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(port))
        await site.start()
        logger.info(f"Health-check HTTP сервер запущен на 0.0.0.0:{port} (Render-совместимость)")
        return runner
    except Exception as e:
        logger.warning(f"Не удалось запустить health-check сервер на порту {port}: {e}")
        return None


async def setup_bot_profile(bot: Bot):
    """
    Устанавливает официальное описание и презентацию бота в Telegram Bot API.
    Отображается пользователю в окне чата ДО ТОГО, как он нажмет «Запустить» (/start).
    """
    bot_description = (
        "🔥 AVITO RADAR — перехватывай лучшие предложения с выгодой!\n\n"
        "📌 Мониторинг Авито 24/7!\n"
        "Оповещения о новых лотах после проверки Авито.\n\n"
        "🎯 Что умеет бот?\n"
        "✔️ Доставка новинок с реальными живыми HD фото\n"
        "✔️ 📉 Price Drop 💎: авто-алерт при снижении цен продавцом\n"
        "✔️ Умный фильтр стоп-слов и чёрный список спамеров\n"
        "✔️ Точный поиск только в названиях или по всему тексту\n\n"
        "🎁 Бонус! Нажмите «Старт» и получите 6 часов бесплатного тест-драйва прямо сейчас!"
    )

    bot_short_description = "⚡️ Мониторинг новых объявлений и скидок на Авито 24/7. Фото, стоп-слова и алерты на снижение цен."

    try:
        await bot.set_my_description(description=bot_description, language_code="ru")
        await bot.set_my_description(description=bot_description)
        await bot.set_my_short_description(short_description=bot_short_description, language_code="ru")
        await bot.set_my_short_description(short_description=bot_short_description)
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="tariffs", description="Тарифы и оплата Telegram Stars"),
            BotCommand(command="profile", description="Моя подписка и радары"),
            BotCommand(command="help", description="Инструкция"),
            BotCommand(command="terms", description="Условия подписки и возврата"),
            BotCommand(command="paysupport", description="Помощь с оплатой / возврат Stars"),
        ])
        logger.info("Презентационное описание бота (до /start) успешно обновлено в Telegram API!")
    except Exception as e:
        logger.warning(f"Не удалось обновить описание бота в Telegram API: {e}")


async def main():
    if not config.BOT_TOKEN or config.BOT_TOKEN == "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ":
        logger.error(
            "ОШИБКА: Не указан BOT_TOKEN в файле .env!\n"
            "Пожалуйста, создайте бота через @BotFather в Telegram и укажите токен в файле .env."
        )
        return

    # Запуск легковесного сервера для Render Web Service (если передан PORT)
    web_runner = await start_health_server()
    bot = None
    background_tasks = []

    try:
        await database.init_db()
        bot = Bot(
            token=config.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dp = Dispatcher(storage=MemoryStorage())
        dp.message.middleware(ActionLoggingMiddleware())
        dp.callback_query.middleware(ActionLoggingMiddleware())
        access_middleware = AccessControlMiddleware()
        dp.message.middleware(access_middleware)
        dp.callback_query.middleware(access_middleware)
        dp.include_router(main_router)

        # A deploy must not discard unread Telegram updates.
        await bot.delete_webhook(drop_pending_updates=False)
        await setup_bot_profile(bot)
        logger.info("Stars checkout: enabled=%s standart=%s pro=%s days=%s",
                    config.STARS_PAYMENTS_ENABLED, config.STARS_PRICE_STANDART,
                    config.STARS_PRICE_PRO, config.STARS_SUBSCRIPTION_DAYS)
        background_tasks = start_background_services(bot)
        logger.info("Бот успешно запущен и готов к работе!")
        await dp.start_polling(bot)
    finally:
        logger.info("Завершение работы бота...")
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await close_image_session()
        if bot is not None:
            await bot.session.close()
        if web_runner:
            await web_runner.cleanup()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")

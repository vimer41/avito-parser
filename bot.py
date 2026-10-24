import os
import asyncio
import logging
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
import database
from logging.handlers import RotatingFileHandler
from handlers import main_router
from middlewares import AccessControlMiddleware, ActionLoggingMiddleware
from scheduler import monitoring_worker

# Принудительная поддержка UTF-8 в консоли Windows (для эмодзи)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Настройка логирования с автоматической ротацией (максимум 3 файла по 10 МБ)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler("bot.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)





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


async def main():
    if not config.BOT_TOKEN or config.BOT_TOKEN == "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ":
        logger.error(
            "ОШИБКА: Не указан BOT_TOKEN в файле .env!\n"
            "Пожалуйста, создайте бота через @BotFather в Telegram и укажите токен в файле .env."
        )
        return

    # Запуск легковесного сервера для Render Web Service (если передан PORT)
    web_runner = await start_health_server()

    # Инициализация базы данных
    await database.init_db()

    # Инициализация бота и диспетчера
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    
    # Регистрация детального логирования действий каждого пользователя
    dp.message.middleware(ActionLoggingMiddleware())
    dp.callback_query.middleware(ActionLoggingMiddleware())

    # Регистрация проверки прав доступа (вайтлист с одобрением админом)
    access_middleware = AccessControlMiddleware()
    dp.message.middleware(access_middleware)
    dp.callback_query.middleware(access_middleware)

    dp.include_router(main_router)


    # Удаляем вебхуки перед началом поллинга
    await bot.delete_webhook(drop_pending_updates=True)

    # Запуск фонового мониторинга поисков Авито
    worker_task = asyncio.create_task(monitoring_worker(bot))

    logger.info("Бот успешно запущен и готов к работе!")

    try:
        await dp.start_polling(bot)
    finally:
        logger.info("Завершение работы бота...")
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        if web_runner:
            await web_runner.cleanup()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")

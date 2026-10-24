import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
import database
from handlers import main_router
from middlewares import AccessControlMiddleware
from scheduler import monitoring_worker

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)


async def main():
    if not config.BOT_TOKEN or config.BOT_TOKEN == "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ":
        logger.error(
            "ОШИБКА: Не указан BOT_TOKEN в файле .env!\n"
            "Пожалуйста, создайте бота через @BotFather в Telegram и укажите токен в файле .env."
        )
        return

    # Инициализация базы данных
    await database.init_db()

    # Инициализация бота и диспетчера
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    
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
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")

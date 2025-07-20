import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardRemove
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# Импорт роутеров
from daily_report import daily_router, start_daily_report
from garbage_report import garbage_router, start_garbage_report

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=os.getenv("API_TOKEN"))
storage = MemoryStorage()
main_dp = Dispatcher(storage=storage)

# Регистрация роутеров
main_dp.include_router(daily_router)
main_dp.include_router(garbage_router)

@main_dp.message(Command("start", "help"))
async def start_command(message: types.Message, state: FSMContext):
    """Главное меню с выбором типа отчета"""
    await state.clear()
    
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📅 Ежедневный отчет")],
            [types.KeyboardButton(text="🗑️ Отчет по вывозу мусора")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer(
        "Выберите тип отчета:",
        reply_markup=keyboard
    )

@main_dp.message(lambda message: message.text == "📅 Ежедневный отчет")
async def handle_daily_report(message: types.Message, state: FSMContext):
    """Обработчик ежедневного отчета"""
    await message.answer(
        "Запускаем ежедневный отчет...",
        reply_markup=ReplyKeyboardRemove()
    )
    await start_daily_report(message, state)

@main_dp.message(lambda message: message.text == "🗑️ Отчет по вывозу мусора")
async def handle_garbage_report(message: types.Message, state: FSMContext):
    """Обработчик отчета по вывозу мусора"""
    await message.answer(
        "Запускаем отчет по вывозу мусора...",
        reply_markup=ReplyKeyboardRemove()
    )
    await start_garbage_report(message, state)

@main_dp.message(Command("stop", "cancel"))
async def reset_handler(message: types.Message, state: FSMContext):
    """Сброс состояния"""
    await state.clear()
    await message.answer(
        "Все действия отменены. Состояние сброшено.",
        reply_markup=ReplyKeyboardRemove()
    )

async def setup_webhook(webhook_url: str):
    """Настройка вебхука с повторными попытками"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            await bot.set_webhook(
                url=webhook_url,
                drop_pending_updates=True,
                allowed_updates=main_dp.resolve_used_update_types()
            )
            logger.info(f"Вебхук успешно установлен: {webhook_url}")
            return True
        except Exception as e:
            logger.error(f"Попытка {attempt + 1} из {max_retries} не удалась: {str(e)}")
            if attempt < max_retries - 1:
                await asyncio.sleep(5)
    
    logger.critical("Не удалось установить вебхук после нескольких попыток")
    return False

async def on_startup(app: web.Application):
    """Действия при запуске"""
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        if not await setup_webhook(webhook_url):
            logger.warning("Переключаюсь в polling-режим из-за ошибок вебхука")
            await bot.delete_webhook()
    else:
        logger.warning("WEBHOOK_URL не указан, использую polling-режим")

async def on_shutdown(app: web.Application):
    """Действия при выключении"""
    await bot.delete_webhook(drop_pending_updates=True)
    await storage.close()
    logger.info("Бот остановлен. Вебхук удален, хранилище закрыто.")

async def web_app():
    """Настройка веб-приложения для вебхука"""
    app = web.Application()
    webhook_handler = SimpleRequestHandler(main_dp, bot)
    webhook_handler.register(app, path="/webhook")
    
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    setup_application(app, main_dp, bot=bot)
    return app

async def polling_mode():
    """Режим polling для отладки"""
    logger.info("Запуск в polling-режиме...")
    await bot.delete_webhook()
    await main_dp.start_polling(bot)

async def main():
    """Основная функция запуска"""
    try:
        webhook_url = os.getenv("WEBHOOK_URL")
        use_webhook = webhook_url is not None
        
        if use_webhook:
            app = await web_app()
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host="0.0.0.0", port=8080)
            await site.start()
            logger.info("Сервер вебхука запущен на порту 8080")
        else:
            await polling_mode()
        
        await asyncio.Event().wait()
        
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}", exc_info=True)
    finally:
        await storage.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"Необработанная ошибка: {e}", exc_info=True)

import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# Импорт роутеров
from daily_report import router as daily_router
from garbage_report import router as garbage_router

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token=os.getenv("API_TOKEN"))
storage = MemoryStorage()
main_dp = Dispatcher(storage=storage)

# Регистрация роутеров
main_dp.include_router(daily_router)
main_dp.include_router(garbage_router)

@main_dp.message(Command("start"))
async def start_command(message: types.Message, state: FSMContext):
    """Обработчик команды /start с выбором типа отчета"""
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📅 Ежедневный отчет")],
            [types.KeyboardButton(text="🗑️ Отчет по вывозу мусора")]
        ],
        resize_keyboard=True
    )
    await message.answer("Выберите тип отчета:", reply_markup=keyboard)
    await state.clear()

@main_dp.message(lambda message: message.text == "📅 Ежедневный отчет")
async def handle_daily_report(message: types.Message, state: FSMContext):
    """Запуск сценария ежедневного отчета"""
    from daily_report import start_daily_report
    await start_daily_report(message, state)

@main_dp.message(lambda message: message.text == "🗑️ Отчет по вывозу мусора")
async def handle_garbage_report(message: types.Message, state: FSMContext):
    """Запуск сценария отчета по вывозу мусора"""
    from garbage_report import start_garbage_report
    await start_garbage_report(message, state)

async def on_startup(bot: Bot):
    """Действия при запуске бота"""
    webhook_url = os.getenv("WEBHOOK_URL")  # Полный URL вашего вебхука
    await bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
        allowed_updates=main_dp.resolve_used_update_types()
    )
    logger.info(f"Вебхук установлен на {webhook_url}")

async def on_shutdown(bot: Bot):
    """Действия при остановке бота"""
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Вебхук удален")

async def main():
    """Главная функция для запуска бота в режиме вебхука"""
    from aiohttp import web
    
    # Создаем aiohttp приложение
    app = web.Application()
    
    # Настраиваем вебхук
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=main_dp,
        bot=bot,
    )
    
    # Регистрируем обработчик вебхука
    webhook_requests_handler.register(app, path="/webhook")
    
    # Настраиваем обработчики запуска/остановки
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    # Настраиваем приложение aiogram
    setup_application(app, main_dp, bot=bot)
    
    # Запускаем сервер
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=8080)
    await site.start()
    
    logger.info("Сервер вебхука запущен")
    
    # Бесконечный цикл для работы сервера
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}", exc_info=True)

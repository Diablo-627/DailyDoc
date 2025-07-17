import logging
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from daily_report import dp as daily_dp, start_daily_report
from garbage_report import dp as garbage_dp, start_garbage_report

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token=os.getenv("API_TOKEN"))
storage = MemoryStorage()
main_dp = Dispatcher(storage=storage)

# Регистрация роутеров других модулей
main_dp.include_router(daily_dp)
main_dp.include_router(garbage_dp)

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
    await start_daily_report(message, state)

@main_dp.message(lambda message: message.text == "🗑️ Отчет по вывозу мусора")
async def handle_garbage_report(message: types.Message, state: FSMContext):
    """Запуск сценария отчета по вывозу мусора"""
    await start_garbage_report(message, state)

if __name__ == "__main__":
    from aiogram import executor
    executor.start_polling(main_dp, skip_updates=True)
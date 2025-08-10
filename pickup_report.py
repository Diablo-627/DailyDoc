import os
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)

class PickupReport:
    def __init__(self, bot, template_path: str, photos_dir: str, temp_dir: str):
        self.bot = bot
        self.template_path = template_path
        self.photos_dir = photos_dir
        self.temp_dir = temp_dir
        os.makedirs(self.photos_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)
        self.router = Router()

        # Обработчик команды /start в этом сценарии
        @self.router.message(Command("start"))
        async def _local_start(message: Message):
            await message.answer(
                "Вы в режиме 'Вывозной'.\n"
                "Это упрощённый модуль — добавьте поля/теги и генерацию по примеру daily_report.py"
            )

    async def start_for_user(self, chat_id: int):
        try:
            await self.bot.send_message(chat_id, "Начинаем Вывозной отчет. Введите /start для запуска локального режима.")
        except Exception as e:
            logger.error("pickup start error: %s", e)

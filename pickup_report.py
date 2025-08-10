# pickup_report.py
import os
import logging
from aiogram import Router
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

        # простой /start в роутере, чтобы при тестах не было ошибок
        @self.router.message("start")
        async def _local_start(message: Message):
            await message.answer(
                "Вы в режиме 'Вывозной'.\n"
                "Я пока что — упрощённый модуль. Для полноценной логики напишите, какие поля и теги нужны."
            )

    async def start_for_user(self, chat_id: int):
        """
        Вызывается из main_bot при выборе 'Вывозной'.
        Сделаем простой ответ, чтобы не ломать запуск.
        """
        try:
            await self.bot.send_message(chat_id, "Начинаем Вывозной отчет. Введите /start для запуска локального режима.")
        except Exception as e:
            logger.error("pickup start error: %s", e)

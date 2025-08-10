import os
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Router
from aiohttp import web

# импорт сценариев
from daily_report import DailyReport
from pickup_report import PickupReport

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_TOKEN = os.getenv("API_TOKEN")
if not API_TOKEN:
    raise RuntimeError("API_TOKEN not set in .env")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATE_DOCX = os.getenv("TEMPLATE_DOCX") or os.path.join(BASE_DIR, "template22.docx")
TEMPLATE_PICKUP = os.getenv("TEMPLATE_PICKUP") or os.path.join(BASE_DIR, "template_pickup.docx")
PHOTOS_DIR = os.path.join(BASE_DIR, "photos")
TEMP_DIR = os.path.join(BASE_DIR, "temp")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# создаём экземпляры сценариев
daily = DailyReport(bot=bot, template_path=TEMPLATE_DOCX, photos_dir=PHOTOS_DIR, temp_dir=TEMP_DIR)
pickup = PickupReport(bot=bot, template_path=TEMPLATE_PICKUP, photos_dir=PHOTOS_DIR, temp_dir=TEMP_DIR)

main_router = Router()

@main_router.message(Command("start"))
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ежедневный", callback_data="choose_daily")],
        [InlineKeyboardButton(text="Вывозной", callback_data="choose_pickup")]
    ])
    await message.answer("Выберите вид отчета:", reply_markup=kb)

@main_router.callback_query(lambda c: c.data in ("choose_daily", "choose_pickup"))
async def handle_choice(callback: CallbackQuery):
    data = callback.data
    chat_id = callback.message.chat.id

    if data == "choose_daily":
        await callback.message.answer("Вы выбрали: Ежедневный. Начинаем сценарий.")
        await daily.start_for_user(chat_id)
    else:
        await callback.message.answer("Вы выбрали: Вывозной. Начинаем сценарий.")
        await pickup.start_for_user(chat_id)

    try:
        await callback.message.delete()
    except:
        pass

dp.include_router(main_router)
dp.include_router(daily.router)
dp.include_router(pickup.router)

if __name__ == "__main__":
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler

    async def on_startup():
        logger.info("Bot started")
        webhook_url = os.getenv("WEBHOOK_URL")
        if webhook_url:
            await bot.set_webhook(webhook_url)

    async def on_shutdown():
        logger.info("Bot stopping")
        try:
            await bot.delete_webhook()
        except:
            pass

    app = web.Application()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")

    port = int(os.environ.get("PORT", 5000))
    web.run_app(app, host="0.0.0.0", port=port)

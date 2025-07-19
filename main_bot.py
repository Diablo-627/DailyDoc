import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# Import routers at top level to avoid circular imports
from daily_report import daily_router, start_daily_report
from garbage_report import garbage_router, start_garbage_report
# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Initialize bot and dispatcher
bot = Bot(token=os.getenv("API_TOKEN"))
storage = MemoryStorage()
main_dp = Dispatcher(storage=storage)

# Register routers
main_dp.include_router(daily_router)  # Assuming these are defined in their modules
main_dp.include_router(garbage_router)

@main_dp.message(Command("start", "help"))
async def start_command(message: types.Message, state: FSMContext):
    """Main menu with report type selection"""
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
    await state.clear()

@main_dp.message(lambda message: message.text == "📅 Ежедневный отчет")
async def handle_daily_report(message: types.Message, state: FSMContext):
    """Handle daily report request"""
    await start_daily_report(message, state)

@main_dp.message(lambda message: message.text == "🗑️ Отчет по вывозу мусора")
async def handle_garbage_report(message: types.Message, state: FSMContext):
    """Handle garbage report request"""
    await start_garbage_report(message, state)

@main_dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    """Cancel any ongoing operation"""
    await state.clear()
    await message.answer(
        "Действие отменено",
        reply_markup=types.ReplyKeyboardRemove()
    )

async def on_startup(app: web.Application):
    """Webhook setup on startup"""
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        logger.error("WEBHOOK_URL environment variable is not set!")
        return
    
    await bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
        allowed_updates=main_dp.resolve_used_update_types()
    )
    logger.info(f"Webhook configured for {webhook_url}")

async def on_shutdown(app: web.Application):
    """Cleanup on shutdown"""
    await bot.delete_webhook(drop_pending_updates=True)
    await storage.close()
    logger.info("Bot stopped. Webhook removed and storage closed.")

async def main():
    """Main entry point for webhook setup"""
    app = web.Application()
    
    # Configure webhook handler
    webhook_handler = SimpleRequestHandler(main_dp, bot)
    webhook_handler.register(app, path="/webhook")
    
    # Setup startup/shutdown hooks
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    # Mount dispatcher
    setup_application(app, main_dp, bot=bot)
    
    # Start server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=8080)
    await site.start()
    
    logger.info("Webhook server started on port 8080")
    
    # Keep running
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Critical error: {e}", exc_info=True)

import asyncio
import logging
import os
import shutil
import time
import zipfile
import tempfile
from PIL import Image
from dotenv import load_dotenv
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from asyncio import Semaphore

# Импорты aiogram
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode, ContentType
from aiogram.types import (
    Message,
    FSInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# Для веб-сервера
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
API_TOKEN = os.getenv("API_TOKEN")
if not API_TOKEN:
    raise ValueError("API_TOKEN environment variable is not set")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PHOTOS_DIR = os.path.join(BASE_DIR, "photos")
TEMP_DIR = os.path.join(BASE_DIR, "temp")
TEMPLATE_DOCX = os.path.join(BASE_DIR, "template22.docx")

# Создание директорий
os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Ограничители параллелизма
executor = ThreadPoolExecutor(max_workers=3)
processing_semaphore = Semaphore(3)

# Инициализация бота
bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# Константы
PHOTO_SIZES = {
    "ТБ1": (10.67, 6.0),
    "ТБ2": (10.67, 6.0),
    "ИТОГ": (20.0, 12.0),
    "default": (10.67, 6.0)
}

photo_tags = [
    "ТБ1", "ТБ2",
    "ДО1", "ДО2", "ДО3", "ДО4",
    "ПОСЛЕ1", "ПОСЛЕ2", "ПОСЛЕ3", "ПОСЛЕ4",
    "ПРОЦЕСС1", "ПРОЦЕСС2", "ПРОЦЕСС3", "ПРОЦЕСС4",
    "ИТОГ"
]

# Состояния
class ReportState(StatesGroup):
    fio = State()
    team = State()
    date = State()
    address = State()
    bags = State()
    fighters = State()
    input_photos = State()
    choosing_tag = State()

# Глобальные переменные
user_sessions = {}
session_lock = Lock()

def get_or_create_session(chat_id):
    """Потокобезопасное создание/получение сессии"""
    with session_lock:
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {
                "fields": {
                    "{}1{}": "", "{}2{}": "", 
                    "{3}": "", "{4}": "", 
                    "{5}": "", "{6}": ""
                },
                "photos": {},
                "remaining_tags": photo_tags.copy(),
                "photo_queue": [],
                "current_file_id": None,
                "current_photo_message_id": None,
                "lock": Lock(),
                "processing": False
            }
        return user_sessions[chat_id]

def resize_and_crop_image(image_path, target_width_cm, target_height_cm):
    """Обработка изображения с сохранением пропорций"""
    target_width = int(target_width_cm * 37.8)
    target_height = int(target_height_cm * 37.8)
    
    with Image.open(image_path) as img:
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        width, height = img.size
        target_ratio = target_width / target_height
        image_ratio = width / height
        
        if image_ratio > target_ratio:
            new_height = height
            new_width = int(height * target_ratio)
            left = (width - new_width) / 2
            top, right, bottom = 0, left + new_width, height
        else:
            new_width = width
            new_height = int(width / target_ratio)
            left, top = 0, (height - new_height) / 2
            right, bottom = width, top + new_height
        
        img = img.crop((left, top, right, bottom))
        img = img.resize((target_width, target_height), Image.LANCZOS)
        img.save(image_path, format="JPEG", quality=90, subsampling=0)
        logger.info(f"Изображение обработано: {target_width}x{target_height} пикселей")

async def download_photo_with_retry(file_id: str, destination_path: str, max_attempts: int = 3) -> bool:
    """Загрузка фото с повторами"""
    for attempt in range(max_attempts):
        try:
            file = await bot.get_file(file_id)
            await bot.download_file(file.file_path, destination_path)
            
            start_time = time.time()
            while not os.path.exists(destination_path):
                if time.time() - start_time > 30:
                    logger.error("Таймаут загрузки файла")
                    return False
                await asyncio.sleep(1)
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка загрузки (попытка {attempt + 1}): {e}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(3)
    
    return False

# Обработчики команд
@router.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    """Обработчик команды /start"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"] = {k: "" for k in session["fields"]}
        session["photos"] = {}
        session["remaining_tags"] = photo_tags.copy()
        session["photo_queue"] = []
        session["current_file_id"] = None
        session["current_photo_message_id"] = None
        session["processing"] = False
    
    await state.set_state(ReportState.fio)
    await message.answer("Введите ФИО координатора:")

@router.message(Command("reset"))
async def reset_session(message: Message, state: FSMContext):
    """Сброс сессии"""
    chat_id = message.chat.id
    with session_lock:
        if chat_id in user_sessions:
            for tag, path in user_sessions[chat_id]["photos"].items():
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    logger.error(f"Ошибка удаления фото: {e}")
            del user_sessions[chat_id]
    
    await state.clear()
    await message.answer("Сессия сброшена. Введите /start для начала.")

@router.message(Command("debug"))
async def debug_command(message: Message, state: FSMContext):
    """Отладочная информация"""
    chat_id = message.chat.id
    current_state = await state.get_state()
    
    debug_info = f"Текущее состояние: {current_state}\n"
    
    with session_lock:
        if chat_id in user_sessions:
            session = user_sessions[chat_id]
            debug_info += (
                f"Осталось тегов: {len(session['remaining_tags'])}\n"
                f"Фото в очереди: {len(session['photo_queue'])}\n"
                f"Обработанные фото: {list(session['photos'].keys())}\n"
                f"Текущее фото: {session['current_file_id']}\n"
                f"Обрабатывается: {session['processing']}"
            )
        else:
            debug_info += "Активной сессии нет"
    
    await message.answer(debug_info)

# Обработчики состояний
@router.message(ReportState.fio)
async def handle_fio(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{}1{}"] = message.text
    
    await state.set_state(ReportState.team)
    await message.answer("Введите название отряда:")

@router.message(ReportState.team)
async def handle_team(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{}2{}"] = message.text
    
    await state.set_state(ReportState.date)
    await message.answer("Введите дату уборки:")

@router.message(ReportState.date)
async def handle_date(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{3}"] = message.text
    
    await state.set_state(ReportState.address)
    await message.answer("Введите адрес уборки:")

@router.message(ReportState.address)
async def handle_address(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{4}"] = message.text
    
    await state.set_state(ReportState.bags)
    await message.answer("Введите количество мешков:")

@router.message(ReportState.bags)
async def handle_bags(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{5}"] = message.text
    
    await state.set_state(ReportState.fighters)
    await message.answer("Введите количество бойцов:")

@router.message(ReportState.fighters)
async def handle_fighters(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{6}"] = message.text
    
    await state.set_state(ReportState.input_photos)
    await message.answer("Отправьте фото. После загрузки всех фото введите 'Готово'")

# Обработчики фото
@router.message(F.content_type == ContentType.PHOTO)
async def handle_any_photo(message: Message, state: FSMContext):
    """Общий обработчик фото"""
    current_state = await state.get_state()
    if current_state != ReportState.input_photos:
        await message.answer("Сначала заполните данные через /start")
        return
    
    await handle_photos(message, state)

async def handle_photos(message: Message, state: FSMContext):
    """Основная обработка фото"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["photo_queue"].append(message.photo[-1].file_id)
    
    await process_next_photo(message, state)

async def process_next_photo(message: Message, state: FSMContext):
    """Обработка следующего фото в очереди"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        if session["processing"] or not session["photo_queue"] or not session["remaining_tags"]:
            return
            
        session["current_file_id"] = session["photo_queue"].pop(0)
        session["processing"] = True
    
    if not session["remaining_tags"]:
        await message.answer("Все фото обработаны. Введите 'Готово' для генерации отчета")
        with session["lock"]:
            session["current_file_id"] = None
            session["processing"] = False
        return
    
    buttons = [[InlineKeyboardButton(text=tag, callback_data=f"choose:{tag}")] 
              for tag in session["remaining_tags"]]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    try:
        sent_msg = await bot.send_photo(
            chat_id=chat_id,
            photo=session["current_file_id"],
            caption="Выберите тип фото:",
            reply_markup=markup
        )
        with session["lock"]:
            session["current_photo_message_id"] = sent_msg.message_id
        await state.set_state(ReportState.choosing_tag)
    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}")
        with session["lock"]:
            session["current_file_id"] = None
            session["processing"] = False
        await message.answer("Ошибка обработки фото. Попробуйте снова.")

@router.callback_query(F.data.startswith("choose:"))
async def handle_tag(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора тега для фото"""
    chat_id = callback.message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        if not session["current_file_id"]:
            await callback.answer("Нет активного фото")
            return
            
        tag = callback.data.split(":")[1]
        photo_path = os.path.join(PHOTOS_DIR, f"{chat_id}_{tag}.jpg")
        file_id = session["current_file_id"]
        session["current_file_id"] = None
    
    try:
        # Удаляем старое фото если есть
        if os.path.exists(photo_path):
            try:
                os.remove(photo_path)
            except Exception as e:
                logger.error(f"Ошибка удаления фото: {e}")
        
        # Загружаем фото
        if not await download_photo_with_retry(file_id, photo_path):
            await callback.message.answer("Ошибка загрузки фото")
            return
        
        # Обрабатываем изображение
        width_cm, height_cm = PHOTO_SIZES.get(tag, PHOTO_SIZES["default"])
        
        async with processing_semaphore:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                executor,
                resize_and_crop_image,
                photo_path, width_cm, height_cm
            )
        
        # Обновляем сессию
        with session["lock"]:
            session["photos"][tag] = photo_path
            if tag in session["remaining_tags"]:
                session["remaining_tags"].remove(tag)
            session["processing"] = False
        
        # Удаляем сообщение с кнопками
        if session.get("current_photo_message_id"):
            try:
                await bot.delete_message(chat_id, session["current_photo_message_id"])
            except Exception as e:
                logger.error(f"Ошибка удаления сообщения: {e}")
            with session["lock"]:
                session["current_photo_message_id"] = None
        
        await callback.answer(f"Фото сохранено как {tag}")
        
        # Проверяем завершение
        with session["lock"]:
            if not session["remaining_tags"]:
                await callback.message.answer("Все фото обработаны. Введите 'Готово'")
            else:
                await state.set_state(ReportState.input_photos)
                await process_next_photo(callback.message, state)
            
    except Exception as e:
        logger.error(f"Ошибка обработки фото: {e}", exc_info=True)
        await callback.message.answer("Ошибка обработки фото")
        with session["lock"]:
            session["processing"] = False
        await state.set_state(ReportState.input_photos)

# Обработчик текста в режиме фото
@router.message(ReportState.input_photos, F.text)
async def handle_text_during_photos(message: Message, state: FSMContext):
    """Обработка текста при загрузке фото"""
    if message.text.lower() == 'готово':
        chat_id = message.chat.id
        session = get_or_create_session(chat_id)
        
        with session["lock"]:
            if not session["photos"]:
                await message.answer("Нет фото для отчета")
                return
        
        await message.answer("Генерирую отчет...")
        await generate_docx(message, chat_id)
        await state.clear()
    else:
        await message.answer("Отправьте фото или введите 'Готово'")

# Генерация документа
async def replace_image_in_docx(doc_path: str, image_tag: str, new_image_path: str):
    """Замена изображения в docx"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(doc_path, 'r') as zip_ref:
            zip_ref.extractall(tmp_dir)
        
        document_xml_path = os.path.join(tmp_dir, 'word', 'document.xml')
        relationships_path = os.path.join(tmp_dir, 'word', '_rels', 'document.xml.rels')
        
        tree = ET.parse(document_xml_path)
        root = tree.getroot()
        
        namespaces = {
            'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
            'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
            'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture',
            'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
        }
        
        for prefix, uri in namespaces.items():
            ET.register_namespace(prefix, uri)
        
        found = False
        for pic in root.findall('.//pic:pic', namespaces):
            nv_pr = pic.find('pic:nvPicPr/pic:cNvPr', namespaces)
            if nv_pr is not None and nv_pr.get('descr') == image_tag:
                blip = pic.find('.//a:blip', namespaces)
                if blip is not None:
                    r_id = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                    
                    rel_tree = ET.parse(relationships_path)
                    rel_root = rel_tree.getroot()
                    
                    for rel in rel_root.findall('.//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship'):
                        if rel.get('Id') == r_id:
                            image_file = os.path.join(tmp_dir, 'word', rel.get('Target'))
                            shutil.copy(new_image_path, image_file)
                            found = True
                            break
        
        if not found:
            logger.warning(f"Тег {image_tag} не найден")
        
        tree.write(document_xml_path, encoding='UTF-8', xml_declaration=True)
        
        with zipfile.ZipFile(doc_path, 'w') as zip_ref:
            for root, dirs, files in os.walk(tmp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, tmp_dir)
                    zip_ref.write(file_path, arcname)

async def generate_docx(message: Message, chat_id: int):
    """Генерация итогового документа"""
    session = get_or_create_session(chat_id)
    user_temp_dir = os.path.join(TEMP_DIR, str(chat_id))
    os.makedirs(user_temp_dir, exist_ok=True)
    output_path = os.path.join(user_temp_dir, "Final_Отчет.docx")
    
    try:
        shutil.copy(TEMPLATE_DOCX, output_path)
        
        # Проверка фото
        missing_photos = [tag for tag, path in session["photos"].items() 
                         if not os.path.exists(path)]
        if missing_photos:
            await message.answer(f"Отсутствуют фото: {', '.join(missing_photos)}")
            return
        
        # Замена изображений
        for tag, image_path in session["photos"].items():
            await replace_image_in_docx(output_path, tag, image_path)
        
        # Замена текста
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(output_path, 'r') as zip_ref:
                zip_ref.extractall(tmp_dir)
            
            document_xml_path = os.path.join(tmp_dir, 'word', 'document.xml')
            tree = ET.parse(document_xml_path)
            root = tree.getroot()
            
            namespaces = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            ET.register_namespace('w', namespaces['w'])
            
            for text_elem in root.findall('.//w:t', namespaces):
                if text_elem.text:
                    for key, val in session["fields"].items():
                        if key in text_elem.text:
                            text_elem.text = text_elem.text.replace(key, val)
            
            tree.write(document_xml_path, encoding='UTF-8', xml_declaration=True)
            
            with zipfile.ZipFile(output_path, 'w') as zip_ref:
                for root, dirs, files in os.walk(tmp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, tmp_dir)
                        zip_ref.write(file_path, arcname)
        
        if not os.path.exists(output_path):
            await message.answer("Ошибка создания отчета")
            return
        
        await bot.send_document(chat_id, FSInputFile(output_path), caption="Ваш отчет")
        
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}", exc_info=True)
        await message.answer("Ошибка генерации отчета")
    finally:
        # Очистка
        try:
            shutil.rmtree(user_temp_dir)
        except Exception as e:
            logger.error(f"Ошибка очистки: {e}")
        
        with session_lock:
            if chat_id in user_sessions:
                for tag, path in user_sessions[chat_id]["photos"].items():
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except Exception as e:
                        logger.error(f"Ошибка удаления фото: {e}")
                del user_sessions[chat_id]

# Обработчик неизвестных сообщений
@router.message()
async def handle_unknown(message: Message):
    """Фолбэк обработчик"""
    logger.warning(f"Необработанное сообщение: {message.content_type}")
    await message.answer("Используйте /start для начала работы")

# Запуск/остановка
async def on_startup(dispatcher: Dispatcher):
    """Действия при запуске"""
    logger.info("Бот запущен")
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await bot.set_webhook(webhook_url)

async def on_shutdown(dispatcher: Dispatcher):
    """Действия при остановке"""
    logger.info("Бот останавливается")
    await bot.delete_webhook()
    
    # Очистка временных файлов
    for root, dirs, files in os.walk(BASE_DIR):
        for file in files:
            if file.endswith((".jpg", ".docx")):
                try:
                    os.remove(os.path.join(root, file))
                except Exception as e:
                    logger.error(f"Ошибка очистки: {e}")
    
    # Очистка сессий
    with session_lock:
        user_sessions.clear()

# Запуск приложения
if __name__ == "__main__":
    app = web.Application()
    
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_handler.register(app, path="/webhook")
    
    port = int(os.environ.get("PORT", 5000))
    web.run_app(app, host="0.0.0.0", port=port)

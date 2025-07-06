import asyncio
import logging
import os
import shutil
import time
import zipfile
import tempfile
import re
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
    "ОБЩЕЕФОТО": (20.0, 12.0),
    "default": (10.67, 6.0)
}

MAX_PHOTOS = 15  # Максимальное количество фото
photo_tags = [
    "ТБ1", "ТБ2",
    "ДО1", "ДО2", "ДО3", "ДО4",
    "ПОСЛЕ1", "ПОСЛЕ2", "ПОСЛЕ3", "ПОСЛЕ4",
    "ПРОЦЕСС1", "ПРОЦЕСС2", "ПРОЦЕСС3", "ПРОЦЕСС4",
    "ОБЩЕЕФОТО"
]

# Таймаут сессии в секундах (30 минут)
SESSION_TIMEOUT = 1800

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
                "lock": Lock(),
                "processing": False,
                "last_activity": time.time()  # Время последней активности
            }
        return user_sessions[chat_id]

def update_session_activity(chat_id):
    """Обновление времени последней активности сессии"""
    with session_lock:
        if chat_id in user_sessions:
            user_sessions[chat_id]["last_activity"] = time.time()

def cleanup_expired_sessions():
    """Очистка просроченных сессий"""
    current_time = time.time()
    expired_sessions = []
    
    with session_lock:
        for chat_id, session in list(user_sessions.items()):
            if current_time - session["last_activity"] > SESSION_TIMEOUT:
                expired_sessions.append(chat_id)
        
        for chat_id in expired_sessions:
            # Удаляем файлы фото
            for tag, path in user_sessions[chat_id]["photos"].items():
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    logger.error(f"Ошибка удаления фото: {e}")
            del user_sessions[chat_id]
            logger.info(f"Сессия {chat_id} удалена по таймауту")
    
    return len(expired_sessions)

async def periodic_session_cleanup():
    """Периодическая очистка сессий"""
    while True:
        try:
            cleaned = cleanup_expired_sessions()
            logger.info(f"Очищено сессий: {cleaned}")
            await asyncio.sleep(300)  # Проверка каждые 5 минут
        except Exception as e:
            logger.error(f"Ошибка при очистке сессий: {e}")

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
        session["processing"] = False
        session["last_activity"] = time.time()
    
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

@router.message(Command("help"))
async def help_handler(message: Message):
    """Обработчик команды /help"""
    help_text = (
        "Доступные команды:\n"
        "/start - Начать заполнение отчета\n"
        "/reset - Сбросить текущую сессию\n"
        "/generate - Сгенерировать отчет (если все фото собраны)\n"
        "/help - Показать эту справку"
    )
    await message.answer(help_text)

@router.message(Command("generate"))
async def generate_command(message: Message, state: FSMContext):
    """Команда для принудительной генерации отчета"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    update_session_activity(chat_id)
    
    # Проверяем, есть ли необходимые данные
    if not session["fields"]["{}1{}"]:
        await message.answer("Сначала заполните основные данные. Введите /start")
        return
    
    # Очищаем очередь фото
    with session["lock"]:
        session["photo_queue"] = []
        session["current_file_id"] = None
        session["processing"] = False
    
    # Генерируем отчет
    await generate_docx(message, chat_id)

# Обработчики состояний
@router.message(ReportState.fio)
async def handle_fio(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    update_session_activity(chat_id)
    
    with session["lock"]:
        session["fields"]["{}1{}"] = message.text
    
    await state.set_state(ReportState.team)
    await message.answer("Введите название отряда:")

@router.message(ReportState.team)
async def handle_team(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    update_session_activity(chat_id)
    
    with session["lock"]:
        session["fields"]["{}2{}"] = message.text
    
    await state.set_state(ReportState.date)
    await message.answer("Введите дату уборки:")

@router.message(ReportState.date)
async def handle_date(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    update_session_activity(chat_id)
    
    with session["lock"]:
        session["fields"]["{3}"] = message.text
    
    await state.set_state(ReportState.address)
    await message.answer("Введите адрес уборки:")

@router.message(ReportState.address)
async def handle_address(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    update_session_activity(chat_id)
    
    with session["lock"]:
        session["fields"]["{4}"] = message.text
    
    await state.set_state(ReportState.bags)
    await message.answer("Введите количество мешков:")

@router.message(ReportState.bags)
async def handle_bags(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    update_session_activity(chat_id)
    
    with session["lock"]:
        session["fields"]["{5}"] = message.text
    
    await state.set_state(ReportState.fighters)
    await message.answer("Введите количество бойцов:")

@router.message(ReportState.fighters)
async def handle_fighters(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    update_session_activity(chat_id)
    
    with session["lock"]:
        session["fields"]["{6}"] = message.text
    
    await state.set_state(ReportState.input_photos)
    await message.answer("Отправляйте фото. Для каждого будет запрошен тип.")

# Обработчики фото
@router.message(F.photo)
async def handle_photo_only(message: Message, state: FSMContext):
    """Обработчик фото (игнорирует подписи)"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    update_session_activity(chat_id)
    
    with session["lock"]:
        # Проверяем, не превышен ли лимит фото
        if len(session["photos"]) >= MAX_PHOTOS:
            await message.answer(f"⚠️ Достигнут лимит в {MAX_PHOTOS} фото! Используйте /generate для создания отчета")
            return
        
        # Проверяем, есть ли еще доступные теги
        if not session["remaining_tags"]:
            await message.answer("⚠️ Все типы фото использованы! Используйте /generate для создания отчета")
            return
            
        session["photo_queue"].append(message.photo[-1].file_id)
        logger.info(f"Фото добавлено в очередь. Всего в очереди: {len(session['photo_queue'])}")
    
    # Если это первое фото в очереди - начинаем обработку
    if len(session["photo_queue"]) == 1:
        await process_next_photo(chat_id, state)

async def process_next_photo(chat_id: int, state: FSMContext):
    """Обработка следующего фото в очереди"""
    session = get_or_create_session(chat_id)
    update_session_activity(chat_id)
    
    with session["lock"]:
        if session["processing"] or not session["photo_queue"]:
            return
            
        # Проверяем, не превышен ли лимит фото
        if len(session["photos"]) >= MAX_PHOTOS:
            session["photo_queue"] = []
            await bot.send_message(chat_id, f"⚠️ Достигнут лимит в {MAX_PHOTOS} фото! Используйте /generate")
            return
            
        session["current_file_id"] = session["photo_queue"][0]  # Берем первое фото, но не удаляем из очереди
        session["processing"] = True
        
        if not session["remaining_tags"]:
            await bot.send_message(chat_id, "⚠️ Все типы фото использованы! Используйте /generate")
            session["photo_queue"] = []  # Очищаем очередь
            session["current_file_id"] = None
            session["processing"] = False
            return
    
    # Показываем фото с кнопками выбора типа
    buttons = [
        [InlineKeyboardButton(text=tag, callback_data=f"tag_{tag}")] 
        for tag in session["remaining_tags"]
    ]
    
    # Добавляем кнопку "Пропустить"
    buttons.append([InlineKeyboardButton(text="⏭ Пропустить", callback_data="tag_skip")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=session["current_file_id"],
            caption="Выберите тип этого фото или пропустите:",
            reply_markup=markup
        )
        await state.set_state(ReportState.choosing_tag)
    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}")
        with session["lock"]:
            # Удаляем текущее фото из очереди (которое не удалось отправить)
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            session["current_file_id"] = None
            session["processing"] = False

@router.callback_query(F.data.startswith("tag_"))
async def handle_photo_tag(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора типа фото"""
    chat_id = callback.message.chat.id
    session = get_or_create_session(chat_id)
    update_session_activity(chat_id)
    tag = callback.data.replace("tag_", "")
    
    # Обработка пропуска фото
    if tag == "skip":
        try:
            await callback.message.delete()
        except:
            pass
        
        await callback.message.answer("⏭ Фото пропущено.")
        
        with session["lock"]:
            # Удаляем текущее фото из очереди
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            session["current_file_id"] = None
            session["processing"] = False
        
        # Обрабатываем следующее фото, если есть
        if session["photo_queue"]:
            await process_next_photo(chat_id, state)
        else:
            # Если фото больше нет, но остались теги, напоминаем о них
            if session["remaining_tags"]:
                await callback.message.answer(f"Остались невыбранные типы: {', '.join(session['remaining_tags'])}\nОтправьте фото или используйте /generate")
        return
    
    # Обычная обработка выбора тега
    if not session["current_file_id"]:
        await callback.answer("Фото уже обработано")
        return
    
    # Сохраняем фото
    photo_path = os.path.join(PHOTOS_DIR, f"{chat_id}_{tag}.jpg")
    if await download_photo_with_retry(session["current_file_id"], photo_path):
        width, height = PHOTO_SIZES.get(tag, PHOTO_SIZES["default"])
        
        async with processing_semaphore:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                executor,
                resize_and_crop_image,
                photo_path, width, height
            )
        
        # Удаляем сообщение с фото и кнопками
        try:
            await callback.message.delete()
        except Exception as e:
            logger.error(f"Ошибка при удалении сообщения: {e}")
        
        # Отправляем новое текстовое сообщение вместо редактирования
        await callback.message.answer(f"✅ Фото сохранено как: {tag}")
        
        with session["lock"]:
            # Удаляем обработанное фото из очереди
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            
            session["photos"][tag] = photo_path
            if tag in session["remaining_tags"]:
                session["remaining_tags"].remove(tag)
            session["current_file_id"] = None
            session["processing"] = False
        
        # Обрабатываем следующее фото, если есть
        if session["photo_queue"]:
            await process_next_photo(chat_id, state)
        elif not session["remaining_tags"] or len(session["photos"]) >= MAX_PHOTOS:
            await generate_docx(callback.message, chat_id)
    else:
        # Отправляем новое сообщение об ошибке
        await callback.message.answer("❌ Ошибка загрузки фото")
        with session["lock"]:
            # Удаляем текущее фото из очереди
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            session["current_file_id"] = None
            session["processing"] = False
        
        # Обрабатываем следующее фото, если есть
        if session["photo_queue"]:
            await process_next_photo(chat_id, state)

    await state.set_state(ReportState.input_photos)

# Игнорирование текста (кроме команд)
@router.message(F.text)
async def ignore_text_messages(message: Message):
    """Игнорирует текст, если это не команда"""
    update_session_activity(message.chat.id)
    if not message.text.startswith('/'):
        return
    await message.answer("Используйте /help для списка команд")

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
    update_session_activity(chat_id)
    user_temp_dir = os.path.join(TEMP_DIR, str(chat_id))
    os.makedirs(user_temp_dir, exist_ok=True)
    
    # Формируем имя файла на основе имени координатора
    coordinator_name = session["fields"]["{}1{}"]
    # Удаляем недопустимые символы для имени файла
    safe_name = re.sub(r'[\\/*?:"<>|]', "", coordinator_name)[:50]  # Ограничиваем длину
    output_path = os.path.join(user_temp_dir, f"{safe_name}_отчёт.docx")
    
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
        # Очистка временных файлов
        try:
            shutil.rmtree(user_temp_dir)
        except Exception as e:
            logger.error(f"Ошибка очистки временных файлов: {e}")
        
        # Очистка сессии после генерации
        with session_lock:
            if chat_id in user_sessions:
                # Удаляем файлы фото
                for tag, path in user_sessions[chat_id]["photos"].items():
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except Exception as e:
                        logger.error(f"Ошибка удаления фото: {e}")
                del user_sessions[chat_id]

# Запуск/остановка
async def on_startup(dispatcher: Dispatcher):
    """Действия при запуске"""
    logger.info("Бот запущен")
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await bot.set_webhook(webhook_url)
    
    # Запускаем фоновую задачу очистки сессий
    asyncio.create_task(periodic_session_cleanup())

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

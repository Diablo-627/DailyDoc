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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton
)
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession  # Добавлено

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
executor = ThreadPoolExecutor(max_workers=5)  # Увеличено количество воркеров
processing_semaphore = Semaphore(5)  # Увеличено количество одновременных обработок

# Инициализация бота с увеличенными таймаутами
session = AiohttpSession(
    timeout=60  # Установка общего таймаута в секундах
)
bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML, session=session)
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

photo_tags = [
    "ТБ1", "ТБ2",
    "ДО1", "ДО2", "ДО3", "ДО4",
    "ПОСЛЕ1", "ПОСЛЕ2", "ПОСЛЕ3", "ПОСЛЕ4",
    "ПРОЦЕСС1", "ПРОЦЕСС2", "ПРОЦЕСС3", "ПРОЦЕСС4",
    "ОБЩЕЕФОТО"
]

# Максимальный размер очереди фото
MAX_QUEUE_SIZE = 15

# Состояния
class ReportState(StatesGroup):
    fio = State()
    team = State()
    date = State()
    address = State()
    address_status = State()
    bags = State()
    fighters = State()
    input_photos = State()
    choosing_tag = State()
    status_sending = State()

# Глобальные переменные
user_sessions = {}
session_lock = Lock()

# Клавиатура с основными командами
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/help"), KeyboardButton(text="/reset")],
            [KeyboardButton(text="/generate"), KeyboardButton(text="/status")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def get_status_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Закончен"), KeyboardButton(text="Ведутся работы")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

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
                "address_status": None,
                "lock": Lock(),
                "processing": False,
                "last_activity": time.time()  # Отслеживание активности
            }
        return user_sessions[chat_id]

def resize_and_crop_image(image_path, target_width_cm, target_height_cm):
    """Обработка изображения с сохранением пропорций"""
    target_width = int(target_width_cm * 37.8)
    target_height = int(target_height_cm * 37.8)
    
    start_time = time.time()
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
        
        elapsed = time.time() - start_time
        logger.info(f"Изображение обработано за {elapsed:.2f} сек: {target_width}x{target_height} пикселей")

async def download_photo_with_retry(file_id: str, destination_path: str, max_attempts: int = 3) -> bool:
    """Загрузка фото с повторами"""
    for attempt in range(max_attempts):
        try:
            start_time = time.time()
            file = await bot.get_file(file_id)
            await bot.download_file(file.file_path, destination_path)
            
            while not os.path.exists(destination_path):
                if time.time() - start_time > 60:  # Увеличенный таймаут
                    logger.error("Таймаут загрузки файла")
                    return False
                await asyncio.sleep(1)
            
            elapsed = time.time() - start_time
            logger.info(f"Фото загружено за {elapsed:.2f} сек")
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
        session["address_status"] = None
        session["processing"] = False
        session["last_activity"] = time.time()
    
    await state.set_state(ReportState.fio)
    await message.answer(
        "Введите ФИО координатора:",
        reply_markup=get_main_keyboard()
    )

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
    await message.answer(
        "Сессия сброшена. Введите /start для начала.",
        reply_markup=get_main_keyboard()
    )

@router.message(Command("help"))
async def help_handler(message: Message):
    """Обработчик команды /help"""
    help_text = (
        "Доступные команды:\n"
        "/start - Начать заполнение отчета\n"
        "/reset - Сбросить текущую сессию\n"
        "/help - Показать эту справку\n"
        "/generate - Сгенерировать отчет\n"
        "/status - Отправить статус адреса контакту\n\n"
        "Во время загрузки фото:\n"
        "• Можно пропустить фото с помощью кнопки\n"
        "• Если загружено много фото, используйте /generate для принудительной генерации"
    )
    await message.answer(help_text, reply_markup=get_main_keyboard())

@router.message(Command("generate"))
async def force_generate(message: Message, state: FSMContext):
    """Обработчик команды /generate"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        if not session["photos"]:
            await message.answer("❌ Нет фото для генерации отчёта!")
            return
            
        # Проверяем, есть ли недостающие фото
        missing_photos = [tag for tag in photo_tags if tag not in session["photos"]]
        
        if missing_photos:
            # Создаем клавиатуру для подтверждения
            confirm_markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, сгенерировать", callback_data="force_generate")],
                [InlineKeyboardButton(text="❌ Нет, продолжить", callback_data="cancel_generate")]
            ])
            
            await message.answer(
                f"⚠️ У вас загружено только {len(session['photos'])} из {len(photo_tags)} фото. "
                f"Отсутствуют: {', '.join(missing_photos)}\n"
                "Всё равно создать отчёт?",
                reply_markup=confirm_markup
            )
        else:
            await generate_docx(message, chat_id)

@router.callback_query(F.data == "force_generate")
async def confirm_force_generate(callback: CallbackQuery):
    """Подтверждение принудительной генерации"""
    await callback.message.delete()
    await generate_docx(callback.message, callback.message.chat.id)

@router.callback_query(F.data == "cancel_generate")
async def cancel_force_generate(callback: CallbackQuery):
    """Отмена принудительной генерации"""
    await callback.message.delete()
    await callback.message.answer("Продолжаем загрузку фото...")

@router.message(Command("status"))
async def status_command(message: Message, state: FSMContext):
    """Обработчик команды /status"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    if not session.get("address_status") or not session.get("fields", {}).get("{4}"):
        await message.answer("❌ Статус адреса не указан. Заполните информацию об адресе.")
        return
    
    await state.set_state(ReportState.status_sending)
    await message.answer(
        "Введите Telegram @username контакта, которому отправить статус:",
        reply_markup=ReplyKeyboardRemove()
    )

@router.message(ReportState.status_sending)
async def handle_status_sending(message: Message, state: FSMContext):
    """Обработка отправки статуса"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    contact = message.text.strip()
    
    # Проверяем формат контакта
    if not contact.startswith('@'):
        await message.answer("❌ Контакт должен начинаться с @. Пример: @username")
        return
    
    # Отправляем статус контакту
    try:
        address = session["fields"].get("{4}", "неизвестный адрес")
        status = session.get("address_status", "неизвестный статус")
        
        await bot.send_message(
            contact,
            f"ℹ️ Статус адреса: {address}\n"
            f"🔄 Состояние: {status}"
        )
        await message.answer(f"✅ Статус успешно отправлен контакту {contact}")
    except Exception as e:
        logger.error(f"Ошибка отправки статуса: {e}")
        await message.answer(f"❌ Не удалось отправить статус контакту {contact}. Проверьте правильность контакта.")
    
    await state.clear()

# Обработчики состояний
@router.message(ReportState.fio)
async def handle_fio(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{}1{}"] = message.text
        session["last_activity"] = time.time()
    
    await state.set_state(ReportState.team)
    await message.answer("Введите название отряда:", reply_markup=get_main_keyboard())

@router.message(ReportState.team)
async def handle_team(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{}2{}"] = message.text
        session["last_activity"] = time.time()
    
    await state.set_state(ReportState.date)
    await message.answer("Введите дату уборки:", reply_markup=get_main_keyboard())

@router.message(ReportState.date)
async def handle_date(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{3}"] = message.text
        session["last_activity"] = time.time()
    
    await state.set_state(ReportState.address)
    await message.answer("Введите адрес уборки:", reply_markup=get_main_keyboard())

@router.message(ReportState.address)
async def handle_address(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{4}"] = message.text
        session["last_activity"] = time.time()
    
    await state.set_state(ReportState.address_status)
    await message.answer(
        "Укажите статус адреса:",
        reply_markup=get_status_keyboard()
    )

@router.message(ReportState.address_status)
async def handle_address_status(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    if message.text not in ["Закончен", "Ведутся работы"]:
        await message.answer("Пожалуйста, выберите один из предложенных вариантов", reply_markup=get_status_keyboard())
        return
    
    with session["lock"]:
        session["address_status"] = message.text
        session["last_activity"] = time.time()
    
    await state.set_state(ReportState.bags)
    await message.answer("Введите количество мешков:", reply_markup=get_main_keyboard())

@router.message(ReportState.bags)
async def handle_bags(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{5}"] = message.text
        session["last_activity"] = time.time()
    
    await state.set_state(ReportState.fighters)
    await message.answer("Введите количество бойцов:", reply_markup=get_main_keyboard())

@router.message(ReportState.fighters)
async def handle_fighters(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{6}"] = message.text
        session["last_activity"] = time.time()
    
    await state.set_state(ReportState.input_photos)
    await message.answer(
        "Отправляйте фото. Для каждого будет запрошен тип. "
        "Можно пропустить фото с помощью кнопки.",
        reply_markup=get_main_keyboard()
    )

# Обработчики фото
@router.message(F.photo)
async def handle_photo_only(message: Message, state: FSMContext):
    """Обработчик фото (игнорирует подписи)"""
    start_time = time.time()
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["last_activity"] = time.time()
        
        # Проверяем, не переполнена ли очередь
        if len(session["photo_queue"]) >= MAX_QUEUE_SIZE:
            await message.answer(
                f"🚫 Очередь переполнена! Максимум {MAX_QUEUE_SIZE} фото. "
                "Используйте /generate для генерации отчёта."
            )
            return
            
        session["photo_queue"].append(message.photo[-1].file_id)
        logger.info(f"Фото добавлено в очередь. Всего в очереди: {len(session['photo_queue'])}")
        
        # Отправляем обновленный статус
        progress = f"📊 Прогресс: {len(session['photos'])}/{len(photo_tags)} фото\n"
        progress += f"⏳ В очереди: {len(session['photo_queue'])} фото"
        await message.answer(progress)
    
    # Если это первое фото в очереди - начинаем обработку
    if len(session["photo_queue"]) == 1:
        await process_next_photo(chat_id, state)
    
    elapsed = time.time() - start_time
    logger.info(f"Обработка фото завершена за {elapsed:.2f} сек")

async def process_next_photo(chat_id: int, state: FSMContext):
    """Обработка следующего фото в очереди"""
    start_time = time.time()
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        if session["processing"] or not session["photo_queue"]:
            return
            
        session["current_file_id"] = session["photo_queue"][0]  # Берем первое фото, но не удаляем из очереди
        session["processing"] = True
        session["last_activity"] = time.time()
        
        if not session["remaining_tags"]:
            await bot.send_message(chat_id, "Все типы фото использованы! Введите /generate")
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
    buttons.append([InlineKeyboardButton(text="⏭ Пропустить фото", callback_data="tag_skip")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    try:
        # Сообщаем пользователю, что начали обработку
        await bot.send_chat_action(chat_id, "typing")
        
        await bot.send_photo(
            chat_id=chat_id,
            photo=session["current_file_id"],
            caption="Выберите тип этого фото:",
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
    
    elapsed = time.time() - start_time
    logger.info(f"Отправка фото для выбора типа завершена за {elapsed:.2f} сек")

@router.callback_query(F.data.startswith("tag_"))
async def handle_photo_tag(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора типа фото"""
    start_time = time.time()
    chat_id = callback.message.chat.id
    session = get_or_create_session(chat_id)
    tag = callback.data.replace("tag_", "")
    
    # Сообщаем пользователю, что начали обработку
    await bot.send_chat_action(chat_id, "upload_photo")
    
    # Обработка пропуска фото
    if tag == "skip":
        with session["lock"]:
            session["last_activity"] = time.time()
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            session["current_file_id"] = None
            session["processing"] = False
        
        try:
            await callback.message.delete()
        except Exception as e:
            logger.error(f"Ошибка при удалении сообщения: {e}")
        
        await callback.message.answer("⏭ Фото пропущено")
        
        # Обрабатываем следующее фото, если есть
        if session["photo_queue"]:
            await process_next_photo(chat_id, state)
        elif not session["remaining_tags"]:
            await generate_docx(callback.message, chat_id)
        
        elapsed = time.time() - start_time
        logger.info(f"Пропуск фото завершен за {elapsed:.2f} сек")
        return
    
    if not session["current_file_id"]:
        await callback.answer("Фото уже обработано")
        return
    
    # Сохраняем фото
    photo_path = os.path.join(PHOTOS_DIR, f"{chat_id}_{tag}.jpg")
    if await download_photo_with_retry(session["current_file_id"], photo_path):
        width, height = PHOTO_SIZES.get(tag, PHOTO_SIZES["default"])
        
        # Обработка в фоновом режиме
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                executor,
                resize_and_crop_image,
                photo_path, width, height
            )
        except Exception as e:
            logger.error(f"Ошибка обработки фото: {e}")
            await callback.message.answer(f"❌ Ошибка обработки фото: {e}")
        
        # Удаляем сообщение с фото и кнопками
        try:
            await callback.message.delete()
        except Exception as e:
            logger.error(f"Ошибка при удалении сообщения: {e}")
        
        # Отправляем новое текстовое сообщение вместо редактирования
        await callback.message.answer(f"✅ Фото сохранено как: {tag}")
        
        with session["lock"]:
            session["last_activity"] = time.time()
            # Удаляем обработанное фото из очереди
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            
            session["photos"][tag] = photo_path
            if tag in session["remaining_tags"]:
                session["remaining_tags"].remove(tag)
            session["current_file_id"] = None
            session["processing"] = False
        
        # Отправляем обновленный статус
        progress = f"📊 Прогресс: {len(session['photos'])}/{len(photo_tags)} фото\n"
        if session["photo_queue"]:
            progress += f"⏳ В очереди: {len(session['photo_queue'])} фото"
        await callback.message.answer(progress)
        
        # Обрабатываем следующее фото, если есть
        if session["photo_queue"]:
            await process_next_photo(chat_id, state)
        elif not session["remaining_tags"]:
            await generate_docx(callback.message, chat_id)
    else:
        # Отправляем новое сообщение об ошибке
        await callback.message.answer("❌ Ошибка загрузки фото")
        with session["lock"]:
            session["last_activity"] = time.time()
            # Удаляем текущее фото из очереди
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            session["current_file_id"] = None
            session["processing"] = False
        
        # Обрабатываем следующее фото, если есть
        if session["photo_queue"]:
            await process_next_photo(chat_id, state)

    await state.set_state(ReportState.input_photos)
    elapsed = time.time() - start_time
    logger.info(f"Обработка фото типа '{tag}' завершена за {elapsed:.2f} сек")

# Игнорирование текста (кроме команд)
@router.message(F.text)
async def ignore_text_messages(message: Message):
    """Игнорирует текст, если это не команда"""
    if not message.text.startswith('/'):
        return
    await message.answer("Используйте /help для списка команд", reply_markup=get_main_keyboard())

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
    start_time = time.time()
    session = get_or_create_session(chat_id)
    user_temp_dir = os.path.join(TEMP_DIR, str(chat_id))
    os.makedirs(user_temp_dir, exist_ok=True)
    
    # Получаем имя координатора из сессии
    coordinator_name = session["fields"]["{}1{}"]
    # Удаляем запрещенные символы для имени файла
    safe_name = "".join(c for c in coordinator_name if c.isalnum() or c in (' ', '_')).rstrip()
    # Формируем имя файла
    output_filename = f"{safe_name}_Отчёт.docx"
    output_path = os.path.join(user_temp_dir, output_filename)
    
    try:
        # Сообщаем пользователю, что начали генерацию
        await bot.send_chat_action(chat_id, "upload_document")
        
        shutil.copy(TEMPLATE_DOCX, output_path)
        
        # Проверка фото
        missing_photos = [tag for tag, path in session["photos"].items() 
                         if not os.path.exists(path)]
        if missing_photos:
            await message.answer(f"Отсутствуют фото: {', '.join(missing_photos)}")
            return
        
        # Замена изображений - выполняем параллельно
        tasks = []
        for tag, image_path in session["photos"].items():
            tasks.append(replace_image_in_docx(output_path, tag, image_path))
        
        # Ожидаем завершения всех задач
        await asyncio.gather(*tasks)
        
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
        
        await bot.send_document(
            chat_id, 
            FSInputFile(output_path, filename=output_filename), 
            caption=f"Отчёт координатора {coordinator_name}"
        )
        
        # Очищаем очередь фото после успешной генерации
        with session_lock:
            if chat_id in user_sessions:
                user_sessions[chat_id]["photo_queue"] = []
        
        await message.answer(
            "✅ Отчёт успешно сгенерирован!\n"
            "Теперь вы можете отправить статус адреса контакту с помощью команды /status",
            reply_markup=get_main_keyboard()
        )
        
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}", exc_info=True)
        await message.answer("❌ Ошибка генерации отчета")
    finally:
        # Очистка временных файлов
        try:
            shutil.rmtree(user_temp_dir)
        except Exception as e:
            logger.error(f"Ошибка очистки: {e}")
    
    elapsed = time.time() - start_time
    logger.info(f"Генерация отчёта завершена за {elapsed:.2f} сек")

# Запуск/остановка
async def cleanup_old_sessions():
    """Очистка старых сессий"""
    while True:
        try:
            now = time.time()
            with session_lock:
                expired = []
                for chat_id, session in user_sessions.items():
                    if now - session.get("last_activity", now) > 3600:  # 1 час бездействия
                        expired.append(chat_id)
                
                for chat_id in expired:
                    for tag, path in user_sessions[chat_id]["photos"].items():
                        try:
                            if os.path.exists(path):
                                os.remove(path)
                        except Exception as e:
                            logger.error(f"Ошибка удаления фото: {e}")
                    del user_sessions[chat_id]
                    logger.info(f"Удалена старая сессия: {chat_id}")
            
            await asyncio.sleep(300)  # Проверка каждые 5 минут
        except Exception as e:
            logger.error(f"Ошибка в cleanup_old_sessions: {e}")

async def on_startup(dispatcher: Dispatcher):
    """Действия при запуске"""
    logger.info("Бот запущен")
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await bot.set_webhook(webhook_url)
    
    # Запуск фоновой задачи для очистки старых сессий
    asyncio.create_task(cleanup_old_sessions())

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
    
    # Обязательный обработчик для Render health-check
    async def handle_root(request):
        return web.Response(text="OK")
    
    app.router.add_get("/", handle_root)
    
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_handler.register(app, path="/webhook")
    
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting server on port {port}")
    web.run_app(app, host="0.0.0.0", port=port)

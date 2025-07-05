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

# Импорты aiogram 3.x
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
        logging.FileHandler("bot.log"),  # Логи в файл
        logging.StreamHandler()           # Логи в консоль
    ]
)
logger = logging.getLogger(__name__)

API_TOKEN = os.getenv("API_TOKEN")
if not API_TOKEN:
    raise ValueError("API_TOKEN environment variable is not set")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PHOTOS_DIR = os.path.join(BASE_DIR, "photos")
TEMP_DIR = os.path.join(BASE_DIR, "temp")
TEMPLATE_DOCX = os.path.join(BASE_DIR, "template22.docx")

# Создаем необходимые директории
os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Инициализация пула потоков для тяжелых операций
executor = ThreadPoolExecutor(max_workers=3)  # Ограничиваем до 3 параллельных обработок

# Инициализация бота и диспетчера
bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage(), executor=executor)
router = Router()
dp.include_router(router)

# Размеры в сантиметрах для разных типов фото (ширина, высота)
PHOTO_SIZES = {
    "ТБ1": (10.67, 6.0),
    "ТБ2": (10.67, 6.0),
    "ИТОГ": (20.0, 12.0),
    "default": (10.67, 6.0)  # Для всех остальных тегов
}

photo_tags = [
    "ТБ1", "ТБ2",
    "ДО1", "ДО2", "ДО3", "ДО4",
    "ПОСЛЕ1", "ПОСЛЕ2", "ПОСЛЕ3", "ПОСЛЕ4",
    "ПРОЦЕСС1", "ПРОЦЕСС2", "ПРОЦЕСС3", "ПРОЦЕСС4",
    "ИТОГ"
]

class ReportState(StatesGroup):
    fio = State()
    team = State()
    date = State()
    address = State()
    bags = State()
    fighters = State()
    input_photos = State()
    choosing_tag = State()

# Глобальный словарь для хранения сессий пользователей с блокировкой
user_sessions = {}
session_lock = Lock()

def get_or_create_session(chat_id):
    """Получает или создает сессию пользователя с блокировкой"""
    with session_lock:
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {
                "fields": {
                    "{}1{}": "",  # ФИО
                    "{}2{}": "",  # Отряд
                    "{3}": "",    # Дата
                    "{4}": "",    # Адрес
                    "{5}": "",    # Мешки
                    "{6}": ""     # Бойцы
                },
                "photos": {},
                "remaining_tags": photo_tags.copy(),
                "photo_queue": [],
                "current_file_id": None,
                "current_photo_message_id": None,
                "lock": Lock(),  # Лок для сессии
                "processing": False  # Флаг обработки
            }
        return user_sessions[chat_id]

def resize_and_crop_image(image_path, target_width_cm, target_height_cm):
    """Изменяет размер изображения с сохранением пропорций и обрезкой под нужный размер"""
    # Конвертация сантиметров в пиксели (1 см ≈ 37.8 пикселей при 96 DPI)
    target_width = int(target_width_cm * 37.8)
    target_height = int(target_height_cm * 37.8)
    
    with Image.open(image_path) as img:
        # Конвертируем в RGB, если нужно
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Уменьшаем качество для экономии памяти
        quality = 90  # Уменьшено с 95 до 90
        
        # Вычисляем соотношения сторон
        width, height = img.size
        target_ratio = target_width / target_height
        image_ratio = width / height
        
        # Определяем, как будем ресайзить
        if image_ratio > target_ratio:
            # Обрезаем по ширине
            new_height = height
            new_width = int(height * target_ratio)
            left = (width - new_width) / 2
            top = 0
            right = left + new_width
            bottom = height
        else:
            # Обрезаем по высоте
            new_width = width
            new_height = int(width / target_ratio)
            left = 0
            top = (height - new_height) / 2
            right = width
            bottom = top + new_height
        
        # Обрезаем изображение
        img = img.crop((left, top, right, bottom))
        
        # Ресайзим до целевого размера с использованием LANCZOS (высокое качество)
        img = img.resize((target_width, target_height), Image.LANCZOS)
        
        # Сохраняем обратно с указанным качеством
        img.save(image_path, format="JPEG", quality=quality, subsampling=0)
        logger.info(f"Изображение изменено до {target_width}x{target_height} пикселей ({target_width_cm}x{target_height_cm} см), качество: {quality}")

async def download_photo_with_retry(file_id: str, destination_path: str, max_attempts: int = 3, timeout: int = 30) -> bool:
    """Пытается скачать фото с заданным таймаутом и количеством попыток"""
    for attempt in range(max_attempts):
        try:
            file = await bot.get_file(file_id)
            await bot.download_file(file.file_path, destination_path)
            
            # Проверяем, что файл действительно скачался
            start_time = time.time()
            while not os.path.exists(destination_path):
                if time.time() - start_time > timeout:
                    logger.error(f"Файл {destination_path} не появился после {timeout} секунд ожидания")
                    return False
                await asyncio.sleep(1)
            
            logger.info(f"Фото успешно скачано в {destination_path}")
            return True
            
        except Exception as e:
            logger.error(f"Попытка {attempt + 1} из {max_attempts} не удалась: {e}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(3)  # Уменьшено время ожидания
    
    return False

@router.message(F.text == "го")
async def start(message: Message, state: FSMContext):
    chat_id = message.chat.id
    
    # Инициализация сессии для пользователя
    session = get_or_create_session(chat_id)
    
    # Сбрасываем состояние сессии
    with session["lock"]:
        session["fields"] = {
            "{}1{}": "", "{}2{}": "", "{3}": "", 
            "{4}": "", "{5}": "", "{6}": ""
        }
        session["photos"] = {}
        session["remaining_tags"] = photo_tags.copy()
        session["photo_queue"] = []
        session["current_file_id"] = None
        session["current_photo_message_id"] = None
        session["processing"] = False
    
    await state.set_state(ReportState.fio)
    await message.answer("Введите ФИО координатора:")

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
    await message.answer("Отправьте одно или несколько фото. После каждого будет предложено выбрать метку.")

@router.message(ReportState.input_photos, F.content_type == ContentType.PHOTO)
async def handle_photos(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    # Добавляем фото в очередь
    with session["lock"]:
        session["photo_queue"].append(message.photo[-1].file_id)
    
    # Обрабатываем следующее фото
    await process_next_photo(message, state)

async def process_next_photo(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    # Проверяем флаг обработки
    with session["lock"]:
        if session["processing"]:
            return
            
        # Проверка, что есть фото для обработки
        if not session["photo_queue"] or not session["remaining_tags"]:
            return
            
        session["current_file_id"] = session["photo_queue"].pop(0)
        session["processing"] = True
    
    # Проверка, что остались доступные теги
    if not session["remaining_tags"]:
        await message.answer("Все фото уже были отмечены.")
        with session["lock"]:
            session["current_file_id"] = None
            session["processing"] = False
        return
    
    buttons = [[InlineKeyboardButton(text=tag, callback_data=f"choose:{tag}")] for tag in session["remaining_tags"]]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    try:
        sent_msg = await bot.send_photo(
            message.chat.id, 
            session["current_file_id"], 
            caption="Выберите, что изображено на фото:", 
            reply_markup=markup
        )
        with session["lock"]:
            session["current_photo_message_id"] = sent_msg.message_id
        await state.set_state(ReportState.choosing_tag)
    except Exception as e:
        logger.error(f"Ошибка при отправке фото: {e}")
        with session["lock"]:
            session["current_file_id"] = None
            session["processing"] = False
        await message.answer("Произошла ошибка при обработке фото. Попробуйте отправить его снова.")
        await state.set_state(ReportState.input_photos)

@router.callback_query(F.data.startswith("choose:"))
async def handle_tag(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    session = get_or_create_session(chat_id)
    
    # Проверка состояния с блокировкой
    with session["lock"]:
        if session["current_file_id"] is None:
            await callback.answer("Нет активного фото для обработки.")
            return
            
        tag = callback.data.split(":")[1]
        photo_path = os.path.join(PHOTOS_DIR, f"{chat_id}_{tag}.jpg")
        file_id = session["current_file_id"]
        session["current_file_id"] = None
    
    try:
        # Удаляем старое фото, если оно есть
        if os.path.exists(photo_path):
            try:
                os.remove(photo_path)
            except Exception as e:
                logger.error(f"Ошибка при удалении старого фото: {e}")
        
        # Пытаемся скачать фото с ожиданием
        success = await download_photo_with_retry(file_id, photo_path)
        
        if not success:
            await callback.message.answer(f"Не удалось скачать фото {tag}. Попробуйте отправить его снова.")
            # Сбрасываем состояние для продолжения работы
            with session["lock"]:
                session["processing"] = False
            await state.set_state(ReportState.input_photos)
            return
        
        # Определяем размеры для этого типа фото
        width_cm, height_cm = PHOTO_SIZES.get(tag, PHOTO_SIZES["default"])
        
        # Обрабатываем фото в потоке (не блокируем event loop)
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
                logger.error(f"Ошибка при удалении сообщения: {e}")
            with session["lock"]:
                session["current_photo_message_id"] = None
        
        await callback.answer(f"Фото сохранено как {tag} ({width_cm}x{height_cm} см)")
        
        # Проверяем завершение
        with session["lock"]:
            if not session["remaining_tags"]:
                await callback.message.answer("Генерирую отчёт... Подождите 5–10 секунд.")
                await generate_docx(callback.message, chat_id)
                await state.clear()
            else:
                # Возвращаем в состояние ожидания фото
                await state.set_state(ReportState.input_photos)
                # Обрабатываем следующее фото в очереди
                await process_next_photo(callback.message, state)
            
    except Exception as e:
        logger.error(f"Ошибка обработки фото {tag}: {e}", exc_info=True)
        await callback.message.answer(f"Ошибка обработки фото: {e}")
        
        # Сбрасываем состояние для продолжения работы
        with session["lock"]:
            session["processing"] = False
        await state.set_state(ReportState.input_photos)
        await process_next_photo(callback.message, state)

@router.message(ReportState.input_photos, F.text)
async def handle_text_during_photos(message: Message):
    await message.answer("Пожалуйста, отправьте фото. Если вы закончили отправку фото, введите команду /generate")

@router.message(Command("generate"))
async def force_generate(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        if not session["photos"]:
            await message.answer("Нет фото для генерации отчета.")
            return
            
        await message.answer("Генерирую отчёт... Подождите 5–10 секунд.")
        await generate_docx(message, chat_id)
        await state.clear()

@router.message(Command("reset"))
async def reset_session(message: Message, state: FSMContext):
    chat_id = message.chat.id
    with session_lock:
        if chat_id in user_sessions:
            # Удаляем все файлы пользователя
            for tag, path in user_sessions[chat_id]["photos"].items():
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    logger.error(f"Ошибка при удалении фото: {e}")
            del user_sessions[chat_id]
    
    # Полностью очищаем состояние
    await state.clear()
    await message.answer("Сессия сброшена. Введите 'го' для начала.")

@router.message(Command("debug"))
async def debug_command(message: Message, state: FSMContext):
    chat_id = message.chat.id
    current_state = await state.get_state()
    
    debug_info = f"Текущее состояние: {current_state}\n"
    
    with session_lock:
        if chat_id in user_sessions:
            session = user_sessions[chat_id]
            debug_info += (
                f"Осталось тегов: {len(session['remaining_tags']}\n"
                f"Фото в очереди: {len(session['photo_queue']}\n"
                f"Обработанные фото: {list(session['photos'].keys())}\n"
                f"Текущее фото: {session['current_file_id']}\n"
                f"Обрабатывается: {session['processing']}"
            )
        else:
            debug_info += "Активной сессии нет"
    
    await message.answer(debug_info)

async def replace_image_in_docx(doc_path: str, image_tag: str, new_image_path: str):
    """Заменяет изображение в документе по тегу, сохраняя все свойства оригинала"""
    # Временная директория для распаковки docx
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Распаковываем docx как zip-архив
        with zipfile.ZipFile(doc_path, 'r') as zip_ref:
            zip_ref.extractall(tmp_dir)
        
        # Путь к файлу отношений документа
        document_xml_path = os.path.join(tmp_dir, 'word', 'document.xml')
        relationships_path = os.path.join(tmp_dir, 'word', '_rels', 'document.xml.rels')
        
        # Парсим XML документ
        tree = ET.parse(document_xml_path)
        root = tree.getroot()
        
        # Находим все изображения
        namespaces = {
            'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
            'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
            'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture',
            'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
        }
        
        # Регистрируем пространства имен
        for prefix, uri in namespaces.items():
            ET.register_namespace(prefix, uri)
        
        found = False
        for pic in root.findall('.//pic:pic', namespaces=namespaces):
            nv_pr = pic.find('pic:nvPicPr/pic:cNvPr', namespaces=namespaces)
            if nv_pr is not None and nv_pr.get('descr') == image_tag:
                # Находим ID изображения
                blip = pic.find('.//a:blip', namespaces=namespaces)
                if blip is not None:
                    r_id = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                    
                    # Находим файл изображения в relationships
                    rel_tree = ET.parse(relationships_path)
                    rel_root = rel_tree.getroot()
                    
                    for rel in rel_root.findall('.//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship'):
                        if rel.get('Id') == r_id:
                            image_file = os.path.join(tmp_dir, 'word', rel.get('Target'))
                            
                            # Заменяем изображение
                            shutil.copy(new_image_path, image_file)
                            logger.info(f"Изображение {image_tag} заменено")
                            found = True
                            break
        
        if not found:
            logger.warning(f"Изображение с тегом {image_tag} не найдено в документе")
        
        # Сохраняем изменения
        tree.write(document_xml_path, encoding='UTF-8', xml_declaration=True)
        
        # Перепаковываем в docx
        with zipfile.ZipFile(doc_path, 'w') as zip_ref:
            for root, dirs, files in os.walk(tmp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, tmp_dir)
                    zip_ref.write(file_path, arcname)

async def generate_docx(message: Message, chat_id: int):
    if chat_id not in user_sessions:
        await message.answer("Сессия устарела. Начните заново.")
        return
    
    session = user_sessions[chat_id]
    
    # Создаем временные директории для пользователя
    user_temp_dir = os.path.join(TEMP_DIR, str(chat_id))
    os.makedirs(user_temp_dir, exist_ok=True)
    
    output_path = os.path.join(user_temp_dir, "Final_Отчет.docx")
    shutil.copy(TEMPLATE_DOCX, output_path)
    
    try:
        # Проверяем наличие всех фото перед обработкой
        missing_photos = []
        for tag, path in session["photos"].items():
            if not os.path.exists(path):
                missing_photos.append(tag)
        
        if missing_photos:
            await message.answer(f"Не найдены фото: {', '.join(missing_photos)}. Отчёт не может быть сгенерирован.")
            return
        
        # Заменяем изображения в документе
        for tag, image_path in session["photos"].items():
            await replace_image_in_docx(output_path, tag, image_path)
        
        # Теперь заменяем текстовые поля
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Распаковываем docx
            with zipfile.ZipFile(output_path, 'r') as zip_ref:
                zip_ref.extractall(tmp_dir)
            
            document_xml_path = os.path.join(tmp_dir, 'word', 'document.xml')
            
            # Загружаем XML
            tree = ET.parse(document_xml_path)
            root = tree.getroot()
            
            # Пространства имен
            namespaces = {
                'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
            }
            
            # Регистрируем пространство имен
            ET.register_namespace('w', namespaces['w'])
            
            # Заменяем текст во всех текстовых элементах
            for text_elem in root.findall('.//w:t', namespaces=namespaces):
                if text_elem.text:
                    for key, val in session["fields"].items():
                        if key in text_elem.text:
                            text_elem.text = text_elem.text.replace(key, val)
            
            # Сохраняем изменения
            tree.write(document_xml_path, encoding='UTF-8', xml_declaration=True)
            
            # Перепаковываем документ
            with zipfile.ZipFile(output_path, 'w') as zip_ref:
                for root, dirs, files in os.walk(tmp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, tmp_dir)
                        zip_ref.write(file_path, arcname)
        
        # Проверяем, что файл создан
        if not os.path.exists(output_path):
            await message.answer("Ошибка: не удалось создать файл отчёта.")
            return
        
        await bot.send_document(chat_id, FSInputFile(output_path), caption="Вот ваш отчет")
        
    except Exception as e:
        logger.error(f"Ошибка при генерации отчёта: {e}")
        await message.answer(f"Произошла ошибка при генерации отчёта: {e}")
    finally:
        # Очищаем временные файлы пользователя
        try:
            shutil.rmtree(user_temp_dir)
        except Exception as e:
            logger.error(f"Ошибка при очистке временных файлов: {e}")
        
        # Очищаем фото пользователя
        for tag, path in session["photos"].items():
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.error(f"Ошибка при удалении фото: {e}")
        
        # Очищаем сессию
        if chat_id in user_sessions:
            with session_lock:
                if chat_id in user_sessions:
                    del user_sessions[chat_id]

# Функции запуска/остановки
async def on_startup(dispatcher: Dispatcher):
    logger.info("Бот успешно запущен")
    # Устанавливаем вебхук при запуске
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        logger.info(f"Устанавливаю вебхук: {webhook_url}")
        await bot.set_webhook(webhook_url)

async def on_shutdown(dispatcher: Dispatcher):
    logger.info("Бот выключается...")
    # Удаляем вебхук при выключении
    await bot.delete_webhook()
    
    # Очищаем все временные файлы
    for root, dirs, files in os.walk(BASE_DIR):
        for file in files:
            if file.endswith(".jpg") or file.endswith(".docx"):
                try:
                    os.remove(os.path.join(root, file))
                except Exception as e:
                    logger.error(f"Ошибка удаления временного файла: {e}")
    
    # Очищаем сессии
    with session_lock:
        user_sessions.clear()

# Запуск веб-сервера
if __name__ == "__main__":
    # Создаем aiohttp приложение
    app = web.Application()
    
    # Регистрируем обработчики запуска/остановки
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Создаем обработчик вебхуков
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_handler.register(app, path="/webhook")
    
    # Настраиваем порт для Render
    port = int(os.environ.get("PORT", 5000))
    
    # Запускаем веб-сервер
    web.run_app(
        app,
        host="0.0.0.0",
        port=port,
    )

import asyncio
import logging
import os
import shutil
import time
import zipfile
import tempfile
import re
from PIL import Image
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from asyncio import Semaphore

# Импорты aiogram
from aiogram import Bot, types, F, Router
from aiogram.enums import ParseMode
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

logger = logging.getLogger(__name__)
daily_router = Router()

# Константы
PHOTO_SIZES = {
    "ТБ1": (10.4, 7.4),
    "ТБ2": (10.4, 7.4),
    "ПРОЦЕСС1": (10.4, 7.4),
    "ПРОЦЕСС2": (10.4, 7.4),
    "ПРОЦЕСС3": (10.4, 7.4),
    "ПРОЦЕСС4": (10.4, 7.4),
    "ОБЩЕЕФОТО": (20.0, 12.0),
    "default": (10.4, 7.4)
}

MAX_PHOTOS = 15
photo_tags = [
    "ТБ1", "ТБ2",
    "ДО1", "ДО2", "ДО3", "ДО4",
    "ПОСЛЕ1", "ПОСЛЕ2", "ПОСЛЕ3", "ПОСЛЕ4",
    "ПРОЦЕСС1", "ПРОЦЕСС2", "ПРОЦЕСС3", "ПРОЦЕСС4",
    "ОБЩЕЕФОТО"
]

# Константа для преобразования см в пиксели
CM_TO_PX = 37.8

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
session_timers = {}
executor = ThreadPoolExecutor(max_workers=3)
processing_semaphore = Semaphore(3)

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
                "processing": False
            }
        return user_sessions[chat_id]

async def reset_session_timer(chat_id, state):
    """Сброс и перезапуск таймера сессии"""
    if chat_id in session_timers:
        try:
            session_timers[chat_id].cancel()
        except:
            pass
    
    session_timers[chat_id] = asyncio.create_task(
        session_timeout_handler(chat_id, state)
    )

async def session_timeout_handler(chat_id, state: FSMContext):
    """Обработчик таймаута сессии"""
    await asyncio.sleep(360)  # 6 минут
    
    with session_lock:
        if chat_id in user_sessions:
            session = user_sessions[chat_id]
            for tag, path in session["photos"].items():
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    logger.error(f"Ошибка удаления фото: {e}")
            del user_sessions[chat_id]
    
    await state.clear()
    
    try:
        bot = Bot.get_current()
        await bot.send_message(
            chat_id,
            "⏳ Ваша сессия завершена из-за неактивности.\n"
            "Используйте /start чтобы начать заново."
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения о таймауте: {e}")

def resize_and_crop_image(image_path, target_width_cm, target_height_cm):
    """Агрессивное заполнение изображения до заданного размера без рамки"""
    target_width_px = int(target_width_cm * CM_TO_PX)
    target_height_px = int(target_height_cm * CM_TO_PX)

    with Image.open(image_path) as img:
        if img.mode != 'RGB':
            img = img.convert('RGB')

        width, height = img.size
        scale = max(
            target_width_px / width,
            target_height_px / height
        )
        scaled_width = int(width * scale)
        scaled_height = int(height * scale)

        img = img.resize((scaled_width, scaled_height), Image.LANCZOS)
        left = (scaled_width - target_width_px) // 2
        top = (scaled_height - target_height_px) // 2
        img = img.crop((
            left,
            top,
            left + target_width_px,
            top + target_height_px
        ))

        img.save(image_path, format="JPEG", quality=95, subsampling=0)
        logger.info(f"Изображение обработано: {target_width_px}x{target_height_px}px")

async def download_photo_with_retry(file_id: str, destination_path: str, bot: Bot, max_attempts: int = 3) -> bool:
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
                await asyncio.sleep(0.5)
            
            return True
        except Exception as e:
            logger.error(f"Ошибка загрузки (попытка {attempt + 1}): {e}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(2)
    
    return False

@daily_router.message(Command("start"))
async def start_daily_report(message: Message, state: FSMContext):
    """Запуск сценария"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"] = {k: "" for k in session["fields"]}
        session["photos"] = {}
        session["remaining_tags"] = photo_tags.copy()
        session["photo_queue"] = []
        session["current_file_id"] = None
        session["processing"] = False
    
    await reset_session_timer(chat_id, state)
    await state.set_state(ReportState.fio)
    await message.answer("Введите ФИО координатора:")

@daily_router.message(Command("reset"))
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
    
    if chat_id in session_timers:
        try:
            session_timers[chat_id].cancel()
            del session_timers[chat_id]
        except:
            pass
    
    await state.clear()
    await message.answer("Сессия сброшена. Введите /start для начала.")

@daily_router.message(Command("help"))
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

@daily_router.message(Command("generate"))
async def generate_command(message: Message, state: FSMContext):
    """Принудительная генерация отчета"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    if not session["fields"]["{}1{}"]:
        await message.answer("Сначала заполните основные данные. Введите /start")
        return
    
    with session["lock"]:
        session["photo_queue"] = []
        session["current_file_id"] = None
        session["processing"] = False
    
    await generate_docx(message, chat_id, state)

# Обработчики состояний
@daily_router.message(ReportState.fio)
async def handle_fio(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{}1{}"] = message.text
    
    await reset_session_timer(chat_id, state)
    await state.set_state(ReportState.team)
    await message.answer("Введите название отряда:")

@daily_router.message(ReportState.team)
async def handle_team(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{}2{}"] = message.text
    
    await reset_session_timer(chat_id, state)
    await state.set_state(ReportState.date)
    await message.answer("Введите дату уборки:")

@daily_router.message(ReportState.date)
async def handle_date(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{3}"] = message.text
    
    await reset_session_timer(chat_id, state)
    await state.set_state(ReportState.address)
    await message.answer("Введите адрес уборки:")

@daily_router.message(ReportState.address)
async def handle_address(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{4}"] = message.text
    
    await reset_session_timer(chat_id, state)
    await state.set_state(ReportState.bags)
    await message.answer("Введите количество мешков:")

@daily_router.message(ReportState.bags)
async def handle_bags(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{5}"] = message.text
    
    await reset_session_timer(chat_id, state)
    await state.set_state(ReportState.fighters)
    await message.answer("Введите количество бойцов:")

@daily_router.message(ReportState.fighters)
async def handle_fighters(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        session["fields"]["{6}"] = message.text
    
    await reset_session_timer(chat_id, state)
    await state.set_state(ReportState.input_photos)
    await message.answer("Отправляйте фото. Для каждого будет запрошен тип.")

# Обработчики фото
@daily_router.message(ReportState.input_photos, F.photo)
async def handle_photo_only(message: Message, state: FSMContext):
    """Обработчик фото (игнорирует подписи)"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    bot = state.dispatcher.bot
    
    await reset_session_timer(chat_id, state)
    
    with session["lock"]:
        if len(session["photos"]) >= MAX_PHOTOS:
            await message.answer(f"⚠️ Достигнут лимит в {MAX_PHOTOS} фото! Используйте /generate для создания отчета")
            return
        
        if not session["remaining_tags"]:
            await message.answer("⚠️ Все типы фото использованы! Используйте /generate для создания отчета")
            return
            
        session["photo_queue"].append(message.photo[-1].file_id)
        logger.info(f"Фото добавлено в очередь. Всего в очереди: {len(session['photo_queue'])}")
    
    if len(session["photo_queue"]) == 1:
        await process_next_photo(chat_id, state, bot)

async def process_next_photo(chat_id: int, state: FSMContext, bot: Bot):
    """Обработка следующего фото в очереди"""
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        if session["processing"] or not session["photo_queue"]:
            return
            
        if len(session["photos"]) >= MAX_PHOTOS:
            session["photo_queue"] = []
            await bot.send_message(chat_id, f"⚠️ Достигнут лимит в {MAX_PHOTOS} фото! Используйте /generate")
            return
            
        session["current_file_id"] = session["photo_queue"][0]
        session["processing"] = True
        
        if not session["remaining_tags"]:
            await bot.send_message(chat_id, "⚠️ Все типы фото использованы! Используйте /generate")
            session["photo_queue"] = []
            session["current_file_id"] = None
            session["processing"] = False
            return
    
    buttons = [
        [InlineKeyboardButton(text=tag, callback_data=f"tag_{tag}")] 
        for tag in session["remaining_tags"]
    ]
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
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            session["current_file_id"] = None
            session["processing"] = False

@daily_router.callback_query(ReportState.choosing_tag, F.data.startswith("tag_"))
async def handle_photo_tag(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора типа фото"""
    chat_id = callback.message.chat.id
    session = get_or_create_session(chat_id)
    bot = callback.bot
    tag = callback.data.replace("tag_", "")
    
    await reset_session_timer(chat_id, state)
    
    if tag == "skip":
        try:
            await callback.message.delete()
        except:
            pass
        
        await callback.message.answer("⏭ Фото пропущено.")
        
        with session["lock"]:
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            session["current_file_id"] = None
            session["processing"] = False
        
        if session["photo_queue"]:
            await process_next_photo(chat_id, state, bot)
        elif session["remaining_tags"]:
            await callback.message.answer(f"Остались невыбранные типы: {', '.join(session['remaining_tags'])}\nОтправьте фото или используйте /generate")
        return
    
    if not session["current_file_id"]:
        await callback.answer("Фото уже обработано")
        return
    
    photo_path = os.path.join(os.getcwd(), "photos", f"{chat_id}_{tag}.jpg")
    os.makedirs(os.path.dirname(photo_path), exist_ok=True)
    
    if await download_photo_with_retry(session["current_file_id"], photo_path, bot):
        width, height = PHOTO_SIZES.get(tag, PHOTO_SIZES["default"])
        
        async with processing_semaphore:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                executor,
                resize_and_crop_image,
                photo_path, width, height
            )
        
        try:
            await callback.message.delete()
        except Exception as e:
            logger.error(f"Ошибка при удалении сообщения: {e}")
        
        await callback.message.answer(f"✅ Фото сохранено как: {tag}")
        
        with session["lock"]:
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            
            session["photos"][tag] = photo_path
            if tag in session["remaining_tags"]:
                session["remaining_tags"].remove(tag)
            session["current_file_id"] = None
            session["processing"] = False
        
        if session["photo_queue"]:
            await process_next_photo(chat_id, state, bot)
        elif not session["remaining_tags"] or len(session["photos"]) >= MAX_PHOTOS:
            await generate_docx(callback.message, chat_id, state)
    else:
        await callback.message.answer("❌ Ошибка загрузки фото")
        with session["lock"]:
            if session["photo_queue"]:
                session["photo_queue"].pop(0)
            session["current_file_id"] = None
            session["processing"] = False
        
        if session["photo_queue"]:
            await process_next_photo(chat_id, state, bot)

    await state.set_state(ReportState.input_photos)

# Игнорирование текста
@daily_router.message(ReportState.input_photos, F.text)
async def ignore_text_messages(message: Message, state: FSMContext):
    """Игнорирует текст, если это не команда"""
    if message.text.startswith('/'):
        await reset_session_timer(message.chat.id, state)
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
            for root_dir, _, files in os.walk(tmp_dir):
                for file in files:
                    file_path = os.path.join(root_dir, file)
                    arcname = os.path.relpath(file_path, tmp_dir)
                    zip_ref.write(file_path, arcname)

async def generate_docx(message: Message, chat_id: int, state: FSMContext):
    """Генерация итогового документа"""
    session = get_or_create_session(chat_id)
    bot = message.bot
    user_temp_dir = os.path.join(os.getcwd(), "temp", str(chat_id))
    os.makedirs(user_temp_dir, exist_ok=True)
    
    coordinator_name = session["fields"]["{}1{}"]
    safe_name = re.sub(r'[\\/*?:"<>|]', "", coordinator_name)[:50]
    output_path = os.path.join(user_temp_dir, f"{safe_name}_отчёт.docx")
    template_path = os.path.join(os.getcwd(), "template22.docx")
    
    try:
        if not os.path.exists(template_path):
            await message.answer("❌ Шаблон отчета не найден!")
            return
            
        shutil.copy(template_path, output_path)
        
        # Проверка фото
        missing_photos = []
        for tag, path in session["photos"].items():
            if not os.path.exists(path):
                missing_photos.append(tag)
        
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
                for root_dir, _, files in os.walk(tmp_dir):
                    for file in files:
                        file_path = os.path.join(root_dir, file)
                        arcname = os.path.relpath(file_path, tmp_dir)
                        zip_ref.write(file_path, arcname)
        
        if not os.path.exists(output_path):
            await message.answer("Ошибка создания отчета")
            return
        
        await bot.send_document(chat_id, FSInputFile(output_path), caption="Ваш отчет")
        
        # Очистка
        with session_lock:
            if chat_id in user_sessions:
                for tag, path in user_sessions[chat_id]["photos"].items():
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except Exception as e:
                        logger.error(f"Ошибка удаления фото: {e}")
                del user_sessions[chat_id]
        
        if chat_id in session_timers:
            try:
                session_timers[chat_id].cancel()
                del session_timers[chat_id]
            except:
                pass
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}", exc_info=True)
        await message.answer("Ошибка генерации отчета")
    finally:
        try:
            shutil.rmtree(user_temp_dir, ignore_errors=True)
        except Exception as e:
            logger.error(f"Ошибка очистки временных файлов: {e}")

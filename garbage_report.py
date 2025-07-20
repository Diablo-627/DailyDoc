import os
import re
import asyncio
import logging
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from asyncio import Semaphore
from typing import Dict, List, Tuple

from aiogram import Bot, types, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    CallbackQuery
)
from PIL import Image

logger = logging.getLogger(__name__)
garbage_router = Router()

# Константы
PHOTO_WIDTH = 13.33  # см
PHOTO_HEIGHT = 7.5   # см
CM_TO_PX = 37.8      # 1 см ≈ 37.8 пикселей
TEMPLATE_NAME = "template21.docx"
MAX_ADDRESSES = 15
PHOTOS_PER_ADDRESS = 2
TOTAL_PHOTOS = 30
PHOTO_SIZES = {
    "default": (PHOTO_WIDTH, PHOTO_HEIGHT)
}

class GarbageReportState(StatesGroup):
    DATE = State()
    ADDRESSES = State()
    EQUIPMENT = State()
    GARBAGE_AMOUNT = State()
    PARTICIPANTS = State()
    HOURS = State()
    INPUT_PHOTOS = State()
    ASSIGN_PHOTO = State()

# Глобальные переменные для управления сессиями
user_sessions: Dict[int, Dict] = {}
session_lock = Lock()
executor = ThreadPoolExecutor(max_workers=3)
processing_semaphore = Semaphore(3)

def get_or_create_session(chat_id: int) -> Dict:
    with session_lock:
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {
                "date": "",
                "addresses": [],
                "equipment": "",
                "garbage_amount": "",
                "participants": "",
                "hours": "",
                "photos": {},
                "photo_queue": [],
                "current_photo": None,
                "lock": Lock(),
                "processing": False,
                "used_photo_tags": set(),
            }
        return user_sessions[chat_id]

async def reset_session(chat_id: int):
    with session_lock:
        if chat_id in user_sessions:
            # Удаляем временные файлы фото
            for address, photos in user_sessions[chat_id]["photos"].items():
                for photo_path in photos:
                    try:
                        if os.path.exists(photo_path):
                            os.remove(photo_path)
                    except Exception as e:
                        logger.error(f"Ошибка удаления фото: {e}")
            del user_sessions[chat_id]

async def start_garbage_report(message: types.Message, state: FSMContext):
    """Запуск сценария отчета по вывозу мусора"""
    chat_id = message.chat.id
    await reset_session(chat_id)
    await state.clear()
    await state.set_state(GarbageReportState.DATE)
    await message.answer(
        "📅 Введите дату вывоза мусора:",
        reply_markup=ReplyKeyboardRemove()
    )

@garbage_router.message(GarbageReportState.DATE)
async def process_date(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    with session["lock"]:
        session["date"] = message.text
    await state.set_state(GarbageReportState.ADDRESSES)
    await message.answer(
        "🏠 Введите адреса вывоза (каждый адрес с новой строки):\n"
        f"Максимум: {MAX_ADDRESSES} адресов"
    )

@garbage_router.message(GarbageReportState.ADDRESSES)
async def process_addresses(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    raw_addresses = message.text.split('\n')
    addresses = []
    seen = set()
    duplicates = []
    
    for addr in raw_addresses:
        addr_clean = addr.strip()
        if addr_clean:
            if addr_clean in seen:
                duplicates.append(addr_clean)
            else:
                seen.add(addr_clean)
                addresses.append(addr_clean)
    
    if duplicates:
        await message.answer(f"⚠️ Обнаружены повторяющиеся адреса: {', '.join(set(duplicates))}. Пожалуйста, введите уникальные адреса.")
        return
    
    if len(addresses) > MAX_ADDRESSES:
        await message.answer(f"⚠️ Слишком много адресов! Максимум: {MAX_ADDRESSES}")
        return
    
    if not addresses:
        await message.answer("⚠️ Нет адресов! Введите хотя бы один адрес")
        return
    
    with session["lock"]:
        session["addresses"] = addresses
    
    await state.set_state(GarbageReportState.EQUIPMENT)
    await message.answer("🚛 Введите задействованную технику:")

@garbage_router.message(GarbageReportState.EQUIPMENT)
async def process_equipment(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    with session["lock"]:
        session["equipment"] = message.text
    await state.set_state(GarbageReportState.GARBAGE_AMOUNT)
    await message.answer("🗑️ Введите количество вывезенного мусора (в тоннах):")

@garbage_router.message(GarbageReportState.GARBAGE_AMOUNT)
async def process_garbage_amount(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    with session["lock"]:
        session["garbage_amount"] = message.text
    await state.set_state(GarbageReportState.PARTICIPANTS)
    await message.answer("👥 Введите количество участвовавших бойцов:")

@garbage_router.message(GarbageReportState.PARTICIPANTS)
async def process_participants(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    with session["lock"]:
        session["participants"] = message.text
    await state.set_state(GarbageReportState.HOURS)
    await message.answer("⏱️ Введите часы работы техники (с учетом +1 часа на свалку):")

@garbage_router.message(GarbageReportState.HOURS)
async def process_hours(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    with session["lock"]:
        session["hours"] = message.text
        # Инициализируем структуру для фото
        session["photos"] = {addr: [] for addr in session["addresses"]}
    
    total_photos = len(session["addresses"]) * PHOTOS_PER_ADDRESS
    await state.set_state(GarbageReportState.INPUT_PHOTOS)
    await message.answer(
        f"📸 Теперь загрузите {total_photos} фото (по {PHOTOS_PER_ADDRESS} на каждый адрес).\n"
        f"Порядок адресов:\n" + "\n".join(
            f"{i+1}. {addr}" for i, addr in enumerate(session["addresses"])
        )
    )

@garbage_router.message(GarbageReportState.INPUT_PHOTOS, F.photo)
async def process_photo_upload(message: Message, state: FSMContext):
    """Обработка загруженных фото"""
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    bot = Bot.get_current()
    
    # Берем самое качественное фото
    photo_file_id = message.photo[-1].file_id
    
    with session["lock"]:
        # Проверяем, не превысили ли лимит фото
        total_uploaded = sum(len(photos) for photos in session["photos"].values())
        max_photos = len(session["addresses"]) * PHOTOS_PER_ADDRESS
        
        if total_uploaded + len(session["photo_queue"]) + 1 > max_photos:
            await message.answer(f"⚠️ Вы уже загрузили максимальное количество фото ({max_photos}).")
            return
            
        session["photo_queue"].append(photo_file_id)
    
    if not session.get("processing", False):
        await process_next_photo(chat_id, state, bot)

async def process_next_photo(chat_id: int, state: FSMContext, bot: Bot):
    """Обработка следующего фото в очереди"""
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        if session.get("processing", False) or not session["photo_queue"]:
            return
        
        session["current_photo"] = session["photo_queue"].pop(0)
        session["processing"] = True
    
    # Получаем адреса, которым не хватает фото
    addresses_needed = []
    for addr in session["addresses"]:
        if len(session["photos"].get(addr, [])) < PHOTOS_PER_ADDRESS:
            count = len(session["photos"].get(addr, []))
            addresses_needed.append((addr, f"{addr} ({count+1}/{PHOTOS_PER_ADDRESS})"))
    
    if not addresses_needed:
        await bot.send_message(chat_id, "✅ Все фото распределены! Генерирую отчет...")
        await generate_garbage_report(chat_id, state)
        return
    
    # Создаем клавиатуру с адресами
    keyboard = []
    for addr, text in addresses_needed:
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"addr_{addr}")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=session["current_photo"],
            caption="Выберите адрес для этого фото:",
            reply_markup=markup
        )
        await state.set_state(GarbageReportState.ASSIGN_PHOTO)
    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}")
        with session["lock"]:
            session["current_photo"] = None
            session["processing"] = False
        
        if session["photo_queue"]:
            await process_next_photo(chat_id, state, bot)

@garbage_router.callback_query(GarbageReportState.ASSIGN_PHOTO, F.data.startswith("addr_"))
async def assign_photo_to_address(callback: CallbackQuery, state: FSMContext):
    """Привязка фото к адресу"""
    chat_id = callback.message.chat.id
    bot = Bot.get_current()
    session = get_or_create_session(chat_id)
    address = callback.data.split("_", 1)[1]
    
    if not session["current_photo"]:
        await callback.answer("Фото уже обработано")
        return
    
    # Создаем папку для фото, если ее нет
    photos_dir = os.path.join(os.getcwd(), "garbage_photos")
    os.makedirs(photos_dir, exist_ok=True)
    
    # Скачиваем и обрабатываем фото
    photo_path = os.path.join(photos_dir, f"{chat_id}_{address}_{int(time.time())}.jpg")
    if await download_photo_with_retry(session["current_photo"], photo_path, bot):
        width, height = PHOTO_SIZES["default"]
        
        async with processing_semaphore:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(executor, resize_and_crop_image, photo_path, width, height)
        
        with session["lock"]:
            if address not in session["photos"]:
                session["photos"][address] = []
            session["photos"][address].append(photo_path)
    
    await callback.message.delete()
    await callback.answer(f"✅ Фото привязано к адресу: {address}")
    
    with session["lock"]:
        session["current_photo"] = None
        session["processing"] = False
    
    # Обрабатываем следующее фото
    await process_next_photo(chat_id, state, bot)

async def download_photo_with_retry(file_id: str, destination_path: str, bot: Bot, max_attempts: int = 3) -> bool:
    """Скачивание фото с повторами"""
    for attempt in range(max_attempts):
        try:
            file = await bot.get_file(file_id)
            await bot.download_file(file.file_path, destination_path)
            return True
        except Exception as e:
            logger.error(f"Ошибка загрузки фото (попытка {attempt+1}): {e}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(2)
    return False

def resize_and_crop_image(image_path: str, target_width_cm: float, target_height_cm: float):
    """Изменение размера и обрезка фото"""
    target_width_px = int(target_width_cm * CM_TO_PX)
    target_height_px = int(target_height_cm * CM_TO_PX)
    
    with Image.open(image_path) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        
        width, height = img.size
        scale = max(
            target_width_px / width,
            target_height_px / height,
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
            top + target_height_px,
        ))
        
        img.save(image_path, format="JPEG", quality=95, subsampling=0)

async def generate_garbage_report(chat_id: int, state: FSMContext):
    """Генерация итогового отчета"""
    session = get_or_create_session(chat_id)
    bot = Bot.get_current()
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Копируем шаблон
        template_path = os.path.join(os.getcwd(), TEMPLATE_NAME)
        output_path = os.path.join(temp_dir, "Отчет_вывоза_мусора.docx")
        shutil.copy(template_path, output_path)
        
        # Подготовка текстовых замен
        replacements = {
            "<<DATE>>": session["date"],
            "<<EQUIPMENT>>": session["equipment"],
            "<<GARBAGE_AMOUNT>>": session["garbage_amount"],
            "<<PARTICIPANTS>>": session["participants"],
            "<<HOURS>>": session["hours"],
        }
        
        # Заполняем адреса (1-15) и очищаем неиспользованные
        for i in range(1, MAX_ADDRESSES + 1):
            key = f"<<ADDRESS_{i}>>"
            if i <= len(session["addresses"]):
                replacements[key] = session["addresses"][i-1]
            else:
                replacements[key] = ""
        
        # Заменяем текстовые плейсхолдеры
        await replace_text_in_docx(output_path, replacements)
        
        # Обработка фото
        photo_counter = 1
        for i, address in enumerate(session["addresses"]):
            photos = session["photos"].get(address, [])
            for j, photo_path in enumerate(photos[:PHOTOS_PER_ADDRESS]):
                photo_tag = f"<<PHOTO_{photo_counter}>>"
                if os.path.exists(photo_path):
                    await replace_image_in_docx(output_path, photo_tag, photo_path)
                    session["used_photo_tags"].add(photo_tag)
                photo_counter += 1
        
        # Очищаем неиспользованные фото-метки
        for i in range(1, TOTAL_PHOTOS + 1):
            photo_tag = f"<<PHOTO_{i}>>"
            if photo_tag not in session["used_photo_tags"]:
                await clear_image_placeholder(output_path, photo_tag)
        
        # Отправляем документ
        await bot.send_document(
            chat_id,
            FSInputFile(output_path, filename="Отчет_вывоза_мусора.docx"),
            caption="✅ Ваш отчет готов!"
        )
    
    # Очищаем сессию
    await reset_session(chat_id)
    await state.clear()

async def clear_image_placeholder(doc_path: str, image_tag: str):
    """Удаляет изображение и его метку из документа DOCX по тегу"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(doc_path, "r") as zip_ref:
            zip_ref.extractall(tmp_dir)
        
        document_xml_path = os.path.join(tmp_dir, "word", "document.xml")
        relationships_path = os.path.join(tmp_dir, "word", "_rels", "document.xml.rels")
        
        tree = ET.parse(document_xml_path)
        root = tree.getroot()
        
        namespaces = {
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
            "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        }
        
        for prefix, uri in namespaces.items():
            ET.register_namespace(prefix, uri)
        
        found = False
        r_id_to_remove = None
        # Ищем изображение по тегу в описании
        for pic in root.findall(".//pic:pic", namespaces):
            nv_pr = pic.find("pic:nvPicPr/pic:cNvPr", namespaces)
            if nv_pr is not None and nv_pr.get("descr") == image_tag:
                # Найдем элемент blip, чтобы получить rId
                blip = pic.find(".//a:blip", namespaces)
                if blip is not None:
                    r_id_to_remove = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                # Ищем родительский элемент w:drawing
                parent = pic
                while parent is not None:
                    if parent.tag == "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing":
                        grandparent = parent.getparent()
                        if grandparent is not None:
                            grandparent.remove(parent)
                            found = True
                        break
                    parent = parent.getparent()
                break  # удаляем только первый найденный (тег уникален)
        
        # Если нашли рисунок и r_id_to_remove, то удаляем связь и файл изображения
        if found and r_id_to_remove is not None:
            # Обрабатываем файл отношений
            rel_tree = ET.parse(relationships_path)
            rel_root = rel_tree.getroot()
            rel_to_remove = None
            for rel in rel_root.findall(".//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
                if rel.get("Id") == r_id_to_remove:
                    rel_to_remove = rel
                    # Удаляем файл изображения
                    image_filename = rel.get("Target")
                    image_path = os.path.join(tmp_dir, "word", image_filename)
                    if os.path.exists(image_path):
                        os.remove(image_path)
                    break
            if rel_to_remove is not None:
                rel_root.remove(rel_to_remove)
                rel_tree.write(relationships_path, encoding="UTF-8", xml_declaration=True)
        
        # Сохраняем изменения в document.xml
        tree.write(document_xml_path, encoding="UTF-8", xml_declaration=True)
        
        # Перепаковываем docx
        with zipfile.ZipFile(doc_path, "w") as zip_ref:
            for root_dir, _, files in os.walk(tmp_dir):
                for file in files:
                    file_path = os.path.join(root_dir, file)
                    arcname = os.path.relpath(file_path, tmp_dir)
                    zip_ref.write(file_path, arcname)

async def replace_image_in_docx(doc_path: str, image_tag: str, new_image_path: str):
    """Замена изображения в DOCX по тегу"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(doc_path, "r") as zip_ref:
            zip_ref.extractall(tmp_dir)
        
        document_xml_path = os.path.join(tmp_dir, "word", "document.xml")
        relationships_path = os.path.join(tmp_dir, "word", "_rels", "document.xml.rels")
        
        tree = ET.parse(document_xml_path)
        root = tree.getroot()
        
        namespaces = {
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
            "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        }
        
        for prefix, uri in namespaces.items():
            ET.register_namespace(prefix, uri)
        
        found = False
        for pic in root.findall(".//pic:pic", namespaces):
            nv_pr = pic.find("pic:nvPicPr/pic:cNvPr", namespaces)
            if nv_pr is not None and nv_pr.get("descr") == image_tag:
                blip = pic.find(".//a:blip", namespaces)
                if blip is not None:
                    r_id = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                    
                    rel_tree = ET.parse(relationships_path)
                    rel_root = rel_tree.getroot()
                    
                    for rel in rel_root.findall(".//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
                        if rel.get("Id") == r_id:
                            image_file = os.path.join(tmp_dir, "word", rel.get("Target"))
                            shutil.copy(new_image_path, image_file)
                            found = True
                            break
        
        if not found:
            logger.warning(f"Тег изображения {image_tag} не найден")
        
        tree.write(document_xml_path, encoding="UTF-8", xml_declaration=True)
        
        with zipfile.ZipFile(doc_path, "w") as zip_ref:
            for root_dir, _, files in os.walk(tmp_dir):
                for file in files:
                    file_path = os.path.join(root_dir, file)
                    arcname = os.path.relpath(file_path, tmp_dir)
                    zip_ref.write(file_path, arcname)

async def replace_text_in_docx(doc_path: str, replacements: Dict[str, str]):
    """Замена текста в DOCX"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(doc_path, "r") as zip_ref:
            zip_ref.extractall(tmp_dir)
        
        document_xml_path = os.path.join(tmp_dir, "word", "document.xml")
        
        tree = ET.parse(document_xml_path)
        root = tree.getroot()
        
        namespaces = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        }
        
        # Ищем и заменяем текст
        for elem in root.iter():
            if elem.text:
                for old, new in replacements.items():
                    if old in elem.text:
                        elem.text = elem.text.replace(old, new)
            
            if elem.tail:
                for old, new in replacements.items():
                    if old in elem.tail:
                        elem.tail = elem.tail.replace(old, new)
        
        tree.write(document_xml_path, encoding="UTF-8", xml_declaration=True)
        
        with zipfile.ZipFile(doc_path, "w") as zip_ref:
            for root_dir, _, files in os.walk(tmp_dir):
                for file in files:
                    file_path = os.path.join(root_dir, file)
                    arcname = os.path.relpath(file_path, tmp_dir)
                    zip_ref.write(file_path, arcname)

__all__ = ['garbage_router', 'start_garbage_report']

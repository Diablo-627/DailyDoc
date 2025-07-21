import os
import asyncio
import logging
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from asyncio import Semaphore
from typing import Dict, List

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
from lxml import etree
import zipfile

logger = logging.getLogger(__name__)
garbage_router = Router()

# Константы
PHOTO_WIDTH = 13.33  # см
PHOTO_HEIGHT = 7.5   # см
CM_TO_PX = 37.8      # 1 см ≈ 37.8 пикселей
TEMPLATE_NAME = "template21.docx"
MAX_ADDRESSES = 15
PHOTOS_PER_ADDRESS = 2
TOTAL_PHOTOS = MAX_ADDRESSES * PHOTOS_PER_ADDRESS
MAX_PHOTOS = TOTAL_PHOTOS
PHOTO_SIZES = {"default": (PHOTO_WIDTH, PHOTO_HEIGHT)}

class GarbageReportState(StatesGroup):
    DATE = State()
    ADDRESSES = State()
    EQUIPMENT = State()
    GARBAGE_AMOUNT = State()
    PARTICIPANTS = State()
    HOURS = State()
    INPUT_PHOTOS = State()
    ASSIGN_PHOTO = State()

# Сессии пользователей
user_sessions: Dict[int, Dict] = {}
session_lock = Lock()
executor = ThreadPoolExecutor(max_workers=3)
processing_semaphore = Semaphore(3)

# Папка для хранения фотографий
PHOTOS_DIR = os.path.join(os.getcwd(), "garbage_photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)

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
            # Удаляем файлы
            for photos in user_sessions[chat_id]["photos"].values():
                for p in photos:
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except Exception as e:
                        logger.error(f"Ошибка удаления фото: {e}")
            del user_sessions[chat_id]

async def start_garbage_report(message: types.Message, state: FSMContext):
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
    session = get_or_create_session(message.chat.id)
    with session["lock"]:
        session["date"] = message.text
    await state.set_state(GarbageReportState.ADDRESSES)
    await message.answer(
        f"🏠 Введите адреса вывоза (по строкам), макс. {MAX_ADDRESSES}:"
    )

@garbage_router.message(GarbageReportState.ADDRESSES)
async def process_addresses(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    raw = message.text.split("\n")
    seen, addresses, dup = set(), [], []
    
    for a in raw:
        c = a.strip()
        if c:
            if c in seen: 
                dup.append(c)
            else:
                seen.add(c)
                addresses.append(c)
    
    if dup:
        await message.answer(f"⚠️ Дубли: {set(dup)}. Повторите ввод.")
        return
    if len(addresses) > MAX_ADDRESSES:
        await message.answer(f"⚠️ Слишком много адресов (макс {MAX_ADDRESSES}).")
        return
    if not addresses:
        await message.answer("⚠️ Не найден ни один адрес.")
        return
    
    with session["lock"]:
        session["addresses"] = addresses
    await state.set_state(GarbageReportState.EQUIPMENT)
    await message.answer("🚛 Введите задействованную технику:")

@garbage_router.message(GarbageReportState.EQUIPMENT)
async def process_equipment(message: types.Message, state: FSMContext):
    session = get_or_create_session(message.chat.id)
    with session["lock"]:
        session["equipment"] = message.text
    await state.set_state(GarbageReportState.GARBAGE_AMOUNT)
    await message.answer("🗑️ Введите количество мусора (тонн):")

@garbage_router.message(GarbageReportState.GARBAGE_AMOUNT)
async def process_amount(message: types.Message, state: FSMContext):
    session = get_or_create_session(message.chat.id)
    with session["lock"]:
        session["garbage_amount"] = message.text
    await state.set_state(GarbageReportState.PARTICIPANTS)
    await message.answer("👥 Введите число бойцов:")

@garbage_router.message(GarbageReportState.PARTICIPANTS)
async def process_participants(message: types.Message, state: FSMContext):
    session = get_or_create_session(message.chat.id)
    with session["lock"]:
        session["participants"] = message.text
    await state.set_state(GarbageReportState.HOURS)
    await message.answer("⏱️ Введите часы работы техники (+1 на свалку):")

@garbage_router.message(GarbageReportState.HOURS)
async def process_hours(message: types.Message, state: FSMContext):
    session = get_or_create_session(message.chat.id)
    with session["lock"]:
        session["hours"] = message.text
        session["photos"] = {addr: [] for addr in session["addresses"]}
    
    total = len(session["addresses"]) * PHOTOS_PER_ADDRESS
    await state.set_state(GarbageReportState.INPUT_PHOTOS)
    await message.answer(f"📸 Загрузите {total} фото ({PHOTOS_PER_ADDRESS} на адрес).")

@garbage_router.message(GarbageReportState.INPUT_PHOTOS, F.photo)
async def process_photo_upload(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    bot = Bot.get_current()

    fid = message.photo[-1].file_id
    
    with session["lock"]:
        uploaded = sum(len(v) for v in session["photos"].values())
        limit = len(session["addresses"]) * PHOTOS_PER_ADDRESS
        
        if uploaded + len(session["photo_queue"]) + 1 > limit:
            await message.answer(f"⚠️ Уже загружено {limit} фото.")
            return
        
        session["photo_queue"].append(fid)

    if not session["processing"]:
        await process_next_photo(chat_id, state, bot)

async def process_next_photo(chat_id: int, state: FSMContext, bot: Bot):
    session = get_or_create_session(chat_id)
    
    with session["lock"]:
        if session["processing"] or not session["photo_queue"]:
            return
        
        session["current_photo"] = session["photo_queue"].pop(0)
        session["processing"] = True

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=addr, callback_data=f"addr_{addr}")]
            for addr in session["addresses"]
        ]
    )
    
    try:
        await bot.send_photo(
            chat_id, session["current_photo"],
            caption="Выберите адрес:",
            reply_markup=kb
        )
        await state.set_state(GarbageReportState.ASSIGN_PHOTO)
    except Exception as e:
        logger.error(f"send_photo error: {e}")
        with session["lock"]:
            session["photo_queue"].insert(0, session["current_photo"])
            session["processing"] = False
        
        if session["photo_queue"]:
            await process_next_photo(chat_id, state, bot)

@garbage_router.callback_query(GarbageReportState.ASSIGN_PHOTO, F.data.startswith("addr_"))
async def assign_photo(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    bot = Bot.get_current()
    session = get_or_create_session(chat_id)
    address = callback.data.split("_", 1)[1]

    if not session["current_photo"]:
        await callback.answer("Уже обработано")
        return

    path = os.path.join(PHOTOS_DIR, f"{chat_id}_{address}_{int(time.time())}.jpg")
    
    if await download_photo_with_retry(session["current_photo"], path, bot):
        w, h = PHOTO_SIZES["default"]
        async with processing_semaphore:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, resize_and_crop_image, path, w, h)
        
        with session["lock"]:
            session["photos"][address].append(path)

    await callback.message.delete()
    await callback.answer(f"✅ Фото привязано к {address}")

    with session["lock"]:
        session["current_photo"] = None
        session["processing"] = False

    if session["photo_queue"]:
        await process_next_photo(chat_id, state, bot)
    else:
        await generate_garbage_report(chat_id, state)

async def download_photo_with_retry(file_id: str, dest: str, bot: Bot, max_attempts: int = 3) -> bool:
    for i in range(max_attempts):
        try:
            f = await bot.get_file(file_id)
            await bot.download_file(f.file_path, dest)
            return True
        except Exception as e:
            logger.error(f"download error #{i+1}: {e}")
            if i < max_attempts - 1:
                await asyncio.sleep(1)
    return False

def resize_and_crop_image(image_path: str, tw_cm: float, th_cm: float):
    tw, th = int(tw_cm * CM_TO_PX), int(th_cm * CM_TO_PX)
    img = Image.open(image_path)
    if img.mode != "RGB": 
        img = img.convert("RGB")
    
    w, h = img.size
    scale = max(tw/w, th/h)
    img = img.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
    
    left = (img.width - tw)//2
    top = (img.height - th)//2
    img = img.crop((left, top, left+tw, top+th))
    
    img.save(image_path, format="JPEG", quality=95, subsampling=0)

def replace_text_in_docx_sync(tmp_dir: str, replacements: Dict[str, str]):
    document_xml_path = os.path.join(tmp_dir, 'word', 'document.xml')
    if not os.path.exists(document_xml_path):
        logger.error("document.xml not found")
        return

    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.parse(document_xml_path, parser)
    root = tree.getroot()

    namespaces = {'w': "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    
    for t in root.xpath(".//w:t", namespaces=namespaces):
        if t.text:
            for placeholder, value in replacements.items():
                if placeholder in t.text:
                    t.text = t.text.replace(placeholder, value)

    tree.write(document_xml_path, encoding="UTF-8", xml_declaration=True, pretty_print=True)

def replace_image_in_docx_sync(tmp_dir: str, tag: str, new_path: str):
    document_xml_path = os.path.join(tmp_dir, 'word', 'document.xml')
    relationships_path = os.path.join(tmp_dir, 'word', '_rels', 'document.xml.rels')

    if not os.path.exists(document_xml_path) or not os.path.exists(relationships_path):
        logger.error("document.xml or .rels not found")
        return

    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.parse(document_xml_path, parser)
    root = tree.getroot()
    rel_tree = etree.parse(relationships_path, parser)
    rel_root = rel_tree.getroot()

    namespaces = {
        'a': "http://schemas.openxmlformats.org/drawingml/2006/main",
        'r': "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        'pic': "http://schemas.openxmlformats.org/drawingml/2006/picture",
        'wp': "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    }

    found = False
    for pic in root.xpath(".//pic:pic", namespaces=namespaces):
        nv_pr = pic.xpath(".//pic:cNvPr", namespaces=namespaces)
        if nv_pr and nv_pr[0].get("descr") == tag:
            blip = pic.xpath(".//a:blip", namespaces=namespaces)
            if blip:
                r_id = blip[0].get(f"{{{namespaces['r']}}}embed")
                if r_id:
                    for rel in rel_root.xpath(".//rels:Relationship", 
                                           namespaces={"rels": "http://schemas.openxmlformats.org/package/2006/relationships"}):
                        if rel.get("Id") == r_id:
                            image_path_in_zip = rel.get("Target")
                            image_file = os.path.join(tmp_dir, 'word', image_path_in_zip)
                            shutil.copy(new_path, image_file)
                            found = True
                            break

    if not found:
        logger.warning(f"Тег {tag} не найден в document.xml")

    tree.write(document_xml_path, encoding="UTF-8", xml_declaration=True, pretty_print=True)

def remove_empty_address_blocks(tmp_dir: str, session: dict):
    """Удаляет блоки для адресов без фотографий"""
    document_xml_path = os.path.join(tmp_dir, 'word', 'document.xml')
    relationships_path = os.path.join(tmp_dir, 'word', '_rels', 'document.xml.rels')
    
    if not os.path.exists(document_xml_path) or not os.path.exists(relationships_path):
        return

    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.parse(document_xml_path, parser)
    root = tree.getroot()
    rel_tree = etree.parse(relationships_path, parser)
    rel_root = rel_tree.getroot()

    namespaces = {
        'w': "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        'a': "http://schemas.openxmlformats.org/drawingml/2006/main",
        'r': "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        'wp': "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        'pic': "http://schemas.openxmlformats.org/drawingml/2006/picture"
    }

    # Собираем адреса без фотографий
    empty_addresses = []
    for i, addr in enumerate(session["addresses"], 1):
        if not session["photos"].get(addr) or len(session["photos"][addr]) == 0:
            empty_addresses.append(i)

    # Собираем элементы для удаления
    to_remove = set()
    rels_to_remove = set()
    images_to_remove = set()

    for addr_num in empty_addresses:
        # Теги для этого адреса
        tags = [
            f"<<ADDRESS_{addr_num}>>",
            f"<<PHOTO_{2*addr_num-1}>>",
            f"<<PHOTO_{2*addr_num}>>"
        ]

        for tag in tags:
            # Удаляем текстовые элементы
            for t in root.xpath(".//w:t[contains(., $tag)]", namespaces=namespaces, tag=tag):
                para = t.getparent()
                while para is not None and para.tag != f"{{{namespaces['w']}}}p":
                    para = para.getparent()
                if para is not None:
                    to_remove.add(para)

            # Удаляем изображения
            for pic in root.xpath(".//pic:pic[pic:nvPicPr/pic:cNvPr/@descr=$tag]", namespaces=namespaces, tag=tag):
                to_remove.add(pic)
                blip = pic.xpath(".//a:blip", namespaces=namespaces)
                if blip:
                    r_id = blip[0].get(f"{{{namespaces['r']}}}embed")
                    if r_id:
                        for rel in rel_root.xpath(f".//rels:Relationship[@Id='{r_id}']", 
                                               namespaces={"rels": "http://schemas.openxmlformats.org/package/2006/relationships"}):
                            rels_to_remove.add(rel)
                            image_path = os.path.join(tmp_dir, 'word', rel.get("Target"))
                            images_to_remove.add(image_path)

    # Удаляем элементы
    for elem in to_remove:
        parent = elem.getparent()
        if parent is not None:
            parent.remove(elem)

    # Удаляем связи
    for rel in rels_to_remove:
        parent = rel.getparent()
        if parent is not None:
            parent.remove(rel)

    # Удаляем файлы изображений
    for img_path in images_to_remove:
        try:
            if os.path.exists(img_path):
                os.remove(img_path)
        except Exception as e:
            logger.error(f"Ошибка удаления изображения {img_path}: {e}")

    # Сохраняем изменения
    tree.write(document_xml_path, encoding="UTF-8", xml_declaration=True, pretty_print=True)
    rel_tree.write(relationships_path, encoding="UTF-8", xml_declaration=True, pretty_print=True)

async def generate_garbage_report(chat_id: int, state: FSMContext):
    """Генерация итогового отчета"""
    session = get_or_create_session(chat_id)
    bot = Bot.get_current()

    with tempfile.TemporaryDirectory() as tmp:
        # Копируем шаблон
        tpl = os.path.join(os.getcwd(), TEMPLATE_NAME)
        out = os.path.join(tmp, "Отчет_вывоза_мусора.docx")
        shutil.copy(tpl, out)

        # Распаковываем для редактирования
        unpacked_dir = os.path.join(tmp, "unpacked")
        os.makedirs(unpacked_dir, exist_ok=True)
        with zipfile.ZipFile(out, 'r') as zip_ref:
            zip_ref.extractall(unpacked_dir)

        # Удаляем блоки для пустых адресов
        await asyncio.to_thread(remove_empty_address_blocks, unpacked_dir, session)

        # Текстовые замены
        reps = {
            "<<DATE>>": session["date"],
            "<<EQUIPMENT>>": session["equipment"],
            "<<GARBAGE_AMOUNT>>": session["garbage_amount"],
            "<<PARTICIPANTS>>": session["participants"],
            "<<HOURS>>": session["hours"],
        }
        
        for i in range(1, MAX_ADDRESSES+1):
            key = f"<<ADDRESS_{i}>>"
            reps[key] = session["addresses"][i-1] if i <= len(session["addresses"]) else ""

        await asyncio.to_thread(replace_text_in_docx_sync, unpacked_dir, reps)

        # Замена изображений
        cnt = 1
        for addr in session["addresses"]:
            photos = session["photos"].get(addr, [])
            for photo in photos[:PHOTOS_PER_ADDRESS]:
                tag = f"<<PHOTO_{cnt}>>"
                await asyncio.to_thread(replace_image_in_docx_sync, unpacked_dir, tag, photo)
                cnt += 1

        # Перепаковываем документ
        with zipfile.ZipFile(out, 'w') as zip_ref:
            for root, _, files in os.walk(unpacked_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, unpacked_dir)
                    zip_ref.write(file_path, arcname)

        # Отправляем результат
        await bot.send_document(chat_id, FSInputFile(out), caption="✅ Отчет готов!")

    await reset_session(chat_id)
    await state.clear()

__all__ = ['garbage_router', 'start_garbage_report']

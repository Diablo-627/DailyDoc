import os
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
from typing import Dict

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
TOTAL_PHOTOS = MAX_ADDRESSES * PHOTOS_PER_ADDRESS
MAX_PHOTOS = TOTAL_PHOTOS  # лимит по всем фотографиям
PHOTO_SIZES = {"default": (PHOTO_WIDTH, PHOTO_HEIGHT)}

# Заглушка для сброса таймера (если не нужна — остаётся no-op)
async def reset_session_timer(chat_id: int, state: FSMContext):
    pass

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
                "photos": {},          # { address: [paths...] }
                "photo_queue": [],     # очередь file_id
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
            if c in seen: dup.append(c)
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
        # инициализация списков фото
        session["photos"] = {addr: [] for addr in session["addresses"]}
    total = len(session["addresses"]) * PHOTOS_PER_ADDRESS
    await state.set_state(GarbageReportState.INPUT_PHOTOS)
    await message.answer(f"📸 Загрузите {total} фото ({PHOTOS_PER_ADDRESS} на адрес).")

@garbage_router.message(GarbageReportState.INPUT_PHOTOS, F.photo)
async def process_photo_upload(message: Message, state: FSMContext):
    chat_id = message.chat.id
    session = get_or_create_session(chat_id)
    bot = Bot.get_current()

    # самое крупное фото
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
        session["current_photo"] = session["photo_queue"][0]
        session["processing"] = True

    # Кнопки с адресами
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
            session["photo_queue"].pop(0)
            session["processing"] = False
        if session["photo_queue"]:
            await process_next_photo(chat_id, state, bot)

@garbage_router.callback_query(F.data.startswith("addr_"))
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
        session["photo_queue"].pop(0)
        session["processing"] = False

    if session["photo_queue"]:
        await process_next_photo(chat_id, state, bot)
    else:
        # все фото загружены — генерим отчёт
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
    if img.mode != "RGB": img = img.convert("RGB")
    w, h = img.size
    scale = max(tw/w, th/h)
    img = img.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
    left = (img.width - tw)//2; top = (img.height - th)//2
    img = img.crop((left, top, left+tw, top+th))
    img.save(image_path, format="JPEG", quality=95, subsampling=0)

async def replace_text_in_docx(doc_path: str, replacements: Dict[str,str]):
    # Оставляем реализацию из оригинала
    pass

async def replace_image_in_docx(doc_path: str, tag: str, new_path: str):
    # Оставляем реализацию из оригинала
    pass

async def clear_image_placeholder(doc_path: str, tag: str):
    # Оставляем реализацию из оригинала
    pass

async def generate_garbage_report(chat_id: int, state: FSMContext):
    session = get_or_create_session(chat_id)
    bot = Bot.get_current()

    with tempfile.TemporaryDirectory() as tmp:
        tpl = os.path.join(os.getcwd(), TEMPLATE_NAME)
        out = os.path.join(tmp, "Отчет_вывоза_мусора.docx")
        shutil.copy(tpl, out)

        # Текстовые плейсхолдеры
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
        await replace_text_in_docx(out, reps)

        # Фото-плейсхолдеры
        cnt = 1
        for addr in session["addresses"]:
            for photo in session["photos"].get(addr, [])[:PHOTOS_PER_ADDRESS]:
                tag = f"<<PHOTO_{cnt}>>"
                await replace_image_in_docx(out, tag, photo)
                session["used_photo_tags"].add(tag)
                cnt += 1
        for i in range(1, TOTAL_PHOTOS+1):
            tag = f"<<PHOTO_{i}>>"
            if tag not in session["used_photo_tags"]:
                await clear_image_placeholder(out, tag)

        await bot.send_document(chat_id, FSInputFile(out), caption="✅ Отчет готов!")

    await reset_session(chat_id)
    await state.clear()

__all__ = ['garbage_router', 'start_garbage_report']

import os
import re
import time
import shutil
import zipfile
import tempfile
import logging
import asyncio
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
import xml.etree.ElementTree as ET
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, CallbackQuery

logger = logging.getLogger(__name__)

PHOTO_SIZES = {
    "ТБ1": (10.4,7.4),
    "ТБ2": (10.4,7.4),
    "ПРОЦЕСС1": (10.4,7.4),
    "ПРОЦЕСС2": (10.4,7.4),
    "ПРОЦЕСС3": (10.4,7.4),
    "ПРОЦЕСС4": (10.4,7.4),
    "ОБЩЕЕФОТО": (20.0, 12.0),
    "default": (10.4,7.4)
}
photo_tags = [
    "ТБ1", "ТБ2",
    "ДО1", "ДО2", "ДО3", "ДО4",
    "ПОСЛЕ1", "ПОСЛЕ2", "ПОСЛЕ3", "ПОСЛЕ4",
    "ПРОЦЕСС1", "ПРОЦЕСС2", "ПРОЦЕСС3", "ПРОЦЕСС4",
    "ОБЩЕЕФОТО"
]
MAX_PHOTOS = 15
SESSION_TIMEOUT = 360

TEXT_FIELDS_ORDER = [
    ("fio", "Введите ФИО координатора:", "{}1{}"),
    ("team", "Введите название команды/бригады:", "{}2{}"),
    ("date", "Введите дату (например 2025-08-10):", "{3}"),
    ("address", "Введите адрес объекта:", "{4}"),
    ("bags", "Введите количество мешков/ящиков (если есть):", "{5}"),
    ("fighters", "Введите участников/работников (через запятую):", "{6}")
]

class DailyReport:
    def __init__(self, bot, template_path: str, photos_dir: str, temp_dir: str):
        self.bot = bot
        self.template_path = template_path
        self.photos_dir = photos_dir
        self.temp_dir = temp_dir
        os.makedirs(self.photos_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)

        self.router = Router()
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.processing_semaphore = asyncio.Semaphore(3)

        self.user_sessions = {}
        self.session_lock = Lock()
        self.session_timers = {}

        self.router.message(F.photo)(self._handle_photo_only)
        self.router.callback_query(F.data.startswith("tag_"))(self._handle_photo_tag)
        self.router.message(F.text)(self._process_text_input)

    def _get_or_create_session(self, chat_id: int):
        with self.session_lock:
            if chat_id not in self.user_sessions:
                fields = {tpl: "" for _, _, tpl in TEXT_FIELDS_ORDER}
                self.user_sessions[chat_id] = {
                    "fields": fields,
                    "photos": {},
                    "remaining_tags": photo_tags.copy(),
                    "photo_queue": [],
                    "current_file_id": None,
                    "lock": Lock(),
                    "processing": False,
                    "state": None
                }
            return self.user_sessions[chat_id]

    async def _reset_session_timer(self, chat_id: int):
        if chat_id in self.session_timers:
            try:
                self.session_timers[chat_id].cancel()
            except:
                pass
        self.session_timers[chat_id] = asyncio.create_task(self._session_timeout_handler(chat_id))

    async def _session_timeout_handler(self, chat_id: int):
        await asyncio.sleep(SESSION_TIMEOUT)
        with self.session_lock:
            session = self.user_sessions.pop(chat_id, None)
        if session:
            for p in session.get("photos", {}).values():
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except:
                    pass
        try:
            await self.bot.send_message(chat_id, "⏳ Ваша сессия завершена из-за неактивности. Используйте /start")
        except:
            pass

    async def start_for_user(self, chat_id: int):
        session = self._get_or_create_session(chat_id)
        with session["lock"]:
            session["fields"] = {tpl: "" for _, _, tpl in TEXT_FIELDS_ORDER}
            session["photos"] = {}
            session["remaining_tags"] = photo_tags.copy()
            session["photo_queue"] = []
            session["current_file_id"] = None
            session["processing"] = False
            session["state"] = "fio"

        await self._reset_session_timer(chat_id)
        await self.bot.send_message(chat_id, "Начинаем Ежедневный отчет. Введите ФИО координатора:")

    async def _process_text_input(self, message: Message):
        if message.text.startswith("/"):
            return
        chat_id = message.chat.id
        session = self._get_or_create_session(chat_id)
        await self._reset_session_timer(chat_id)

        state = session.get("state")
        if state is None:
            await message.answer("Сначала выберите сценарий: /start")
            return

        field_names = [f[0] for f in TEXT_FIELDS_ORDER]
        field_map = {f[0]: f for f in TEXT_FIELDS_ORDER}
        if state in field_map:
            tpl = field_map[state][2]
            session["fields"][tpl] = message.text.strip()
            idx = field_names.index(state)
            if idx + 1 < len(field_names):
                next_state = field_names[idx + 1]
                session["state"] = next_state
                await message.answer(field_map[next_state][1])
            else:
                session["state"] = "input_photos"
                await message.answer("Текстовые поля сохранены. Теперь отправьте фото.")
        elif state == "input_photos":
            await message.answer("Ожидаю фото. Отправьте фотографию.")
        else:
            await message.answer("Непонятное состояние. /start для начала.")

    async def _handle_photo_only(self, message: Message):
    chat_id = message.chat.id
    session = self._get_or_create_session(chat_id)
    await self._reset_session_timer(chat_id)

    # Если сценарий уже завершён
    if session.get("state") is None:
        await message.answer("Сценарий завершен. Начните заново через /start")
        return

    if session.get("state") != "input_photos":
        await message.answer("Сначала заполните текстовые поля.")
        return

    # Если уже есть 15 фото
    if len(session["photos"]) >= MAX_PHOTOS or not session["remaining_tags"]:
        session["state"] = None
        return

    session["photo_queue"].append(message.photo[-1].file_id)
    if len(session["photo_queue"]) == 1:
        await self._process_next_photo(chat_id)


    async def _process_next_photo(self, chat_id: int):
        session = self._get_or_create_session(chat_id)
        if session["processing"] or not session["photo_queue"]:
            return
        session["current_file_id"] = session["photo_queue"][0]
        session["processing"] = True
        buttons = [[InlineKeyboardButton(text=tag, callback_data=f"tag_{tag}")] for tag in session["remaining_tags"]]
        buttons.append([InlineKeyboardButton(text="⏭ Пропустить", callback_data="tag_skip")])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.bot.send_photo(chat_id=chat_id, photo=session["current_file_id"], caption="Выберите тип фото:", reply_markup=markup)

    async def _handle_photo_tag(self, callback: CallbackQuery):
    chat_id = callback.message.chat.id
    session = self._get_or_create_session(chat_id)
    tag = callback.data.replace("tag_", "")
    await self._reset_session_timer(chat_id)

    # Удаляем сообщение с кнопками
    try:
        await callback.message.delete()
    except:
        pass

    if tag == "skip":
        session["photo_queue"].pop(0)
        session["current_file_id"] = None
        session["processing"] = False
        if session["photo_queue"]:
            await self._process_next_photo(chat_id)
        return

    # Сохраняем фото
    photo_path = os.path.join(self.photos_dir, f"{chat_id}_{tag}.jpg")
    file = await self.bot.get_file(session["current_file_id"])
    await self.bot.download_file(file.file_path, photo_path)
    width, height = PHOTO_SIZES.get(tag, PHOTO_SIZES["default"])
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(self.executor, self._resize_and_crop_image, photo_path, width, height)
    session["photos"][tag] = photo_path
    if tag in session["remaining_tags"]:
        session["remaining_tags"].remove(tag)

    # Убираем из очереди
    session["photo_queue"].pop(0)
    session["current_file_id"] = None
    session["processing"] = False

    # Проверка на завершение
    if len(session["photos"]) >= MAX_PHOTOS or not session["remaining_tags"]:
        session["photo_queue"].clear()
        session["state"] = None
        await self._generate_docx(callback.message)
        return

    if session["photo_queue"]:
        await self._process_next_photo(chat_id)

    def _resize_and_crop_image(self, image_path, target_w_cm, target_h_cm):
        CM_TO_PX = 37.8
        target_w = int(target_w_cm * CM_TO_PX)
        target_h = int(target_h_cm * CM_TO_PX)
        with Image.open(image_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            width, height = img.size
            scale = max(target_w / width, target_h / height)
            img = img.resize((int(width*scale), int(height*scale)), Image.LANCZOS)
            left = (img.width - target_w) // 2
            top = (img.height - target_h) // 2
            img = img.crop((left, top, left + target_w, top + target_h))
            img.save(image_path, format='JPEG', quality=95, subsampling=0)

    async def _replace_image_in_docx(self, doc_path: str, image_tag: str, new_image_path: str):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(doc_path, 'r') as zip_ref:
                zip_ref.extractall(tmp_dir)
            document_xml_path = os.path.join(tmp_dir, 'word', 'document.xml')
            relationships_path = os.path.join(tmp_dir, 'word', '_rels', 'document.xml.rels')
            tree = ET.parse(document_xml_path)
            root = tree.getroot()
            namespaces = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
                          'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
                          'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture'}
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
                                shutil.copy(new_image_path, os.path.join(tmp_dir, 'word', rel.get('Target')))
            tree.write(document_xml_path, encoding='UTF-8', xml_declaration=True)
            with zipfile.ZipFile(doc_path, 'w') as zip_ref:
                for root, _, files in os.walk(tmp_dir):
                    for file in files:
                        zip_ref.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), tmp_dir))

    async def _generate_docx(self, message: Message):
        chat_id = message.chat.id
        session = self._get_or_create_session(chat_id)
        user_temp_dir = os.path.join(self.temp_dir, str(chat_id))
        os.makedirs(user_temp_dir, exist_ok=True)
        fio_key = TEXT_FIELDS_ORDER[0][2]
        safe_name = re.sub(r'[\\/*?:"<>|]', "", session["fields"].get(fio_key, "report"))[:50]
        output_path = os.path.join(user_temp_dir, f"{safe_name}_отчет.docx")
        shutil.copy(self.template_path, output_path)
        for tag, image_path in session['photos'].items():
            await self._replace_image_in_docx(output_path, tag, image_path)
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(output_path, 'r') as zip_ref:
                zip_ref.extractall(tmp_dir)
            document_xml_path = os.path.join(tmp_dir, 'word', 'document.xml')
            tree = ET.parse(document_xml_path)
            namespaces = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            for text_elem in tree.findall('.//w:t', namespaces):
                if text_elem.text:
                    for key, val in session['fields'].items():
                        if key in text_elem.text:
                            text_elem.text = text_elem.text.replace(key, val)
            tree.write(document_xml_path, encoding='UTF-8', xml_declaration=True)
            with zipfile.ZipFile(output_path, 'w') as zip_ref:
                for root, _, files in os.walk(tmp_dir):
                    for file in files:
                        zip_ref.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), tmp_dir))
        await self.bot.send_document(chat_id, FSInputFile(output_path), caption="Ваш отчет готов")


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
from aiogram.filters import Command

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

# порядок текстовых полей и маппинг на ключи в шаблоне
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

        # per-chat sessions (внутреннее хранение)
        self.user_sessions = {}
        self.session_lock = Lock()
        self.session_timers = {}

        # регистрация обработчиков (текстовый обработчик добавлен до общего ignore)
        self.router.message(Command("start"))(self._start_from_route)
        self.router.message(Command("reset"))(self._reset_from_route)
        self.router.message(Command("help"))(self._help_from_route)
        self.router.callback_query(F.data.startswith("tag_"))(self._handle_photo_tag)
        self.router.message(F.photo)(self._handle_photo_only)
        # текстовый обработчик, который действует только если сессия ожидает текстовое поле
        self.router.message(F.text)(self._process_text_input)
        # fallback для команд/прочего
        self.router.message(F.text)(self._ignore_text_messages)

    # ---------------- session helpers ----------------
    def _get_or_create_session(self, chat_id: int):
        with self.session_lock:
            if chat_id not in self.user_sessions:
                # fields keys are the placeholders used in template. Default empty.
                fields = {tpl: "" for _, _, tpl in TEXT_FIELDS_ORDER}
                self.user_sessions[chat_id] = {
                    "fields": fields,
                    "photos": {},
                    "remaining_tags": photo_tags.copy(),
                    "photo_queue": [],
                    "current_file_id": None,
                    "lock": Lock(),
                    "processing": False,
                    "state": None  # internal simple state machine (fio, team, date, input_photos, choosing_tag, etc)
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

    # --------------- image helpers -------------------
    def _resize_and_crop_image(self, image_path, target_w_cm, target_h_cm):
        CM_TO_PX = 37.8
        target_w = int(target_w_cm * CM_TO_PX)
        target_h = int(target_h_cm * CM_TO_PX)
        with Image.open(image_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            width, height = img.size
            scale = max(target_w / width, target_h / height)
            scaled_w = int(width * scale)
            scaled_h = int(height * scale)
            img = img.resize((scaled_w, scaled_h), Image.LANCZOS)
            left = (scaled_w - target_w) // 2
            top = (scaled_h - target_h) // 2
            img = img.crop((left, top, left + target_w, top + target_h))
            img.save(image_path, format='JPEG', quality=95, subsampling=0)

    async def _download_photo_with_retry(self, file_id: str, destination_path: str, max_attempts: int = 3) -> bool:
        for attempt in range(max_attempts):
            try:
                file = await self.bot.get_file(file_id)
                await self.bot.download_file(file.file_path, destination_path)
                start = time.time()
                while not os.path.exists(destination_path):
                    if time.time() - start > 30:
                        return False
                    await asyncio.sleep(0.5)
                return True
            except Exception as e:
                logger.error("download attempt %s failed: %s", attempt + 1, e)
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2)
        return False

    # ------------- публичный старт (вызов из main) --------------
    async def start_for_user(self, chat_id: int):
        """
        Автоматический старт сценария: инициализируем сессию, ставим state='fio' и просим ФИО.
        """
        session = self._get_or_create_session(chat_id)
        with session["lock"]:
            # сбрасываем предыдущие данные
            session["fields"] = {tpl: "" for _, _, tpl in TEXT_FIELDS_ORDER}
            session["photos"] = {}
            session["remaining_tags"] = photo_tags.copy()
            session["photo_queue"] = []
            session["current_file_id"] = None
            session["processing"] = False
            session["state"] = "fio"

        await self._reset_session_timer(chat_id)
        try:
            await self.bot.send_message(chat_id, "Начинаем Ежедневный отчет. Введите ФИО координатора:")
        except Exception as e:
            logger.error("start_for_user send_message error: %s", e)

    # ------------ route handlers (команды) --------------------
    async def _start_from_route(self, message: Message):
        # тот же эффект, что и start_for_user, но вызывается через /start
        await self.start_for_user(message.chat.id)

    async def _reset_from_route(self, message: Message):
        chat_id = message.chat.id
        with self.session_lock:
            session = self.user_sessions.pop(chat_id, None)
            if session:
                for p in session.get("photos", {}).values():
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except:
                        pass
        if chat_id in self.session_timers:
            try:
                self.session_timers[chat_id].cancel()
                del self.session_timers[chat_id]
            except:
                pass
        await message.answer("Сессия сброшена. Введите /start для начала.")

    async def _help_from_route(self, message: Message):
        await message.answer("Доступные команды: /start, /reset, /help")

    # ------------- обработка текстовых полей (последовательно) ------------
    async def _process_text_input(self, message: Message):
        # игнорируем команды тут
        if message.text.startswith("/"):
            return
        chat_id = message.chat.id
        session = self._get_or_create_session(chat_id)
        await self._reset_session_timer(chat_id)

        with session["lock"]:
            state = session.get("state")

        # если мы ожидаем одно из текстовых полей — принимаем
        if state is None:
            # не в сессии — просим выбрать сценарий
            await message.answer("Сначала выберите сценарий: /start")
            return

        # find current field in order
        field_names = [f[0] for f in TEXT_FIELDS_ORDER]
        field_map = {f[0]: f for f in TEXT_FIELDS_ORDER}
        if state in field_map:
            # save into template key
            tpl = field_map[state][2]
            with session["lock"]:
                session["fields"][tpl] = message.text.strip()
            # move to next field or to photo input
            idx = field_names.index(state)
            if idx + 1 < len(field_names):
                next_state = field_names[idx + 1]
                with session["lock"]:
                    session["state"] = next_state
                await message.answer(field_map[next_state][1])
            else:
                # все текстовые поля заполнены — переходим к загрузке фото
                with session["lock"]:
                    session["state"] = "input_photos"
                await message.answer("Текстовые поля сохранены. Теперь отправьте фото (по одному). Для каждого фото выберите тип.")
        elif state == "input_photos":
            await message.answer("Ожидаю фото. Отправьте фотографию.")
        else:
            await message.answer("Непонятное состояние сессии. Используйте /reset и начните сначала.")

    async def _ignore_text_messages(self, message: Message):
        # fallback — не мешаем
        if message.text.startswith("/"):
            return
        # если сюда дошёл пользователь — значит мы не ожидали текст — подсказка
        await message.answer("Используйте /start для выбора сценария или отправьте фото (если уже в режиме фото).")

    # ------------- фото и теги (как раньше) -----------------
    async def _handle_photo_only(self, message: Message):
        chat_id = message.chat.id
        session = self._get_or_create_session(chat_id)
        await self._reset_session_timer(chat_id)
        with session["lock"]:
            # убедимся, что пользователь перешёл к фото
            if session.get("state") != "input_photos":
                await message.answer("Сначала заполните текстовые поля. Если хотите начать загрузку фото сразу, используйте /start и следуйте подсказкам.")
                return
            if len(session["photos"]) >= MAX_PHOTOS:
                await message.answer(f"⚠️ Достигнут лимит в {MAX_PHOTOS} фото! Используйте /reset и начните заново")
                return
            if not session["remaining_tags"]:
                await message.answer("⚠️ Все типы фото использованы! Используйте /generate (если есть) или /reset")
                return
            session["photo_queue"].append(message.photo[-1].file_id)

        if len(session["photo_queue"]) == 1:
            await self._process_next_photo(chat_id)

    async def _process_next_photo(self, chat_id: int):
        session = self._get_or_create_session(chat_id)
        with session["lock"]:
            if session["processing"] or not session["photo_queue"]:
                return
            if len(session["photos"]) >= MAX_PHOTOS:
                session["photo_queue"] = []
                await self.bot.send_message(chat_id, f"⚠️ Достигнут лимит в {MAX_PHOTOS} фото! Используйте /reset")
                return
            session["current_file_id"] = session["photo_queue"][0]
            session["processing"] = True
            if not session["remaining_tags"]:
                await self.bot.send_message(chat_id, "⚠️ Все типы фото использованы! Используйте /reset")
                session["photo_queue"] = []
                session["current_file_id"] = None
                session["processing"] = False
                return

        buttons = [[InlineKeyboardButton(text=tag, callback_data=f"tag_{tag}")] for tag in session["remaining_tags"]]
        buttons.append([InlineKeyboardButton(text="⏭ Пропустить", callback_data="tag_skip")])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        try:
            await self.bot.send_photo(chat_id=chat_id, photo=session["current_file_id"], caption="Выберите тип этого фото или пропустите:", reply_markup=markup)
        except Exception as e:
            logger.error("Ошибка отправки фото: %s", e)
            with session["lock"]:
                if session["photo_queue"]:
                    session["photo_queue"].pop(0)
                session["current_file_id"] = None
                session["processing"] = False

    async def _handle_photo_tag(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        session = self._get_or_create_session(chat_id)
        tag = callback.data.replace("tag_", "")
        await self._reset_session_timer(chat_id)
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
                await self._process_next_photo(chat_id)
            else:
                if session["remaining_tags"]:
                    await callback.message.answer(f"Остались невыбранные типы: {', '.join(session['remaining_tags'])}\nОтправьте фото или используйте /reset")
            return

        if not session["current_file_id"]:
            await callback.answer("Фото уже обработано")
            return

        photo_path = os.path.join(self.photos_dir, f"{chat_id}_{tag}.jpg")
        ok = await self._download_photo_with_retry(session["current_file_id"], photo_path)
        if ok:
            width, height = PHOTO_SIZES.get(tag, PHOTO_SIZES["default"])
            async with self.processing_semaphore:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self.executor, self._resize_and_crop_image, photo_path, width, height)
            try:
                await callback.message.delete()
            except:
                pass
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
                await self._process_next_photo(chat_id)
            elif not session["remaining_tags"] or len(session["photos"]) >= MAX_PHOTOS:
                # автоматически генерируем, если всё собрано
                # в generate мы ожидаем Message, так отправим уведомление и предложим команду /generate
                await self.bot.send_message(chat_id, "Фото достаточно. Используйте команду /generate чтобы получить отчет.")
        else:
            await callback.message.answer("❌ Ошибка загрузки фото")
            with session["lock"]:
                if session["photo_queue"]:
                    session["photo_queue"].pop(0)
                session["current_file_id"] = None
                session["processing"] = False
            if session["photo_queue"]:
                await self._process_next_photo(chat_id)

    # --------------- docx generation (как раньше) ----------------
    async def _replace_image_in_docx(self, doc_path: str, image_tag: str, new_image_path: str):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(doc_path, "r") as zip_ref:
                zip_ref.extractall(tmp_dir)
            document_xml_path = os.path.join(tmp_dir, "word", "document.xml")
            relationships_path = os.path.join(tmp_dir, "word", "_rels", "document.xml.rels")
            tree = ET.parse(document_xml_path)
            root = tree.getroot()
            namespaces = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
                          'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
                          'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture',
                          'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'}
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

    async def _generate_docx(self, message: Message):
        chat_id = message.chat.id
        session = self._get_or_create_session(chat_id)
        user_temp_dir = os.path.join(self.temp_dir, str(chat_id))
        os.makedirs(user_temp_dir, exist_ok=True)
        # возьмём ФИО для имени
        # ключ ФИО — первый tpl
        first_tpl = TEXT_FIELDS_ORDER[0][2]
        coordinator_name = session["fields"].get(first_tpl, "report")
        safe_name = re.sub(r'[\\/*?:"<>|]', "", coordinator_name)[:50]
        output_path = os.path.join(user_temp_dir, f"{safe_name}_отчет.docx")
        try:
            shutil.copy(self.template_path, output_path)
            missing_photos = [tag for tag, path in session['photos'].items() if not os.path.exists(path)]
            if missing_photos:
                await message.answer(f"Отсутствуют фото: {', '.join(missing_photos)}")
                return
            for tag, image_path in session['photos'].items():
                await self._replace_image_in_docx(output_path, tag, image_path)
            # замена текста
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
                        for key, val in session['fields'].items():
                            if key in text_elem.text:
                                text_elem.text = text_elem.text.replace(key, val)
                tree.write(document_xml_path, encoding='UTF-8', xml_declaration=True)
                with zipfile.ZipFile(output_path, 'w') as zip_ref:
                    for root, dirs, files in os.walk(tmp_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, tmp_dir)
                            zip_ref.write(file_path, arcname)
            await self.bot.send_document(chat_id, FSInputFile(output_path), caption="Ваш отчет")
            # очистка сессии
            with self.session_lock:
                s = self.user_sessions.pop(chat_id, None)
                if s:
                    for p in s.get('photos', {}).values():
                        try:
                            if os.path.exists(p):
                                os.remove(p)
                        except:
                            pass
            if chat_id in self.session_timers:
                try:
                    self.session_timers[chat_id].cancel()
                    del self.session_timers[chat_id]
                except:
                    pass
        except Exception as e:
            logger.exception("Ошибка генерации: %s", e)
            await message.answer("Ошибка генерации отчета")
        finally:
            try:
                shutil.rmtree(user_temp_dir)
            except:
                pass

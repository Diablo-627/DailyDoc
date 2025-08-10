# pickup_report.py
import os
import re
import shutil
import tempfile
import zipfile
import asyncio
import logging
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple

from PIL import Image
import xml.etree.ElementTree as ET

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

logger = logging.getLogger(__name__)

# configuration
MAX_PHOTOS = 30
MAX_ADDRESSES = 15
PHOTOS_PER_ADDRESS = 2
SESSION_TIMEOUT = 360  # seconds

# default resize sizes (cm)
PHOTO_SIZE_CM = (10.4, 7.4)

# text placeholders in template
TEXT_FIELDS_ORDER = [
    ("DATE", "Введите дату (например 2025-08-10):"),
    ("EQUIPMENT", "Введите задействованную технику (через запятую):"),
    ("GARBAGE_AMOUNT", "Введите количество вывезенного мусора (в числе техники):"),
    ("PARTICIPANTS", "Введите количество участвовавших бойцов:"),
    ("HOURS", "Введите часы техники (включая +1ч на свалку у самосвалов):")
]
# address placeholders look like <<ADDRESS_1>> .. <<ADDRESS_15>>
ADDRESS_PLACEHOLDER = "<<ADDRESS_{}>>"


class PickupReport:
    def __init__(self, bot, template_path: str, photos_dir: str, temp_dir: str):
        self.bot = bot
        self.template_path = template_path
        self.photos_dir = photos_dir
        self.temp_dir = temp_dir
        os.makedirs(self.photos_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)

        self.router = Router()
        self.executor = ThreadPoolExecutor(max_workers=3)

        # per-chat sessions
        self.sessions = {}
        self.sessions_lock = Lock()
        self.session_timers = {}

        # handlers
        self.router.message(F.text)(self._on_text)
        self.router.message(F.photo)(self._on_photo)
        # callbacks for address navigation
        self.router.callback_query(lambda c: c.data in ("addr_more", "addr_next", "addr_skip"))(self._on_addr_nav)

    # ---------------- session helpers ----------------
    def _get_or_create_session(self, chat_id: int):
        with self.sessions_lock:
            if chat_id not in self.sessions:
                self.sessions[chat_id] = {
                    # text fields map: placeholder -> value
                    "fields": {k: "" for k, _ in TEXT_FIELDS_ORDER},
                    "addresses": [],  # list[str]
                    "current_address": 0,  # index into addresses
                    # photos: list of tuples (address_index:int, path:str, tag_num:str)
                    "photos": [],
                    "photo_count": 0,
                    "per_address_count": {},  # address_index -> count accepted or skipped (int)
                    "state": None,  # 'text_fields', 'addresses_input', 'address_photos', None
                    "text_stage": 0,
                    "lock": Lock()
                }
            return self.sessions[chat_id]

    async def _reset_session_timer(self, chat_id: int):
        if chat_id in self.session_timers:
            try:
                self.session_timers[chat_id].cancel()
            except:
                pass
        self.session_timers[chat_id] = asyncio.create_task(self._session_timeout_handler(chat_id))

    async def _session_timeout_handler(self, chat_id: int):
        await asyncio.sleep(SESSION_TIMEOUT)
        with self.sessions_lock:
            session = self.sessions.pop(chat_id, None)
        if session:
            for _, p, _ in session["photos"]:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except:
                    pass
        try:
            await self.bot.send_message(chat_id, "⏳ Сессия завершена по таймауту. Начните заново через меню.")
        except:
            pass

    # ---------------- public start ----------------
    async def start_for_user(self, chat_id: int):
        """
        Автостарт сценария — вызывается из main_bot.
        Запускает последовательный ввод текстовых полей, затем адресов, затем фото.
        """
        session = self._get_or_create_session(chat_id)
        with session["lock"]:
            session["fields"] = {k: "" for k, _ in TEXT_FIELDS_ORDER}
            session["addresses"] = []
            session["current_address"] = 0
            session["photos"] = []
            session["photo_count"] = 0
            session["per_address_count"] = {}
            session["state"] = "text_fields"
            session["text_stage"] = 0

        await self._reset_session_timer(chat_id)
        first_prompt = TEXT_FIELDS_ORDER[0][1]
        await self.bot.send_message(chat_id, f"Начинаем 'Вывозной' отчёт.\nСначала заполните данные:\n{first_prompt}")

    # ---------------- text flow ----------------
    async def _on_text(self, message: Message):
        chat_id = message.chat.id
        text = (message.text or "").strip()
        session = self._get_or_create_session(chat_id)
        await self._reset_session_timer(chat_id)

        # ignore if no active flow
        if session["state"] is None:
            return

        # handle text fields stage
        if session["state"] == "text_fields":
            idx = session["text_stage"]
            if idx < len(TEXT_FIELDS_ORDER):
                key, prompt = TEXT_FIELDS_ORDER[idx]
                session["fields"][key] = text
                idx += 1
                session["text_stage"] = idx
                if idx < len(TEXT_FIELDS_ORDER):
                    await message.answer(TEXT_FIELDS_ORDER[idx][1])
                    return
                # done text fields -> ask for addresses
                session["state"] = "addresses_input"
                await message.answer("Отлично. Теперь отправьте список адресов — по одному на строку (до 15). Когда закончите, отправьте слово 'Готово'.")
                return
            else:
                # shouldn't happen, but set to addresses_input
                session["state"] = "addresses_input"
                await message.answer("Перейдите к вводу адресов. Отправьте их по одной строке или множественную строку. Затем отправьте 'Готово'.")
                return

        # handle addresses input
        if session["state"] == "addresses_input":
            if text.lower() in ("готово", "done"):
                if not session["addresses"]:
                    await message.answer("Похоже адреса не указаны — отправьте их по одной на строку.")
                    return
                # proceed to photos for first address
                session["state"] = "address_photos"
                session["current_address"] = 0
                # init per_address_count
                for i in range(len(session["addresses"])):
                    session["per_address_count"].setdefault(i, 0)
                await self._ask_photos_for_current(chat_id)
                return
            # parse lines and append
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if not lines:
                await message.answer("Не распознал адрес. Отправьте адрес(а) по одной строке.")
                return
            with session["lock"]:
                for ln in lines:
                    if len(session["addresses"]) < MAX_ADDRESSES:
                        session["addresses"].append(ln)
                    else:
                        logger.warning("User %s tried to add more than %s addresses", chat_id, MAX_ADDRESSES)
            await message.answer(f"Добавлено адресов: {len(session['addresses'])}. Когда закончите ввод, отправьте 'Готово'.")
            return

        # handle free text during photos stage: we ignore or provide hint
        if session["state"] == "address_photos":
            await message.answer("Ожидаю фотографии для текущего адреса или нажмите 'Следующий адрес' / 'Пропустить адрес'.")
            return

    # ---------------- photo handling ----------------
    async def _on_photo(self, message: Message):
        chat_id = message.chat.id
        session = self._get_or_create_session(chat_id)
        await self._reset_session_timer(chat_id)

        if session["state"] != "address_photos":
            await message.answer("Сначала заполните текстовые поля и адреса. Затем отправляйте фото.")
            return

        addr_idx = session["current_address"]
        if addr_idx >= len(session["addresses"]):
            await message.answer("Все адреса обработаны. Если хотите добавить — начните заново.")
            return

        # Check totals
        if session["photo_count"] >= MAX_PHOTOS:
            await message.answer(f"Достигнут лимит {MAX_PHOTOS} фото — формирую отчёт...")
            await self._generate_report_and_cleanup(chat_id)
            return

        # Check per-address count limit
        cur_count = session["per_address_count"].get(addr_idx, 0)
        if cur_count >= PHOTOS_PER_ADDRESS:
            await message.answer("Для этого адреса уже получено необходимое количество фото. Нажмите 'Следующий адрес' или 'Пропустить адрес'.")
            return

        # download photo
        file_id = message.photo[-1].file_id
        user_dir = os.path.join(self.photos_dir, str(chat_id))
        os.makedirs(user_dir, exist_ok=True)
        next_num = session["photo_count"] + 1
        fname = f"{chat_id}_photo_{next_num}.jpg"
        dest = os.path.join(user_dir, fname)
        try:
            file = await self.bot.get_file(file_id)
            await self.bot.download_file(file.file_path, dest)
        except Exception as e:
            logger.exception("download error: %s", e)
            await message.answer("Ошибка загрузки фото — попробуйте ещё раз.")
            return

        # resize
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._resize_and_crop_image, dest, PHOTO_SIZE_CM[0], PHOTO_SIZE_CM[1])

        # store
        with session["lock"]:
            session["photos"].append((addr_idx, dest, str(next_num)))
            session["photo_count"] += 1
            session["per_address_count"][addr_idx] = session["per_address_count"].get(addr_idx, 0) + 1

        # build keyboard: add more / next / skip
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Добавить ещё фото к этому адресу", callback_data="addr_more")],
            [InlineKeyboardButton(text="Следующий адрес", callback_data="addr_next"),
             InlineKeyboardButton(text="Пропустить адрес", callback_data="addr_skip")]
        ])
        # reply with confirmation and buttons (we will delete this message on callback)
        try:
            await message.reply(f"Фото принято для адреса #{addr_idx+1}: {session['addresses'][addr_idx]}", reply_markup=kb)
        except:
            pass

        # if reached totals auto-generate
        if session["photo_count"] >= MAX_PHOTOS:
            await self.bot.send_message(chat_id, f"Достигнут лимит {MAX_PHOTOS} фото — формирую отчёт...")
            await self._generate_report_and_cleanup(chat_id)
            return

    # ---------------- callbacks for navigation ----------------
    async def _on_addr_nav(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        action = callback.data  # addr_more, addr_next, addr_skip
        session = self._get_or_create_session(chat_id)
        await self._reset_session_timer(chat_id)

        # delete the confirmation message with buttons
        try:
            await callback.message.delete()
        except:
            pass

        if action == "addr_more":
            await callback.answer("Ожидаю следующее фото для этого адреса.")
            return

        if action in ("addr_next", "addr_skip"):
            # move to next address
            with session["lock"]:
                session["current_address"] += 1
                cur = session["current_address"]

            if cur >= len(session["addresses"]):
                # finished
                session["state"] = None
                await self.bot.send_message(chat_id, "Все адреса обработаны — формирую отчёт...")
                await self._generate_report_and_cleanup(chat_id)
                return
            # else ask for photos for new address
            await self._ask_photos_for_current(chat_id)
            await callback.answer("Перешёл к следующему адресу.")
            return

    async def _ask_photos_for_current(self, chat_id: int):
        session = self._get_or_create_session(chat_id)
        idx = session["current_address"]
        addr = session["addresses"][idx] if idx < len(session["addresses"]) else ""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить адрес", callback_data="addr_skip"),
             InlineKeyboardButton(text="Готово (перейти дальше)", callback_data="addr_next")]
        ])
        await self.bot.send_message(chat_id, f"Адрес {idx+1}/{len(session['addresses'])}: {addr}\nОтправляйте фото (до {PHOTOS_PER_ADDRESS}).", reply_markup=kb)

    # ---------------- report generation ----------------
    async def _generate_report_and_cleanup(self, chat_id: int):
        """
        Формируем docx:
        - заменяем текстовые плейсхолдеры <<DATE>>, <<ADDRESSES>> и т.д.;
        - заменяем картинки по descr '1'..'30' в document.xml.rels -> media/..;
        - удаляем неиспользуемые ADDRESS_i и попытка удалить ненужные picture blocks.
        """
        session = self._get_or_create_session(chat_id)
        with session["lock"]:
            addresses = session["addresses"].copy()
            photos = session["photos"].copy()  # list of (addr_idx, path, tag_str)
            fields = session["fields"].copy()

        user_tmp = os.path.join(self.temp_dir, str(chat_id))
        os.makedirs(user_tmp, exist_ok=True)
        out_doc = os.path.join(user_tmp, f"pickup_{chat_id}.docx")
        try:
            shutil.copy(self.template_path, out_doc)
        except Exception as e:
            logger.exception("copy template error: %s", e)
            await self.bot.send_message(chat_id, "Ошибка подготовки шаблона.")
            return

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                with zipfile.ZipFile(out_doc, 'r') as z:
                    z.extractall(tmpdir)

                doc_xml = os.path.join(tmpdir, 'word', 'document.xml')
                tree = ET.parse(doc_xml)
                root = tree.getroot()
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

                # Replace general text fields
                for key, _ in TEXT_FIELDS_ORDER:
                    placeholder = f"<<{key}>>"
                    repl = fields.get(key, "")
                    for t in root.findall('.//w:t', ns):
                        if t.text and placeholder in t.text:
                            t.text = t.text.replace(placeholder, repl)

                # Replace address placeholders <<ADDRESS_i>>
                for i in range(1, MAX_ADDRESSES + 1):
                    placeholder = ADDRESS_PLACEHOLDER.format(i)
                    repl = addresses[i-1] if i-1 < len(addresses) else ""
                    for t in root.findall('.//w:t', ns):
                        if t.text and placeholder in t.text:
                            t.text = t.text.replace(placeholder, repl)

                # Write back document.xml
                tree.write(doc_xml, encoding='UTF-8', xml_declaration=True)

                # Replace images: look for <pic:pic> with cNvPr/@descr == tag
                # Map used tags
                used_tags = {tag for _, _, tag in photos}

                # Replace used photos
                for _, path, tag in photos:
                    if not os.path.exists(path):
                        continue
                    await self._replace_image_in_tmpdoc(tmpdir, tag, path)

                # Remove unused address placeholders already replaced with empty strings.
                # Now attempt to remove picture blocks for unused numeric tags
                for num in range(1, MAX_PHOTOS + 1):
                    s = str(num)
                    if s not in used_tags:
                        self._remove_pic_by_descr(tmpdir, s)

                # rezip to out_doc
                with zipfile.ZipFile(out_doc, 'w') as z:
                    for rootdir, _, files in os.walk(tmpdir):
                        for f in files:
                            full = os.path.join(rootdir, f)
                            arc = os.path.relpath(full, tmpdir)
                            z.write(full, arc)

        except Exception as e:
            logger.exception("Error generating docx: %s", e)
            await self.bot.send_message(chat_id, "Ошибка формирования отчёта.")
            try:
                shutil.rmtree(user_tmp)
            except:
                pass
            return

        # send file
        try:
            await self.bot.send_document(chat_id, FSInputFile(out_doc), caption="Отчёт по вывозу готов")
        except Exception as e:
            logger.exception("send doc error: %s", e)
            await self.bot.send_message(chat_id, "Ошибка отправки отчёта.")

        # cleanup user files and session
        try:
            if chat_id in self.sessions:
                s = None
                with self.sessions_lock:
                    s = self.sessions.pop(chat_id, None)
                if s:
                    for _, p, _ in s["photos"]:
                        try:
                            if os.path.exists(p):
                                os.remove(p)
                        except:
                            pass
            if os.path.exists(user_tmp):
                shutil.rmtree(user_tmp)
        except Exception:
            pass

        # cancel timer
        if chat_id in self.session_timers:
            try:
                self.session_timers[chat_id].cancel()
            except:
                pass
            try:
                del self.session_timers[chat_id]
            except:
                pass

    # ---------------- xml/image helpers ----------------
    async def _replace_image_in_tmpdoc(self, tmpdir: str, image_tag: str, new_image_path: str) -> bool:
        """
        Find <pic:pic> elements in document.xml with pic:cNvPr/@descr == image_tag
        and replace the referenced target file in word/...
        """
        document_xml = os.path.join(tmpdir, 'word', 'document.xml')
        rels_xml = os.path.join(tmpdir, 'word', '_rels', 'document.xml.rels')
        try:
            tree = ET.parse(document_xml)
            root = tree.getroot()
        except Exception:
            return False

        namespaces = {
            'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
            'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
            'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture',
            'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
        }
        for pfx, uri in namespaces.items():
            ET.register_namespace(pfx, uri)

        # parse rels
        try:
            rel_tree = ET.parse(rels_xml)
            rel_root = rel_tree.getroot()
        except Exception:
            rel_root = None

        found = False
        for pic in root.findall('.//pic:pic', namespaces):
            nv = pic.find('pic:nvPicPr/pic:cNvPr', namespaces)
            if nv is not None and nv.get('descr') == image_tag:
                blip = pic.find('.//a:blip', namespaces)
                if blip is None:
                    continue
                rId = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                if rel_root is None:
                    continue
                for rel in rel_root.findall('.//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship'):
                    if rel.get('Id') == rId:
                        target = rel.get('Target')  # typically 'media/imageN.png'
                        dest = os.path.join(tmpdir, 'word', target)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        try:
                            shutil.copy(new_image_path, dest)
                            found = True
                        except Exception:
                            logger.exception("copy new image failed")
                        break
        return found

    def _remove_pic_by_descr(self, tmpdir: str, descr_value: str):
        """
        Remove w:p paragraphs containing pic:pic nodes whose cNvPr/@descr == descr_value.
        """
        document_xml = os.path.join(tmpdir, 'word', 'document.xml')
        try:
            tree = ET.parse(document_xml)
            root = tree.getroot()
            ns = {
                'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
                'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
                'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture',
                'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
            }
            changed = False
            # iterate over all paragraphs and remove those that contain the picture with matching descr
            for wp in root.findall('.//w:p', ns):
                has_match = False
                for pic in wp.findall('.//pic:pic', ns):
                    nv = pic.find('pic:nvPicPr/pic:cNvPr', ns)
                    if nv is not None and nv.get('descr') == descr_value:
                        has_match = True
                        break
                if has_match:
                    parent = wp.getparent() if hasattr(wp, 'getparent') else None
                    # ElementTree doesn't guarantee getparent; safe removal: find parent by traversal
                    if parent is None:
                        # search for parent by walking from root
                        self._remove_element_by_search(root, wp)
                    else:
                        try:
                            parent.remove(wp)
                        except Exception:
                            self._remove_element_by_search(root, wp)
                    changed = True
            if changed:
                tree.write(document_xml, encoding='UTF-8', xml_declaration=True)
        except Exception:
            # Non-fatal: skip
            pass

    def _remove_element_by_search(self, root: ET.Element, target: ET.Element):
        """
        Helper to remove target element by searching parents (fallback).
        """
        for parent in root.findall('.//*'):
            for child in list(parent):
                if child is target:
                    try:
                        parent.remove(child)
                        return True
                    except:
                        pass
        return False

    # ---------------- image resize helper ----------------
    def _resize_and_crop_image(self, image_path: str, target_w_cm: float, target_h_cm: float):
        CM_TO_PX = 37.8
        target_w = int(target_w_cm * CM_TO_PX)
        target_h = int(target_h_cm * CM_TO_PX)
        try:
            with Image.open(image_path) as img:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                w, h = img.size
                scale = max(target_w / w, target_h / h)
                new_w = int(w * scale)
                new_h = int(h * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                left = (new_w - target_w) // 2
                top = (new_h - target_h) // 2
                img = img.crop((left, top, left + target_w, top + target_h))
                img.save(image_path, format='JPEG', quality=95, subsampling=0)
        except Exception:
            logger.exception("resize error for %s", image_path)
            return

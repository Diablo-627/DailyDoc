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
from typing import List

from PIL import Image
import xml.etree.ElementTree as ET

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

logger = logging.getLogger(__name__)

# --- Настройки ---
MAX_PHOTOS = 30        # макс фото в шаблоне
MAX_ADDRESSES = 15     # в шаблоне адресов 1..15
SESSION_TIMEOUT = 360  # сек

# Размеры картинок (в см) для ресайза — по умолчанию можно оставить 10.4 x 7.4, если нужно менять — поправь
PHOTO_SIZES_CM = {
    "default": (10.4, 7.4)
}

# ТЕКСТОВЫЕ плейсхолдеры в шаблоне (смотрел файл — там ADDRESS_1..ADDRESS_15, DATE, ADDRESSES и т.д.)
ADDRESS_TPL = "ADDRESS_{}"  # ADDRESS_1 ... ADDRESS_15
COMMON_TPLS = ["DATE", "ADDRESSES", "GARBAGE_AMOUNT", "PARTICIPANTS", "EQUIPMENT", "HOURS"]


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
        # text handler (addresses input and possibly other text during flow)
        self.router.message(F.text)(self._handle_text)
        # photos
        self.router.message(F.photo)(self._handle_photo)
        # callback buttons
        self.router.callback_query(F.data.in_({"addr_next", "addr_skip", "addr_more"}))(self._handle_address_navigation)
        self.router.callback_query(F.data.startswith("photo_skip_"))(self._handle_photo_skip)
        # for safety also allow generic tag removal callbacks (not used here)
    
    # ---------------- session helpers ----------------
    def _get_or_create_session(self, chat_id: int):
        with self.sessions_lock:
            if chat_id not in self.sessions:
                self.sessions[chat_id] = {
                    "addresses": [],              # list[str]
                    "current_address": 0,         # index
                    "photos": [],                 # list of file paths in order [(address_index, path), ...]
                    "photo_count": 0,
                    "state": None,                # "addresses_input", "address_photos", None (finished)
                    "lock": Lock()
                }
            return self.sessions[chat_id]

    async def _reset_session_timer(self, chat_id: int):
        # cancel old
        if chat_id in self.session_timers:
            try:
                self.session_timers[chat_id].cancel()
            except:
                pass
        # schedule new
        self.session_timers[chat_id] = asyncio.create_task(self._session_timeout_handler(chat_id))

    async def _session_timeout_handler(self, chat_id: int):
        await asyncio.sleep(SESSION_TIMEOUT)
        with self.sessions_lock:
            session = self.sessions.pop(chat_id, None)
        if session:
            # cleanup files
            for _, p in session["photos"]:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except:
                    pass
        try:
            await self.bot.send_message(chat_id, "⏳ Сессия завершена по таймауту. Используйте /start чтобы начать заново.")
        except:
            pass

    # ---------------- public start ----------------
    async def start_for_user(self, chat_id: int):
        """
        Автостарт сценария: вызывается из main_bot при выборе 'Вывозной'.
        Запрашиваем список адресов (несколько строк, Enter между адресами).
        """
        session = self._get_or_create_session(chat_id)
        with session["lock"]:
            session["addresses"] = []
            session["current_address"] = 0
            session["photos"] = []
            session["photo_count"] = 0
            session["state"] = "addresses_input"

        await self._reset_session_timer(chat_id)
        await self.bot.send_message(chat_id,
                                    "Начинаем 'Вывозной' отчет.\n"
                                    "Отправьте список адресов — по одному на строку (макс 15). Пример:\n"
                                    "Адрес 1\nАдрес 2\nАдрес 3\n\n"
                                    "После отправки адресов нажмите кнопку 'Готово' (введите слово Готово) или просто отправьте слово \"Готово\".")

    # ---------------- text handling ----------------
    async def _handle_text(self, message: Message):
        chat_id = message.chat.id
        text = (message.text or "").strip()
        session = self._get_or_create_session(chat_id)
        await self._reset_session_timer(chat_id)

        # Only process when in an active flow
        if session["state"] is None:
            return  # ignore free text

        # If we are waiting for addresses_input
        if session["state"] == "addresses_input":
            # If user sends the word "Готово" (case-insensitive) — proceed
            if text.lower() == "готово" or text.lower() == "done":
                if not session["addresses"]:
                    await message.answer("Похоже, вы не прислали адресов. Отправьте их, по одному на строку.")
                    return
                # start photo collection for first address
                session["state"] = "address_photos"
                session["current_address"] = 0
                await self._ask_for_photos_for_current_address(chat_id)
                return

            # Otherwise parse addresses (multi-line). We'll accept either a multi-line message
            # or repeated messages: here we append lines to addresses.
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if not lines:
                await message.answer("Не поняла адрес. Отправьте адрес(а) по одному на строку или сообщение с несколькими строками.")
                return

            with session["lock"]:
                for ln in lines:
                    if len(session["addresses"]) < MAX_ADDRESSES:
                        session["addresses"].append(ln)
                    else:
                        # ignore extra addresses beyond template capacity
                        logger.warning("User %s tried to add more than %s addresses", chat_id, MAX_ADDRESSES)
                count = len(session["addresses"])

            await message.answer(f"Принято {count} адрес(ов). Когда закончите ввод адресов, отправьте «Готово».")
            return

        # If we are in address_photos and user wrote something that looks like "Пропустить адрес" or "Следующий"
        if session["state"] == "address_photos":
            if text.lower() in ("пропустить", "пропустить адрес", "следующий", "далее"):
                await self._goto_next_address(chat_id, message)
                return
            # Otherwise ignore free text (they must send photos or buttons)
            await message.answer("На этом шаге ожидаются фотографии для текущего адреса или нажмите 'Следующий адрес'.")
            return

    # ---------------- photo handling ----------------
    async def _handle_photo(self, message: Message):
        """
        Обрабатываем фото; если мы в режиме address_photos — сохраняем его и связываем с текущим адресом.
        После сохранения отправляем сообщение с кнопками: Добавить ещё / Следующий адрес / Пропустить адрес.
        """
        chat_id = message.chat.id
        session = self._get_or_create_session(chat_id)
        await self._reset_session_timer(chat_id)

        if session["state"] != "address_photos":
            await message.answer("Сначала отправьте список адресов (по одному на строку), затем введите «Готово».")
            return

        # limit total photos
        if session["photo_count"] >= MAX_PHOTOS:
            await message.answer(f"⚠️ Достигнут лимит в {MAX_PHOTOS} фото — лишние фото игнорируются. Начинаю формирование отчёта...")
            # generate
            await self._generate_report_and_cleanup(chat_id, message)
            return

        addr_idx = session["current_address"]
        if addr_idx >= len(session["addresses"]):
            # No more addresses to assign to
            await message.answer("Все адреса обработаны. Если хотите добавить фото к предыдущему адресу — нажмите 'Назад' (если будет поддержка) или начните заново.")
            return

        file_id = message.photo[-1].file_id
        # download to temp file
        user_dir = os.path.join(self.photos_dir, str(chat_id))
        os.makedirs(user_dir, exist_ok=True)
        # choose next numeric tag from 1..MAX_PHOTOS that is free: basically photo_count+1 -> tag number
        next_num = session["photo_count"] + 1
        filename = f"{chat_id}_photo_{next_num}.jpg"
        dest_path = os.path.join(user_dir, filename)

        # download with retries (simple)
        try:
            file = await self.bot.get_file(file_id)
            await self.bot.download_file(file.file_path, dest_path)
        except Exception as e:
            logger.exception("Ошибка загрузки фото: %s", e)
            await message.answer("Ошибка загрузки фото, попробуйте ещё раз.")
            return

        # resize/crop to approximate size (we'll use default cm sizes)
        cm_w, cm_h = PHOTO_SIZES_CM.get("default")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._resize_and_crop_image, dest_path, cm_w, cm_h)

        # store photo entry as tuple (address_index, path, placeholder_number)
        with session["lock"]:
            session["photos"].append((addr_idx, dest_path, str(next_num)))  # store number as string tag
            session["photo_count"] += 1

        # Build inline keyboard: Add more / Next address / Skip address
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Добавить ещё фото к этому адресу", callback_data="addr_more")],
            [InlineKeyboardButton(text="Следующий адрес", callback_data="addr_next"),
             InlineKeyboardButton(text="Пропустить адрес", callback_data="addr_skip")]
        ])

        # Send a small confirmation message with buttons (delete later when chosen)
        sent = await message.reply("Фото принято для адреса №{} — {}.\nВыберите действие:".format(addr_idx + 1, session["addresses"][addr_idx]),
                                   reply_markup=kb)
        # store the message id to be possibly removed after callback handling (we'll not keep it in session to simplify)
        # If we've reached MAX_PHOTOS after storing, auto-generate
        if session["photo_count"] >= MAX_PHOTOS:
            await self.bot.send_message(chat_id, f"Достигнут лимит {MAX_PHOTOS} фото — формирую отчёт...")
            await self._generate_report_and_cleanup(chat_id, message)
            return

    # ---------------- callbacks for address navigation ----------------
    async def _handle_address_navigation(self, callback: CallbackQuery):
        chat_id = callback.message.chat.id
        data = callback.data  # 'addr_more' | 'addr_next' | 'addr_skip'
        session = self._get_or_create_session(chat_id)
        await self._reset_session_timer(chat_id)

        # delete the confirmation message with buttons to avoid chat clutter
        try:
            await callback.message.delete()
        except Exception:
            pass

        if data == "addr_more":
            # Stay on same address, wait for next photo
            await callback.answer("Ожидаю следующее фото для текущего адреса.")
            return

        if data == "addr_next":
            # Move to next address
            await self._goto_next_address(chat_id, callback.message)
            return

        if data == "addr_skip":
            # skip current address (no more photos) and move next
            # (no changes to photos list)
            await callback.answer("Адрес пропущен.")
            await self._goto_next_address(chat_id, callback.message)
            return

    async def _handle_photo_skip(self, callback: CallbackQuery):
        """
        Если понадобиться отдельный callback для пропуска конкретного фото - здесь можно реализовать.
        (в текущем интерфейсе мы используем addr_skip)
        """
        try:
            await callback.message.delete()
        except:
            pass
        await callback.answer("Фото пропущено.")

    async def _goto_next_address(self, chat_id: int, message_context):
        session = self._get_or_create_session(chat_id)
        with session["lock"]:
            session["current_address"] += 1
            idx = session["current_address"]

        if idx >= len(session["addresses"]):
            # done with all addresses -> generate
            session["state"] = None
            await self.bot.send_message(chat_id, "Все адреса обработаны. Формирую отчёт...")
            await self._generate_report_and_cleanup(chat_id, message_context)
            return

        # else ask for photos for the new address
        await self._ask_for_photos_for_current_address(chat_id)

    async def _ask_for_photos_for_current_address(self, chat_id: int):
        session = self._get_or_create_session(chat_id)
        idx = session["current_address"]
        addr_text = session["addresses"][idx] if idx < len(session["addresses"]) else "—"
        # small keyboard: пользователь может сразу пропустить адрес, либо отправить фото
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить адрес", callback_data="addr_skip"),
             InlineKeyboardButton(text="Готово (перейти дальше)", callback_data="addr_next")]
        ])
        await self.bot.send_message(chat_id, f"Адрес {idx+1}/{len(session['addresses'])}: {addr_text}\nОтправляйте фото для этого адреса (или нажмите Пропустить адрес).",
                                    reply_markup=kb)

    # ---------------- report generation ----------------
    async def _generate_report_and_cleanup(self, chat_id: int, ctx_message):
        """
        Формируем docx: 1) заполняем текстовые placeholders ADDRESS_1..ADDRESS_15 и общие поля;
        2) заменяем картинки по тегам '1'..'30' (альт-текст в шаблоне);
        3) удаляем неиспользуемые текстовые плейсхолдеры и картинные плейсхолдеры (если остались).
        """
        session = self._get_or_create_session(chat_id)
        with session["lock"]:
            addresses: List[str] = session["addresses"].copy()
            photos = session["photos"].copy()  # list of (addr_idx, path, num_tag)
            # note: photos already have numeric tags starting from '1' sequentially by insertion

        # prepare temp dir
        user_temp_dir = os.path.join(self.temp_dir, str(chat_id))
        os.makedirs(user_temp_dir, exist_ok=True)
        out_doc = os.path.join(user_temp_dir, f"pickup_report_{chat_id}.docx")

        try:
            # copy template
            shutil.copy(self.template_path, out_doc)
        except Exception as e:
            logger.exception("Ошибка копирования шаблона: %s", e)
            await self.bot.send_message(chat_id, "Ошибка при подготовке шаблона отчёта.")
            return

        # 1) replace text placeholders ADDRESS_i and common placeholders
        # We'll do this by unzipping docx and editing document.xml
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                with zipfile.ZipFile(out_doc, 'r') as z:
                    z.extractall(tmpdir)

                doc_xml = os.path.join(tmpdir, 'word', 'document.xml')
                tree = ET.parse(doc_xml)
                root = tree.getroot()
                namespaces = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                # Replace ADDRESS_i
                for i in range(1, MAX_ADDRESSES + 1):
                    key = f"<<{ADDRESS_TPL.format(i)}>>"  # template has <<ADDRESS_1>> etc in your docx
                    replacement = addresses[i-1] if i-1 < len(addresses) else ""
                    # iterate over text nodes
                    for t in root.findall('.//w:t', namespaces):
                        if t.text and key in t.text:
                            t.text = t.text.replace(key, replacement)
                # Common placeholders (if present)
                for key in COMMON_TPLS:
                    placeholder = f"<<{key}>>"
                    # empty by default; user could be asked later to provide them (not implemented now)
                    for t in root.findall('.//w:t', namespaces):
                        if t.text and placeholder in t.text:
                            # leave empty or keep as is — here we clear
                            t.text = t.text.replace(placeholder, "")

                tree.write(doc_xml, encoding='UTF-8', xml_declaration=True)

                # 2) Replace images by numeric tags: photos list contains items with tag strings '1','2',...
                # We will copy files into the docx zip by matching relationship ids for pictures whose cNvPr descr equals tag.
                # For simplicity we will implement a replace for each photo tag.
                # If there are unused numeric tags in 1..MAX_PHOTOS, we'll remove corresponding <pic> nodes.

                # First, replace provided photos
                for _, path, tag in photos:
                    if not os.path.exists(path):
                        continue
                    await self._replace_image_in_docx_file(tmpdir, tag, path)

                # Then remove unused picture placeholders (numbers that were not used)
                used_tags = {tag for _, _, tag in photos}
                for num in range(1, MAX_PHOTOS + 1):
                    s = str(num)
                    if s not in used_tags:
                        # attempt to remove picture nodes with descr == s
                        self._remove_pic_by_descr(tmpdir, s)

                # write back zip
                with zipfile.ZipFile(out_doc, 'w') as z:
                    for rootdir, _, files in os.walk(tmpdir):
                        for f in files:
                            full = os.path.join(rootdir, f)
                            arc = os.path.relpath(full, tmpdir)
                            z.write(full, arc)

        except Exception as e:
            logger.exception("Ошибка при подготовке document.xml: %s", e)
            await self.bot.send_message(chat_id, "Ошибка при формировании отчёта.")
            # cleanup
            try:
                shutil.rmtree(user_temp_dir)
            except:
                pass
            return

        # send document
        try:
            await self.bot.send_document(chat_id, FSInputFile(out_doc), caption="Отчёт по вывозу готов")
        except Exception as e:
            logger.exception("Ошибка отправки документа: %s", e)
            await self.bot.send_message(chat_id, "Ошибка отправки отчёта.")
        finally:
            # cleanup session files and session
            with self.sessions_lock:
                s = self.sessions.pop(chat_id, None)
            if s:
                for _, p, _ in s["photos"]:
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except:
                        pass
            # remove temp user dir
            try:
                if os.path.exists(user_temp_dir):
                    shutil.rmtree(user_temp_dir)
            except:
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

    # ---------------- helper XML/image utils ----------------
    async def _replace_image_in_docx_file(self, tmpdir: str, image_tag: str, new_image_path: str):
        """
        В tmpdir содержится распакованный docx.
        Ищем <pic:pic> элементы, у которых pic:cNvPr/@descr == image_tag, и заменяем связанный файл (по relationship).
        """
        document_xml_path = os.path.join(tmpdir, 'word', 'document.xml')
        relationships_path = os.path.join(tmpdir, 'word', '_rels', 'document.xml.rels')
        try:
            tree = ET.parse(document_xml_path)
            root = tree.getroot()
        except Exception:
            return
        namespaces = {
            'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
            'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
            'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture',
            'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
        }
        for prefix, uri in namespaces.items():
            ET.register_namespace(prefix, uri)

        # parse rels
        try:
            rel_tree = ET.parse(relationships_path)
            rel_root = rel_tree.getroot()
        except Exception:
            rel_root = None

        found_any = False
        for pic in root.findall('.//pic:pic', namespaces):
            nv_pr = pic.find('pic:nvPicPr/pic:cNvPr', namespaces)
            if nv_pr is not None and nv_pr.get('descr') == image_tag:
                blip = pic.find('.//a:blip', namespaces)
                if blip is None:
                    continue
                r_id = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                if rel_root is None:
                    continue
                for rel in rel_root.findall('.//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship'):
                    if rel.get('Id') == r_id:
                        target = rel.get('Target')  # like media/image1.png
                        dest = os.path.join(tmpdir, 'word', target)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.copy(new_image_path, dest)
                        found_any = True
                        break
        # if not found — nothing to do
        return found_any

    def _remove_pic_by_descr(self, tmpdir: str, descr_value: str):
        """
        Удаляем pic:pic nodes в документе с cNvPr/@descr == descr_value.
        (Это не всегда чисто — но для удаления неиспользуемых картинок в шаблоне подойдёт.)
        """
        try:
            document_xml_path = os.path.join(tmpdir, 'word', 'document.xml')
            tree = ET.parse(document_xml_path)
            root = tree.getroot()
            namespaces = {
                'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
                'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
                'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture',
                'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
            }
            # find all pic elements and remove their parent <w:p> if desired
            removed = False
            for pic in root.findall('.//pic:pic', namespaces):
                nv_pr = pic.find('pic:nvPicPr/pic:cNvPr', namespaces)
                if nv_pr is not None and nv_pr.get('descr') == descr_value:
                    # climb up to paragraph and remove it
                    parent = pic
                    # climb up to root to find the w:p ancestor
                    for _ in range(6):
                        parent = parent.getparent() if hasattr(parent, 'getparent') else None
                        if parent is None:
                            break
                    # simpler approach: set the text around to empty by locating text nodes that reference the picture
                    # but safest is to remove the pic node itself
                    # remove pic node
                    parent_of_pic = pic.getparent() if hasattr(pic, 'getparent') else None
                    if parent_of_pic is not None:
                        try:
                            parent_of_pic.remove(pic)
                            removed = True
                        except Exception:
                            pass
            # write back if changed
            if removed:
                tree.write(document_xml_path, encoding='UTF-8', xml_declaration=True)
        except Exception:
            # non-fatal
            pass

    # ---------------- image helpers ----------------
    def _resize_and_crop_image(self, image_path: str, target_w_cm: float, target_h_cm: float):
        CM_TO_PX = 37.8
        target_w = int(target_w_cm * CM_TO_PX)
        target_h = int(target_h_cm * CM_TO_PX)
        try:
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
        except Exception as e:
            logger.exception("resize error: %s", e)
            return


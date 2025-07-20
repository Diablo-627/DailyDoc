import os
import re
import asyncio
import logging
import shutil
import tempfile
from aiogram import Bot, types, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from docx import Document
from docx.shared import Cm
from docx.oxml import parse_xml
from docx.enum.section import WD_SECTION
from PIL import Image
import xml.etree.ElementTree as ET
import zipfile

logger = logging.getLogger(__name__)
garbage_router = Router()

# Константы
PHOTO_WIDTH = 13.33  # см
PHOTO_HEIGHT = 7.5   # см
CM_TO_PX = 37.8      # 1 см ≈ 37.8 пикселей
TEMPLATE_NAME = "template21.docx"  # Название файла шаблона
MAX_ADDRESSES = 15    # Максимальное количество адресов
PHOTOS_PER_ADDRESS = 2 # Фото на каждый адрес

class GarbageReportState(StatesGroup):
    DATE = State()
    ADDRESSES = State()
    EQUIPMENT = State()
    GARBAGE_AMOUNT = State()
    PARTICIPANTS = State()
    HOURS = State()
    INPUT_PHOTOS = State()
    PHOTO_ASSIGNMENT = State()

async def start_garbage_report(message: types.Message, state: FSMContext):
    """Запуск сценария отчета по вывозу мусора"""
    await state.clear()
    await state.set_state(GarbageReportState.DATE)
    await message.answer(
        "📅 Введите дату вывоза мусора:",
        reply_markup=ReplyKeyboardRemove()
    )

# [Остальные обработчики состояний остаются без изменений до process_hours]

@garbage_router.message(GarbageReportState.HOURS)
async def process_hours(message: types.Message, state: FSMContext):
    """Обработка информации о часах работы"""
    await state.update_data(hours=message.text)
    data = await state.get_data()
    
    total_photos = len(data['addresses']) * PHOTOS_PER_ADDRESS
    await state.set_state(GarbageReportState.INPUT_PHOTOS)
    await message.answer(
        f"📸 Теперь загрузите {total_photos} фото (по {PHOTOS_PER_ADDRESS} на каждый адрес).\n"
        f"Порядок адресов:\n" + "\n".join(
            f"{i+1}. {addr}" for i, addr in enumerate(data['addresses'])
        )
    )

@garbage_router.message(GarbageReportState.INPUT_PHOTOS, F.photo)
async def process_photo_upload(message: types.Message, state: FSMContext):
    """Обработка загруженных фото с поддержкой альбомов"""
    data = await state.get_data()
    bot = Bot.get_current()
    
    # Получаем самый качественный вариант каждого фото
    photo_file_ids = [photo.file_id for photo in message.photo]
    
    if len(photo_file_ids) > 1:
        # Если это альбом, сохраняем фото в буфер
        await state.update_data(photo_buffer=photo_file_ids)
        await message.answer("📸 Получено несколько фото. Начнем обработку...")
        await process_next_photo_from_buffer(message, state, bot)
    else:
        # Одно фото
        await state.update_data(current_photo=photo_file_ids[0])
        await ask_photo_assignment(message, state, bot)

async def process_next_photo_from_buffer(message: Message, state: FSMContext, bot: Bot):
    """Обработка следующего фото из буфера"""
    data = await state.get_data()
    photo_buffer = data.get('photo_buffer', [])
    
    if not photo_buffer:
        await check_completion(message, state)
        return
    
    current_photo = photo_buffer.pop(0)
    await state.update_data(
        current_photo=current_photo,
        photo_buffer=photo_buffer,
        photo_counter=data.get('photo_counter', 0) + 1
    )
    await ask_photo_assignment(message, state, bot)

async def ask_photo_assignment(message: Message, state: FSMContext, bot: Bot):
    """Запрос привязки фото к адресу"""
    data = await state.get_data()
    addresses = data['addresses']
    
    # Создаем клавиатуру с адресами, которым еще нужны фото
    keyboard = []
    for address in addresses:
        photo_count = len(data['photos'].get(address, []))
        if photo_count < PHOTOS_PER_ADDRESS:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{address} ({photo_count+1}/{PHOTOS_PER_ADDRESS})", 
                    callback_data=f"address_{address}"
                )
            ])
    
    if not keyboard:
        await message.answer("❌ Все фото уже распределены!")
        await check_completion(message, state)
        return
        
    await state.set_state(GarbageReportState.PHOTO_ASSIGNMENT)
    await message.answer(
        "Выберите адрес для этого фото:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

@garbage_router.callback_query(GarbageReportState.PHOTO_ASSIGNMENT, F.data.startswith("address_"))
async def assign_photo_to_address(callback: CallbackQuery, state: FSMContext):
    """Привязка фото к адресу"""
    bot = Bot.get_current()
    address = callback.data.split("_", 1)[1]
    data = await state.get_data()
    
    # Обновляем данные о фото
    photos = data['photos'].copy()
    if address not in photos:
        photos[address] = []
    
    if len(photos[address]) >= PHOTOS_PER_ADDRESS:
        await callback.answer("❌ Нельзя добавить больше фото на этот адрес!")
        return
    
    photos[address].append(data['current_photo'])
    await state.update_data(photos=photos)
    
    await callback.answer(f"✅ Фото привязано к адресу: {address}")
    await callback.message.delete()
    
    # Проверяем завершение
    total_photos = len(data['addresses']) * PHOTOS_PER_ADDRESS
    current_count = sum(len(p) for p in photos.values())
    
    if current_count >= total_photos:
        await generate_garbage_report(callback.message, state)
    else:
        await state.set_state(GarbageReportState.INPUT_PHOTOS)
        await callback.message.answer(
            f"✅ Фото {current_count}/{total_photos} сохранено. Отправьте следующее:"
        )

async def generate_garbage_report(message: Message, state: FSMContext):
    """Генерация итогового отчета"""
    data = await state.get_data()
    bot = Bot.get_current()
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Создаем временный файл отчета
        doc_path = os.path.join(temp_dir, "Отчет_вывоза_мусора.docx")
        shutil.copy(TEMPLATE_NAME, doc_path)
        
        # Основные замены текста
        replacements = {
            "<<DATE>>": data.get('date', ''),
            "<<EQUIPMENT>>": data.get('equipment', ''),
            "<<GARBAGE_AMOUNT>>": data.get('garbage_amount', ''),
            "<<PARTICIPANTS>>": data.get('participants', ''),
            "<<HOURS>>": data.get('hours', ''),
            "<<ADDRESSES>>": "\n".join(data['addresses'])
        }
        
        # Загружаем документ
        doc = Document(doc_path)
        
        # Находим секцию-шаблон для адреса
        template_section = None
        for i, para in enumerate(doc.paragraphs):
            if "<<ADDRESS_SECTION>>" in para.text:
                template_section = i
                break
        
        if template_section is None:
            await message.answer("❌ В шаблоне не найдена секция для адресов!")
            await state.clear()
            return
        
        # Удаляем все существующие адресные секции после шаблона
        for i in range(len(doc.paragraphs)-1, template_section, -1):
            if "<<END_SECTION>>" in doc.paragraphs[i].text:
                break
            del doc.paragraphs[i]
        
        # Добавляем секции только для введенных адресов
        for i, address in enumerate(data['addresses']):
            if i > 0:
                # Добавляем разрыв страницы для нового адреса
                doc.add_section(WD_SECTION.NEW_PAGE)
            
            # Копируем шаблонную секцию
            for para in doc.paragraphs[template_section+1:]:
                if "<<END_SECTION>>" in para.text:
                    break
                
                new_para = doc.add_paragraph(para.text)
                new_para.style = para.style
                
                # Заменяем плейсхолдеры
                if "<<CURRENT_ADDRESS>>" in new_para.text:
                    new_para.text = new_para.text.replace("<<CURRENT_ADDRESS>>", address)
                
                # Вставляем фото
                for photo_num in range(1, PHOTOS_PER_ADDRESS+1):
                    placeholder = f"<<PHOTO_{photo_num}>>"
                    if placeholder in new_para.text:
                        if photo_num-1 < len(data['photos'][address]):
                            try:
                                photo_path = await download_and_process_photo(
                                    data['photos'][address][photo_num-1],
                                    bot,
                                    PHOTO_WIDTH,
                                    PHOTO_HEIGHT
                                )
                                new_para.text = new_para.text.replace(placeholder, "")
                                run = new_para.add_run()
                                run.add_picture(photo_path, width=Cm(PHOTO_WIDTH), height=Cm(PHOTO_HEIGHT))
                                apply_photo_style(run)
                                os.unlink(photo_path)
                            except Exception as e:
                                logger.error(f"Ошибка обработки фото: {e}")
                                new_para.text = new_para.text.replace(placeholder, "[Ошибка загрузки фото]")
        
        # Удаляем шаблонные маркеры
        del doc.paragraphs[template_section]
        for para in doc.paragraphs:
            if "<<END_SECTION>>" in para.text:
                para.text = para.text.replace("<<END_SECTION>>", "")
                break
        
        # Сохраняем и отправляем отчет
        doc.save(doc_path)
        await message.answer("✅ Отчет готов!")
        await message.answer_document(
            FSInputFile(doc_path, filename="Отчет_вывоза_мусора.docx")
        )
    
    await state.clear()

# [Остальные вспомогательные функции остаются без изменений]

__all__ = ['garbage_router', 'start_garbage_report']

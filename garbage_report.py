import os
import re
import asyncio
import logging
import shutil
import tempfile
from datetime import datetime
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
from docx.shared import Cm, Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import nsdecls
from docx.oxml import parse_xml
from PIL import Image

logger = logging.getLogger(__name__)
router = Router()

# Константы
PHOTO_WIDTH = 13.33  # см
PHOTO_HEIGHT = 7.5   # см
CM_TO_PX = 37.8      # 1 см ≈ 37.8 пикселей

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
        "📅 Введите дату вывоза мусора (в формате ДД.ММ.ГГГГ):",
        reply_markup=ReplyKeyboardRemove()
    )

@router.message(GarbageReportState.DATE)
async def process_date(message: types.Message, state: FSMContext):
    """Обработка даты"""
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ")
        return
        
    await state.update_data(date=message.text)
    await state.set_state(GarbageReportState.ADDRESSES)
    await message.answer(
        "🏠 Введите адреса (каждый адрес с новой строки):\n"
        "Пример:\n"
        "Ул. Ленина, д. 1\n"
        "Пр. Мира, д. 15\n"
        "Ул. Центральная, д. 8"
    )

@router.message(GarbageReportState.ADDRESSES)
async def process_addresses(message: types.Message, state: FSMContext):
    """Обработка списка адресов"""
    addresses = [addr.strip() for addr in message.text.split('\n') if addr.strip()]
    if not addresses:
        await message.answer("❌ Адреса не введены. Введите хотя бы один адрес")
        return
        
    await state.update_data(
        addresses=addresses,
        photos={addr: [] for addr in addresses},
        photo_counter=0
    )
    await state.set_state(GarbageReportState.EQUIPMENT)
    await message.answer("🚛 Введите задействованную технику:")

@router.message(GarbageReportState.EQUIPMENT)
async def process_equipment(message: types.Message, state: FSMContext):
    """Обработка информации о технике"""
    await state.update_data(equipment=message.text)
    await state.set_state(GarbageReportState.GARBAGE_AMOUNT)
    await message.answer("🗑️ Введите количество вывезенного мусора (в тоннах):")

@router.message(GarbageReportState.GARBAGE_AMOUNT)
async def process_garbage_amount(message: types.Message, state: FSMContext):
    """Обработка объема мусора"""
    await state.update_data(garbage_amount=message.text)
    await state.set_state(GarbageReportState.PARTICIPANTS)
    await message.answer("👥 Введите количество участников:")

@router.message(GarbageReportState.PARTICIPANTS)
async def process_participants(message: types.Message, state: FSMContext):
    """Обработка количества участников"""
    await state.update_data(participants=message.text)
    await state.set_state(GarbageReportState.HOURS)
    await message.answer("⏱️ Введите количество часов работы техники:")

@router.message(GarbageReportState.HOURS)
async def process_hours(message: types.Message, state: FSMContext):
    """Обработка информации о часах работы"""
    await state.update_data(hours=message.text)
    data = await state.get_data()
    
    # Подготовка к приему фото
    total_photos = len(data['addresses']) * 2
    await state.set_state(GarbageReportState.INPUT_PHOTOS)
    await message.answer(
        f"📸 Теперь загрузите {total_photos} фото (по 2 на каждый адрес).\n"
        f"Порядок адресов:\n" + "\n".join(
            f"{i+1}. {addr}" for i, addr in enumerate(data['addresses'])
    )

@router.message(GarbageReportState.INPUT_PHOTOS, F.photo)
async def process_photo_upload(message: types.Message, state: FSMContext):
    """Обработка загруженных фото"""
    data = await state.get_data()
    addresses = data['addresses']
    photo_counter = data['photo_counter'] + 1
    total_photos = len(addresses) * 2
    
    # Сохраняем file_id фото
    await state.update_data(
        current_photo=message.photo[-1].file_id,
        photo_counter=photo_counter
    )
    
    # Создаем клавиатуру с адресами
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for address in addresses:
        # Показываем сколько фото уже привязано к адресу
        photo_count = len(data['photos'].get(address, []))
        if photo_count < 2:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"{address} ({photo_count+1}/2)", 
                    callback_data=f"address_{address}"
                )
            ])
    
    await state.set_state(GarbageReportState.PHOTO_ASSIGNMENT)
    await message.answer(
        f"📎 Фото {photo_counter}/{total_photos}\n"
        "Выберите адрес для этого фото:",
        reply_markup=keyboard
    )

@router.callback_query(GarbageReportState.PHOTO_ASSIGNMENT, F.data.startswith("address_"))
async def assign_photo_to_address(callback: types.CallbackQuery, state: FSMContext):
    """Привязка фото к адресу"""
    address = callback.data.split("_", 1)[1]
    data = await state.get_data()
    current_photo = data['current_photo']
    
    # Обновляем данные
    photos = data['photos'].copy()
    photos[address].append(current_photo)
    photo_counter = data['photo_counter']
    total_photos = len(data['addresses']) * 2
    
    await state.update_data(photos=photos)
    await callback.answer(f"Фото привязано к адресу: {address}")
    
    # Удаляем сообщение с кнопками
    try:
        await callback.message.delete()
    except:
        pass
    
    # Проверяем завершение
    if photo_counter >= total_photos:
        await generate_garbage_report(callback.message, state)
        return
        
    await state.set_state(GarbageReportState.INPUT_PHOTOS)
    await callback.message.answer(
        f"✅ Фото {photo_counter}/{total_photos} сохранено\n"
        "Загружайте следующее фото:"
    )

def apply_photo_style(run):
    """Применение стилей к фото в документе"""
    # Скошенные углы
    effect = parse_xml(
        r'<a:prstGeom prst="roundRect" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"/>'
    )
    run._element.xpath('.//pic:spPr')[0].append(effect)
    
    # Белая рамка
    ln = parse_xml(
        r'<a:ln w="12700" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        r'<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
        r'</a:ln>'
    )
    run._element.xpath('.//pic:spPr')[0].append(ln)

async def download_and_process_photo(file_id: str, bot: Bot, target_width: float, target_height: float):
    """Скачивание и обработка фото"""
    # Скачивание фото
    file = await bot.get_file(file_id)
    photo_data = await bot.download_file(file.file_path)
    
    # Обработка в памяти
    with Image.open(photo_data) as img:
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Расчет размеров в пикселях
        target_width_px = int(target_width * CM_TO_PX)
        target_height_px = int(target_height * CM_TO_PX)
        
        # Агрессивное заполнение
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
        
        # Сохранение во временный файл
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
        img.save(temp_file, format='JPEG', quality=95)
        return temp_file.name

async def generate_garbage_report(message: types.Message, state: FSMContext):
    """Генерация итогового документа"""
    data = await state.get_data()
    bot = message.bot
    
    # Создаем временную директорию
    with tempfile.TemporaryDirectory() as temp_dir:
        doc_path = os.path.join(temp_dir, "Отчет_вывоза_мусора.docx")
        doc = Document()
        
        # Заголовок документа
        title = doc.add_paragraph("Отчет вывоза мусора")
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.runs[0].bold = True
        title.runs[0].font.size = Pt(14)
        
        # Добавляем таблицу с данными
        table = doc.add_table(rows=1, cols=6)
        table.style = 'Table Grid'
        
        # Заголовки таблицы
        headers = [
            "Дата", "Адрес", "Техника", 
            "Мусор (т)", "Участники", "Часы"
        ]
        hdr_cells = table.rows[0].cells
        for i, header in enumerate(headers):
            hdr_cells[i].text = header
            hdr_cells[i].paragraphs[0].runs[0].bold = True
        
        # Добавляем данные
        addresses = data['addresses']
        for address in addresses:
            row_cells = table.add_row().cells
            row_cells[0].text = data['date']  # Дата
            row_cells[1].text = address       # Адрес
            row_cells[2].text = data.get('equipment', '')  # Техника
            row_cells[3].text = data.get('garbage_amount', '')  # Мусор
            row_cells[4].text = data.get('participants', '')  # Участники
            row_cells[5].text = data.get('hours', '')  # Часы
        
        # Добавляем секции с фото для каждого адреса
        for address in addresses:
            # Заголовок адреса
            doc.add_page_break()
            addr_para = doc.add_paragraph(f"{data['date']}\n{address}")
            addr_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            addr_para.runs[0].font.size = Pt(14)
            addr_para.runs[0].font.name = 'Times New Roman'
            
            # Добавляем фото
            photo_paths = []
            for i, file_id in enumerate(data['photos'].get(address, [])):
                photo_path = await download_and_process_photo(
                    file_id, bot, PHOTO_WIDTH, PHOTO_HEIGHT
                )
                photo_paths.append(photo_path)
            
            # Вставляем фото в документ
            for photo_path in photo_paths:
                para = doc.add_paragraph()
                run = para.add_run()
                run.add_picture(photo_path, width=Cm(PHOTO_WIDTH), height=Cm(PHOTO_HEIGHT))
                apply_photo_style(run)
                os.unlink(photo_path)  # Удаляем временный файл
        
        # Сохраняем документ
        doc.save(doc_path)
        
        # Отправляем документ пользователю
        await message.answer("✅ Отчет готов!")
        await message.answer_document(FSInputFile(doc_path))
    
    # Завершаем сессию
    await state.clear()

# Экспорт для главного бота
dp = router

async def start_garbage_report(message: types.Message, state: FSMContext):
    """Функция для запуска из главного бота"""
    await start_garbage_report(message, state)
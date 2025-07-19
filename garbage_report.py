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
from PIL import Image
from datetime import datetime

logger = logging.getLogger(__name__)
garbage_router = Router()

# Константы
PHOTO_WIDTH = 13.33  # см
PHOTO_HEIGHT = 7.5   # см
CM_TO_PX = 37.8      # 1 см ≈ 37.8 пикселей
TEMPLATE_NAME = "template21.docx"  # Название файла шаблона

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

def normalize_date(date_str: str) -> str | None:
    """Пытается привести дату к формату ДД.ММ.ГГГГ (возвращает None при неудаче)"""
    # Поддерживаемые форматы
    formats = [
        "%d.%m.%Y", "%d.%m.%y",  # 19.07.2025 / 19.07.25
        "%d/%m/%Y", "%d/%m/%y",    # 19/07/2025 / 19/07/25
        "%d-%m-%Y", "%d-%m-%y",    # 19-07-2025 / 19-07-25
        "%Y.%m.%d", "%y.%m.%d"     # 2025.07.19 / 25.07.19 (обратный формат)
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%d.%m.%Y")  # Единый формат
        except ValueError:
            continue
    return None  # Не удалось распознать

@garbage_router.message(GarbageReportState.DATE)
async def process_date(message: types.Message, state: FSMContext):
    """Обработка даты (принимает любой формат)"""
    user_input = message.text.strip()
    normalized_date = normalize_date(user_input)
    
    if not normalized_date:
        await message.answer("❌ Неверный формат даты. Попробуйте снова (например: 19.07.2025)")
        return
        
    await state.update_data(date=normalized_date)
    await state.set_state(GarbageReportState.ADDRESSES)
    await message.answer(
        "🏠 Введите адреса (каждый адрес с новой строки):\n"
        "Пример:\n"
        "Ул. Ленина, д. 1\n"
        "Пр. Мира, д. 15\n"
        "Ул. Центральная, д. 8"
    )

@garbage_router.message(GarbageReportState.ADDRESSES)
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

@garbage_router.message(GarbageReportState.EQUIPMENT)
async def process_equipment(message: types.Message, state: FSMContext):
    """Обработка информации о технике"""
    await state.update_data(equipment=message.text)
    await state.set_state(GarbageReportState.GARBAGE_AMOUNT)
    await message.answer("🗑️ Введите количество вывезенного мусора (в тоннах):")

@garbage_router.message(GarbageReportState.GARBAGE_AMOUNT)
async def process_garbage_amount(message: types.Message, state: FSMContext):
    """Обработка объема мусора"""
    await state.update_data(garbage_amount=message.text)
    await state.set_state(GarbageReportState.PARTICIPANTS)
    await message.answer("👥 Введите количество участников:")

@garbage_router.message(GarbageReportState.PARTICIPANTS)
async def process_participants(message: types.Message, state: FSMContext):
    """Обработка количества участников"""
    await state.update_data(participants=message.text)
    await state.set_state(GarbageReportState.HOURS)
    await message.answer("⏱️ Введите количество часов работы техники:")

@garbage_router.message(GarbageReportState.HOURS)
async def process_hours(message: types.Message, state: FSMContext):
    """Обработка информации о часах работы"""
    await state.update_data(hours=message.text)
    data = await state.get_data()
    
    total_photos = len(data['addresses']) * 2
    await state.set_state(GarbageReportState.INPUT_PHOTOS)
    await message.answer(
        f"📸 Теперь загрузите {total_photos} фото (по 2 на каждый адрес).\n"
        f"Порядок адресов:\n" + "\n".join(
            f"{i+1}. {addr}" for i, addr in enumerate(data['addresses'])
        )
    )

# Обработчик для не-фото в состоянии INPUT_PHOTOS
@garbage_router.message(GarbageReportState.INPUT_PHOTOS, ~F.photo)
async def handle_non_photo_input(message: types.Message):
    """Обработка некорректного ввода (не фото)"""
    await message.answer("❌ Пожалуйста, загрузите фото. Отправьте изображение как фото (не как файл)")

@garbage_router.message(GarbageReportState.INPUT_PHOTOS, F.photo)
async def process_photo_upload(message: types.Message, state: FSMContext):
    """Обработка загруженных фото"""
    data = await state.get_data()
    addresses = data['addresses']
    photo_counter = data['photo_counter'] + 1
    total_photos = len(addresses) * 2
    
    await state.update_data(
        current_photo=message.photo[-1].file_id,
        photo_counter=photo_counter
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for address in addresses:
        photo_count = len(data['photos'].get(address, []))
        if photo_count < 2:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"{address} ({photo_count+1}/2)", 
                    callback_data=f"address_{address}"
                )
            ])
    
    # Если все фото уже распределены (не должно происходить, но на всякий случай)
    if not keyboard.inline_keyboard:
        await message.answer("❌ Все фото уже распределены!")
        return
        
    await state.set_state(GarbageReportState.PHOTO_ASSIGNMENT)
    await message.answer(
        f"📎 Фото {photo_counter}/{total_photos}\n"
        "Выберите адрес для этого фото:",
        reply_markup=keyboard
    )

@garbage_router.callback_query(GarbageReportState.PHOTO_ASSIGNMENT, F.data.startswith("address_"))
async def assign_photo_to_address(callback: types.CallbackQuery, state: FSMContext):
    """Привязка фото к адресу"""
    address = callback.data.split("_", 1)[1]
    data = await state.get_data()
    current_photo = data['current_photo']
    
    photos = data['photos'].copy()
    if address in photos and len(photos[address]) < 2:
        photos[address].append(current_photo)
    else:
        await callback.answer("❌ Нельзя добавить больше 2 фото на адрес!")
        return
        
    photo_counter = data['photo_counter']
    total_photos = len(data['addresses']) * 2
    
    await state.update_data(photos=photos)
    await callback.answer(f"✅ Фото привязано к адресу: {address}")
    
    try:
        await callback.message.delete()
    except Exception as e:
        logger.error(f"Ошибка при удалении сообщения: {e}")
    
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
    effect = parse_xml(
        r'<a:prstGeom prst="roundRect" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"/>'
    )
    run._element.xpath('.//pic:spPr')[0].append(effect)
    
    ln = parse_xml(
        r'<a:ln w="12700" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        r'<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
        r'</a:ln>'
    )
    run._element.xpath('.//pic:spPr')[0].append(ln)

async def download_and_process_photo(file_id: str, bot: Bot, target_width: float, target_height: float):
    """Скачивание и обработка фото"""
    file = await bot.get_file(file_id)
    photo_data = await bot.download_file(file.file_path)
    
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as temp_input:
        # Сохраняем оригинальное фото
        temp_input.write(photo_data.read())
        temp_input.flush()
        
        with Image.open(temp_input.name) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            target_width_px = int(target_width * CM_TO_PX)
            target_height_px = int(target_height * CM_TO_PX)
            
            # Рассчитываем масштаб
            width, height = img.size
            scale = max(
                target_width_px / width,
                target_height_px / height
            )
            scaled_width = int(width * scale)
            scaled_height = int(height * scale)
            
            # Масштабируем и обрезаем
            img = img.resize((scaled_width, scaled_height), Image.LANCZOS)
            left = (scaled_width - target_width_px) // 2
            top = (scaled_height - target_height_px) // 2
            img = img.crop((
                max(0, left),
                max(0, top),
                min(scaled_width, left + target_width_px),
                min(scaled_height, top + target_height_px)
            ))
            
            temp_output = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
            img.save(temp_output, format='JPEG', quality=95)
            temp_output.close()
            
            os.unlink(temp_input.name)
            return temp_output.name

def find_and_replace_text(doc, placeholder, replacement):
    """Поиск и замена текста в документе"""
    for para in doc.paragraphs:
        if placeholder in para.text:
            para.text = para.text.replace(placeholder, replacement)
    
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if placeholder in cell.text:
                    cell.text = cell.text.replace(placeholder, replacement)

async def generate_garbage_report(message: types.Message, state: FSMContext):
    """Генерация отчета на основе шаблона"""
    data = await state.get_data()
    bot = message.bot
    
    with tempfile.TemporaryDirectory() as temp_dir:
        doc_path = os.path.join(temp_dir, "Отчет_вывоза_мусора.docx")
        
        template_path = TEMPLATE_NAME
        if not os.path.exists(template_path):
            await message.answer(f"❌ Шаблон {TEMPLATE_NAME} не найден!")
            await state.clear()
            return
            
        doc = Document(template_path)
        
        replacements = {
            "<<DATE>>": data['date'],
            "<<EQUIPMENT>>": data.get('equipment', ''),
            "<<GARBAGE_AMOUNT>>": data.get('garbage_amount', ''),
            "<<PARTICIPANTS>>": data.get('participants', ''),
            "<<HOURS>>": data.get('hours', '')
        }
        
        for placeholder, value in replacements.items():
            find_and_replace_text(doc, placeholder, value)
        
        addresses = data['addresses']
        addresses_text = "\n".join(addresses)
        find_and_replace_text(doc, "<<ADDRESSES>>", addresses_text)
        
        # Обработка и вставка фотографий
        for i, address in enumerate(addresses):
            for j in range(2):
                placeholder = f"<<PHOTO_{i+1}_{j+1}>>"
                if j < len(data['photos'][address]):
                    file_id = data['photos'][address][j]
                    try:
                        photo_path = await download_and_process_photo(
                            file_id, bot, PHOTO_WIDTH, PHOTO_HEIGHT
                        )
                        
                        found = False
                        for para in doc.paragraphs:
                            if placeholder in para.text:
                                para.text = para.text.replace(placeholder, '')
                                run = para.add_run()
                                run.add_picture(photo_path, width=Cm(PHOTO_WIDTH), height=Cm(PHOTO_HEIGHT))
                                apply_photo_style(run)
                                found = True
                                break
                        
                        if not found:
                            logger.warning(f"Не найден плейсхолдер для фото: {placeholder}")
                        
                        os.unlink(photo_path)
                    except Exception as e:
                        logger.error(f"Ошибка обработки фото: {e}")
                        await message.answer(f"❌ Ошибка обработки фото для адреса {address}")
        
        doc.save(doc_path)
        await message.answer("✅ Отчет готов!")
        await message.answer_document(FSInputFile(doc_path, filename="Отчет_вывоза_мусора.docx"))
    
    await state.clear()

# Экспорт для использования в других модулях
__all__ = ['garbage_router', 'start_garbage_report']

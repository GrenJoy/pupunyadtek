import os
import logging
import threading
import signal
import sys
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, List
from io import BytesIO
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
from telegram.constants import ParseMode

from database import Database
from gemini_service import (
    analyze_schedule_image, 
    generate_schedule_response, 
    check_schedule_post_and_date, 
    analyze_schedule_text, 
    is_complete_schedule, 
    merge_schedules,
    detect_schedule_message_type,
    apply_dnipro_partial_update
)
from helpers import get_schedule_status, get_kyiv_time
from constants import ELECTRICITY_GROUPS
from channel_fetcher import find_and_process_schedule_for_user, get_schedule_photos_from_channel
from group_schedule import generate_group_schedule_message, format_person_schedule_block

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
(
    WAITING_CITY_NAME,
    WAITING_PERSON_NAME,
    WAITING_PERSON_GROUP,
    WAITING_EDIT_PERSON_NAME,
    WAITING_EDIT_PERSON_GROUP,
    WAITING_SCHEDULE_PHOTO,
    WAITING_CHANNEL_USERNAME,
    WAITING_EDIT_CITY_NAME,
    WAITING_ADMIN_POST,
    WAITING_ADMIN_CITY,
    WAITING_ADMIN_CONFIRM,
) = range(11)

# Инициализация базы данных
db = Database()

# Временное хранилище для контекста пользователей
user_context: Dict[int, Dict] = {}

# Глобальные переменные для хранения ссылки на мониторинг
monitor_thread = None
monitor_instance = None  # Ссылка на экземпляр ChannelMonitor для остановки
monitoring_restart_in_progress = False  # Флаг для предотвращения одновременных перезапусков

# Список заблокированных каналов (нельзя добавлять в мониторинг)
BLOCKED_CHANNELS = [
    "dtek_ua",  # Заблокированный канал
]


# ========== HTTP SERVER ДЛЯ ПИНГА (предотвращение засыпания на Render) ==========


import uvicorn
from api import app as api_app

def start_api_server(port=8000):
    """Запускает FastAPI сервер в отдельном потоке"""
    def run_server():
        logger.info(f"🌐 API сервер запускается на порту {port}")
        # uvicorn.run блокирует поток, поэтому запускаем его здесь
        uvicorn.run(api_app, host="0.0.0.0", port=port, log_level="info")
    
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    return thread


def get_main_menu_keyboard():
    """Главное меню бота"""
    keyboard = [
        [InlineKeyboardButton("👤 Управление людьми", callback_data="manage_people")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="notifications")],
        [
            InlineKeyboardButton("📅 Посмотреть график", callback_data="view_schedule"),
            InlineKeyboardButton("📸 Загрузить график", callback_data="upload_schedule")
        ],
        [InlineKeyboardButton("🏙️ Управление городами", callback_data="manage_cities")],
        [InlineKeyboardButton("👥 График группы", callback_data="view_group_schedule")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_back_keyboard():
    """Кнопка "Назад" в главное меню"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    try:
        await update.message.reply_text(
            "🏠 <b>Главное меню</b>\n\n"
            "Выберите действие:",
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"❌ Ошибка в команде /start: {e}", exc_info=True)


async def start_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для ручного запуска мониторинга всех каналов"""
    try:
        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        
        if not api_id or not api_hash:
            await update.message.reply_text(
                "❌ <b>Мониторинг недоступен</b>\n\n"
                "TELEGRAM_API_ID и TELEGRAM_API_HASH не установлены в переменных окружения.",
                parse_mode=ParseMode.HTML
            )
            return
        
        # Проверяем, есть ли каналы в БД
        channels = db.get_all_channels()
        if not channels:
            await update.message.reply_text(
                "❌ <b>Нет каналов для мониторинга</b>\n\n"
                "Добавьте каналы через интерфейс бота:\n"
                "Управление городами → Выберите город → Управление каналом → Добавить канал",
                parse_mode=ParseMode.HTML
            )
            return
        
        global monitor_thread, monitor_instance
        
        # Останавливаем старый мониторинг, если он запущен
        if monitor_instance and hasattr(monitor_instance, 'is_running') and monitor_instance.is_running:
            logger.info("🔄 Останавливаю старый мониторинг для перезапуска...")
            try:
                monitor_instance.is_running = False
                if hasattr(monitor_instance, 'client') and monitor_instance.client:
                    import asyncio
                    stop_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(stop_loop)
                    try:
                        stop_loop.run_until_complete(monitor_instance.client.disconnect())
                    except:
                        pass
                    finally:
                        stop_loop.close()
            except Exception as e:
                logger.warning(f"Не удалось корректно остановить мониторинг: {e}")
            
            import time
            time.sleep(2)
        
        # Запускаем новый мониторинг
        logger.info("=" * 60)
        logger.info("🚀 РУЧНОЙ ЗАПУСК МОНИТОРИНГА (команда /start_mon)")
        logger.info("=" * 60)
        logger.info(f"📊 Найдено каналов в БД: {len(channels)}")
        for ch in channels:
            city = db.get_city(ch.city_id)
            city_name = city.name if city else f"ID {ch.city_id}"
            logger.info(f"   📌 @{ch.channel_username} (город: {city_name})")
        logger.info("=" * 60)
        
        from channel_monitor import start_monitor_task
        monitor_instance_ref = [None]
        monitor_thread = start_monitor_task(db, context.application, monitor_instance_ref)
        
        # Ждём, пока monitor_instance будет создан
        import time
        for _ in range(10):
            if monitor_instance_ref[0] is not None:
                monitor_instance = monitor_instance_ref[0]
                break
            time.sleep(0.2)
        
        if monitor_instance_ref[0]:
            monitor_instance = monitor_instance_ref[0]
            logger.info("✅ Мониторинг успешно запущен!")
            
            channel_list = "\n".join([f"   • @{ch.channel_username}" for ch in channels[:10]])
            if len(channels) > 10:
                channel_list += f"\n   ... и ещё {len(channels) - 10} каналов"
            
            await update.message.reply_text(
                f"✅ <b>Мониторинг запущен!</b>\n\n"
                f"📊 Отслеживается каналов: <b>{len(channels)}</b>\n\n"
                f"📋 Каналы:\n{channel_list}\n\n"
                f"🔍 Мониторинг активен и ждёт новых постов.\n"
                f"📝 Все события логируются в консоль.",
                parse_mode=ParseMode.HTML
            )
        else:
            logger.warning("⚠️ Не удалось получить ссылку на monitor_instance")
            await update.message.reply_text(
                "⚠️ <b>Мониторинг запускается...</b>\n\n"
                "Проверьте логи для деталей. Если не заработает, попробуйте ещё раз через несколько секунд.",
                parse_mode=ParseMode.HTML
            )
            
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске мониторинга: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ <b>Ошибка при запуске мониторинга</b>\n\n"
            f"Ошибка: {str(e)}\n\n"
            f"Проверьте логи для деталей.",
            parse_mode=ParseMode.HTML
        )


async def handle_unknown_message_in_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback для обработки неправильных сообщений в разговоре"""
    # Этот обработчик используется как fallback в ConversationHandler'ах
    # для обработки сообщений, которые не соответствуют ожидаемому формату
    await update.message.reply_text(
        "❌ Пожалуйста, следуйте инструкциям выше.\n\n"
        "Используйте кнопку 'Назад' для отмены или команду /start для возврата в главное меню.",
        reply_markup=get_back_keyboard()
    )


# ========== ПАНЕЛЬ АДМИНИСТРАТОРА ==========

def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    admin_id = os.getenv("ADMIN_ID")
    if not admin_id:
        # Если ADMIN_ID не установлен, разрешаем всем (для разработки)
        return True
    try:
        return str(user_id) == str(admin_id)
    except:
        return False


async def admin_test_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admin или /test_post - панель администратора для тестирования постов"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            "❌ <b>Доступ запрещен</b>\n\n"
            "Эта команда доступна только администратору.",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END
    
    # Получаем список городов
    cities = db.get_all_cities()
    
    if not cities:
        await update.message.reply_text(
            "❌ <b>Ошибка</b>\n\n"
            "В базе данных нет городов. Добавьте город через интерфейс бота.",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END
    
    # Создаем кнопки для выбора города
    keyboard = []
    for city in cities:
        keyboard.append([InlineKeyboardButton(
            f"🏙️ {city.name}",
            callback_data=f"admin_city_{city.id}"
        )])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel")])
    
    await update.message.reply_text(
        "🔧 <b>Панель администратора</b>\n\n"
        "Выберите город для тестирования поста:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    
    return WAITING_ADMIN_CITY


async def admin_select_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор города администратором"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.edit_message_text("❌ Доступ запрещен")
        return ConversationHandler.END
    
    if query.data == "admin_cancel":
        await query.edit_message_text("❌ Отменено")
        return ConversationHandler.END
    
    # Извлекаем ID города из callback_data
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    
    if not city:
        await query.edit_message_text("❌ Город не найден")
        return ConversationHandler.END
    
    # Сохраняем выбранный город в context
    context.user_data['admin_city_id'] = city_id
    context.user_data['admin_city_name'] = city.name
    # Инициализируем хранилище для фото
    context.user_data['admin_photos'] = []
    context.user_data['admin_photo_mime_types'] = []
    
    await query.edit_message_text(
        f"✅ Выбран город: <b>{city.name}</b>\n\n"
        "Теперь отправьте текст поста ИЛИ фото (до 3 штук) из канала для тестирования.\n\n"
        "💡 <b>Важно:</b> Если отправляете несколько фото, они будут обработаны все вместе после нажатия кнопки '✅ Обработать'.\n\n"
        "Бот обработает через Gemini и покажет:\n"
        "• Тип сообщения (full_today, partial_today, ignore и т.д.)\n"
        "• График ДО изменений\n"
        "• График ПОСЛЕ изменений\n\n"
        "Используйте /cancel для отмены.",
        parse_mode=ParseMode.HTML
    )
    
    return WAITING_ADMIN_POST


async def admin_process_photos_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все загруженные фото в админ панели после нажатия 'Обработать'"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.edit_message_text("❌ Доступ запрещен")
        return ConversationHandler.END
    
    # Получаем выбранный город
    city_id = context.user_data.get('admin_city_id')
    if not city_id:
        await query.edit_message_text(
            "❌ <b>Ошибка</b>\n\n"
            "Город не выбран. Начните заново с команды /admin",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END
    
    city = db.get_city(city_id)
    if not city:
        await query.edit_message_text("❌ Город не найден")
        return ConversationHandler.END
    
    # Получаем загруженные фото
    photos_bytes = context.user_data.get('admin_photos', [])
    mime_types = context.user_data.get('admin_photo_mime_types', [])
    text = context.user_data.get('admin_text', '')
    
    if not photos_bytes:
        await query.edit_message_text(
            "⚠️ Не загружено ни одного фото.\n\n"
            "Отправьте фото или текст поста.",
            parse_mode=ParseMode.HTML
        )
        return WAITING_ADMIN_POST
    
    # Показываем, что обрабатываем
    await query.edit_message_text(f"⏳ Обрабатываю {len(photos_bytes)} фото через Gemini...")
    
    try:
        # Получаем текущее время
        post_time_kyiv = get_kyiv_time()
        
        # Определяем тип сообщения по тексту (если есть)
        if text and len(text.strip()) >= 10:
            msg_type = await asyncio.to_thread(
                detect_schedule_message_type,
                text,
                post_time_kyiv,
                city.name.lower() if "dnipro" in city.name.lower() else ""
            )
        else:
            # Если нет текста, определяем по времени поста
            current_time = get_kyiv_time()
            today_date = current_time.date()
            post_date = post_time_kyiv.date()
            
            if post_date == today_date and post_time_kyiv.hour < 12:
                msg_type = "full_today"
            else:
                msg_type = "full_tomorrow"
        
        if msg_type == "ignore":
            await query.edit_message_text(
                "🚫 <b>Сообщение проигнорировано</b>\n\n"
                "Gemini определил это как мусор/рекламу/ЦЕК и т.д.",
                parse_mode=ParseMode.HTML
            )
            # Очищаем контекст
            context.user_data.pop('admin_photos', None)
            context.user_data.pop('admin_photo_mime_types', None)
            context.user_data.pop('admin_text', None)
            return ConversationHandler.END
        
        # Определяем день
        if "today" in msg_type:
            target_day = "today"
        else:
            target_day = "tomorrow"
        
        # Получаем старый график
        old_schedule = db.get_schedule(city.id, target_day) or {}
        
        # Форматируем старый график для отправки
        old_schedule_text = format_schedule_for_admin(old_schedule, city.name, target_day, "ДО")
        
        # Отправляем график ДО
        await query.message.reply_text(
            old_schedule_text,
            parse_mode=ParseMode.HTML
        )
        
        # ВАЖНО: Теперь обрабатываем ВСЕ фото через Gemini
        merged_schedule = {}
        processed_count = 0
        
        for i, (photo_bytes, mime_type) in enumerate(zip(photos_bytes, mime_types), 1):
            logger.info(f"📸 Админ панель: обрабатываю фото {i}/{len(photos_bytes)} через Gemini...")
            
            # Анализируем фото через Gemini
            schedule_data = await asyncio.to_thread(analyze_schedule_image, photo_bytes, mime_type)
            
            if schedule_data:
                groups_list = list(schedule_data.keys())
                logger.info(f"   ✅ Фото {i}: распознано {len(schedule_data)} групп - {groups_list}")
                
                # Объединяем данные из всех фото
                merged_schedule.update(schedule_data)
                processed_count += 1
        
        if not merged_schedule:
            await query.message.reply_text(
                "❌ <b>Ошибка</b>\n\n"
                "Не удалось извлечь график ни из одного фото.",
                parse_mode=ParseMode.HTML
            )
            # Очищаем контекст
            context.user_data.pop('admin_photos', None)
            context.user_data.pop('admin_photo_mime_types', None)
            context.user_data.pop('admin_text', None)
            return ConversationHandler.END
        
        # Обрабатываем график
        is_dnipro = city.name.lower() in ["днепр", "дніпро", "dnipro"]
        new_schedule = None
        
        if msg_type.startswith("full_"):
            # Полный график - заменяем полностью
            new_schedule = merged_schedule
            db.save_schedule(city.id, new_schedule, target_day)
        elif msg_type.startswith("partial_") and is_dnipro:
            # Частичное обновление для Днепра
            from gemini_service import merge_schedules
            if old_schedule:
                new_schedule = merge_schedules(old_schedule, merged_schedule)
            else:
                new_schedule = merged_schedule
            if new_schedule:
                db.save_schedule(city.id, new_schedule, target_day)
        else:
            # Для других городов - объединяем
            from gemini_service import merge_schedules
            if old_schedule:
                new_schedule = merge_schedules(old_schedule, merged_schedule)
            else:
                new_schedule = merged_schedule
            if new_schedule:
                db.save_schedule(city.id, new_schedule, target_day)
        
        if new_schedule is None:
            new_schedule = old_schedule
        
        # Форматируем новый график для отправки
        new_schedule_text = format_schedule_for_admin(new_schedule, city.name, target_day, "ПОСЛЕ")
        
        # Отправляем график ПОСЛЕ
        await query.message.reply_text(
            new_schedule_text,
            parse_mode=ParseMode.HTML
        )
        
        # Показываем изменения
        if old_schedule != new_schedule:
            changes = get_schedule_changes(old_schedule, new_schedule)
            if changes:
                await query.message.reply_text(
                    f"📊 <b>Изменения:</b>\n\n{changes}\n\n"
                    f"📸 Обработано фото: {processed_count}/{len(photos_bytes)}",
                    parse_mode=ParseMode.HTML
                )
        else:
            await query.message.reply_text(
                f"ℹ️ График не изменился.\n\n"
                f"📸 Обработано фото: {processed_count}/{len(photos_bytes)}"
            )
        
        # Очищаем контекст
        context.user_data.pop('admin_photos', None)
        context.user_data.pop('admin_photo_mime_types', None)
        context.user_data.pop('admin_text', None)
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Ошибка при обработке фото в админ панели: {e}", exc_info=True)
        await query.message.reply_text(
            f"❌ <b>Ошибка при обработке</b>\n\n"
            f"Ошибка: {str(e)}",
            parse_mode=ParseMode.HTML
        )
        # Очищаем контекст
        context.user_data.pop('admin_photos', None)
        context.user_data.pop('admin_photo_mime_types', None)
        context.user_data.pop('admin_text', None)
        return ConversationHandler.END


async def process_admin_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текст или фото поста от администратора"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ Доступ запрещен")
        return ConversationHandler.END
    
    # Получаем выбранный город из context
    city_id = context.user_data.get('admin_city_id')
    if not city_id:
        await update.message.reply_text(
            "❌ <b>Ошибка</b>\n\n"
            "Город не выбран. Начните заново с команды /admin",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END
    
    city = db.get_city(city_id)
    if not city:
        await update.message.reply_text(
            "❌ <b>Ошибка</b>\n\n"
            "Город не найден в базе данных.",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END
    
    # Показываем, что обрабатываем
    processing_msg = await update.message.reply_text("⏳ Обрабатываю пост через Gemini...")
    
    try:
        # Получаем текущее время (как будто пост только что опубликован)
        post_time_kyiv = get_kyiv_time()
        
        # Проверяем, есть ли фото
        has_photo = update.message.photo is not None
        text = update.message.text or update.message.caption or ""
        
        if has_photo:
            # Обработка фото
            photo_file = await update.message.photo[-1].get_file()
            buffer = BytesIO()
            await photo_file.download_to_memory(buffer)
            photo_bytes = buffer.getvalue()
            
            # Определяем тип сообщения по тексту (если есть) или по умолчанию full_today
            if text and len(text.strip()) >= 10:
                msg_type = await asyncio.to_thread(
                    detect_schedule_message_type,
                    text,
                    post_time_kyiv,
                    city.name.lower() if "dnipro" in city.name.lower() else ""
                )
            else:
                # Если нет текста, определяем по времени поста
                current_time = get_kyiv_time()
                today_date = current_time.date()
                post_date = post_time_kyiv.date()
                
                if post_date == today_date and post_time_kyiv.hour < 12:
                    msg_type = "full_today"
                else:
                    msg_type = "full_tomorrow"
            
            await processing_msg.edit_text(f"✅ Тип сообщения: <b>{msg_type}</b> (фото)", parse_mode=ParseMode.HTML)
            
            if msg_type == "ignore":
                await update.message.reply_text(
                    "🚫 <b>Сообщение проигнорировано</b>\n\n"
                    "Gemini определил это как мусор/рекламу/ЦЕК и т.д.",
                    parse_mode=ParseMode.HTML
                )
                return ConversationHandler.END
            
            # Определяем день
            if "today" in msg_type:
                target_day = "today"
            else:
                target_day = "tomorrow"
            
            # Получаем старый график
            old_schedule = db.get_schedule(city.id, target_day) or {}
            
            # Форматируем старый график для отправки
            old_schedule_text = format_schedule_for_admin(old_schedule, city.name, target_day, "ДО")
            
            # Отправляем график ДО
            await update.message.reply_text(
                old_schedule_text,
                parse_mode=ParseMode.HTML
            )
            
            # Анализируем фото через Gemini
            schedule_data = await asyncio.to_thread(analyze_schedule_image, photo_bytes, "image/jpeg")
            
            if not schedule_data:
                await update.message.reply_text(
                    "❌ <b>Ошибка</b>\n\n"
                    "Не удалось извлечь график из фото.",
                    parse_mode=ParseMode.HTML
                )
                return ConversationHandler.END
            
            # Обрабатываем график
            is_dnipro = city.name.lower() in ["днепр", "дніпро", "dnipro"]
            new_schedule = None
            
            if msg_type.startswith("full_"):
                # Полный график - заменяем полностью
                new_schedule = schedule_data
                db.save_schedule(city.id, new_schedule, target_day)
            elif msg_type.startswith("partial_") and is_dnipro:
                # Частичное обновление для Днепра
                from gemini_service import merge_schedules
                if old_schedule:
                    new_schedule = merge_schedules(old_schedule, schedule_data)
                else:
                    new_schedule = schedule_data
                if new_schedule:
                    db.save_schedule(city.id, new_schedule, target_day)
            else:
                # Для других городов - объединяем
                from gemini_service import merge_schedules
                if old_schedule:
                    new_schedule = merge_schedules(old_schedule, schedule_data)
                else:
                    new_schedule = schedule_data
                if new_schedule:
                    db.save_schedule(city.id, new_schedule, target_day)
            
            if new_schedule is None:
                new_schedule = old_schedule
            
            # Форматируем новый график для отправки
            new_schedule_text = format_schedule_for_admin(new_schedule, city.name, target_day, "ПОСЛЕ")
            
            # Отправляем график ПОСЛЕ
            await update.message.reply_text(
                new_schedule_text,
                parse_mode=ParseMode.HTML
            )
            
            # Показываем изменения
            if old_schedule != new_schedule:
                changes = get_schedule_changes(old_schedule, new_schedule)
                if changes:
                    await update.message.reply_text(
                        f"📊 <b>Изменения:</b>\n\n{changes}",
                        parse_mode=ParseMode.HTML
                    )
            else:
                await update.message.reply_text(
                    "ℹ️ График не изменился."
                )
            
            return ConversationHandler.END
        
        # Обработка текста (если нет фото)
        if not text or len(text.strip()) < 10:
            await update.message.reply_text(
                "❌ Текст слишком короткий. Отправьте полный текст поста или фото."
            )
            return WAITING_ADMIN_POST
        
        # Определяем тип сообщения
        msg_type = await asyncio.to_thread(
            detect_schedule_message_type,
            text,
            post_time_kyiv,
            city.name.lower() if "dnipro" in city.name.lower() else ""
        )
        
        await processing_msg.edit_text(f"✅ Тип сообщения: <b>{msg_type}</b>", parse_mode=ParseMode.HTML)
        
        if msg_type == "ignore":
            await update.message.reply_text(
                "🚫 <b>Сообщение проигнорировано</b>\n\n"
                "Gemini определил это как мусор/рекламу/ЦЕК и т.д.",
                parse_mode=ParseMode.HTML
            )
            return ConversationHandler.END
        
        # Определяем день
        if "today" in msg_type:
            target_day = "today"
        else:
            target_day = "tomorrow"
        
        # Получаем старый график (город уже проверен в начале функции)
        old_schedule = db.get_schedule(city.id, target_day) or {}
        
        # Форматируем старый график для отправки
        old_schedule_text = format_schedule_for_admin(old_schedule, city.name, target_day, "ДО")
        
        # Отправляем график ДО
        await update.message.reply_text(
            old_schedule_text,
            parse_mode=ParseMode.HTML
        )
        
        # Обрабатываем пост
        is_dnipro = city.name.lower() in ["днепр", "дніпро", "dnipro"]
        new_schedule = None
        
        if msg_type.startswith("full_"):
            # Полный график
            new_schedule = await asyncio.to_thread(analyze_schedule_text, text)
            if new_schedule:
                db.save_schedule(city.id, new_schedule, target_day)
        elif msg_type.startswith("partial_") and is_dnipro:
            # Частичное обновление для Днепра
            new_schedule = await asyncio.to_thread(
                apply_dnipro_partial_update,
                old_schedule,
                text,
                post_time_kyiv.strftime("%H:%M")
            )
            if new_schedule != old_schedule:
                db.save_schedule(city.id, new_schedule, target_day)
        else:
            # Для других городов или случаев
            schedule_data = await asyncio.to_thread(analyze_schedule_text, text)
            if schedule_data:
                if old_schedule:
                    new_schedule = merge_schedules(old_schedule, schedule_data)
                else:
                    new_schedule = schedule_data
                if new_schedule:
                    db.save_schedule(city.id, new_schedule, target_day)
        
        if new_schedule is None:
            new_schedule = old_schedule
        
        # Форматируем новый график для отправки
        new_schedule_text = format_schedule_for_admin(new_schedule, city.name, target_day, "ПОСЛЕ")
        
        # Отправляем график ПОСЛЕ
        await update.message.reply_text(
            new_schedule_text,
            parse_mode=ParseMode.HTML
        )
        
        # Показываем изменения
        if old_schedule != new_schedule:
            changes = get_schedule_changes(old_schedule, new_schedule)
            if changes:
                await update.message.reply_text(
                    f"📊 <b>Изменения:</b>\n\n{changes}",
                    parse_mode=ParseMode.HTML
                )
        else:
            await update.message.reply_text(
                "ℹ️ График не изменился."
            )
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Ошибка при обработке администраторского поста: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ <b>Ошибка при обработке</b>\n\n"
            f"Ошибка: {str(e)}",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END


def format_schedule_for_admin(schedule: Dict, city_name: str, day: str, prefix: str) -> str:
    """Форматирует график для отправки администратору"""
    day_ukr = "сьогодні" if day == "today" else "завтра"
    
    if not schedule:
        return f"📋 <b>{prefix} изменений - График {day_ukr} ({city_name})</b>\n\nГрафик пуст."
    
    text = f"📋 <b>{prefix} изменений - График {day_ukr} ({city_name})</b>\n\n"
    
    # Сортируем группы
    groups = sorted([k for k in schedule.keys() if k != '_meta'], key=lambda x: (int(x.split('.')[0]), int(x.split('.')[1])))
    
    for group in groups:
        intervals = schedule[group]
        if intervals:
            intervals_str = ", ".join(intervals)
            text += f"<b>{group}:</b> {intervals_str}\n"
        else:
            text += f"<b>{group}:</b> без відключень\n"
    
    return text


def get_schedule_changes(old_schedule: Dict, new_schedule: Dict) -> str:
    """Определяет изменения между старым и новым графиком"""
    changes = []
    
    all_groups = set(list(old_schedule.keys()) + list(new_schedule.keys()))
    all_groups.discard('_meta')
    
    for group in sorted(all_groups, key=lambda x: (int(x.split('.')[0]), int(x.split('.')[1]))):
        old_intervals = old_schedule.get(group, [])
        new_intervals = new_schedule.get(group, [])
        
        if old_intervals != new_intervals:
            if not old_intervals:
                changes.append(f"➕ <b>{group}:</b> добавлено {', '.join(new_intervals)}")
            elif not new_intervals:
                changes.append(f"➖ <b>{group}:</b> удалено {', '.join(old_intervals)}")
            else:
                changes.append(f"🔄 <b>{group}:</b> {', '.join(old_intervals)} → {', '.join(new_intervals)}")
    
    return "\n".join(changes) if changes else ""


async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в главное меню"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🏠 <b>Главное меню</b>\n\n"
        "Выберите действие:",
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.HTML
    )


# ========== УПРАВЛЕНИЕ ГОРОДАМИ ==========

async def manage_cities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления городами"""
    query = update.callback_query
    await query.answer()
    
    cities = db.get_cities()
    keyboard = [
        [InlineKeyboardButton("➕ Добавить город", callback_data="add_city")],
    ]
    
    if cities:
        for city in cities:
            # Получаем информацию о канале для города
            channel = db.get_channel(city.id)
            channel_info = f" 📱 @{channel.channel_username}" if channel else " ❌ Нет канала"
            
            keyboard.append([
                InlineKeyboardButton(
                    f"🏙️ {city.name}{channel_info}",
                    callback_data=f"city_details_{city.id}"
                )
            ])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
    
    cities_text = "\n".join([f"• {city.name}" for city in cities]) if cities else "Городов пока нет."
    
    await query.edit_message_text(
        f"🏙️ <b>Управление городами</b>\n\n"
        f"<b>Список городов:</b>\n{cities_text}\n\n"
        f"Выберите город для просмотра и редактирования:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def add_city_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления города"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🏙️ <b>Добавить город</b>\n\n"
        "Введите название города:",
        reply_markup=get_back_keyboard(),
        parse_mode=ParseMode.HTML
    )
    return WAITING_CITY_NAME


async def add_city_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка названия города"""
    city_name = update.message.text.strip()
    
    if not city_name:
        await update.message.reply_text("❌ Название города не может быть пустым. Попробуйте снова:")
        return WAITING_CITY_NAME
    
    # Нормализуем название перед проверкой
    normalized_name = db.normalize_city_name(city_name)
    
    # Проверяем на дубликаты
    if db.city_exists(normalized_name):
        existing_cities = db.get_cities()
        similar = [c.name for c in existing_cities if c.name.lower() == normalized_name.lower()]
        if similar:
            await update.message.reply_text(
                f"❌ Город <b>'{similar[0]}'</b> уже существует в базе.\n\n"
                f"Проверьте список городов в меню управления.",
                reply_markup=get_back_keyboard(),
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                f"❌ Город '{normalized_name}' уже существует.",
                reply_markup=get_back_keyboard()
            )
        return ConversationHandler.END
    
    try:
        db.add_city(normalized_name)
        await update.message.reply_text(
            f"✅ Город <b>'{normalized_name}'</b> успешно добавлен!",
            reply_markup=get_back_keyboard(),
            parse_mode=ParseMode.HTML
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ {str(e)}", reply_markup=get_back_keyboard())
    
    return ConversationHandler.END


async def delete_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление города"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    
    if not city:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())
        return
    
    # Подтверждение удаления
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_city_{city_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data="manage_cities")
        ]
    ]
    
    await query.edit_message_text(
        f"⚠️ Вы уверены, что хотите удалить город '{city.name}'?\n\n"
        "Это также удалит всех людей и графики, связанные с этим городом.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def confirm_delete_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления города"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    
    if city:
        db.delete_city(city_id)
        await query.edit_message_text(
            f"✅ Город '{city.name}' удалён.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="manage_cities")]])
        )
    else:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())


async def clear_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистка графика (сегодня или завтра)"""
    query = update.callback_query
    await query.answer()
    
    # Парсим callback_data: clear_schedule_today_{city_id} или clear_schedule_tomorrow_{city_id}
    parts = query.data.split("_")
    schedule_type = parts[2]  # "today" или "tomorrow"
    city_id = int(parts[-1])
    
    city = db.get_city(city_id)
    
    if not city:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())
        return
    
    # Очищаем график
    try:
        db.clear_schedule(city_id, schedule_type)
        schedule_type_ukr = "сьогодні" if schedule_type == "today" else "завтра"
        await query.edit_message_text(
            f"✅ График на {schedule_type_ukr} для города '{city.name}' очищен.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"city_details_{city_id}")]])
        )
    except Exception as e:
        logger.error(f"Ошибка при очистке графика: {e}")
        await query.edit_message_text(
            f"❌ Ошибка при очистке графика: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"city_details_{city_id}")]])
        )


async def city_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Детали города - меню с опциями"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    
    if not city:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())
        return
    
    # Получаем информацию о канале
    channel = db.get_channel(city.id)
    channel_text = f"📱 <b>Канал:</b> @{channel.channel_username}" if channel else "📱 <b>Канал:</b> Не настроен"
    
    # Получаем количество людей в городе
    people = db.get_people(city.id)
    people_count = len(people)
    
    # Получаем информацию о графике (проверяем оба графика)
    schedules = db.get_both_schedules(city.id)
    today_count = len(schedules.get("today") or {})
    tomorrow_count = len(schedules.get("tomorrow") or {})
    if today_count > 0 or tomorrow_count > 0:
        schedule_text = f"✅ Есть график (сегодня: {today_count} групп, завтра: {tomorrow_count} групп)"
    else:
        schedule_text = "❌ График не загружен"
    
    keyboard = [
        [InlineKeyboardButton("✏️ Редактировать название", callback_data=f"edit_city_name_{city_id}")],
        [InlineKeyboardButton("📱 Управление каналом", callback_data=f"manage_channel_{city_id}")],
        [InlineKeyboardButton("🗑️ Очистить график (сегодня)", callback_data=f"clear_schedule_today_{city_id}")],
        [InlineKeyboardButton("🗑️ Очистить график (завтра)", callback_data=f"clear_schedule_tomorrow_{city_id}")],
        [InlineKeyboardButton("🗑️ Удалить город", callback_data=f"delete_city_{city_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="manage_cities")]
    ]
    
    await query.edit_message_text(
        f"🏙️ <b>Город: {city.name}</b>\n\n"
        f"{channel_text}\n"
        f"👥 <b>Людей:</b> {people_count}\n"
        f"📅 <b>График:</b> {schedule_text}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def edit_city_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало редактирования названия города"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    
    if not city:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())
        return
    
    # Сохраняем city_id в контексте
    user_id = update.effective_user.id
    if user_id not in user_context:
        user_context[user_id] = {}
    user_context[user_id]["edit_city_id"] = city_id
    
    await query.edit_message_text(
        f"✏️ <b>Редактировать название города</b>\n\n"
        f"Текущее название: <b>{city.name}</b>\n\n"
        f"Введите новое название города:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=f"city_details_{city_id}")]]),
        parse_mode=ParseMode.HTML
    )
    return WAITING_EDIT_CITY_NAME


async def edit_city_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нового названия города"""
    user_id = update.effective_user.id
    city_id = user_context.get(user_id, {}).get("edit_city_id")
    
    if not city_id:
        await update.message.reply_text("❌ Ошибка: не найден ID города. Попробуйте снова.")
        return ConversationHandler.END
    
    city = db.get_city(city_id)
    if not city:
        await update.message.reply_text("❌ Город не найден.")
        return ConversationHandler.END
    
    new_name = update.message.text.strip()
    
    if not new_name:
        await update.message.reply_text("❌ Название города не может быть пустым. Попробуйте снова:")
        return WAITING_EDIT_CITY_NAME
    
    # Нормализуем название
    normalized_name = db.normalize_city_name(new_name)
    
    # Проверяем на дубликаты (исключая текущий город)
    if db.city_exists(normalized_name):
        existing_city = next((c for c in db.get_cities() if c.name.lower() == normalized_name.lower() and c.id != city_id), None)
        if existing_city:
            await update.message.reply_text(
                f"❌ Город <b>'{normalized_name}'</b> уже существует.",
                parse_mode=ParseMode.HTML
            )
            return WAITING_EDIT_CITY_NAME
    
    # Обновляем название города
    try:
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE cities SET name = ? WHERE id = ?", (normalized_name, city_id))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            f"✅ Название города обновлено: <b>{city.name}</b> → <b>{normalized_name}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"city_details_{city_id}")]]),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при обновлении: {e}")
    
    # Очищаем контекст
    if user_id in user_context:
        user_context[user_id].pop("edit_city_id", None)
    
    return ConversationHandler.END


async def manage_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления каналом для города"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    
    if not city:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())
        return
    
    channel = db.get_channel(city.id)
    
    keyboard = []
    if channel:
        keyboard.append([InlineKeyboardButton("✏️ Изменить канал", callback_data=f"edit_channel_{city_id}")])
        keyboard.append([InlineKeyboardButton("🗑️ Удалить канал", callback_data=f"delete_channel_{city_id}")])
    else:
        keyboard.append([InlineKeyboardButton("➕ Добавить канал", callback_data=f"add_channel_{city_id}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"city_details_{city_id}")])
    
    channel_text = f"📱 <b>Текущий канал:</b> @{channel.channel_username}" if channel else "📱 <b>Канал не настроен</b>\n\nДля автоматического мониторинга графиков добавьте канал."
    
    await query.edit_message_text(
        f"📱 <b>Управление каналом</b>\n\n"
        f"Город: <b>{city.name}</b>\n\n"
        f"{channel_text}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def add_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления канала"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    
    if not city:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())
        return
    
    # Сохраняем city_id в контексте
    user_id = update.effective_user.id
    if user_id not in user_context:
        user_context[user_id] = {}
    user_context[user_id]["add_channel_city_id"] = city_id
    
    await query.edit_message_text(
        f"➕ <b>Добавить канал</b>\n\n"
        f"Город: <b>{city.name}</b>\n\n"
        f"Отправьте username канала или ссылку на канал.\n\n"
        f"<i>Примеры:</i>\n"
        f"• dtekoficial\n"
        f"• @dtekoficial\n"
        f"• https://t.me/dtekoficial",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=f"manage_channel_{city_id}")]]),
        parse_mode=ParseMode.HTML
    )
    return WAITING_CHANNEL_USERNAME


async def edit_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало редактирования канала"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    channel = db.get_channel(city_id)
    
    if not city:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())
        return
    
    if not channel:
        await query.edit_message_text("❌ Канал не найден.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"manage_channel_{city_id}")]]))
        return
    
    # Сохраняем city_id в контексте
    user_id = update.effective_user.id
    if user_id not in user_context:
        user_context[user_id] = {}
    user_context[user_id]["edit_channel_city_id"] = city_id
    
    await query.edit_message_text(
        f"✏️ <b>Изменить канал</b>\n\n"
        f"Город: <b>{city.name}</b>\n"
        f"Текущий канал: @{channel.channel_username}\n\n"
        f"Отправьте новый username канала или ссылку на канал.\n\n"
        f"<i>Примеры:</i>\n"
        f"• dtekoficial\n"
        f"• @dtekoficial\n"
        f"• https://t.me/dtekoficial",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=f"manage_channel_{city_id}")]]),
        parse_mode=ParseMode.HTML
    )
    return WAITING_CHANNEL_USERNAME


async def process_channel_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка username канала"""
    user_id = update.effective_user.id
    user_ctx = user_context.get(user_id, {})
    
    # Определяем, это добавление или редактирование
    city_id = user_ctx.get("add_channel_city_id") or user_ctx.get("edit_channel_city_id")
    is_edit = "edit_channel_city_id" in user_ctx
    
    if not city_id:
        await update.message.reply_text("❌ Ошибка: не найден ID города. Попробуйте снова.")
        return ConversationHandler.END
    
    city = db.get_city(city_id)
    if not city:
        await update.message.reply_text("❌ Город не найден.")
        return ConversationHandler.END
    
    # Извлекаем username из текста
    text = update.message.text.strip()
    logger.info(f"📥 Получен текст для обработки канала: '{text}'")
    
    # Обрабатываем разные форматы:
    # - t.me/pupunyadtek → pupunyadtek
    # - https://t.me/pupunyadtek → pupunyadtek
    # - http://t.me/pupunyadtek → pupunyadtek
    # - @pupunyadtek → pupunyadtek
    # - pupunyadtek → pupunyadtek
    
    username = text.strip()
    
    # Убираем протокол если есть
    username = username.replace("https://", "").replace("http://", "")
    
    # Если это ссылка (t.me/), извлекаем username
    if "t.me/" in username.lower():
        # Находим позицию t.me/ и берем всё после неё (сохраняем оригинальный регистр)
        idx = username.lower().find("t.me/")
        if idx != -1:
            username = username[idx + len("t.me/"):]  # Берём всё после t.me/
            logger.info(f"   🔍 Извлечён username из ссылки: '{username}'")
        else:
            # Если формат нестандартный, пробуем другой способ
            username = username.split("/")[-1]
    
    # Убираем параметры запроса если есть (например, ?start=123)
    username = username.split("?")[0]
    
    # Убираем слэш в конце если есть
    username = username.rstrip("/")
    
    # Убираем @ если есть
    username = username.replace("@", "").strip()
    
    logger.info(f"   ✅ Итоговый username: '{username}'")
    
    if not username:
        await update.message.reply_text("❌ Username канала не может быть пустым. Попробуйте снова:")
        return WAITING_CHANNEL_USERNAME
    
    # Проверяем, что канал не заблокирован
    if username.lower() in [ch.lower() for ch in BLOCKED_CHANNELS]:
        await update.message.reply_text(
            f"🚫 Канал <b>@{username}</b> заблокирован и не может быть добавлен в систему мониторинга.",
            parse_mode=ParseMode.HTML
        )
        return WAITING_CHANNEL_USERNAME
    
    # Проверяем, что username не содержит запрещённых символов
    if not username.replace("_", "").replace("-", "").isalnum():
        await update.message.reply_text("❌ Неверный формат username. Используйте только буквы, цифры, подчёркивания и дефисы.")
        return WAITING_CHANNEL_USERNAME
    
    try:
        logger.info(f"💾 Сохранение канала: city_id={city_id}, username='{username}', is_edit={is_edit}")
        
        if is_edit:
            # Обновляем канал
            db.update_channel(city_id, username)
            logger.info(f"✅ Канал обновлён успешно")
            await update.message.reply_text(
                f"✅ Канал обновлён для города <b>{city.name}</b>:\n"
                f"📱 @{username}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"manage_channel_{city_id}")]]),
                parse_mode=ParseMode.HTML
            )
        else:
            # Добавляем канал
            db.add_channel(city_id, username)
            logger.info(f"✅ Канал добавлен успешно")
            
            # Перезапускаем мониторинг, чтобы подхватить новый канал
            api_id = os.getenv("TELEGRAM_API_ID")
            api_hash = os.getenv("TELEGRAM_API_HASH")
            
            monitor_message = ""
            if api_id and api_hash:
                try:
                    global monitor_thread, monitor_instance
                    from channel_monitor import start_monitor_task
                    
                    # Останавливаем старый мониторинг, если он запущен
                    if monitor_instance and hasattr(monitor_instance, 'client') and monitor_instance.client:
                        logger.info("🔄 Останавливаю старый мониторинг для перезапуска...")
                        try:
                            # Устанавливаем флаг остановки
                            monitor_instance.is_running = False
                            # Отключаем клиент - это остановит мониторинг
                            import asyncio
                            # Создаём новый event loop для остановки
                            stop_loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(stop_loop)
                            try:
                                stop_loop.run_until_complete(monitor_instance.client.disconnect())
                            except:
                                pass
                            finally:
                                stop_loop.close()
                        except Exception as e:
                            logger.warning(f"Не удалось корректно остановить мониторинг: {e}")
                        
                        # Ждём немного, чтобы поток завершился
                        import time
                        time.sleep(2)
                    
                    # Запускаем новый мониторинг
                    logger.info("🔄 Запускаю мониторинг с новым каналом...")
                    monitor_instance_ref = [None]  # Используем список для передачи по ссылке
                    monitor_thread = start_monitor_task(db, context.application, monitor_instance_ref)
                    
                    # Ждём, пока monitor_instance будет создан
                    import time
                    for _ in range(10):  # Ждём до 2 секунд
                        if monitor_instance_ref[0] is not None:
                            monitor_instance = monitor_instance_ref[0]
                            break
                        time.sleep(0.2)
                    
                    if monitor_instance_ref[0]:
                        monitor_instance = monitor_instance_ref[0]
                        monitor_message = "\n\n✅ <b>Мониторинг автоматически перезапущен!</b> Новый канал уже отслеживается.\n\n<i>💡 Мониторинг поддерживает несколько каналов одновременно.</i>"
                    else:
                        monitor_message = "\n\n⚠️ <i>Мониторинг запускается... Если не заработает, напишите Лёхе, чтобы перезапустил бота.</i>\n\n<i>💡 Мониторинг поддерживает несколько каналов одновременно.</i>"
                except Exception as e:
                    logger.error(f"Ошибка при перезапуске мониторинга: {e}", exc_info=True)
                    monitor_message = "\n\n⚠️ <i>Не удалось автоматически перезапустить мониторинг. Напишите Лёхе, чтобы перезапустил бота.</i>\n\n<i>💡 Мониторинг поддерживает несколько каналов одновременно.</i>"
            else:
                monitor_message = "\n\n⚠️ <i>Мониторинг не настроен (нет API_ID/API_HASH). Напишите Лёхе, чтобы настроил и перезапустил бота.</i>"
            
            await update.message.reply_text(
                f"✅ Канал добавлен для города <b>{city.name}</b>:\n"
                f"📱 @{username}"
                f"{monitor_message}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"manage_channel_{city_id}")]]),
                parse_mode=ParseMode.HTML
            )
    except ValueError as e:
        logger.error(f"❌ ValueError при сохранении канала: {e}")
        await update.message.reply_text(
            f"❌ Ошибка: {str(e)}\n\n"
            f"Проверьте, что канал указан правильно.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"❌ Исключение при сохранении канала: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Ошибка при сохранении канала: {e}\n\n"
            f"Попробуйте ещё раз или обратитесь к администратору.",
            parse_mode=ParseMode.HTML
        )
    
    # Очищаем контекст
    if user_id in user_context:
        user_context[user_id].pop("add_channel_city_id", None)
        user_context[user_id].pop("edit_channel_city_id", None)
    
    return ConversationHandler.END


async def delete_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление канала"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    channel = db.get_channel(city_id)
    
    if not city:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())
        return
    
    if not channel:
        await query.edit_message_text("❌ Канал не найден.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"manage_channel_{city_id}")]]))
        return
    
    # Подтверждение удаления
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_channel_{city_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"manage_channel_{city_id}")
        ]
    ]
    
    await query.edit_message_text(
        f"⚠️ Вы уверены, что хотите удалить канал @{channel.channel_username} для города '{city.name}'?\n\n"
        f"Мониторинг этого канала будет остановлен.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def confirm_delete_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления канала"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    channel = db.get_channel(city_id)
    
    if city and channel:
        db.delete_channel(city_id)
        await query.edit_message_text(
            f"✅ Канал @{channel.channel_username} удалён для города '{city.name}'.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"manage_channel_{city_id}")]])
        )
    else:
        await query.edit_message_text("❌ Канал не найден.", reply_markup=get_back_keyboard())


# ========== УПРАВЛЕНИЕ ЛЮДЬМИ ==========

async def manage_people(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления людьми"""
    query = update.callback_query
    await query.answer()
    
    cities = db.get_cities()
    if not cities:
        await query.edit_message_text(
            "❌ Сначала добавьте город в разделе 'Управление городами'.",
            reply_markup=get_back_keyboard()
        )
        return
    
    keyboard = [
        [InlineKeyboardButton("➕ Добавить человека", callback_data="add_person")],
        [InlineKeyboardButton("✏️ Редактировать человека", callback_data="edit_person")],
        [InlineKeyboardButton("🗑️ Удалить человека", callback_data="delete_person")],
        [InlineKeyboardButton("👥 Создать группу людей", callback_data="create_group")],
        [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        "👤 <b>Управление людьми</b>\n\n"
        "Выберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def add_person_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления человека - выбор города"""
    query = update.callback_query
    await query.answer()
    
    cities = db.get_cities()
    if not cities:
        await query.edit_message_text(
            "❌ Сначала добавьте город.",
            reply_markup=get_back_keyboard()
        )
        return
    
    keyboard = []
    for city in cities:
        keyboard.append([InlineKeyboardButton(
            city.name,
            callback_data=f"add_person_city_{city.id}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="manage_people")])
    
    await query.edit_message_text(
        "👤 <b>Добавить человека</b>\n\n"
        "Выберите город:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def add_person_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор города для добавления человека"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    user_id = update.effective_user.id
    
    if user_id not in user_context:
        user_context[user_id] = {}
    user_context[user_id]["add_person_city_id"] = city_id
    
    city = db.get_city(city_id)
    await query.edit_message_text(
        f"👤 <b>Добавить человека</b>\n\n"
        f"Город: <b>{city.name}</b>\n\n"
        "Введите имя человека:",
        reply_markup=get_back_keyboard(),
        parse_mode=ParseMode.HTML
    )
    return WAITING_PERSON_NAME


async def add_person_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка имени человека"""
    person_name = update.message.text.strip()
    user_id = update.effective_user.id
    
    if not person_name:
        await update.message.reply_text("❌ Имя не может быть пустым. Попробуйте снова:")
        return WAITING_PERSON_NAME
    
    if user_id not in user_context or "add_person_city_id" not in user_context[user_id]:
        await update.message.reply_text("❌ Ошибка. Начните заново.", reply_markup=get_back_keyboard())
        return ConversationHandler.END
    
    user_context[user_id]["add_person_name"] = person_name
    city_id = user_context[user_id]["add_person_city_id"]
    
    # Создаем клавиатуру для выбора группы
    keyboard = []
    for i in range(0, len(ELECTRICITY_GROUPS), 2):
        row = []
        for j in range(2):
            if i + j < len(ELECTRICITY_GROUPS):
                group = ELECTRICITY_GROUPS[i + j]
                row.append(InlineKeyboardButton(group, callback_data=f"add_person_group_{group}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="add_person")])
    
    await update.message.reply_text(
        f"👤 <b>Добавить человека</b>\n\n"
        f"Имя: <b>{person_name}</b>\n\n"
        "Выберите группу электричества:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return WAITING_PERSON_GROUP


async def add_person_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора группы и сохранение человека"""
    query = update.callback_query
    await query.answer()
    
    group = query.data.split("_")[-1]
    user_id = update.effective_user.id
    
    if user_id not in user_context:
        await query.edit_message_text("❌ Ошибка. Начните заново.", reply_markup=get_back_keyboard())
        return ConversationHandler.END
    
    city_id = user_context[user_id]["add_person_city_id"]
    person_name = user_context[user_id]["add_person_name"]
    
    try:
        db.add_person(person_name, city_id, group)
        city = db.get_city(city_id)
        await query.edit_message_text(
            f"✅ Человек '{person_name}' (группа {group}) успешно добавлен в город '{city.name}'!",
            reply_markup=get_back_keyboard()
        )
        # Очищаем контекст
        if user_id in user_context:
            user_context[user_id].pop("add_person_city_id", None)
            user_context[user_id].pop("add_person_name", None)
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка: {str(e)}", reply_markup=get_back_keyboard())
    
    return ConversationHandler.END


async def edit_person_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало редактирования - выбор города"""
    query = update.callback_query
    await query.answer()
    
    cities = db.get_cities()
    if not cities:
        await query.edit_message_text("❌ Нет городов.", reply_markup=get_back_keyboard())
        return
    
    keyboard = []
    for city in cities:
        keyboard.append([InlineKeyboardButton(
            city.name,
            callback_data=f"edit_person_city_{city.id}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="manage_people")])
    
    await query.edit_message_text(
        "✏️ <b>Редактировать человека</b>\n\n"
        "Выберите город:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def edit_person_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор города для редактирования"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    people = db.get_people(city_id)
    
    if not people:
        await query.edit_message_text(
            "❌ В этом городе нет людей.",
            reply_markup=get_back_keyboard()
        )
        return
    
    keyboard = []
    for person in people:
        keyboard.append([InlineKeyboardButton(
            f"{person.name} (группа {person.group})",
            callback_data=f"edit_person_select_{person.id}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="edit_person")])
    
    await query.edit_message_text(
        "✏️ <b>Редактировать человека</b>\n\n"
        "Выберите человека:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def edit_person_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор человека для редактирования"""
    query = update.callback_query
    await query.answer()
    
    person_id = int(query.data.split("_")[-1])
    person = db.get_person(person_id)
    
    if not person:
        await query.edit_message_text("❌ Человек не найден.", reply_markup=get_back_keyboard())
        return
    
    user_id = update.effective_user.id
    if user_id not in user_context:
        user_context[user_id] = {}
    user_context[user_id]["edit_person_id"] = person_id
    
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить имя", callback_data="edit_person_change_name")],
        [InlineKeyboardButton("✏️ Изменить группу", callback_data="edit_person_change_group")],
        [InlineKeyboardButton("🔙 Назад", callback_data="edit_person")]
    ]
    
    city = db.get_city(person.city_id)
    await query.edit_message_text(
        f"✏️ <b>Редактировать человека</b>\n\n"
        f"👤 Имя: <b>{person.name}</b>\n"
        f"🏙️ Город: <b>{city.name}</b>\n"
        f"⚡ Группа: <b>{person.group}</b>\n\n"
        "Что хотите изменить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def edit_person_change_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Изменение имени"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "✏️ <b>Изменить имя</b>\n\n"
        "Введите новое имя:",
        reply_markup=get_back_keyboard(),
        parse_mode=ParseMode.HTML
    )
    return WAITING_EDIT_PERSON_NAME


async def edit_person_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нового имени"""
    new_name = update.message.text.strip()
    user_id = update.effective_user.id
    
    if not new_name:
        await update.message.reply_text("❌ Имя не может быть пустым. Попробуйте снова:")
        return WAITING_EDIT_PERSON_NAME
    
    if user_id not in user_context or "edit_person_id" not in user_context[user_id]:
        await update.message.reply_text("❌ Ошибка. Начните заново.", reply_markup=get_back_keyboard())
        return ConversationHandler.END
    
    person_id = user_context[user_id]["edit_person_id"]
    db.update_person(person_id, name=new_name)
    
    person = db.get_person(person_id)
    city = db.get_city(person.city_id)
    
    await update.message.reply_text(
        f"✅ Имя обновлено!\n\n"
        f"👤 {person.name}\n"
        f"🏙️ {city.name}\n"
        f"⚡ Группа: {person.group}",
        reply_markup=get_back_keyboard()
    )
    
    user_context[user_id].pop("edit_person_id", None)
    return ConversationHandler.END


async def edit_person_change_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Изменение группы"""
    query = update.callback_query
    await query.answer()
    
    keyboard = []
    for i in range(0, len(ELECTRICITY_GROUPS), 2):
        row = []
        for j in range(2):
            if i + j < len(ELECTRICITY_GROUPS):
                group = ELECTRICITY_GROUPS[i + j]
                row.append(InlineKeyboardButton(group, callback_data=f"edit_person_group_{group}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="edit_person")])
    
    await query.edit_message_text(
        "✏️ <b>Изменить группу</b>\n\n"
        "Выберите новую группу:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return WAITING_EDIT_PERSON_GROUP


async def edit_person_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка новой группы"""
    query = update.callback_query
    await query.answer()
    
    new_group = query.data.split("_")[-1]
    user_id = update.effective_user.id
    
    if user_id not in user_context or "edit_person_id" not in user_context[user_id]:
        await query.edit_message_text("❌ Ошибка. Начните заново.", reply_markup=get_back_keyboard())
        return ConversationHandler.END
    
    person_id = user_context[user_id]["edit_person_id"]
    db.update_person(person_id, group=new_group)
    
    person = db.get_person(person_id)
    city = db.get_city(person.city_id)
    
    await query.edit_message_text(
        f"✅ Группа обновлена!\n\n"
        f"👤 {person.name}\n"
        f"🏙️ {city.name}\n"
        f"⚡ Группа: {person.group}",
        reply_markup=get_back_keyboard()
    )
    
    user_context[user_id].pop("edit_person_id", None)
    return ConversationHandler.END


async def delete_person_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало удаления - выбор города"""
    query = update.callback_query
    await query.answer()
    
    cities = db.get_cities()
    if not cities:
        await query.edit_message_text("❌ Нет городов.", reply_markup=get_back_keyboard())
        return
    
    keyboard = []
    for city in cities:
        keyboard.append([InlineKeyboardButton(
            city.name,
            callback_data=f"delete_person_city_{city.id}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="manage_people")])
    
    await query.edit_message_text(
        "🗑️ <b>Удалить человека</b>\n\n"
        "Выберите город:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def delete_person_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор города для удаления"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    people = db.get_people(city_id)
    
    if not people:
        await query.edit_message_text(
            "❌ В этом городе нет людей.",
            reply_markup=get_back_keyboard()
        )
        return
    
    keyboard = []
    for person in people:
        keyboard.append([InlineKeyboardButton(
            f"{person.name} (группа {person.group})",
            callback_data=f"delete_person_select_{person.id}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="delete_person")])
    
    await query.edit_message_text(
        "🗑️ <b>Удалить человека</b>\n\n"
        "Выберите человека:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def delete_person_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления человека"""
    query = update.callback_query
    await query.answer()
    
    person_id = int(query.data.split("_")[-1])
    person = db.get_person(person_id)
    
    if not person:
        await query.edit_message_text("❌ Человек не найден.", reply_markup=get_back_keyboard())
        return
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_person_{person_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data="delete_person")
        ]
    ]
    
    city = db.get_city(person.city_id)
    await query.edit_message_text(
        f"⚠️ Вы уверены, что хотите удалить '{person.name}' из города '{city.name}'?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def confirm_delete_person(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления человека"""
    query = update.callback_query
    await query.answer()
    
    person_id = int(query.data.split("_")[-1])
    person = db.get_person(person_id)
    
    if person:
        db.delete_person(person_id)
        await query.edit_message_text(
            f"✅ Человек '{person.name}' удалён.",
            reply_markup=get_back_keyboard()
        )
    else:
        await query.edit_message_text("❌ Человек не найден.", reply_markup=get_back_keyboard())


# ========== УПРАВЛЕНИЕ ГРУППОЙ ЛЮДЕЙ ==========

async def create_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало создания группы - показывает список всех людей"""
    query = update.callback_query
    await query.answer()
    
    # Получаем всех людей из всех городов
    all_people = db.get_people()
    
    if not all_people:
        await query.edit_message_text(
            "❌ Нет людей в базе данных.\n\nСначала добавьте людей через 'Управление людьми' → 'Добавить человека'.",
            reply_markup=get_back_keyboard(),
            parse_mode=ParseMode.HTML
        )
        return
    
    user_id = update.effective_user.id
    if user_id not in user_context:
        user_context[user_id] = {}
    user_context[user_id]["group_selected_people"] = []
    
    # Формируем список людей с информацией о городе
    keyboard = []
    for person in all_people:
        city = db.get_city(person.city_id)
        city_name = city.name if city else f"ID {person.city_id}"
        # Используем префикс для отслеживания выбранных людей
        keyboard.append([InlineKeyboardButton(
            f"☐ {person.name} ({city_name}, группа {person.group})",
            callback_data=f"group_toggle_{person.id}"
        )])
    
    keyboard.append([InlineKeyboardButton("✅ Создать группу", callback_data="group_create")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="manage_people")])
    
    await query.edit_message_text(
        "👥 <b>Создать группу людей</b>\n\n"
        "Выберите людей для группы (можно выбрать несколько):\n\n"
        f"Всего людей: {len(all_people)}\n"
        f"Выбрано: 0",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def group_toggle_person(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение выбора человека в группе"""
    query = update.callback_query
    await query.answer()
    
    person_id = int(query.data.split("_")[-1])
    user_id = update.effective_user.id
    
    if user_id not in user_context:
        user_context[user_id] = {}
    if "group_selected_people" not in user_context[user_id]:
        user_context[user_id]["group_selected_people"] = []
    
    selected = user_context[user_id]["group_selected_people"]
    
    # Переключаем выбор
    if person_id in selected:
        selected.remove(person_id)
    else:
        selected.append(person_id)
    
    # Получаем всех людей
    all_people = db.get_people()
    
    # Формируем обновленный список
    keyboard = []
    for person in all_people:
        city = db.get_city(person.city_id)
        city_name = city.name if city else f"ID {person.city_id}"
        is_selected = person.id in selected
        prefix = "☑" if is_selected else "☐"
        keyboard.append([InlineKeyboardButton(
            f"{prefix} {person.name} ({city_name}, группа {person.group})",
            callback_data=f"group_toggle_{person.id}"
        )])
    
    keyboard.append([InlineKeyboardButton("✅ Создать группу", callback_data="group_create")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="manage_people")])
    
    await query.edit_message_text(
        "👥 <b>Создать группу людей</b>\n\n"
        "Выберите людей для группы (можно выбрать несколько):\n\n"
        f"Всего людей: {len(all_people)}\n"
        f"Выбрано: {len(selected)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def group_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание группы из выбранных людей"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    if user_id not in user_context or "group_selected_people" not in user_context[user_id]:
        await query.edit_message_text(
            "❌ Ошибка. Начните заново.",
            reply_markup=get_back_keyboard(),
            parse_mode=ParseMode.HTML
        )
        return
    
    selected_people_ids = user_context[user_id]["group_selected_people"]
    
    if not selected_people_ids:
        await query.edit_message_text(
            "❌ Выберите хотя бы одного человека для группы.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="create_group")
            ]]),
            parse_mode=ParseMode.HTML
        )
        return
    
    # Создаем группу (старая автоматически удаляется)
    success = db.create_person_group(selected_people_ids)
    
    if success:
        # Получаем имена выбранных людей для сообщения
        selected_people = [db.get_person(pid) for pid in selected_people_ids]
        people_names = [p.name for p in selected_people if p]
        
        await query.edit_message_text(
            f"✅ <b>Группа создана!</b>\n\n"
            f"В группе {len(selected_people_ids)} человек:\n"
            f"{', '.join(people_names)}\n\n"
            f"Теперь вы можете посмотреть график группы через главное меню.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="manage_people")
            ]]),
            parse_mode=ParseMode.HTML
        )
        
        # Очищаем контекст
        user_context[user_id].pop("group_selected_people", None)
    else:
        await query.edit_message_text(
            "❌ Ошибка при создании группы. Попробуйте снова.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="create_group")
            ]]),
            parse_mode=ParseMode.HTML
        )


async def view_group_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает график для всех людей из группы"""
    query = update.callback_query
    await query.answer()
    
    # Проверяем, есть ли группа
    if not db.has_person_group():
        await query.edit_message_text(
            "❌ Группа не создана.\n\n"
            "Создайте группу через 'Управление людьми' → 'Создать группу людей'.",
            reply_markup=get_back_keyboard(),
            parse_mode=ParseMode.HTML
        )
        return
    
    # Генерируем сообщение с графиками группы
    try:
        message = generate_group_schedule_message(db)
        
        await query.edit_message_text(
            message,
            reply_markup=get_back_keyboard(),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Ошибка при генерации графика группы: {e}", exc_info=True)
        await query.edit_message_text(
            f"❌ Ошибка при формировании графика группы: {str(e)}",
            reply_markup=get_back_keyboard(),
            parse_mode=ParseMode.HTML
        )


# ========== ЗАГРУЗКА ГРАФИКА ==========

async def upload_schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало загрузки графика - выбор города"""
    query = update.callback_query
    await query.answer()
    
    cities = db.get_cities()
    if not cities:
        await query.edit_message_text(
            "❌ Сначала добавьте город.",
            reply_markup=get_back_keyboard()
        )
        return
    
    keyboard = []
    for city in cities:
        keyboard.append([InlineKeyboardButton(
            city.name,
            callback_data=f"upload_schedule_city_{city.id}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
    
    await query.edit_message_text(
        "📸 <b>Загрузить график</b>\n\n"
        "Выберите город:\n\n"
        "Затем отправьте 1-3 фото с графиками отключений.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def upload_schedule_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор города для загрузки графика - показываем опции"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    user_id = update.effective_user.id
    
    if user_id not in user_context:
        user_context[user_id] = {}
    user_context[user_id]["upload_schedule_city_id"] = city_id
    
    city = db.get_city(city_id)
    
    # Проверяем, доступен ли поиск в канале
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    has_channel_access = bool(api_id and api_hash)
    
    keyboard = [
        [InlineKeyboardButton("📤 Загрузить свой график", callback_data=f"upload_manual_{city_id}")],
        [InlineKeyboardButton("🔍 Найти график в Telegram-канале", callback_data=f"upload_from_channel_{city_id}")],
        [InlineKeyboardButton("📋 Проверить графики", callback_data=f"check_schedule_photos_{city_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="upload_schedule")]
    ]
    
    # Формируем текст с информацией о доступности поиска
    help_text = ""
    if not has_channel_access:
        help_text = "\n\n⚠️ <i>Для поиска в канале нужно настроить TELEGRAM_API_ID и TELEGRAM_API_HASH</i>"
    
    await query.edit_message_text(
        f"📸 <b>Загрузить график</b>\n\n"
        f"Город: <b>{city.name}</b>\n\n"
        "Выберите способ загрузки:"
        f"{help_text}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def upload_manual_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало ручной загрузки графика"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    user_id = update.effective_user.id
    
    if user_id not in user_context:
        user_context[user_id] = {}
    user_context[user_id]["upload_schedule_city_id"] = city_id
    user_context[user_id]["upload_schedule_photos"] = []  # Будет хранить байты фото
    user_context[user_id]["upload_schedule_mime_types"] = []  # Будет хранить MIME типы
    
    city = db.get_city(city_id)
    await query.edit_message_text(
        f"📤 <b>Загрузить свой график</b>\n\n"
        f"Город: <b>{city.name}</b>\n\n"
        "Отправьте 1-3 фото с графиками отключений ИЛИ текст с графиком.\n\n"
        "💡 <b>Важно:</b> Если отправляете несколько фото, они будут обработаны все вместе после нажатия кнопки '✅ Готово'.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Готово", callback_data="upload_schedule_done")
        ]]),
        parse_mode=ParseMode.HTML
    )
    return WAITING_SCHEDULE_PHOTO


async def upload_from_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск и загрузка графика из Telegram-канала"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    city = db.get_city(city_id)
    
    # Получаем канал для города из базы данных
    channel = db.get_channel(city_id)
    if not channel:
        await query.edit_message_text(
            f"❌ <b>Канал не настроен</b>\n\n"
            f"Для города <b>{city.name}</b> не настроен Telegram-канал.\n\n"
            f"Добавьте канал через меню управления городами.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data=f"upload_schedule_city_{city_id}")
            ]]),
            parse_mode=ParseMode.HTML
        )
        return
    
    channel_username = channel.channel_username
    
    # Проверяем доступ к каналу
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    
    if not api_id or not api_hash:
        await query.edit_message_text(
            "❌ <b>Поиск в канале недоступен</b>\n\n"
            "Для работы этой функции необходимо настроить:\n"
            "• TELEGRAM_API_ID\n"
            "• TELEGRAM_API_HASH\n\n"
            "Получите их на https://my.telegram.org\n\n"
            "После настройки перезапустите бота.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data=f"upload_schedule_city_{city_id}")
            ]]),
            parse_mode=ParseMode.HTML
        )
        return
    
    # Сохраняем контекст для уведомления пользователя
    if user_id not in user_context:
        user_context[user_id] = {}
    user_context[user_id]["upload_from_channel_city_id"] = city_id
    user_context[user_id]["upload_from_channel_chat_id"] = chat_id
    
    await query.edit_message_text(
        f"🔍 <b>Поиск графика в канале</b>\n\n"
        f"Город: <b>{city.name}</b>\n"
        f"📱 Канал: @{channel_username}\n\n"
        f"⚙️ Ищу последний пост с графиком...\n"
        "Это может занять несколько секунд.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Обработка...", callback_data="wait")]]),
        parse_mode=ParseMode.HTML
    )
    
    # Запускаем поиск в фоне с таймаутом
    import asyncio
    async def run_with_timeout():
        try:
            # Таймаут 5 минут на всю операцию
            await asyncio.wait_for(
                find_and_process_channel_schedule(context, city_id, city.name, chat_id),
                timeout=300.0  # 5 минут
            )
        except asyncio.TimeoutError:
            logger.error("⏱️ Таймаут при поиске графика из канала (превышено 5 минут)")
            try:
                if context and hasattr(context, 'bot') and context.bot:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⏱️ Операция заняла слишком много времени и была прервана.\n\nПопробуйте ещё раз или загрузите график вручную.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"upload_schedule_city_{city_id}")]])
                    )
            except:
                pass
        except Exception as e:
            logger.error(f"Ошибка в задаче поиска графика: {e}", exc_info=True)
    
    asyncio.create_task(run_with_timeout())


async def check_schedule_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка графиков - показывает данные из базы и список людей"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    
    # Получаем оба графика из базы
    schedules = db.get_both_schedules(city_id)
    schedule_today = schedules.get("today") or {}
    schedule_tomorrow = schedules.get("tomorrow") or {}
    people = db.get_people(city_id)
    
    # Формируем информацию о графике
    if schedule_today or schedule_tomorrow:
        groups_today = sorted(schedule_today.keys()) if schedule_today else []
        groups_tomorrow = sorted(schedule_tomorrow.keys()) if schedule_tomorrow else []
        schedule_info = f"✅ <b>График загружен</b>\n\n"
        if schedule_today:
            schedule_info += f"📅 <b>Сегодня:</b> {len(groups_today)} групп\n"
            schedule_info += f"📋 Группы: {', '.join(groups_today[:10])}"
            if len(groups_today) > 10:
                schedule_info += f" ... и ещё {len(groups_today) - 10}"
            schedule_info += "\n\n"
        if schedule_tomorrow:
            schedule_info += f"📅 <b>Завтра:</b> {len(groups_tomorrow)} групп\n"
            schedule_info += f"📋 Группы: {', '.join(groups_tomorrow[:10])}"
            if len(groups_tomorrow) > 10:
                schedule_info += f" ... и ещё {len(groups_tomorrow) - 10}"
    else:
        schedule_info = "⚠️ <b>График не загружен</b>\n\nИспользуйте кнопку 'Найти график в Telegram-канале' для загрузки."
    
    # Формируем клавиатуру
    keyboard = []
    
    if people:
        # Показываем список людей для выбора графика
        for person in people:
            keyboard.append([
                InlineKeyboardButton(
                    f"📅 {person.name} (группа {person.group})",
                    callback_data=f"view_schedule_person_{person.id}"
                )
            ])
    else:
        # Если нет людей, предлагаем добавить
        keyboard.append([
            InlineKeyboardButton("👤 Добавить человека", callback_data="add_person")
        ])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"upload_schedule_city_{city_id}")])
    
    people_info = ""
    if people:
        people_info = f"\n\n👥 <b>Выберите человека для просмотра графика:</b>"
    else:
        people_info = f"\n\n⚠️ <b>В городе нет людей.</b>\nДобавьте человека, чтобы посмотреть график."
    
    await query.edit_message_text(
        f"📋 <b>Проверка графиков</b>\n\n"
        f"🏙️ Город: <b>{city.name}</b>\n\n"
        f"{schedule_info}"
        f"{people_info}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def send_schedule_photos_to_user(context: ContextTypes.DEFAULT_TYPE, city_id: int, city_name: str, chat_id: int):
    """Отправляет фото графиков пользователю и обрабатывает их через Gemini"""
    try:
        # Получаем фото из канала для конкретного города
        success, result, client = await get_schedule_photos_from_channel(city_id=city_id)
        
        if not success or not result:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Не найдено постов с графиками в канале.\n\nГород: <b>{city_name}</b>",
                parse_mode=ParseMode.HTML
            )
            if client:
                await client.disconnect()
            return
        
        message, text = result
        
        # Импортируем функцию обработки фото
        from channel_fetcher import download_and_process_photo_from_message
        
        # Обрабатываем фото через Gemini и сохраняем в базу
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚙️ Обрабатываю графики через Gemini...",
            parse_mode=ParseMode.HTML
        )
        
        process_success, process_message = await download_and_process_photo_from_message(
            message, client, city_id, city_name, db
        )
        
        # Скачиваем и отправляем фото пользователю
        photos_sent = 0
        
        try:
            # Если одно фото
            if message.photo:
                buffer = BytesIO()
                await message.download_media(file=buffer)
                photo_bytes = buffer.getvalue()
                
                if photo_bytes:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_bytes,
                        caption=f"📋 <b>График для {city_name}</b>\n\n{text[:200] if text else 'График отключений электричества'}",
                        parse_mode=ParseMode.HTML
                    )
                    photos_sent = 1
            
            # Если альбом (несколько фото)
            elif hasattr(message, 'grouped_id') and message.grouped_id:
                photo_list = []
                photo_count = 0
                
                # Получаем все сообщения из альбома
                async for msg in client.iter_messages(
                    message.peer_id,
                    min_id=message.id - 10,
                    max_id=message.id + 10
                ):
                    if (hasattr(msg, 'grouped_id') and 
                        msg.grouped_id == message.grouped_id and 
                        msg.photo):
                        photo_count += 1
                        buffer = BytesIO()
                        await msg.download_media(file=buffer)
                        photo_bytes = buffer.getvalue()
                        
                        if photo_bytes:
                            photo_list.append(photo_bytes)
                
                # Отправляем все фото
                if photo_list:
                    from telegram import InputMediaPhoto
                    media_group = []
                    for i, photo_bytes in enumerate(photo_list):
                        caption = f"📋 График для {city_name} ({i+1}/{len(photo_list)})" if i == 0 else None
                        media_group.append(InputMediaPhoto(media=photo_bytes, caption=caption))
                    
                    await context.bot.send_media_group(
                        chat_id=chat_id,
                        media=media_group
                    )
                    photos_sent = len(photo_list)
            
            # Отправляем итоговое сообщение
            if process_success:
                # Получаем список людей в городе
                people = db.get_people(city_id)
                
                # Формируем клавиатуру
                keyboard = []
                
                if people:
                    # Показываем список людей для выбора графика
                    for person in people:
                        keyboard.append([
                            InlineKeyboardButton(
                                f"📅 {person.name} (группа {person.group})",
                                callback_data=f"view_schedule_person_{person.id}"
                            )
                        ])
                else:
                    # Если нет людей, предлагаем добавить
                    keyboard.append([
                        InlineKeyboardButton("👤 Добавить человека", callback_data="add_person")
                    ])
                
                keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"upload_schedule_city_{city_id}")])
                
                people_info = ""
                if people:
                    people_info = f"\n\n👥 <b>Выберите человека для просмотра графика:</b>"
                else:
                    people_info = f"\n\n⚠️ <b>В городе нет людей.</b>\nДобавьте человека, чтобы посмотреть график."
                
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ <b>Графики обработаны!</b>\n\n"
                         f"{process_message}\n\n"
                         f"📸 Отправлено фото: <b>{photos_sent}</b>\n"
                         f"🏙️ Город: <b>{city_name}</b>"
                         f"{people_info}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ <b>Фото отправлены, но обработка не удалась</b>\n\n"
                         f"{process_message}\n\n"
                         f"📸 Отправлено фото: <b>{photos_sent}</b>\n"
                         f"🏙️ Город: <b>{city_name}</b>",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"upload_schedule_city_{city_id}")]]),
                    parse_mode=ParseMode.HTML
                )
        
        finally:
            if client:
                await client.disconnect()
    
    except Exception as e:
        logger.error(f"Ошибка при отправке фото: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Ошибка при получении фото: {str(e)}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"upload_schedule_city_{city_id}")]])
            )
        except:
            pass


async def handle_schedule_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка загруженного фото графика"""
    user_id = update.effective_user.id
    
    if user_id not in user_context or "upload_schedule_city_id" not in user_context[user_id]:
        await update.message.reply_text(
            "❌ Ошибка. Начните заново.",
            reply_markup=get_back_keyboard()
        )
        return ConversationHandler.END
    
    # Инициализируем список фото (байты), если его нет
    if "upload_schedule_photos" not in user_context[user_id]:
        user_context[user_id]["upload_schedule_photos"] = []
    
    # Инициализируем список MIME типов для фото
    if "upload_schedule_mime_types" not in user_context[user_id]:
        user_context[user_id]["upload_schedule_mime_types"] = []
    
    city_id = user_context[user_id]["upload_schedule_city_id"]
    city = db.get_city(city_id)
    
    # Проверяем, не превышен ли лимит фото
    photo_count = len(user_context[user_id]["upload_schedule_photos"])
    if photo_count >= 3:
        await update.message.reply_text(
            f"⚠️ Уже загружено максимальное количество фото (3).\n\n"
            f"Нажмите '✅ Готово' для обработки всех фото.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Готово", callback_data="upload_schedule_done")
            ]])
        )
        return WAITING_SCHEDULE_PHOTO
    
    photo = update.message.photo[-1]  # Берем фото наибольшего размера
    file = await context.bot.get_file(photo.file_id)
    
    try:
        # Скачиваем фото (НО НЕ ОБРАБАТЫВАЕМ СРАЗУ!)
        photo_bytearray = await file.download_as_bytearray()
        photo_bytes = bytes(photo_bytearray)  # Конвертируем bytearray в bytes
        
        # Определяем MIME тип
        mime_type = "image/jpeg"
        if file.file_path and file.file_path.endswith('.png'):
            mime_type = "image/png"
        
        # ВАЖНО: Сохраняем только байты фото во временное хранилище
        # Обработка через Gemini будет выполнена только после нажатия "Готово"
        user_context[user_id]["upload_schedule_photos"].append(photo_bytes)
        user_context[user_id]["upload_schedule_mime_types"].append(mime_type)
        
        photo_count = len(user_context[user_id]["upload_schedule_photos"])
        
        logger.info(f"📸 Фото {photo_count} сохранено во временное хранилище (город: {city.name})")
        
        # Показываем статус загрузки
        await update.message.reply_text(
            f"✅ Фото {photo_count} загружено!\n\n"
            f"📸 Загружено фото: {photo_count}/3\n\n"
            "Можете отправить ещё фото (до 3 штук) или нажмите '✅ Готово' для обработки всех фото.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Готово", callback_data="upload_schedule_done")
            ]])
        )
            
    except Exception as e:
        logger.error(f"Ошибка при сохранении фото: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Ошибка при сохранении фото: {str(e)}\n\n"
            "Попробуйте отправить другое фото или нажмите '✅ Готово'.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Готово", callback_data="upload_schedule_done")
            ]])
        )


async def handle_schedule_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстового поста с графиком"""
    user_id = update.effective_user.id
    
    if user_id not in user_context or "upload_schedule_city_id" not in user_context[user_id]:
        await update.message.reply_text(
            "❌ Ошибка. Начните заново.",
            reply_markup=get_back_keyboard()
        )
        return ConversationHandler.END
    
    city_id = user_context[user_id]["upload_schedule_city_id"]
    city = db.get_city(city_id)
    text = update.message.text or ""
    
    if not text or len(text.strip()) == 0:
        await update.message.reply_text(
            "⚠️ Текст пустой. Отправьте текст с графиком отключений или фото.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Готово", callback_data="upload_schedule_done")
            ]])
        )
        return WAITING_SCHEDULE_PHOTO
    
    await update.message.reply_text("⚙️ Распознавание графика из текста...")
    
    try:
        # Проверяем через Gemini - это график и определяем дату
        schedule_type = await asyncio.to_thread(check_schedule_post_and_date, text)
        
        if not schedule_type:
            await update.message.reply_text(
                "⚠️ Не удалось распознать график в тексте.\n\n"
                "Убедитесь, что текст содержит график отключений с группами и интервалами.\n"
                "Попробуйте отправить другое сообщение или фото.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Готово", callback_data="upload_schedule_done")
                ]])
            )
            return WAITING_SCHEDULE_PHOTO
        
        # Извлекаем данные графика из текста
        schedule_data = await asyncio.to_thread(analyze_schedule_text, text)
        
        if not schedule_data or len(schedule_data) == 0:
            await update.message.reply_text(
                "⚠️ Не удалось извлечь данные графика из текста.\n\n"
                "Попробуйте отправить другое сообщение или фото.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Готово", callback_data="upload_schedule_done")
                ]])
            )
            return WAITING_SCHEDULE_PHOTO
        
        groups_list = list(schedule_data.keys())
        logger.info(f"📝 Текст обработан. Распознано групп: {len(schedule_data)}, дата: {schedule_type}")
        logger.info(f"   Группы: {groups_list}")
        
        # Получаем старый график
        old_schedule = db.get_schedule(city_id, schedule_type) or {}
        
        # Сохраняем график (объединяем с существующим, если нужно)
        if is_complete_schedule(schedule_data, old_schedule):
            db.save_schedule(city_id, schedule_data, schedule_type)
            final_schedule = schedule_data
        else:
            if old_schedule:
                merged_schedule = merge_schedules(old_schedule, schedule_data)
                db.save_schedule(city_id, merged_schedule, schedule_type)
                final_schedule = merged_schedule
            else:
                db.save_schedule(city_id, schedule_data, schedule_type)
                final_schedule = schedule_data
        
        # Отправляем уведомления подписчикам
        if context and context.application:
            asyncio.create_task(notify_subscribers_about_schedule_update(
                context.application, city_id, city.name, old_schedule, final_schedule
            ))
        
        date_label = "сегодня" if schedule_type == "today" else "завтра"
        groups_count = len(final_schedule)
        
        await update.message.reply_text(
            f"✅ График распознан и сохранён!\n\n"
            f"📅 Дата: {date_label}\n"
            f"📊 Распознано групп: {groups_count}\n\n"
            "Можете отправить ещё текст или фото, или нажмите 'Готово'.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Готово", callback_data="upload_schedule_done")
            ]])
        )
        
        return WAITING_SCHEDULE_PHOTO
        
    except Exception as e:
        logger.error(f"Ошибка при обработке текста графика: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Ошибка при распознавании: {str(e)}\n\n"
            "Попробуйте отправить другое сообщение или фото, или нажмите 'Готово'.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Готово", callback_data="upload_schedule_done")
            ]])
        )
        return WAITING_SCHEDULE_PHOTO


async def upload_schedule_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение загрузки графика - обрабатываем все фото через Gemini"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    if user_id not in user_context or "upload_schedule_city_id" not in user_context[user_id]:
        await query.edit_message_text("❌ Ошибка. Начните заново.")
        return ConversationHandler.END
    
    city_id = user_context[user_id]["upload_schedule_city_id"]
    city = db.get_city(city_id)
    
    # Получаем список загруженных фото (байты)
    photos_bytes = user_context[user_id].get("upload_schedule_photos", [])
    mime_types = user_context[user_id].get("upload_schedule_mime_types", [])
    
    if not photos_bytes:
        await query.edit_message_text(
            "⚠️ Не загружено ни одного фото.\n\n"
            "Отправьте фото с графиком или начните заново.",
            reply_markup=get_back_keyboard()
        )
        # Очищаем контекст
        user_context[user_id].pop("upload_schedule_city_id", None)
        user_context[user_id].pop("upload_schedule_photos", None)
        user_context[user_id].pop("upload_schedule_mime_types", None)
        return ConversationHandler.END
    
    # Показываем, что начинаем обработку
    await query.edit_message_text(f"⏳ Обрабатываю {len(photos_bytes)} фото через Gemini...")
    
    try:
        # ВАЖНО: Теперь обрабатываем ВСЕ фото через Gemini
        merged_schedule = {}
        processed_count = 0
        
        for i, (photo_bytes, mime_type) in enumerate(zip(photos_bytes, mime_types), 1):
            logger.info(f"📸 Обрабатываю фото {i}/{len(photos_bytes)} через Gemini...")
            
            # Анализируем фото через Gemini
            schedule_data = await asyncio.to_thread(analyze_schedule_image, photo_bytes, mime_type)
            
            if schedule_data:
                groups_list = list(schedule_data.keys())
                logger.info(f"   ✅ Фото {i}: распознано {len(schedule_data)} групп - {groups_list}")
                
                # Объединяем данные из всех фото
                merged_schedule.update(schedule_data)
                processed_count += 1
            else:
                logger.warning(f"   ⚠️ Фото {i}: не удалось распознать график")
        
        if not merged_schedule:
            await query.edit_message_text(
                "❌ Не удалось распознать график ни на одном фото.\n\n"
                "Попробуйте отправить другие фото.",
                reply_markup=get_back_keyboard()
            )
            # Очищаем контекст
            user_context[user_id].pop("upload_schedule_city_id", None)
            user_context[user_id].pop("upload_schedule_photos", None)
            user_context[user_id].pop("upload_schedule_mime_types", None)
            return ConversationHandler.END
        
        # По умолчанию сохраняем как завтра (так как дата не определена из загруженного фото)
        schedule_type = "tomorrow"
        
        # Получаем старый график для сравнения (перед сохранением нового)
        old_schedule = db.get_schedule(city_id, schedule_type) or {}
        
        # Сохраняем объединённый график в базу
        db.save_schedule(city_id, merged_schedule, schedule_type)
        logger.info(f"💾 Сохранён объединённый график: {len(merged_schedule)} групп из {processed_count} фото")
        
        # Отправляем уведомления подписчикам
        if context and context.application:
            asyncio.create_task(notify_subscribers_about_schedule_update(
                context.application, city_id, city.name, old_schedule, merged_schedule
            ))
        
        groups_count = len(merged_schedule)
        
        # Очищаем контекст
        user_context[user_id].pop("upload_schedule_city_id", None)
        user_context[user_id].pop("upload_schedule_photos", None)
        user_context[user_id].pop("upload_schedule_mime_types", None)
        
        await query.edit_message_text(
            f"✅ График успешно обработан и сохранён!\n\n"
            f"🏙️ Город: <b>{city.name}</b>\n"
            f"📸 Обработано фото: {processed_count}/{len(photos_bytes)}\n"
            f"📊 Распознано групп: {groups_count}\n"
            f"📅 Дата: завтра",
            reply_markup=get_back_keyboard(),
            parse_mode=ParseMode.HTML
        )
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Ошибка при обработке фото графика: {e}", exc_info=True)
        await query.edit_message_text(
            f"❌ Ошибка при обработке: {str(e)}\n\n"
            "Попробуйте загрузить фото заново.",
            reply_markup=get_back_keyboard()
        )
        # Очищаем контекст
        user_context[user_id].pop("upload_schedule_city_id", None)
        user_context[user_id].pop("upload_schedule_photos", None)
        user_context[user_id].pop("upload_schedule_mime_types", None)
        return ConversationHandler.END


# ========== ПРОСМОТР ГРАФИКА ==========

async def view_schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало просмотра графика - выбор города"""
    query = update.callback_query
    await query.answer()
    
    cities = db.get_cities()
    if not cities:
        await query.edit_message_text(
            "❌ Сначала добавьте город.",
            reply_markup=get_back_keyboard()
        )
        return
    
    keyboard = []
    for city in cities:
        keyboard.append([InlineKeyboardButton(
            city.name,
            callback_data=f"view_schedule_city_{city.id}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
    
    await query.edit_message_text(
        "📅 <b>Посмотреть график</b>\n\n"
        "Выберите город:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def view_schedule_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор города для просмотра графика"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    if not city:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())
        return
    
    people = db.get_people(city_id)
    
    keyboard = []
    
    # Всегда добавляем кнопку "Повний графік"
    keyboard.append([InlineKeyboardButton(
        "📊 Повний графік",
        callback_data=f"view_schedule_full_{city_id}"
    )])
    
    # Добавляем людей, если они есть
    if people:
        keyboard.append([])  # Пустая строка для разделения
        for person in people:
            keyboard.append([InlineKeyboardButton(
                f"{person.name} (группа {person.group})",
                callback_data=f"view_schedule_person_{person.id}"
            )])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="view_schedule")])
    
    message_text = f"📅 <b>Посмотреть график</b>\n\n🏙️ <b>Город: {city.name}</b>\n\n"
    if people:
        message_text += "Выберите человека или посмотрите полный график:"
    else:
        message_text += "В городе нет людей, но вы можете посмотреть полный график:"
    
    await query.edit_message_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def view_schedule_person(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр графика для человека"""
    query = update.callback_query
    await query.answer()
    
    person_id = int(query.data.split("_")[-1])
    person = db.get_person(person_id)
    
    if not person:
        await query.edit_message_text("❌ Человек не найден.", reply_markup=get_back_keyboard())
        return
    
    city = db.get_city(person.city_id)
    if not city:
        await query.edit_message_text("❌ Город не найден в базе данных.", reply_markup=get_back_keyboard())
        return
    
    # Получаем оба графика (сегодня и завтра)
    schedule_intervals_today = db.get_schedule_for_group(person.city_id, person.group, "today")
    schedule_intervals_tomorrow = db.get_schedule_for_group(person.city_id, person.group, "tomorrow")
    
    # ВАЖНО: Проверяем актуальность графиков и правильность их размещения
    from helpers import get_kyiv_time
    from datetime import datetime, timedelta
    current_time = get_kyiv_time()
    current_date = current_time.date()
    
    # ВАЖНО: Если график в "tomorrow" был обновлён вчера (т.е. относится к сегодняшней дате),
    # используем его как "today", так как ротация могла ещё не произойти
    schedule_was_promoted = False  # Флаг: был ли график перенесён из tomorrow в today
    if schedule_intervals_tomorrow:
        schedule_tomorrow_updated_at = db.get_schedule_updated_at(person.city_id, "tomorrow")
        if schedule_tomorrow_updated_at:
            if isinstance(schedule_tomorrow_updated_at, datetime):
                tomorrow_schedule_date = schedule_tomorrow_updated_at.date()
            else:
                tomorrow_schedule_date = schedule_tomorrow_updated_at
            
            # Если график в "tomorrow" был обновлён вчера (разница 1 день), он относится к сегодня
            days_diff_tomorrow = (current_date - tomorrow_schedule_date).days
            if days_diff_tomorrow == 1:
                logger.info(f"🔄 График в 'tomorrow' был обновлён вчера ({tomorrow_schedule_date}), используем его как 'today' (ротация ещё не произошла)")
                schedule_intervals_today = schedule_intervals_tomorrow
                schedule_intervals_tomorrow = []  # Очищаем tomorrow, так как он стал today
                schedule_was_promoted = True  # Помечаем, что график был перенесён
    
    # Проверяем актуальность графика на "today"
    # ВАЖНО: Если график был перенесён из "tomorrow", он актуален (не проверяем на устарелость)
    # Старый график в "today" считается устаревшим только если он был обновлён более чем 1 день назад
    # График, загруженный вчера вечером, может быть актуальным для сегодня
    if schedule_intervals_today and not schedule_was_promoted:
        schedule_updated_at = db.get_schedule_updated_at(person.city_id, "today")
        if schedule_updated_at:
            # Приводим к date для сравнения
            if isinstance(schedule_updated_at, datetime):
                schedule_date = schedule_updated_at.date()
            else:
                schedule_date = schedule_updated_at
            
            # Вычисляем разницу в днях
            days_diff = (current_date - schedule_date).days
            
            # График считается устаревшим только если он был обновлён более чем 1 день назад
            # (разница > 1 день, т.е. позавчера или раньше)
            # График, загруженный вчера вечером (разница = 1 день), актуален для сегодня
            if days_diff > 1:
                logger.info(f"⚠️ График на 'today' устарел (обновлён {schedule_date}, сегодня {current_date}, разница {days_diff} дней), не показываю")
                schedule_intervals_today = []  # Не показываем устаревший график
            else:
                logger.debug(f"✅ График на 'today' актуален (обновлён {schedule_date}, сегодня {current_date}, разница {days_diff} дней)")
    
    # Получаем статус (используем время Киева) - на основе графика на сегодня
    status = get_schedule_status(schedule_intervals_today, current_time)
    
    # Получаем даты для отображения
    today_date = current_time.strftime('%d.%m')
    tomorrow_date = (current_time + timedelta(days=1)).strftime('%d.%m')
    
    # Форматируем график без Gemini (как в "График группы")
    try:
        message_block = format_person_schedule_block(
            person=person,
            city_name=city.name,
            schedule_intervals_today=schedule_intervals_today,
            schedule_intervals_tomorrow=schedule_intervals_tomorrow,
            current_time=current_time,
            today_date=today_date,
            tomorrow_date=tomorrow_date
        )
        
        # Добавляем заголовок
        full_message = f"📅 <b>График отключений</b>\n\n{message_block}"
        
    except Exception as e:
        logger.error(f"Ошибка при форматировании графика: {e}", exc_info=True)
        # Fallback на простой формат
        intervals_today_text = ""
        if schedule_intervals_today:
            intervals_today_text = "\n".join([f"• {interval}" for interval in schedule_intervals_today])
        else:
            intervals_today_text = "График для этой группы не загружен."
        
        full_message = (
            f"📅 <b>График отключений</b>\n\n"
            f"👤 <b>{person.name}</b> (группа {person.group})\n"
            f"🏙️ Місто: {city.name}\n\n"
        )
        
        if today_date:
            full_message += f"📅 {today_date}\n"
        full_message += f"⚡ Графік відключень на сьогодні:\n{intervals_today_text}\n\n"
        
        if schedule_intervals_tomorrow:
            intervals_tomorrow_text = "\n".join([f"• {interval}" for interval in schedule_intervals_tomorrow])
            if tomorrow_date:
                full_message += f"📅 {tomorrow_date}\n"
            full_message += f"⚡ Графік відключень на завтра:\n{intervals_tomorrow_text}\n\n"
        
        current_time_str = current_time.strftime('%H:%M')
        full_message += (
            f"🕐 Поточний час: {current_time_str}\n\n"
            f"{status['message']}\n"
            f"{status['nextChange']}\n"
            f"{status['timeToNextChange']}"
        )
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data=f"view_schedule_city_{person.city_id}")]]
    
    await query.edit_message_text(
        full_message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def view_schedule_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр полного графика для города (все группы)"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    
    if not city:
        await query.edit_message_text("❌ Город не найден.", reply_markup=get_back_keyboard())
        return
    
    # Получаем оба графика для города
    schedules = db.get_both_schedules(city_id)
    schedule_today = schedules.get("today") or {}
    schedule_tomorrow = schedules.get("tomorrow") or {}
    
    # ВАЖНО: Проверяем актуальность графиков и правильность их размещения
    from helpers import get_kyiv_time
    from datetime import datetime, timedelta
    current_time = get_kyiv_time()
    current_date = current_time.date()
    
    # ВАЖНО: Если график в "tomorrow" был обновлён вчера (разница 1 день),
    # это означает, что он относится к сегодняшней дате (ротация ещё не произошла)
    # Используем его как "today"
    schedule_was_promoted = False  # Флаг: был ли график перенесён из tomorrow в today
    if schedule_tomorrow:
        schedule_tomorrow_updated_at = db.get_schedule_updated_at(city_id, "tomorrow")
        if schedule_tomorrow_updated_at:
            if isinstance(schedule_tomorrow_updated_at, datetime):
                tomorrow_schedule_date = schedule_tomorrow_updated_at.date()
            else:
                tomorrow_schedule_date = schedule_tomorrow_updated_at
            
            days_diff_tomorrow = (current_date - tomorrow_schedule_date).days
            # Если график в "tomorrow" был обновлён вчера (разница 1 день), он относится к сегодня
            if days_diff_tomorrow == 1:
                logger.info(f"🔄 График в 'tomorrow' был обновлён вчера ({tomorrow_schedule_date}), используем его как 'today' (ротация ещё не произошла)")
                schedule_today = schedule_tomorrow
                schedule_tomorrow = {}  # Очищаем tomorrow, так как он стал today
                schedule_was_promoted = True  # Помечаем, что график был перенесён
    
    # Проверяем актуальность графика на "today"
    # ВАЖНО: Если график был перенесён из "tomorrow", он актуален (не проверяем на устарелость)
    # Старый график в "today" считается устаревшим только если он был обновлён более чем 1 день назад
    # График, загруженный вчера вечером, может быть актуальным для сегодня
    if schedule_today and not schedule_was_promoted:
        schedule_updated_at = db.get_schedule_updated_at(city_id, "today")
        if schedule_updated_at:
            # Приводим к date для сравнения
            if isinstance(schedule_updated_at, datetime):
                schedule_date = schedule_updated_at.date()
            else:
                schedule_date = schedule_updated_at
            
            # Вычисляем разницу в днях
            days_diff = (current_date - schedule_date).days
            
            # График считается устаревшим только если он был обновлён более чем 1 день назад
            # (разница > 1 день, т.е. позавчера или раньше)
            # График, загруженный вчера вечером (разница = 1 день), актуален для сегодня
            if days_diff > 1:
                logger.info(f"⚠️ График на 'today' устарел (обновлён {schedule_date}, сегодня {current_date}, разница {days_diff} дней), не показываю")
                schedule_today = {}  # Не показываем устаревший график
            else:
                logger.debug(f"✅ График на 'today' актуален (обновлён {schedule_date}, сегодня {current_date}, разница {days_diff} дней)")
    
    if not schedule_today and not schedule_tomorrow:
        await query.edit_message_text(
            f"❌ График для города <b>{city.name}</b> не загружен.\n\n"
            f"Используйте функцию 'Загрузить график' для добавления графика.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"view_schedule_city_{city_id}")]]),
            parse_mode=ParseMode.HTML
        )
        return
    
    # Удаляем метаданные из графиков для отображения
    schedule_today_clean = {k: v for k, v in schedule_today.items() if k != '_meta'}
    schedule_tomorrow_clean = {k: v for k, v in schedule_tomorrow.items() if k != '_meta'}
    
    # Получаем текущее время (Киев)
    from helpers import get_kyiv_time
    from datetime import timedelta
    current_time = get_kyiv_time()
    current_time_str = current_time.strftime('%H:%M')
    today_date = current_time.strftime('%d.%m')
    tomorrow_date = (current_time + timedelta(days=1)).strftime('%d.%m')
    
    # Формируем сообщение с полным графиком
    message_parts = [
        f"📅 <b>Повний графік відключень</b>",
        f"",
        f"🏙️ <b>Місто: {city.name}</b>",
        f"🕐 <b>Поточний час: {current_time_str}</b>",
        f""
    ]
    
    # График на сегодня
    if schedule_today_clean:
        message_parts.extend([
            f"📅 <b>{today_date}</b>",
            f"⚡ <b>Графік відключень на сьогодні:</b>",
            f""
        ])
        
        # Сортируем группы для красивого отображения
        sorted_groups = sorted(schedule_today_clean.keys(), key=lambda x: (int(x.split('.')[0]), int(x.split('.')[1])))
        
        for group in sorted_groups:
            intervals = schedule_today_clean[group]
            if intervals:
                intervals_text = ", ".join(intervals)
                message_parts.append(f"⚡ <b>Група {group}:</b> {intervals_text}")
            else:
                message_parts.append(f"⚡ <b>Група {group}:</b> (немає відключень)")
        message_parts.append("")
    
    # График на завтра
    if schedule_tomorrow_clean:
        message_parts.extend([
            f"📅 <b>{tomorrow_date}</b>",
            f"⚡ <b>Графік відключень на завтра:</b>",
            f""
        ])
        
        # Сортируем группы для красивого отображения
        sorted_groups = sorted(schedule_tomorrow_clean.keys(), key=lambda x: (int(x.split('.')[0]), int(x.split('.')[1])))
        
        for group in sorted_groups:
            intervals = schedule_tomorrow_clean[group]
            if intervals:
                intervals_text = ", ".join(intervals)
                message_parts.append(f"⚡ <b>Група {group}:</b> {intervals_text}")
            else:
                message_parts.append(f"⚡ <b>Група {group}:</b> (немає відключень)")
    
    full_message = "\n".join(message_parts)
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data=f"view_schedule_city_{city_id}")]]
    
    await query.edit_message_text(
        full_message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def find_and_process_channel_schedule(context: ContextTypes.DEFAULT_TYPE, city_id: int, city_name: str, chat_id: int):
    """
    Асинхронная функция для поиска и обработки графика из канала
    Уведомляет конкретного пользователя о результате
    """
    try:
        # Проверяем, что context и bot доступны
        if not context or not hasattr(context, 'bot') or context.bot is None:
            logger.error("Context или bot недоступны для отправки сообщения")
            return
        
        # Уведомляем о начале поиска
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔍 Ищу последний пост с графиком..."
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение о начале поиска: {e}")
        
        # Ищем и обрабатываем график
        success, message_text = await find_and_process_schedule_for_user(city_id, city_name, db)
        
        # Уведомляем пользователя о результате
        keyboard = [[InlineKeyboardButton("🔙 Назад в меню", callback_data="main_menu")]]
        
        try:
            if success:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ <b>Готово!</b>\n\n{message_text}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{message_text}\n\nПопробуйте загрузить график вручную.",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logger.warning(f"Не удалось отправить результат пользователю: {e}")
            
    except Exception as e:
        logger.error(f"Ошибка при поиске графика из канала: {e}", exc_info=True)
        # Пытаемся уведомить пользователя об ошибке, если context доступен
        try:
            if context and hasattr(context, 'bot') and context.bot:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Произошла ошибка при поиске графика: {str(e)}\n\nПопробуйте загрузить график вручную.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="upload_schedule")]])
            )
        except:
            pass


# ========== УПРАВЛЕНИЕ УВЕДОМЛЕНИЯМИ ==========

async def notify_subscribers_about_schedule_update(bot_application, city_id: int, city_name: str, old_schedule: dict, new_schedule: dict):
    """
    Отправляет уведомления подписчикам об изменениях в графике (используется при ручной загрузке)
    
    Args:
        bot_application: Приложение бота для отправки уведомлений
        city_id: ID города
        city_name: Название города
        old_schedule: Старый график (dict с группами и интервалами)
        new_schedule: Новый график (dict с группами и интервалами)
    """
    if not bot_application:
        logger.debug("Бот не настроен для отправки уведомлений")
        return
    
    # Находим все группы, которые изменились
    changed_groups = set()
    
    # Если old_schedule пустой - это первое сохранение, все группы считаются новыми
    if not old_schedule:
        for group in new_schedule.keys():
            if group != '_meta':
                changed_groups.add(group)
                logger.info(f"📊 Первое сохранение графика, группа {group}: {new_schedule.get(group, [])}")
    else:
        for group in set(old_schedule.keys()) | set(new_schedule.keys()):
            if group == '_meta':
                continue
            old_intervals = old_schedule.get(group, [])
            new_intervals = new_schedule.get(group, [])
            if sorted(old_intervals) != sorted(new_intervals):
                changed_groups.add(group)
                logger.info(f"📊 Изменение в группе {group}: {old_intervals} → {new_intervals}")
    
    if not changed_groups:
        logger.debug("Нет изменений в графике")
        return
    
    city = db.get_city(city_id)
    if not city:
        logger.error(f"Город с ID {city_id} не найден в базе данных")
        return
    
    city_name = city.name
    people = db.get_people(city_id)
    affected_people = [p for p in people if p.group in changed_groups]
    
    if not affected_people:
        logger.debug(f"Нет людей с группами {changed_groups} в городе {city_name}")
        return
    
    logger.info(f"🔔 Найдено {len(affected_people)} человек с изменёнными графиками в городе {city_name}")
    
    for person in affected_people:
        subscribers = db.get_subscribers_for_person(person.id)
        if not subscribers:
            continue
        
        new_intervals = new_schedule.get(person.group, [])
        
        if not new_intervals:
            message = (
                f"🔔 <b>Обновление графика!</b>\n\n"
                f"👤 {person.name}\n"
                f"🏙️ Город: {city_name}\n"
                f"⚡ Группа: {person.group}\n\n"
                f"⚠️ График обновлён, но данные для группы {person.group} пока не доступны."
            )
        else:
            from helpers import get_kyiv_time
            current_time = get_kyiv_time()
            status_info = get_schedule_status(new_intervals, current_time)
            
            try:
                current_time_str = current_time.strftime("%H:%M")
                message = await asyncio.to_thread(
                    generate_schedule_response,
                    person.name, city_name, person.group, new_intervals,
                    current_time_str,
                    status_info.get("message", "✅ Свет есть"),
                    status_info.get("nextChange", ""),
                    status_info.get("timeToNextChange", "")
                )
                message = f"🔔 <b>Обновление графика!</b>\n\n{message}"
            except Exception as e:
                logger.error(f"Ошибка при генерации ответа через Gemini: {e}")
                intervals_text = "\n".join([f"• {interval}" for interval in new_intervals])
                message = (
                    f"🔔 <b>Обновление графика!</b>\n\n"
                    f"👤 {person.name}\n🏙️ Город: {city_name}\n⚡ Группа: {person.group}\n\n"
                    f"<b>Актуальный график:</b>\n{intervals_text}\n\n"
                    f"🕐 Текущее время: {current_time.strftime('%H:%M')}\n\n"
                    f"{status_info.get('message', '✅ Свет есть')}\n"
                    f"{status_info.get('nextChange', '')}\n{status_info.get('timeToNextChange', '')}"
                )
        
        for user_id in subscribers:
            try:
                await bot_application.bot.send_message(chat_id=user_id, text=message, parse_mode='HTML')
                logger.info(f"📤 Уведомление отправлено пользователю {user_id} о {person.name}")
            except Exception as e:
                logger.error(f"❌ Ошибка при отправке уведомления пользователю {user_id}: {e}")

async def notifications_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления уведомлениями"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    # Получаем текущие подписки пользователя
    subscriptions = db.get_user_subscriptions(user_id)
    
    keyboard = [
        [InlineKeyboardButton("➕ Подписаться", callback_data="subscribe_person")],
    ]
    
    # Добавляем кнопки для отписки от существующих подписок
    if subscriptions:
        for person in subscriptions:
            city = db.get_city(person.city_id)
            keyboard.append([
                InlineKeyboardButton(
                    f"🔔 {person.name} ({city.name if city else '?'}, группа {person.group})",
                    callback_data=f"unsubscribe_{person.id}"
                )
            ])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
    
    subscriptions_text = ""
    if subscriptions:
        subscriptions_text = "\n\n<b>Ваши подписки:</b>\n"
        for person in subscriptions:
            city = db.get_city(person.city_id)
            subscriptions_text += f"• {person.name} ({city.name if city else '?'}, группа {person.group})\n"
    else:
        subscriptions_text = "\n\n<i>У вас пока нет подписок.</i>"
    
    await query.edit_message_text(
        f"🔔 <b>Управление уведомлениями</b>\n\n"
        f"Вы будете получать уведомления, когда график отключений изменится для выбранных людей."
        f"{subscriptions_text}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def subscribe_person_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало подписки - выбор города"""
    query = update.callback_query
    await query.answer()
    
    cities = db.get_cities()
    if not cities:
        await query.edit_message_text(
            "❌ Сначала добавьте город.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="notifications")
            ]])
        )
        return
    
    keyboard = []
    for city in cities:
        keyboard.append([
            InlineKeyboardButton(city.name, callback_data=f"subscribe_city_{city.id}")
        ])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="notifications")])
    
    await query.edit_message_text(
        "🔔 <b>Подписаться на уведомления</b>\n\n"
        "Выберите город:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def subscribe_person_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор города для подписки - показываем список людей"""
    query = update.callback_query
    await query.answer()
    
    city_id = int(query.data.split("_")[-1])
    city = db.get_city(city_id)
    people = db.get_people(city_id)
    
    if not people:
        await query.edit_message_text(
            f"❌ В городе '{city.name}' пока нет людей.\n\n"
            "Сначала добавьте человека в этом городе.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="subscribe_person")
            ]])
        )
        return
    
    user_id = update.effective_user.id
    keyboard = []
    
    for person in people:
        # Проверяем, подписан ли уже пользователь
        is_subscribed = db.is_subscribed(user_id, person.id)
        icon = "✅" if is_subscribed else "➕"
        keyboard.append([
            InlineKeyboardButton(
                f"{icon} {person.name} (группа {person.group})",
                callback_data=f"subscribe_toggle_{person.id}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="subscribe_person")])
    
    await query.edit_message_text(
        f"🔔 <b>Подписаться на уведомления</b>\n\n"
        f"Город: <b>{city.name}</b>\n\n"
        "Выберите человека:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def subscribe_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение подписки (подписаться/отписаться)"""
    query = update.callback_query
    await query.answer()
    
    person_id = int(query.data.split("_")[-1])
    user_id = update.effective_user.id
    person = db.get_person(person_id)
    
    if not person:
        await query.edit_message_text(
            "❌ Человек не найден.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="notifications")
            ]])
        )
        return
    
    city = db.get_city(person.city_id)
    is_subscribed = db.is_subscribed(user_id, person_id)
    
    if is_subscribed:
        # Отписываемся
        db.remove_subscription(user_id, person_id)
        await query.edit_message_text(
            f"✅ Вы отписались от уведомлений для <b>{person.name}</b>.\n\n"
            f"Город: {city.name if city else '?'}\n"
            f"Группа: {person.group}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="notifications")
            ]]),
            parse_mode=ParseMode.HTML
        )
    else:
        # Подписываемся
        success = db.add_subscription(user_id, person_id)
        if success:
            await query.edit_message_text(
                f"✅ Вы подписались на уведомления для <b>{person.name}</b>.\n\n"
                f"Город: {city.name if city else '?'}\n"
                f"Группа: {person.group}\n\n"
                f"Теперь вы будете получать уведомления, когда график отключений изменится для группы {person.group} в городе {city.name if city else '?'}.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Назад", callback_data="notifications")
                ]]),
                parse_mode=ParseMode.HTML
            )
        else:
            await query.edit_message_text(
                f"⚠️ Вы уже подписаны на уведомления для <b>{person.name}</b>.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Назад", callback_data="notifications")
                ]]),
                parse_mode=ParseMode.HTML
            )


async def unsubscribe_person(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отписка от уведомлений"""
    query = update.callback_query
    await query.answer()
    
    person_id = int(query.data.split("_")[-1])
    user_id = update.effective_user.id
    person = db.get_person(person_id)
    
    if not person:
        await query.edit_message_text(
            "❌ Человек не найден.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="notifications")
            ]])
        )
        return
    
    city = db.get_city(person.city_id)
    success = db.remove_subscription(user_id, person_id)
    
    if success:
        await query.edit_message_text(
            f"✅ Вы отписались от уведомлений для <b>{person.name}</b>.\n\n"
            f"Город: {city.name if city else '?'}\n"
            f"Группа: {person.group}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="notifications")
            ]]),
            parse_mode=ParseMode.HTML
        )
    else:
        await query.edit_message_text(
            f"❌ Ошибка при отписке.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="notifications")
            ]])
        )


# ========== ОСНОВНАЯ ФУНКЦИЯ ==========

async def rotate_schedules_task():
    """Задача для автоматического переноса графиков в полночь"""
    from helpers import get_kyiv_time
    from datetime import datetime, timedelta
    
    while True:
        try:
            # Получаем текущее время в Киеве
            current_time = get_kyiv_time()
            
            # Вычисляем время до следующей полночи
            # Если сейчас до полуночи - берём полночь сегодня, иначе - полночь завтра
            today_midnight = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
            if current_time >= today_midnight:
                # Уже прошла полночь сегодня, берём полночь завтра
                next_midnight = (today_midnight + timedelta(days=1))
            else:
                # Ещё не наступила полночь сегодня
                next_midnight = today_midnight
            
            time_until_midnight = (next_midnight - current_time).total_seconds()
            
            # Если время отрицательное или очень маленькое (меньше минуты), ждём до следующей полночи
            if time_until_midnight < 60:
                next_midnight = (today_midnight + timedelta(days=1))
                time_until_midnight = (next_midnight - current_time).total_seconds()
            
            # Ждём до полночи
            logger.info(f"⏰ Задача переноса графиков: следующая полночь через {time_until_midnight / 3600:.1f} часов")
            await asyncio.sleep(time_until_midnight)
            
            # Переносим графики
            logger.info("🔄 Наступила полночь, переношу графики...")
            db.rotate_schedules()
            logger.info("✅ Графики успешно перенесены")
            
            # Ждём 1 секунду, чтобы не выполнить дважды
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"❌ Ошибка в задаче переноса графиков: {e}")
            # Ждём 1 час перед повторной попыткой
            await asyncio.sleep(3600)


def main():
    """Запуск бота"""
    # Загружаем переменные окружения из .env файла
    load_dotenv()
    
    # Инициализируем предустановленные города
    logger.info("Инициализация базы данных...")
    db.init_default_cities()
    logger.info("✅ База данных готова")
    
    # Получаем токен бота из переменной окружения
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN не установлен. Установите переменную окружения TELEGRAM_BOT_TOKEN.")
    
    # Устанавливаем команды бота (появляется кнопка меню у пользователя)
    async def post_init(app: Application) -> None:
        """Инициализация после запуска бота - устанавливает команды меню"""
        await app.bot.set_my_commands([
            BotCommand("start", "🏠 Главное меню")
        ])
        logger.info("✅ Команды бота установлены (кнопка меню активирована)")
    
    # Создаем приложение с post_init для установки команд меню
    application = Application.builder().token(bot_token).post_init(post_init).build()
    
    # Обработчик команды /start
    application.add_handler(CommandHandler("start", start))
    
    # Обработчик команды /start_mon (ручной запуск мониторинга)
    application.add_handler(CommandHandler("start_mon", start_monitoring))
    
    # Обработчик главного меню
    application.add_handler(CallbackQueryHandler(main_menu, pattern="^main_menu$"))
    
    # Обработчики уведомлений
    application.add_handler(CallbackQueryHandler(notifications_menu, pattern="^notifications$"))
    application.add_handler(CallbackQueryHandler(subscribe_person_start, pattern="^subscribe_person$"))
    application.add_handler(CallbackQueryHandler(subscribe_person_city, pattern="^subscribe_city_"))
    application.add_handler(CallbackQueryHandler(subscribe_toggle, pattern="^subscribe_toggle_"))
    application.add_handler(CallbackQueryHandler(unsubscribe_person, pattern="^unsubscribe_"))
    
    # Обработчики управления городами
    application.add_handler(CallbackQueryHandler(manage_cities, pattern="^manage_cities$"))
    # НЕ регистрируем add_city_start отдельно - он уже в ConversationHandler
    application.add_handler(CallbackQueryHandler(delete_city, pattern="^delete_city_"))
    application.add_handler(CallbackQueryHandler(confirm_delete_city, pattern="^confirm_delete_city_"))
    application.add_handler(CallbackQueryHandler(clear_schedule, pattern="^clear_schedule_(today|tomorrow)_"))
    application.add_handler(CallbackQueryHandler(city_details, pattern="^city_details_"))
    # НЕ регистрируем edit_city_name_start отдельно - он уже в ConversationHandler
    application.add_handler(CallbackQueryHandler(manage_channel, pattern="^manage_channel_"))
    # НЕ регистрируем add_channel_start и edit_channel_start отдельно - они уже в ConversationHandler
    application.add_handler(CallbackQueryHandler(delete_channel, pattern="^delete_channel_"))
    application.add_handler(CallbackQueryHandler(confirm_delete_channel, pattern="^confirm_delete_channel_"))
    
    # ConversationHandler для добавления города
    add_city_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_city_start, pattern="^add_city$")],
        states={
            WAITING_CITY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_city_name)],
        },
        fallbacks=[
            CallbackQueryHandler(manage_cities, pattern="^manage_cities$"),
            CommandHandler("start", start),  # Позволяет выйти из разговора через /start
            CommandHandler("cancel", start),  # Позволяет отменить через /cancel
        ],
    )
    application.add_handler(add_city_conv)
    
    # ConversationHandler для редактирования названия города
    edit_city_name_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_city_name_start, pattern="^edit_city_name_")],
        states={
            WAITING_EDIT_CITY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_city_name)],
        },
        fallbacks=[
            CallbackQueryHandler(city_details, pattern="^city_details_"),
            CallbackQueryHandler(manage_cities, pattern="^manage_cities$"),
            CommandHandler("start", start),
            CommandHandler("cancel", start),
        ],
    )
    application.add_handler(edit_city_name_conv)
    
    # ConversationHandler для добавления/редактирования канала
    channel_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_channel_start, pattern="^add_channel_"),
            CallbackQueryHandler(edit_channel_start, pattern="^edit_channel_")
        ],
        states={
            WAITING_CHANNEL_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_channel_username)],
        },
        fallbacks=[
            CallbackQueryHandler(manage_channel, pattern="^manage_channel_"),
            CallbackQueryHandler(city_details, pattern="^city_details_"),
            CommandHandler("start", start),
            CommandHandler("cancel", start),
        ],
    )
    application.add_handler(channel_conv)
    
    # Обработчики управления людьми
    application.add_handler(CallbackQueryHandler(manage_people, pattern="^manage_people$"))
    
    # Обработчики для работы с группами людей
    application.add_handler(CallbackQueryHandler(create_group_start, pattern="^create_group$"))
    application.add_handler(CallbackQueryHandler(group_toggle_person, pattern="^group_toggle_"))
    application.add_handler(CallbackQueryHandler(group_create, pattern="^group_create$"))
    application.add_handler(CallbackQueryHandler(view_group_schedule, pattern="^view_group_schedule$"))
    application.add_handler(CallbackQueryHandler(add_person_start, pattern="^add_person$"))
    application.add_handler(CallbackQueryHandler(edit_person_start, pattern="^edit_person$"))
    application.add_handler(CallbackQueryHandler(delete_person_start, pattern="^delete_person$"))
    
    # ConversationHandler для добавления человека
    add_person_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_person_city, pattern="^add_person_city_")],
        states={
            WAITING_PERSON_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_person_name)],
            WAITING_PERSON_GROUP: [CallbackQueryHandler(add_person_group, pattern="^add_person_group_")],
        },
        fallbacks=[
            CallbackQueryHandler(manage_people, pattern="^manage_people$"),
            CallbackQueryHandler(add_person_start, pattern="^add_person$"),
            CommandHandler("start", start),
            CommandHandler("cancel", start),
        ],
    )
    application.add_handler(add_person_conv)
    
    # Обработчики редактирования человека
    application.add_handler(CallbackQueryHandler(edit_person_city, pattern="^edit_person_city_"))
    application.add_handler(CallbackQueryHandler(edit_person_select, pattern="^edit_person_select_"))
    # НЕ регистрируем edit_person_change_name и edit_person_change_group отдельно - они уже в ConversationHandler'ах
    
    # ConversationHandler для редактирования имени
    edit_name_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_person_change_name, pattern="^edit_person_change_name$")],
        states={
            WAITING_EDIT_PERSON_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_person_name)],
        },
        fallbacks=[
            CallbackQueryHandler(edit_person_select, pattern="^edit_person_select_"),
            CommandHandler("start", start),
            CommandHandler("cancel", start),
        ],
    )
    application.add_handler(edit_name_conv)
    
    # ConversationHandler для редактирования группы
    edit_group_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_person_change_group, pattern="^edit_person_change_group$")],
        states={
            WAITING_EDIT_PERSON_GROUP: [CallbackQueryHandler(edit_person_group, pattern="^edit_person_group_")],
        },
        fallbacks=[
            CallbackQueryHandler(edit_person_select, pattern="^edit_person_select_"),
            CommandHandler("start", start),
            CommandHandler("cancel", start),
        ],
    )
    application.add_handler(edit_group_conv)
    
    # Обработчики удаления человека
    application.add_handler(CallbackQueryHandler(delete_person_city, pattern="^delete_person_city_"))
    application.add_handler(CallbackQueryHandler(delete_person_select, pattern="^delete_person_select_"))
    application.add_handler(CallbackQueryHandler(confirm_delete_person, pattern="^confirm_delete_person_"))
    
    # Обработчики загрузки графика
    application.add_handler(CallbackQueryHandler(upload_schedule_start, pattern="^upload_schedule$"))
    application.add_handler(CallbackQueryHandler(upload_schedule_done, pattern="^upload_schedule_done$"))
    application.add_handler(CallbackQueryHandler(upload_from_channel, pattern="^upload_from_channel_"))
    application.add_handler(CallbackQueryHandler(upload_schedule_city, pattern="^upload_schedule_city_"))
    application.add_handler(CallbackQueryHandler(check_schedule_photos, pattern="^check_schedule_photos_"))
    
    # ConversationHandler для ручной загрузки графика
    upload_schedule_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(upload_manual_start, pattern="^upload_manual_")],
        states={
            WAITING_SCHEDULE_PHOTO: [
                MessageHandler(filters.PHOTO, handle_schedule_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_schedule_text),
                CallbackQueryHandler(upload_schedule_done, pattern="^upload_schedule_done$")
            ],
        },
        fallbacks=[
            CallbackQueryHandler(upload_schedule_start, pattern="^upload_schedule$"),
            CommandHandler("start", start),
            CommandHandler("cancel", start),
        ],
    )
    application.add_handler(upload_schedule_conv)
    
    # ConversationHandler для панели администратора
    admin_post_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", admin_test_post),
            CommandHandler("test_post", admin_test_post),
            CommandHandler("start_test", admin_test_post),  # Добавляем обработчик для /start_test
        ],
        states={
            WAITING_ADMIN_CITY: [
                CallbackQueryHandler(admin_select_city, pattern="^admin_city_"),
                CallbackQueryHandler(admin_select_city, pattern="^admin_cancel$"),
            ],
            WAITING_ADMIN_POST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_post),
                MessageHandler(filters.PHOTO, process_admin_post),  # Поддержка фото
                CallbackQueryHandler(admin_process_photos_handler, pattern="^admin_process_photos$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", start),
        ],
    )
    application.add_handler(admin_post_conv)
    
    # Обработчик ошибок
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обработчик ошибок для всех обработчиков"""
        logger.error(f"❌ Ошибка в обработчике: {context.error}", exc_info=True)
        
        # Пытаемся отправить сообщение пользователю, если это Update
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    f"❌ <b>Произошла ошибка</b>\n\n"
                    f"Попробуйте ещё раз или используйте /start",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass
    
    application.add_error_handler(error_handler)
    
    # Обработчики просмотра графика
    application.add_handler(CallbackQueryHandler(view_schedule_start, pattern="^view_schedule$"))
    application.add_handler(CallbackQueryHandler(view_schedule_city, pattern="^view_schedule_city_"))
    application.add_handler(CallbackQueryHandler(view_schedule_person, pattern="^view_schedule_person_"))
    application.add_handler(CallbackQueryHandler(view_schedule_full, pattern="^view_schedule_full_"))
    
    # Запускаем мониторинг канала в фоне (если настроены API_ID и API_HASH)
    # ВАЖНО: Отсрочка на 10 минут для избежания конфликтов при деплое на Render
    # (первые 5 минут может работать старый бот, поэтому ждем 10 минут)
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    
    if api_id and api_hash:
        def start_monitoring_delayed():
            """Запускает мониторинг с задержкой (по умолчанию 10 минут)"""
            import time
            # Можно настроить через переменную окружения MONITORING_DELAY_MINUTES
            delay_minutes = int(os.getenv("MONITORING_DELAY_MINUTES", "10"))
            delay_seconds = delay_minutes * 60
            logger.info(f"⏳ Мониторинг канала будет запущен через {delay_minutes} минут...")
            logger.info("💡 Это сделано для избежания конфликтов при деплое на Render")
            logger.info(f"💡 Чтобы изменить задержку, установите MONITORING_DELAY_MINUTES (текущее значение: {delay_minutes} мин)")
            time.sleep(delay_seconds)
            
            logger.info("🚀 Запускаю мониторинг канала в фоновом режиме...")
            from channel_monitor import start_monitor_task
            global monitor_thread, monitor_instance
            
            # ВАЖНО: Проверяем, не запущен ли уже мониторинг
            if monitor_instance and hasattr(monitor_instance, 'is_running') and monitor_instance.is_running:
                logger.info("✅ Мониторинг уже запущен (возможно, был запущен вручную через /start_mon)")
                logger.info("📡 Пропускаю автоматический запуск, чтобы избежать дублирования")
                return
            
            # Останавливаем старый мониторинг, если он есть (но не запущен)
            if monitor_instance and hasattr(monitor_instance, 'client') and monitor_instance.client:
                try:
                    logger.info("🔄 Останавливаю старый экземпляр мониторинга...")
                    monitor_instance.is_running = False
                    import asyncio
                    stop_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(stop_loop)
                    try:
                        stop_loop.run_until_complete(monitor_instance.client.disconnect())
                    except:
                        pass
                    finally:
                        stop_loop.close()
                    time.sleep(1)
                except Exception as e:
                    logger.warning(f"Не удалось остановить старый мониторинг: {e}")
            
            monitor_instance_ref = [None]  # Используем список для передачи по ссылке
            monitor_thread = start_monitor_task(db, application, monitor_instance_ref)
            
            # Присваиваем monitor_instance сразу после создания потока
            # monitor_instance_ref[0] будет установлен внутри потока сразу после создания monitor
            # Небольшая задержка для гарантии, что поток стартовал
            time.sleep(0.5)
            monitor_instance = monitor_instance_ref[0]
            
            if monitor_instance:
                logger.info("✅ Мониторинг канала запущен в фоновом режиме")
                logger.info("📡 Мониторинг поддерживает несколько каналов одновременно")
            else:
                logger.warning("⚠️ Не удалось получить ссылку на monitor_instance (возможно, поток ещё не стартовал)")
        
        # Запускаем в отдельном потоке с задержкой
        monitoring_thread = threading.Thread(target=start_monitoring_delayed, daemon=True)
        monitoring_thread.start()
        logger.info("⏰ Запланирован запуск мониторинга через 10 минут")
        
        # Функция для периодической проверки и автозапуска мониторинга
        def check_and_restart_monitoring():
            """Проверяет периодически, работает ли мониторинг, и перезапускает если нет"""
            import time
            # Интервал проверки в секундах (по умолчанию 60 минут = 3600 секунд)
            # ИЗМЕНЕНИЕ: Ставим 5 минут (300 секунд) по умолчанию вместо 60 минут
            check_interval_minutes = int(os.getenv("MONITORING_CHECK_INTERVAL_MINUTES", "5"))
            check_interval = check_interval_minutes * 60
            logger.info(f"⏰ Автоматическая проверка мониторинга будет выполняться каждые {check_interval_minutes} минут")
            
            # Первая проверка через 1 минуту (быстрее обнаружение проблем)
            time.sleep(60)  # 1 минута
            
            while True:
                try:
                    global monitor_instance, monitor_thread, monitoring_restart_in_progress
                    
                    # ВАЖНО: Проверяем, не идет ли уже перезапуск
                    if monitoring_restart_in_progress:
                        logger.debug("⏸️ Перезапуск мониторинга уже выполняется, пропускаю проверку")
                        continue
                    
                    # Проверяем, работает ли мониторинг
                    is_running = False
                    if monitor_instance and hasattr(monitor_instance, 'is_running'):
                        is_running = monitor_instance.is_running
                    
                    # Проверяем, жив ли поток
                    thread_alive = monitor_thread and monitor_thread.is_alive() if monitor_thread else False
                    
                    # Дополнительная проверка: проверяем, что клиент подключен
                    client_connected = False
                    if monitor_instance and hasattr(monitor_instance, 'client') and monitor_instance.client:
                        try:
                            # Проверяем, подключен ли клиент (неблокирующая проверка)
                            client_connected = monitor_instance.client.is_connected() if hasattr(monitor_instance.client, 'is_connected') else True
                        except:
                            client_connected = False
                    
                    # Мониторинг считается работающим только если ВСЕ условия выполнены
                    monitoring_ok = is_running and thread_alive and client_connected
                    
                    if not monitoring_ok:
                        # Устанавливаем флаг, чтобы предотвратить одновременные перезапуски
                        if monitoring_restart_in_progress:
                            logger.debug("⏸️ Перезапуск уже выполняется другим потоком")
                            continue
                        
                        monitoring_restart_in_progress = True
                        
                        logger.warning("=" * 60)
                        logger.warning("⚠️ МОНИТОРИНГ НЕ РАБОТАЕТ!")
                        logger.warning("=" * 60)
                        logger.warning(f"   is_running: {is_running}")
                        logger.warning(f"   thread_alive: {thread_alive}")
                        logger.warning(f"   client_connected: {client_connected}")
                        logger.warning("🔄 Автоматически перезапускаю мониторинг...")
                        logger.warning("=" * 60)
                        
                        # Останавливаем старый мониторинг, если он есть
                        if monitor_instance:
                            try:
                                monitor_instance.is_running = False
                                if hasattr(monitor_instance, 'client') and monitor_instance.client:
                                    import asyncio
                                    stop_loop = asyncio.new_event_loop()
                                    asyncio.set_event_loop(stop_loop)
                                    try:
                                        stop_loop.run_until_complete(monitor_instance.client.disconnect())
                                    except:
                                        pass
                                    finally:
                                        stop_loop.close()
                                time.sleep(2)
                            except Exception as e:
                                logger.warning(f"Не удалось остановить старый мониторинг: {e}")
                            
                            # Очищаем ссылку на старый экземпляр
                            monitor_instance = None
                        
                        # Очищаем ссылку на старый поток
                        monitor_thread = None
                        
                        # Запускаем новый мониторинг
                        try:
                            from channel_monitor import start_monitor_task
                            monitor_instance_ref = [None]
                            monitor_thread = start_monitor_task(db, application, monitor_instance_ref)
                            
                            # Ждём, пока monitor_instance будет создан
                            for _ in range(10):
                                if monitor_instance_ref[0] is not None:
                                    monitor_instance = monitor_instance_ref[0]
                                    break
                                time.sleep(0.5)
                            
                            if monitor_instance:
                                logger.info("✅ Мониторинг успешно перезапущен автоматически")
                            else:
                                logger.warning("⚠️ Не удалось получить ссылку на monitor_instance после перезапуска")
                        except Exception as e:
                            logger.error(f"❌ Ошибка при автоматическом перезапуске мониторинга: {e}", exc_info=True)
                        finally:
                            # Снимаем флаг перезапуска
                            monitoring_restart_in_progress = False
                    else:
                        logger.debug(f"✅ Мониторинг работает нормально (is_running={is_running}, thread_alive={thread_alive}, client_connected={client_connected})")
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка в функции проверки мониторинга: {e}", exc_info=True)
                
                # Ждём перед следующей проверкой
                time.sleep(check_interval)
        
        # Запускаем проверку мониторинга в отдельном потоке
        monitoring_check_thread = threading.Thread(target=check_and_restart_monitoring, daemon=True)
        monitoring_check_thread.start()
        logger.info("✅ Запущена автоматическая проверка мониторинга (каждые 60 минут)")
    else:
        logger.info("TELEGRAM_API_ID и TELEGRAM_API_HASH не установлены. Мониторинг канала отключен.")
    
    # Запускаем API сервер (FastAPI + Uvicorn)
    port = int(os.getenv("PORT", 8000))  # Render автоматически устанавливает PORT
    api_thread = start_api_server(port)
    logger.info(f"✅ API сервер запущен на порту {port}")
    
    # Функция для graceful shutdown
    def shutdown_handler(signum, frame):
        logger.info("Получен сигнал остановки, выполняю graceful shutdown...")
        try:
            # Останавливаем мониторинг
            global monitor_instance
            if monitor_instance and hasattr(monitor_instance, 'client') and monitor_instance.client:
                logger.info("Останавливаю мониторинг канала...")
                monitor_instance.is_running = False
                # Пытаемся корректно отключить клиент
                try:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(monitor_instance.client.disconnect())
                    except:
                        pass
                    finally:
                        loop.close()
                except Exception as e:
                    logger.warning(f"Ошибка при остановке мониторинга: {e}")
            
            # Останавливаем бота
            application.stop()
            application.shutdown()
            logger.info("Бот остановлен")
        except Exception as e:
            logger.error(f"Ошибка при shutdown: {e}")

    # Регистрируем обработчики сигналов
    import signal
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Запускаем задачу для автоматического переноса графиков в полночь
    def run_rotate_task():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(rotate_schedules_task())
        except Exception as e:
            logger.error(f"Ошибка в задаче переноса графиков: {e}")
        finally:
            loop.close()
    
    rotate_thread = threading.Thread(target=run_rotate_task, daemon=True)
    rotate_thread.start()
    logger.info("✅ Задача переноса графиков запущена в отдельном потоке")
    
    # Запускаем бота
    logger.info("Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()


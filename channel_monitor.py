import os
import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, List
from io import BytesIO
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto

from database import Database
from gemini_service import analyze_schedule_image, generate_schedule_response
from helpers import get_schedule_status
from constants import KEYWORDS

# ВАЖНО: Загружаем переменные окружения из .env файла
load_dotenv()

logger = logging.getLogger(__name__)

# Конфигурация
# ВАЖНО: Каналы хранятся в базе данных (таблица channels)
# Добавляйте каналы через интерфейс бота
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")  # Опциональная строка сессии

# KEYWORDS теперь импортируется из constants.py для избежания циклических импортов

# Файл для сохранения последнего обработанного ID
LAST_MESSAGE_FILE = "last_message_id.txt"


class ChannelMonitor:
    """Мониторинг Telegram-канала для автоматического получения графиков"""
    
    def __init__(self, db: Database, bot_application=None):
        self.db = db
        self.client: Optional[TelegramClient] = None
        self.is_running = False
        self.bot_application = bot_application  # Приложение бота для отправки уведомлений
        self.last_processed_id = 0  # ID последнего обработанного поста
        self._processed_albums = set()  # Отслеживание обработанных альбомов (grouped_id)
        self._processed_albums_lock = None  # Будет инициализирован в start_monitoring (asyncio.Lock нельзя создать в __init__)
        
        if not API_ID or not API_HASH:
            logger.warning("TELEGRAM_API_ID и TELEGRAM_API_HASH не установлены. Мониторинг канала недоступен.")
    
    def extract_date_from_text(self, text: str) -> Optional[str]:
        """
        Извлекает дату из текста сообщения и определяет, это сегодня или завтра.
        
        Returns:
            "today" - если график на сегодня
            "tomorrow" - если график на завтра
            None - если дата не определена (по умолчанию будет "tomorrow")
        """
        if not text:
            return None
        
        from helpers import get_kyiv_time
        now = get_kyiv_time()  # Используем время Киева
        today = now.date()
        tomorrow = today + timedelta(days=1)
        
        text_lower = text.lower()
        
        # Проверка на "на завтра" / "завтра"
        if "на завтра" in text_lower or text_lower.startswith("завтра"):
            return "tomorrow"
        
        # Проверка на "на сегодня" / "сегодня"
        if "на сегодня" in text_lower or text_lower.startswith("сегодня"):
            return "today"
        
        # Поиск полной даты в формате DD.MM.YYYY или DD/MM/YYYY
        # Например: "22.11.2025 - Графік" или "міни на 22:18 21.11.2025"
        full_date_match = re.search(r'(\d{1,2})[./](\d{1,2})[./](\d{4})', text)
        if full_date_match:
            day, month, year = map(int, full_date_match.groups())
            try:
                found_date = datetime(year, month, day).date()
                if found_date == today:
                    return "today"
                elif found_date == tomorrow:
                    return "tomorrow"
                # Если дата в будущем (больше чем завтра), считаем это завтра
                elif found_date > tomorrow:
                    logger.info(f"   📅 Найдена дата {day}.{month}.{year} (в будущем), сохраняю как завтра")
                    return "tomorrow"
                # Если дата в прошлом, считаем это сегодня (обновление старого графика)
                else:
                    logger.info(f"   📅 Найдена дата {day}.{month}.{year} (в прошлом), сохраняю как сегодня")
                    return "today"
            except ValueError:
                pass
        
        # Поиск даты в формате DD.MM или DD/MM (без года)
        # Сравниваем с сегодняшней и завтрашней датой
        date_match = re.search(r'(\d{1,2})[./](\d{1,2})(?![./]\d)', text)
        if date_match:
            day, month = map(int, date_match.groups())
            try:
                # Пробуем с текущим годом
                found_date = datetime(today.year, month, day).date()
                if found_date == today:
                    return "today"
                elif found_date == tomorrow:
                    return "tomorrow"
                # Если дата в прошлом (например, 31.12 когда сейчас январь), пробуем следующий год
                elif found_date < today:
                    found_date = datetime(today.year + 1, month, day).date()
                    if found_date == tomorrow:
                        return "tomorrow"
                    elif found_date == today:
                        return "today"
            except ValueError:
                pass
        
        # Поиск даты с месяцем словами (20 ноября)
        months = {
            "січня": 1, "января": 1,
            "лютого": 2, "февраля": 2,
            "березня": 3, "марта": 3,
            "квітня": 4, "апреля": 4,
            "травня": 5, "мая": 5,
            "червня": 6, "июня": 6,
            "липня": 7, "июля": 7,
            "серпня": 8, "августа": 8,
            "вересня": 9, "сентября": 9,
            "жовтня": 10, "октября": 10,
            "листопада": 11, "ноября": 11,
            "грудня": 12, "декабря": 12
        }
        
        for month_name, month_num in months.items():
            match = re.search(rf'(\d{{1,2}})\s+{month_name}', text_lower)
            if match:
                day = int(match.group(1))
                try:
                    found_date = datetime(today.year, month_num, day).date()
                    if found_date == today:
                        return "today"
                    elif found_date == tomorrow:
                        return "tomorrow"
                except ValueError:
                    pass
        
        # Если дата не найдена, возвращаем None (будет использовано значение по умолчанию "tomorrow")
        return None

    def should_download_post(self, text: str) -> bool:
        """
        Проверяет, нужно ли обрабатывать этот пост
        
        Использует специфичные ключевые слова, связанные с графиками отключений.
        Избегает ложных срабатываний на общие слова типа "світло".
        """
        if not text:
            return False
        
        text_lower = text.lower()
        
        # Проверяем наличие хотя бы одного ключевого слова
        for keyword in KEYWORDS:
            if keyword.lower() in text_lower:
                logger.info(f"   ✅ Найдено ключевое слово: '{keyword}'")
                return True
        
        logger.debug(f"   ❌ Ключевые слова не найдены в тексте")
        return False
    
    def save_last_message_id(self, message_id: int, channel_username: str):
        """Сохраняет ID последнего обработанного сообщения для конкретного канала в БД"""
        if not channel_username:
            return
        # Retry логика для надёжности (если БД locked или временная ошибка)
        for attempt in range(3):
            try:
                # Вызываем метод БД с правильным порядком параметров: (channel_username, message_id)
                self.db.save_last_message_id(channel_username, message_id)
                logger.debug(f"💾 Сохранён last_message_id={message_id} для канала @{channel_username} в БД")
                return  # Успешно сохранено
            except Exception as e:
                if attempt < 2:  # Не последняя попытка
                    logger.warning(f"⚠️ Попытка {attempt + 1}/3 сохранения last_message_id не удалась, повторяю через 1 сек: {e}")
                    import time
                    time.sleep(1)
                else:
                    logger.error(f"❌ Ошибка при сохранении last_message_id в БД после 3 попыток: {e}")
    
    def get_last_message_id(self, channel_username: str) -> int:
        """Получает ID последнего обработанного сообщения для конкретного канала из БД"""
        if not channel_username:
            return 0
        try:
            return self.db.get_last_message_id(channel_username)
        except Exception as e:
            logger.error(f"❌ Ошибка при получении last_message_id из БД: {e}")
            return 0
    
    async def process_photo(self, photo_bytes: bytes, city_name: str, text: str = None, schedule_type: str = None) -> bool:
        """
        Обрабатывает фото графика через Gemini API и сохраняет в базу
        
        Args:
            photo_bytes: Байты изображения
            city_name: Название города для сохранения графика
            text: Текст поста для определения даты (опционально)
            schedule_type: Тип графика - "today" или "tomorrow" (опционально, определяется автоматически)
        
        Returns:
            True если успешно обработано, False в противном случае
        """
        try:
            # Анализируем фото через Gemini (не блокируем event loop)
            schedule_data = await asyncio.to_thread(analyze_schedule_image, photo_bytes, "image/jpeg")
            
            if not schedule_data:
                logger.warning("Gemini не вернул данные из фото")
                return False
            
            # Находим или создаём город
            cities = self.db.get_cities()
            city = next((c for c in cities if c.name.lower() == city_name.lower()), None)
            
            if not city:
                # Создаём город если его нет
                try:
                    city_id = self.db.add_city(city_name)
                    city = self.db.get_city(city_id)
                    logger.info(f"Создан новый город: {city_name}")
                except Exception as e:
                    logger.error(f"Ошибка при создании города: {e}")
                    return False
            
            # Определяем тип графика (сегодня или завтра) через Gemini
            # Это делается только для фото, так как для текста уже используется check_schedule_post_and_date
            if schedule_type is None:
                if text:
                    from gemini_service import check_schedule_post_and_date
                    schedule_type = await asyncio.to_thread(check_schedule_post_and_date, text)
                if schedule_type is None:
                    # По умолчанию сохраняем как завтра
                    schedule_type = "tomorrow"
                    logger.info(f"   📅 Дата не определена, сохраняю как завтра (по умолчанию)")
                else:
                    logger.info(f"   📅 Определена дата: {schedule_type}")
            
            # Получаем старый график для сравнения
            old_schedule = self.db.get_schedule(city.id, schedule_type) or {}
            
            # Сохраняем график в базу в правильное поле
            self.db.save_schedule(city.id, schedule_data, schedule_type)
            
            groups_list = sorted(schedule_data.keys())
            logger.info(f"✅ График ({schedule_type}) обновлён для города '{city_name}'. Распознано групп: {len(schedule_data)}")
            logger.info(f"   📋 Распознанные группы: {', '.join(groups_list)}")
            
            # Проверяем изменения и отправляем уведомления
            # Вызываем асинхронно, чтобы не блокировать
            # Передаём old_schedule или пустой dict, если его нет
            asyncio.create_task(self._notify_subscribers_about_changes(city.id, city.name, old_schedule or {}, schedule_data, schedule_type))
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при обработке фото: {e}")
            return False
    
    async def _notify_subscribers_about_changes(self, city_id: int, city_name: str, old_schedule: dict, new_schedule: dict, schedule_type: str = "today"):
        """
        Отправляет уведомления подписчикам об изменениях в графике
        
        Args:
            city_id: ID города
            city_name: Название города
            old_schedule: Старый график (dict с группами и интервалами)
            new_schedule: Новый график (dict с группами и интервалами)
            schedule_type: Тип графика - "today" или "tomorrow"
        """
        if not self.bot_application:
            logger.debug("Бот не настроен для отправки уведомлений")
            return
        
        # Находим все группы, которые изменились
        changed_groups = set()
        
        # Если old_schedule пустой - это первое сохранение, все группы считаются новыми
        if not old_schedule:
            # При первом сохранении все группы из нового графика считаются изменёнными
            for group in new_schedule.keys():
                if group != '_meta':  # Исключаем метаданные
                    changed_groups.add(group)
                    logger.info(f"📊 Первое сохранение графика, группа {group}: {new_schedule.get(group, [])}")
        else:
            # Проверяем изменения в существующих группах
            for group in set(old_schedule.keys()) | set(new_schedule.keys()):
                if group == '_meta':  # Пропускаем метаданные
                    continue
                old_intervals = old_schedule.get(group, [])
                new_intervals = new_schedule.get(group, [])
                
                # Сравниваем интервалы (приводим к спискам для сравнения)
                if sorted(old_intervals) != sorted(new_intervals):
                    changed_groups.add(group)
                    logger.info(f"📊 Изменение в группе {group}: {old_intervals} → {new_intervals}")
        
        if not changed_groups:
            logger.debug("Нет изменений в графике")
            return
        
        # Убеждаемся, что city_name правильный (получаем из БД для надёжности)
        city = self.db.get_city(city_id)
        if not city:
            logger.error(f"Город с ID {city_id} не найден в базе данных")
            return
        
        # Используем имя города из БД (более надёжно)
        actual_city_name = city.name
        if actual_city_name != city_name:
            logger.warning(f"Несоответствие имени города: передан '{city_name}', в БД '{actual_city_name}'. Использую из БД.")
            city_name = actual_city_name
        
        # Находим всех людей с изменёнными группами в этом городе
        # ВАЖНО: Обёртываем в to_thread, чтобы не блокировать event loop
        people = await asyncio.to_thread(self.db.get_people, city_id)
        affected_people = [p for p in people if p.group in changed_groups]
        
        if not affected_people:
            logger.debug(f"Нет людей с группами {changed_groups} в городе {city_name}")
            return
        
        logger.info(f"🔔 Найдено {len(affected_people)} человек с изменёнными графиками в городе {city_name}")
        
        # Отправляем уведомления каждому подписчику
        for person in affected_people:
            # ВАЖНО: Обёртываем в to_thread, чтобы не блокировать event loop
            subscribers = await asyncio.to_thread(self.db.get_subscribers_for_person, person.id)
            if not subscribers:
                continue
            
            # ВАЖНО: Получаем ОБА графика (сегодня и завтра) для группы человека
            # Это нужно для того, чтобы уведомление показывало полную картину
            from helpers import get_kyiv_time
            current_time = get_kyiv_time()
            current_date = current_time.date()
            
            # Получаем график на сегодня для группы
            schedule_intervals_today = await asyncio.to_thread(
                self.db.get_schedule_for_group, city_id, person.group, "today"
            ) or []
            
            # Получаем график на завтра для группы
            schedule_intervals_tomorrow = await asyncio.to_thread(
                self.db.get_schedule_for_group, city_id, person.group, "tomorrow"
            ) or []
            
            # ВАЖНО: Проверяем актуальность графиков и правильность их размещения
            # (та же логика, что и в view_schedule_person)
            schedule_was_promoted = False
            if schedule_intervals_tomorrow:
                schedule_tomorrow_updated_at = await asyncio.to_thread(
                    self.db.get_schedule_updated_at, city_id, "tomorrow"
                )
                if schedule_tomorrow_updated_at:
                    if isinstance(schedule_tomorrow_updated_at, datetime):
                        tomorrow_schedule_date = schedule_tomorrow_updated_at.date()
                    else:
                        tomorrow_schedule_date = schedule_tomorrow_updated_at
                    
                    days_diff_tomorrow = (current_date - tomorrow_schedule_date).days
                    if days_diff_tomorrow == 1:
                        logger.info(f"🔄 График в 'tomorrow' был обновлён вчера, используем его как 'today'")
                        schedule_intervals_today = schedule_intervals_tomorrow
                        schedule_intervals_tomorrow = []
                        schedule_was_promoted = True
            
            # Проверяем актуальность графика на "today"
            if schedule_intervals_today and not schedule_was_promoted:
                schedule_updated_at = await asyncio.to_thread(
                    self.db.get_schedule_updated_at, city_id, "today"
                )
                if schedule_updated_at:
                    if isinstance(schedule_updated_at, datetime):
                        schedule_date = schedule_updated_at.date()
                    else:
                        schedule_date = schedule_updated_at
                    
                    days_diff = (current_date - schedule_date).days
                    if days_diff > 1:
                        logger.info(f"⚠️ График на 'today' устарел, не показываю")
                        schedule_intervals_today = []
            
            # Используем график на сегодня для определения статуса
            active_intervals = schedule_intervals_today or []
            
            if not active_intervals and not schedule_intervals_tomorrow:
                # Если нет данных для группы, отправляем простое уведомление
                message = (
                    f"🔔 <b>Обновление графика!</b>\n\n"
                    f"👤 {person.name}\n"
                    f"🏙️ Город: {city_name}\n"
                    f"⚡ Группа: {person.group}\n\n"
                    f"⚠️ График обновлён, но данные для группы {person.group} пока не доступны."
                )
            else:
                # Получаем текущий статус (используем график на сегодня)
                status_info = get_schedule_status(active_intervals, current_time)
                
                # Получаем даты для отображения
                today_date = current_time.strftime('%d.%m')
                tomorrow_date = (current_time + timedelta(days=1)).strftime('%d.%m') if schedule_intervals_tomorrow else None
                
                # Формируем красивое сообщение с помощью Gemini
                # ВАЖНО: Сохраняем данные в локальные переменные для изоляции между пользователями
                try:
                    current_time_str = current_time.strftime("%H:%M")
                    
                    # Сохраняем данные для этого конкретного человека в локальные переменные
                    notify_person_name = person.name
                    notify_city_name = city_name
                    notify_group = person.group
                    notify_intervals = active_intervals
                    notify_intervals_tomorrow = schedule_intervals_tomorrow or []
                    notify_status = status_info.get("message", "✅ Свет есть")
                    notify_next = status_info.get("nextChange", "")
                    notify_time_to = status_info.get("timeToNextChange", "")
                    
                    # Выполняем синхронный вызов Gemini в отдельном потоке
                    # ВАЖНО: Передаём оба графика (сегодня и завтра) с датами
                    message = await asyncio.to_thread(
                        generate_schedule_response,
                        notify_person_name,
                        notify_city_name,
                        notify_group,
                        notify_intervals,
                        current_time_str,
                        notify_status,
                        notify_next,
                        notify_time_to,
                        notify_intervals_tomorrow if notify_intervals_tomorrow else None,
                        today_date,
                        tomorrow_date
                    )
                    # Добавляем заголовок об обновлении
                    message = f"🔔 <b>Обновление графика!</b>\n\n{message}"
                except Exception as e:
                    logger.error(f"Ошибка при генерации ответа через Gemini: {e}")
                    # Fallback на простой формат с обоими графиками
                    intervals_today_text = "\n".join([f"• {interval}" for interval in active_intervals]) if active_intervals else "График не загружен."
                    message = (
                        f"🔔 <b>Обновление графика!</b>\n\n"
                        f"👤 {person.name}\n"
                        f"🏙️ Город: {city_name}\n"
                        f"⚡ Группа: {person.group}\n\n"
                    )
                    
                    if today_date:
                        message += f"📅 {today_date}\n"
                    message += f"⚡ <b>График відключень на сьогодні:</b>\n{intervals_today_text}\n\n"
                    
                    if schedule_intervals_tomorrow:
                        intervals_tomorrow_text = "\n".join([f"• {interval}" for interval in schedule_intervals_tomorrow])
                        if tomorrow_date:
                            message += f"📅 {tomorrow_date}\n"
                        message += f"⚡ <b>График відключень на завтра:</b>\n{intervals_tomorrow_text}\n\n"
                    
                    message += (
                        f"🕐 Поточний час: {current_time.strftime('%H:%M')}\n\n"
                        f"{status_info.get('message', '✅ Свет есть')}\n"
                        f"{status_info.get('nextChange', '')}\n"
                        f"{status_info.get('timeToNextChange', '')}"
                    )
            
            # Отправляем уведомление каждому подписчику
            # Теперь мы в async контексте, можем использовать await
            for user_id in subscribers:
                try:
                    await self._send_notification(user_id, message)
                    logger.info(f"📤 Уведомление отправлено пользователю {user_id} о {person.name}")
                except Exception as e:
                    logger.error(f"❌ Ошибка при отправке уведомления пользователю {user_id}: {e}")
    
    async def _send_notification(self, user_id: int, message: str):
        """Отправляет уведомление пользователю"""
        try:
            if self.bot_application:
                await self.bot_application.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомления пользователю {user_id}: {e}")
    
    async def download_and_process_photos(self, message, channel_to_city_map: dict = None) -> bool:
        """
        Скачивает и обрабатывает все фото из сообщения
        
        Args:
            message: Telegram сообщение
            channel_to_city_map: Словарь для быстрого определения города по каналу
                                 (оптимизация, чтобы не делать лишние запросы к БД)
        
        Returns:
            True если хотя бы одно фото успешно обработано
        """
        if not message.media:
            return False
        
        success = False
        
        try:
            # ПРИОРИТЕТ 1: Определяем город по каналу из базы данных
            # Это самый надёжный способ, так как канал явно привязан к городу
            city_name = None
            try:
                # Получаем entity канала
                entity = await self.client.get_entity(message.peer_id)
                if hasattr(entity, 'username') and entity.username:
                    # Используем словарь для быстрого доступа (если передан)
                    if channel_to_city_map and entity.username in channel_to_city_map:
                        city_name = channel_to_city_map[entity.username]
                        logger.info(f"   🏙️ [ПРИОРИТЕТ 1] Город определён по каналу из БД: {city_name} (канал: @{entity.username})")
                    else:
                        # Fallback: ищем в базе данных
                        channels = self.db.get_all_channels()
                        for ch in channels:
                            if ch.channel_username == entity.username:
                                city = self.db.get_city(ch.city_id)
                                if city:
                                    city_name = city.name
                                    logger.info(f"   🏙️ [ПРИОРИТЕТ 1] Город определён по каналу из БД: {city_name} (канал: @{entity.username})")
                                    break
            except Exception as e:
                logger.debug(f"   ⚠️ Не удалось определить город по каналу: {e}")
            
            # ПРИОРИТЕТ 2: Если город не найден по каналу, определяем из текста
            if not city_name:
                # В Telethon текст может быть в message.text, message.message, или message.raw_text
                text = (
                    message.text or 
                    getattr(message, 'message', None) or 
                    getattr(message, 'raw_text', None) or 
                    ""
                )
                logger.info(f"   📝 Текст поста: {text[:200] if text else 'Нет текста'}...")
                
                city_name = self.extract_city_from_text(text)
                if city_name:
                    logger.info(f"   🏙️ [ПРИОРИТЕТ 2] Город определён из текста: {city_name}")
            
            # ПРИОРИТЕТ 3: Если город не найден в тексте, пробуем определить из изображения
            # (это дорогая операция, поэтому делаем только если нужно)
            if not city_name:
                logger.info(f"   🔍 Город не найден в тексте, пытаюсь определить из изображения...")
                # Скачиваем первое фото для анализа
                if message.photo:
                    buffer = BytesIO()
                    await message.download_media(file=buffer)
                    photo_bytes = buffer.getvalue()
                    if photo_bytes:
                        city_name = self.extract_city_from_image(photo_bytes)
                        if city_name:
                            logger.info(f"   🏙️ [ПРИОРИТЕТ 3] Город определён из изображения: {city_name}")
            
            # Если всё ещё не найден, пробуем использовать первый город из базы
            if not city_name:
                cities = self.db.get_cities()
                if cities:
                    city_name = cities[0].name
                    logger.warning(f"   ⚠️ Город не определён, использую первый город из базы: {city_name}")
                else:
                    city_name = "Днепр"  # Fallback
                    logger.warning(f"   ⚠️ Город не определён, использую fallback: {city_name}")
            else:
                logger.info(f"   ✅ Определён город: {city_name}")
            
            # Определяем дату из текста поста через Gemini
            text = (
                message.text or 
                getattr(message, 'message', None) or 
                getattr(message, 'raw_text', None) or 
                ""
            )
            from gemini_service import check_schedule_post_and_date
            schedule_type = await asyncio.to_thread(check_schedule_post_and_date, text)
            if schedule_type is None:
                # По умолчанию сохраняем как завтра
                schedule_type = "tomorrow"
                logger.info(f"   📅 Дата не определена из текста, сохраняю как завтра (по умолчанию)")
            else:
                logger.info(f"   📅 Определена дата из текста: {schedule_type}")
            
            # ВАЖНО: Сначала проверяем альбом, потом одно фото
            # Потому что альбом может иметь и photo, и grouped_id
            
            # Если альбом (несколько фото)
            if hasattr(message, 'grouped_id') and message.grouped_id:
                logger.info("📸 Обнаружен альбом, обрабатываю все фото...")
                merged_schedule = {}
                photo_count = 0
                
                # Получаем все сообщения из альбома
                # ВАЖНО: Увеличиваем диапазон поиска для надёжности (альбомы могут приходить не по порядку)
                logger.info(f"   🔍 Ищу все фото альбома (grouped_id: {message.grouped_id})...")
                album_messages_list = []
                async for msg in self.client.iter_messages(
                    message.peer_id,
                    min_id=message.id - 50,
                    max_id=message.id + 50
                ):
                    if (hasattr(msg, 'grouped_id') and 
                        msg.grouped_id == message.grouped_id and 
                        msg.photo):
                        album_messages_list.append(msg)
                
                # Сортируем по ID для правильного порядка
                album_messages_list.sort(key=lambda m: m.id)
                logger.info(f"   📊 Найдено {len(album_messages_list)} фото в альбоме")
                
                for msg in album_messages_list:
                    photo_count += 1
                    logger.info(f"📸 Фото {photo_count}/{len(album_messages_list)} из альбома (ID сообщения: {msg.id})...")
                    
                    try:
                        buffer = BytesIO()
                        await msg.download_media(file=buffer)
                        photo_bytes = buffer.getvalue()
                        
                        if photo_bytes:
                            logger.info(f"✅ Фото {photo_count} скачано ({len(photo_bytes)} байт)")
                            logger.info(f"🤖 Анализирую фото {photo_count}...")
                            # ВАЖНО: Не блокируем event loop
                            schedule_data = await asyncio.to_thread(analyze_schedule_image, photo_bytes, "image/jpeg")
                            if schedule_data:
                                groups_list = list(schedule_data.keys())
                                logger.info(f"✅ Фото {photo_count}: {len(schedule_data)} групп - {groups_list}")
                                
                                # Проверяем пересечения (если группа уже есть в merged_schedule)
                                overlapping_groups = []
                                for group in schedule_data.keys():
                                    if group in merged_schedule:
                                        overlapping_groups.append(group)
                                
                                if overlapping_groups:
                                    logger.warning(f"⚠️ Фото {photo_count}: группы {overlapping_groups} уже есть в предыдущих фото! Значения будут перезаписаны.")
                                
                                # Объединяем данные из всех фото альбома
                                # ВАЖНО: update() объединяет словари:
                                # - Если группа новая → добавляется
                                # - Если группа уже есть → перезаписывается значением из текущего фото
                                merged_schedule.update(schedule_data)
                                logger.info(f"📊 Всего групп в объединённом графике: {len(merged_schedule)}")
                                success = True
                            else:
                                logger.warning(f"⚠️ Фото {photo_count}: Gemini не вернул данные")
                        else:
                            logger.warning(f"⚠️ Фото {photo_count}: не удалось скачать (пустой файл)")
                    except Exception as e:
                        logger.error(f"❌ Ошибка при обработке фото {photo_count} (ID {msg.id}): {e}")
                        continue
                
                logger.info(f"📊 Обработано {photo_count} фото из альбома")
                
                # Сохраняем объединённый график один раз
                if merged_schedule:
                    # Находим или создаём город
                    cities = self.db.get_cities()
                    city = next((c for c in cities if c.name.lower() == city_name.lower()), None)
                    
                    if not city:
                        try:
                            city_id = self.db.add_city(city_name)
                            city = self.db.get_city(city_id)
                            logger.info(f"Создан новый город: {city_name}")
                        except Exception as e:
                            logger.error(f"Ошибка при создании города: {e}")
                            return False
                    
                    # Получаем старый график для сравнения
                    old_schedule = self.db.get_schedule(city.id, schedule_type) or {}
                    
                    # Сохраняем объединённый график в правильное поле
                    all_groups = sorted(merged_schedule.keys())
                    logger.info(f"💾 Сохраняю объединённый график ({schedule_type}) в базу:")
                    logger.info(f"   Всего групп: {len(merged_schedule)}")
                    logger.info(f"   Группы: {all_groups}")
                    self.db.save_schedule(city.id, merged_schedule, schedule_type)
                    
                    groups_list = sorted(merged_schedule.keys())
                    logger.info(f"✅ График ({schedule_type}) обновлён для города '{city_name}'. Распознано групп: {len(merged_schedule)}")
                    logger.info(f"   📋 Распознанные группы: {', '.join(groups_list)}")
                    
                    # Проверяем изменения и отправляем уведомления
                    # Вызываем асинхронно, чтобы не блокировать
                    # Передаём old_schedule или пустой dict, если его нет
                    asyncio.create_task(self._notify_subscribers_about_changes(city.id, city.name, old_schedule or {}, merged_schedule, schedule_type))
            
            # Если одно фото (не альбом)
            elif message.photo:
                logger.info("📸 Обнаружено одно фото, обрабатываю...")
                # Скачиваем фото в BytesIO, затем конвертируем в bytes
                buffer = BytesIO()
                await message.download_media(file=buffer)
                photo_bytes = buffer.getvalue()
                if photo_bytes:
                    # Передаём текст и schedule_type в process_photo
                    if await self.process_photo(photo_bytes, city_name, text, schedule_type):
                        success = True
                        logger.info(f"✅ Обработано фото из поста ID {message.id}")
        
        except Exception as e:
            logger.error(f"Ошибка при скачивании/обработке фото: {e}")
        
        return success
    
    def extract_city_from_text(self, text: str) -> Optional[str]:
        """
        Извлекает название города из текста поста
        Проверяет все города из базы данных, а не только хардкод список
        """
        if not text:
            logger.debug("   📝 Текст поста пуст, город не найден")
            return None
        
        text_lower = text.lower()
        logger.debug(f"   📝 Анализирую текст для определения города: {text[:100]}...")
        
        # Получаем все города из базы данных
        cities = self.db.get_cities()
        
        # Создаём расширенный словарь ключевых слов для каждого города
        # Включаем различные варианты написания и склонения
        cities_map = {}
        
        for city in cities:
            city_name_lower = city.name.lower()
            cities_map[city_name_lower] = city.name
            
            # Добавляем варианты написания для популярных городов
            if "дніпро" in city_name_lower or "днепр" in city_name_lower:
                cities_map["дніпро"] = city.name
                cities_map["днепр"] = city.name
                cities_map["дніпропетровськ"] = city.name
                cities_map["днепропетровск"] = city.name
                cities_map["дніпропетровщина"] = city.name
                cities_map["днепропетровщина"] = city.name
            elif "київ" in city_name_lower or "киев" in city_name_lower:
                cities_map["київ"] = city.name
                cities_map["киев"] = city.name
            elif "харків" in city_name_lower or "харьков" in city_name_lower:
                cities_map["харків"] = city.name
                cities_map["харьков"] = city.name
            elif "одеса" in city_name_lower or "одесса" in city_name_lower:
                cities_map["одеса"] = city.name
                cities_map["одесса"] = city.name
            elif "львів" in city_name_lower or "львов" in city_name_lower:
                cities_map["львів"] = city.name
                cities_map["львов"] = city.name
            elif "кривий" in city_name_lower or "кривой" in city_name_lower:
                cities_map["кривий ріг"] = city.name
                cities_map["кривой рог"] = city.name
                cities_map["кривий"] = city.name
                cities_map["кривой"] = city.name
            elif "запоріжжя" in city_name_lower or "запорожье" in city_name_lower:
                cities_map["запоріжжя"] = city.name
                cities_map["запорожье"] = city.name
                cities_map["запоріжжя"] = city.name
        
        # Проверяем наличие названий городов в тексте
        for keyword, city_name in cities_map.items():
            if keyword in text_lower:
                logger.info(f"   🏙️ Город определён из текста: {city_name} (ключевое слово: '{keyword}')")
                return city_name
        
        # Если не нашли точное совпадение, пробуем частичное совпадение
        # (на случай если в тексте есть "Кривий Ріг" или "Кривого Рогу")
        for city in cities:
            city_name_lower = city.name.lower()
            # Проверяем, содержит ли текст название города (или его часть)
            city_words = city_name_lower.split()
            if len(city_words) > 0:
                # Проверяем первое слово города (например, "кривий" для "Кривий Ріг")
                if city_words[0] in text_lower and len(city_words[0]) > 3:
                    logger.info(f"   🏙️ Город определён из текста (частичное совпадение): {city.name}")
                    return city.name
        
        logger.debug(f"   ⚠️ Город не найден в тексте поста")
        return None
    
    def extract_city_from_image(self, photo_bytes: bytes) -> Optional[str]:
        """
        Пытается определить город из изображения через Gemini
        Проверяет города из базы данных, а не только хардкод список
        """
        try:
            from gemini_service import analyze_schedule_image
            import google.generativeai as genai
            
            # Получаем список городов из базы данных для проверки
            cities = self.db.get_cities()
            city_names_list = ", ".join([city.name for city in cities])
            
            # Используем Gemini для определения города из изображения
            model = genai.GenerativeModel('gemini-2.5-flash-lite')
            
            prompt = f"""Проанализируй это изображение графика отключений электричества.

Определи, для какого города этот график. Название города может быть написано в заголовке, в тексте на изображении, или в других местах.

Верни ТОЛЬКО название города точно так, как оно написано на изображении (с сохранением регистра и украинских букв).

Доступные города в системе: {city_names_list}

Если не можешь определить город, верни "НЕИЗВЕСТНО".

Примеры ответов:
- Днепр
- Кривий Ріг
- Запоріжжя
- Київ
- НЕИЗВЕСТНО"""
            
            response = model.generate_content([prompt, {
                "mime_type": "image/jpeg",
                "data": photo_bytes
            }])
            
            city_name = response.text.strip()
            
            # Проверяем, что это валидное название города из базы данных
            # Сравниваем без учета регистра
            for city in cities:
                if city.name.lower() == city_name.lower():
                    logger.info(f"   🏙️ Город определён из изображения: {city.name}")
                    return city.name
            
            # Если точного совпадения нет, пробуем нормализовать и сравнить
            normalized_response = self.db.normalize_city_name(city_name)
            for city in cities:
                if city.name.lower() == normalized_response.lower():
                    logger.info(f"   🏙️ Город определён из изображения (после нормализации): {city.name}")
                    return city.name
            
            logger.debug(f"   ⚠️ Gemini вернул город '{city_name}', но его нет в базе данных")
            logger.debug(f"   💡 Доступные города: {[c.name for c in cities]}")
            return None
                
        except Exception as e:
            logger.warning(f"   ⚠️ Ошибка при определении города из изображения: {e}")
            return None
    
    async def start_monitoring(self):
        """Запускает мониторинг всех каналов из базы данных"""
        if not API_ID or not API_HASH:
            logger.error("API_ID и API_HASH не установлены. Мониторинг недоступен.")
            return
        
        if self.is_running:
            logger.warning("Мониторинг уже запущен")
            return
        
        try:
            # Создаём клиент с сессией
            if SESSION_STRING:
                logger.info("🔑 Использую StringSession из переменной окружения")
                session = StringSession(SESSION_STRING)
            else:
                logger.info("🔑 Использую файл сессии channel_monitor.session")
                session = 'channel_monitor'
            
            self.client = TelegramClient(session, API_ID, API_HASH)
            
            # Подключаемся
            await self.client.connect()
            
            # Проверяем авторизацию
            if not await self.client.is_user_authorized():
                logger.error("=" * 50)
                logger.error("❌ ОШИБКА АВТОРИЗАЦИИ!")
                logger.error("=" * 50)
                logger.error("Telegram клиент не авторизован!")
                logger.error("💡 Решение:")
                logger.error("   1. Запустите: python generate_session.py")
                logger.error("   2. Или добавьте TELEGRAM_SESSION_STRING в .env файл")
                logger.error("=" * 50)
                await self.client.disconnect()
                self.is_running = False
                return
            
            logger.info("=" * 50)
            logger.info("🤖 МОНИТОРИНГ КАНАЛОВ ЗАПУЩЕН")
            logger.info("=" * 50)
            logger.info(f"✅ Telegram клиент авторизован")
            
            # Список заблокированных каналов (не мониторятся)
            BLOCKED_CHANNELS = ["dtek_ua"]
            
            # Получаем все каналы из базы данных
            channels = self.db.get_all_channels()
            
            # Если каналов нет в БД, сообщаем пользователю
            if not channels:
                logger.warning("=" * 60)
                logger.warning("⚠️ В базе данных нет каналов для мониторинга!")
                logger.warning("=" * 60)
                logger.info("💡 Добавьте каналы через интерфейс бота:")
                logger.info("   1. Управление городами → Выберите город → Управление каналом → Добавить канал")
                logger.info("   2. Или используйте скрипт миграции: python migrate_channels.py")
                logger.warning("=" * 60)
                await self.client.disconnect()
                self.is_running = False
                return
            
            # Фильтруем заблокированные каналы
            channels_to_monitor = []
            for ch in channels:
                if ch.channel_username.lower() not in [blocked.lower() for blocked in BLOCKED_CHANNELS]:
                    channels_to_monitor.append(ch.channel_username)
                else:
                    logger.warning(f"🚫 Канал @{ch.channel_username} заблокирован и будет пропущен")
            
            logger.info(f"📱 Найдено каналов в базе данных: {len(channels)} (активных: {len(channels_to_monitor)})")
            for ch in channels:
                city = self.db.get_city(ch.city_id)
                city_name = city.name if city else f"ID {ch.city_id}"
                status = "🚫 ЗАБЛОКИРОВАН" if ch.channel_username.lower() in [b.lower() for b in BLOCKED_CHANNELS] else "✅"
                logger.info(f"   {status} @{ch.channel_username} (город: {city_name})")
            
            # Проверяем доступ ко всем каналам и сохраняем их ID
            valid_channels = []
            monitored_channels_by_id = {}
            
            # Создаем карту username -> channel object для удобства
            username_to_channel = {ch.channel_username.lower(): ch for ch in channels}
            
            for channel_username in channels_to_monitor:
                try:
                    entity = await self.client.get_entity(channel_username)
                    valid_channels.append(channel_username)
                    
                    # Сохраняем ID канала для надежного определения
                    if hasattr(entity, 'id'):
                        ch_obj = username_to_channel.get(channel_username.lower())
                        if ch_obj:
                            monitored_channels_by_id[entity.id] = ch_obj
                            logger.info(f"✅ Доступ к каналу @{channel_username} подтверждён (ID: {entity.id})")
                        else:
                             logger.info(f"✅ Доступ к каналу @{channel_username} подтверждён")
                    else:
                        logger.info(f"✅ Доступ к каналу @{channel_username} подтверждён")
                        
                except Exception as e:
                    logger.error(f"❌ ОШИБКА: Не удалось получить доступ к каналу @{channel_username}")
                    logger.error(f"   Ошибка: {e}")
                    logger.error("💡 Убедитесь, что:")
                    logger.error("   1. Канал существует и доступен")
                    logger.error("   2. Ваш аккаунт подписан на канал")
                    logger.error("   3. Имя канала указано правильно (без @)")
            
            if not valid_channels:
                logger.error("❌ Нет доступных каналов для мониторинга!")
                await self.client.disconnect()
                self.is_running = False
                return
            
            logger.info(f"\n📊 Мониторинг {len(valid_channels)} каналов")
            
            # Инициализируем Lock для атомарных операций с альбомами
            self._processed_albums_lock = asyncio.Lock()
            
            # Создаём словарь для быстрого доступа: channel_username -> city_name
            # Это позволяет быстро определить город по каналу без запросов к БД
            channel_to_city_map = {}
            if channels:  # Только если есть каналы в БД
                for ch in channels:
                    city = self.db.get_city(ch.city_id)
                    if city:
                        channel_to_city_map[ch.channel_username] = city.name
                        logger.info(f"   📌 @{ch.channel_username} → {city.name}")
            
            # Для каждого канала устанавливаем точку отсчёта при первом запуске
            for channel_username in valid_channels:
                last_processed_id = self.get_last_message_id(channel_username)
                if last_processed_id == 0:
                    try:
                        latest_messages = await self.client.get_messages(channel_username, limit=1)
                        if latest_messages:
                            latest_id = latest_messages[0].id
                            self.save_last_message_id(latest_id, channel_username)
                            logger.info(f"🆕 Точка отсчёта для @{channel_username}: ID {latest_id}")
                    except Exception as e:
                        logger.warning(f"⚠️ Не удалось установить точку отсчёта для @{channel_username}: {e}")
            
            # ВАЖНО: При запуске проверяем последние посты во всех каналах
            # Обрабатываем только последние 5 постов для каждого канала (защита от массовой обработки)
            for channel_username in valid_channels:
                try:
                    last_processed_id = self.get_last_message_id(channel_username)
                    latest_messages = await self.client.get_messages(channel_username, limit=5)
                    
                    if latest_messages:
                        latest_id = latest_messages[0].id
                        logger.info(f"📊 Канал @{channel_username}: последний пост ID {latest_id}")
                        
                        if last_processed_id > 0:
                            logger.info(f"📝 Канал @{channel_username}: последний обработанный пост ID {last_processed_id}")
                            
                            # Проверяем, есть ли необработанные посты
                            if latest_id > last_processed_id:
                                logger.info(f"🆕 Канал @{channel_username}: найдены необработанные посты! Проверяю последние 5...")
                                processed_count = 0
                                for msg in reversed(latest_messages):
                                    if msg.id > last_processed_id:
                                        # Извлекаем текст
                                        text = (
                                            msg.text or 
                                            getattr(msg, 'message', None) or 
                                            getattr(msg, 'raw_text', None) or 
                                            ""
                                        )
                                        
                                        logger.info(f"\n{'='*60}")
                                        logger.info(f"📩 ПРОВЕРКА ПОСТА ПРИ ЗАПУСКЕ (@{channel_username})")
                                        logger.info(f"{'='*60}")
                                        logger.info(f"   📌 ID: {msg.id}")
                                        logger.info(f"   📅 Время: {msg.date.strftime('%Y-%m-%d %H:%M:%S') if msg.date else 'Неизвестно'}")
                                        logger.info(f"   📝 Текст: {text[:200] if text else 'Нет текста'}...")
                                        
                                        # Проверяем наличие фото
                                        has_photo = msg.photo or (hasattr(msg, 'grouped_id') and msg.grouped_id)
                                        
                                        if has_photo:
                                            schedule_type = None  # Fix UnboundLocalError
                                            logger.info(f"   📸 Обнаружены фотографии, начинаю обработку...")
                                            logger.info(f"   🤖 Отправляю фото в Gemini Vision для анализа...")
                                            success = await self.download_and_process_photos(msg, channel_to_city_map)
                                            if success:
                                                logger.info(f"   ✅ Пост ID {msg.id} обработан!")
                                        elif text and len(text.strip()) > 0:
                                            # Нет фото, но есть текст - проверяем через Gemini
                                            from gemini_service import check_schedule_post_and_date, analyze_schedule_text
                                            
                                            # Проверяем через Gemini (выполняем в отдельном потоке)
                                            # Используем новую функцию, которая сразу определяет и график, и дату
                                            schedule_type = await asyncio.to_thread(check_schedule_post_and_date, text)
                                            if schedule_type:
                                                schedule_data = await asyncio.to_thread(analyze_schedule_text, text)
                                                if schedule_data and len(schedule_data) > 0:
                                                    # Определяем город и сохраняем
                                                    city_name = None
                                                    if channel_to_city_map:
                                                        city_name = channel_to_city_map.get(channel_username)
                                                    
                                                    if not city_name:
                                                        city_name = self.extract_city_from_text(text)
                                                    
                                                    if not city_name:
                                                        cities = self.db.get_cities()
                                                        city_name = cities[0].name if cities else "Днепр"
                                                    
                                                    cities = self.db.get_cities()
                                                    city = next((c for c in cities if c.name.lower() == city_name.lower()), None)
                                                    
                                                    if not city:
                                                        try:
                                                            city_id = self.db.add_city(city_name)
                                                            city = self.db.get_city(city_id)
                                                        except:
                                                            city = None
                                                    
                                                    if city:
                                                        old_schedule = self.db.get_schedule(city.id, schedule_type) or {}
                                                        from gemini_service import is_complete_schedule, merge_schedules
                                                        
                                                        if is_complete_schedule(schedule_data, old_schedule):
                                                            self.db.save_schedule(city.id, schedule_data, schedule_type)
                                                        else:
                                                            if old_schedule:
                                                                merged_schedule = merge_schedules(old_schedule, schedule_data)
                                                                self.db.save_schedule(city.id, merged_schedule, schedule_type)
                                                            else:
                                                                self.db.save_schedule(city.id, schedule_data, schedule_type)
                                                        
                                                        final_groups = len([k for k in (self.db.get_schedule(city.id, schedule_type) or {}).keys() if k != '_meta'])
                                                        logger.info(f"   ✅ График ({schedule_type}) из текста сохранён для поста ID {msg.id}. Всего групп: {final_groups}")
                                        
                                        # Сохраняем ID
                                        self.save_last_message_id(msg.id, channel_username)
                                        processed_count += 1
                                        
                                        if processed_count >= 5:
                                            break
                        else:
                            # Первый запуск для этого канала - устанавливаем точку отсчёта
                            self.save_last_message_id(latest_id, channel_username)
                            logger.info(f"🆕 Канал @{channel_username}: точка отсчёта установлена (ID {latest_id})")
                except Exception as e:
                    logger.error(f"⚠️ Ошибка при проверке канала @{channel_username}: {e}")
            
            self.is_running = True
            
            # Запускаем периодическую очистку старых альбомов
            asyncio.create_task(self._cleanup_old_albums())
            
            # Обработчик новых сообщений для всех каналов
            # ВАЖНО: Слушаем ВСЕ новые сообщения и фильтруем внутри
            # Это надежнее, чем фильтр chats=..., который может не сработать если кеш не обновлен
            @self.client.on(events.NewMessage())
            async def handler(event):
                try:
                    message = event.message
                    chat_id = event.chat_id
                    
                    # Получаем информацию о чате
                    try:
                        chat = await event.get_chat()
                        chat_username = getattr(chat, 'username', None)
                        chat_title = getattr(chat, 'title', 'Unknown')
                    except:
                        chat_username = None
                        chat_title = "Unknown"

                    # Логируем каждое входящее сообщение для отладки (но коротко)
                    # logger.info(f"📨 Получено сообщение из: {chat_title} (ID: {chat_id}, @{chat_username})")
                    
                    # Проверяем, отслеживаем ли мы этот канал
                    is_monitored = False
                    monitored_city_name = None
                    channel_username_for_message = chat_username
                    
                    # Проверяем по username
                    monitored_channel_obj = None
                    
                    # ПРИОРИТЕТ 1: Проверка по ID (самая надежная)
                    channel_id = None
                    if hasattr(message.peer_id, 'channel_id'):
                        channel_id = message.peer_id.channel_id
                    elif hasattr(message.peer_id, 'chat_id'):
                        channel_id = message.peer_id.chat_id
                    elif hasattr(message.peer_id, 'user_id'):
                        channel_id = message.peer_id.user_id
                        
                    if channel_id and channel_id in monitored_channels_by_id:
                        is_monitored = True
                        monitored_channel_obj = monitored_channels_by_id[channel_id]
                        # Находим имя города
                        city = self.db.get_city(monitored_channel_obj.city_id)
                        monitored_city_name = city.name if city else "Unknown"
                        logger.debug(f"✅ Канал определен по ID: {channel_id} -> {monitored_city_name}")

                    # ПРИОРИТЕТ 2: Проверка по username (если по ID не нашли)
                    if not is_monitored and chat_username:
                        # ВАЖНО: Получаем актуальный список каналов из БД при каждом сообщении
                        # Это позволяет подхватывать новые каналы без перезагрузки
                        current_channels = self.db.get_all_channels()
                        logger.debug(f"🔍 Проверяю канал @{chat_username} против {len(current_channels)} каналов в БД")
                        
                        for channel in current_channels:
                            if channel.channel_username and chat_username.lower() == channel.channel_username.lower():
                                is_monitored = True
                                monitored_channel_obj = channel
                                # Находим имя города
                                city = self.db.get_city(channel.city_id)
                                monitored_city_name = city.name if city else "Unknown"
                                logger.info(f"✅ Канал @{chat_username} найден в БД (город: {monitored_city_name})")
                                break
                    
                    if not is_monitored:
                        # Логируем пропущенные сообщения для отладки
                        # Но только если это действительно канал (не личные сообщения)
                        if chat_username or (channel_id and channel_id > 0):
                            logger.debug(f"⏭️ Игнорирую сообщение из неотслеживаемого канала: {chat_title} (ID: {channel_id}, @{chat_username})")
                            # Показываем список отслеживаемых каналов для отладки
                            current_channels = self.db.get_all_channels()
                            if current_channels:
                                logger.debug(f"   📋 Отслеживаемые каналы: {[ch.channel_username for ch in current_channels]}")
                        return

                    logger.info(f"\n{'='*60}")
                    logger.info(f"🔔 НОВЫЙ ПОСТ в отслеживаемом канале!")
                    logger.info(f"   📺 Канал: {chat_title} (@{chat_username})")
                    logger.info(f"   🏙️ Город: {monitored_city_name}")
                    logger.info(f"   📌 ID поста: {message.id}")
                    logger.info(f"{'='*60}")
                    
                    # Получаем последний обработанный ID
                    last_processed_id = self.get_last_message_id(chat_username) if chat_username else 0
                    
                    # КРИТИЧЕСКАЯ ПРОВЕРКА: Пропускаем старые посты
                    if last_processed_id > 0 and message.id <= last_processed_id:
                        logger.info(f"⏭️ Пропущен старый пост ID {message.id} (последний обработанный: {last_processed_id})")
                        return
                    
                    # ВАЖНО: Для альбомов (grouped_id) обрабатываем только ПЕРВОЕ сообщение
                    # Используем Lock для атомарности (защита от race condition)
                    if hasattr(message, 'grouped_id') and message.grouped_id:
                        # Проверяем, что Lock инициализирован
                        if self._processed_albums_lock is None:
                            logger.warning("⚠️ Lock не инициализирован, пропускаю проверку альбома")
                        else:
                            async with self._processed_albums_lock:
                                if message.grouped_id in self._processed_albums:
                                    logger.debug(f"⏭️ Пропущено сообщение ID {message.id} - альбом {message.grouped_id} уже обрабатывается")
                                    self.save_last_message_id(message.id, chat_username)
                                    return
                                
                                self._processed_albums.add(message.grouped_id)
                        logger.info(f"📸 Обнаружен альбом {message.grouped_id}, получаю все сообщения...")
                        
                        # Собираем текст альбома
                        album_messages = []
                        album_text = ""
                        
                        # ВАЖНО: Увеличиваем диапазон поиска для надёжности
                        # Альбомы могут приходить не по порядку, поэтому ищем шире
                        async for msg in self.client.iter_messages(
                            message.peer_id,
                            min_id=message.id - 30,
                            max_id=message.id + 30
                        ):
                            if (hasattr(msg, 'grouped_id') and 
                                msg.grouped_id == message.grouped_id and
                                msg.photo):  # ВАЖНО: Только сообщения с фото
                                album_messages.append(msg)
                                msg_text = (msg.text or getattr(msg, 'message', None) or getattr(msg, 'raw_text', None) or "")
                                if msg_text and not album_text:
                                    album_text = msg_text
                        
                        # Если не нашли все фото в диапазоне, пробуем ещё раз с большим диапазоном
                        if len(album_messages) < 2:  # Если нашли меньше 2 фото, возможно альбом больше
                            logger.info(f"   ⚠️ Найдено только {len(album_messages)} фото, расширяю поиск...")
                            async for msg in self.client.iter_messages(
                                message.peer_id,
                                min_id=message.id - 50,
                                max_id=message.id + 50
                            ):
                                if (hasattr(msg, 'grouped_id') and 
                                    msg.grouped_id == message.grouped_id and
                                    msg.photo and
                                    msg not in album_messages):  # Избегаем дубликатов
                                    album_messages.append(msg)
                                    msg_text = (msg.text or getattr(msg, 'message', None) or getattr(msg, 'raw_text', None) or "")
                                    if msg_text and not album_text:
                                        album_text = msg_text
                        
                        logger.info(f"   📊 Найдено {len(album_messages)} фото в альбоме {message.grouped_id}")
                        
                        text = album_text
                        album_messages.sort(key=lambda m: m.id)
                        message = album_messages[0] if album_messages else message  # Работаем с первым сообщением или исходным
                        
                        # Очистка метки альбома будет выполнена периодически (см. _cleanup_old_albums)
                        
                    else:
                        text = (message.text or getattr(message, 'message', None) or getattr(message, 'raw_text', None) or "")

                    logger.info(f"   📌 ID: {message.id}")
                    logger.info(f"   📝 Текст: {text[:200] if text else 'Нет текста'}...")
                    
                    # Проверяем наличие фото
                    has_photo = message.photo or (hasattr(message, 'grouped_id') and message.grouped_id)
                    
                    if has_photo:
                            schedule_type = None  # Fix UnboundLocalError
                            logger.info(f"   📸 Обнаружены фотографии, начинаю обработку...")
                            logger.info(f"   🤖 ШАГ 2: Отправляю фото в Gemini Vision для анализа...")
                            
                            # Скачиваем фото (для одного фото или альбома)
                            # Используем существующий метод, но он блокирующий внутри (analyze_schedule_image)
                            # Поэтому лучше реализовать логику здесь, вызывая analyze_schedule_image через to_thread
                            
                            # ВАЖНО: Используем process_photo, но нужно убедиться что он не блокирует
                            # process_photo вызывает analyze_schedule_image.
                            # Чтобы исправить блокировку, нужно изменить process_photo или вызвать его в отдельном потоке?
                            # process_photo делает и БД операции, так что лучше изменить его внутри или здесь переписать логику.
                            
                            # Перепишем логику здесь для надежности и неблокируемости
                            from gemini_service import analyze_schedule_image
                            
                            success = False
                            processed_groups = 0
                            
                            # Если альбом
                            if hasattr(message, 'grouped_id') and message.grouped_id:
                                logger.info(f"   📸 Обрабатываю альбом (найдено {len(album_messages)} фото)...")
                                merged_schedule = {}
                                
                                # ВАЖНО: Если album_messages пуст, собираем заново
                                if not album_messages:
                                    logger.warning("   ⚠️ album_messages пуст, собираю заново...")
                                    async for msg in self.client.iter_messages(
                                        message.peer_id,
                                        min_id=message.id - 50,
                                        max_id=message.id + 50
                                    ):
                                        if (hasattr(msg, 'grouped_id') and 
                                            msg.grouped_id == message.grouped_id and
                                            msg.photo):
                                            album_messages.append(msg)
                                    album_messages.sort(key=lambda m: m.id)
                                    logger.info(f"   📊 Пересобрано {len(album_messages)} фото")
                                
                                # Мы уже получили album_messages выше (или пересобрали)
                                photo_count = 0
                                for msg in album_messages:
                                    if msg.photo:
                                        photo_count += 1
                                        try:
                                            logger.info(f"   📥 Скачиваю фото {photo_count}/{len(album_messages)} из альбома (ID сообщения: {msg.id})...")
                                            buffer = BytesIO()
                                            await msg.download_media(file=buffer)
                                            photo_bytes = buffer.getvalue()
                                            
                                            if photo_bytes:
                                                logger.info(f"   ✅ Фото {photo_count} скачано ({len(photo_bytes)} байт)")
                                                logger.info(f"   🤖 Отправляю фото {photo_count} в Gemini Vision для анализа...")
                                                # ВАЖНО: Выполняем анализ в отдельном потоке!
                                                schedule_data = await asyncio.to_thread(analyze_schedule_image, photo_bytes, "image/jpeg")
                                                
                                                if schedule_data:
                                                    groups_list = list(schedule_data.keys())
                                                    logger.info(f"   ✅ Фото {photo_count}: Gemini вернул {len(schedule_data)} групп - {groups_list}")
                                                    
                                                    # Проверяем пересечения
                                                    overlapping_groups = [g for g in schedule_data.keys() if g in merged_schedule]
                                                    if overlapping_groups:
                                                        logger.warning(f"   ⚠️ Фото {photo_count}: группы {overlapping_groups} уже есть, перезаписываю...")
                                                    
                                                    merged_schedule.update(schedule_data)
                                                    logger.info(f"   📊 Всего групп в объединённом графике: {len(merged_schedule)}")
                                                    success = True
                                                else:
                                                    logger.warning(f"   ⚠️ Фото {photo_count}: Gemini не вернул данные")
                                        except Exception as e:
                                            logger.error(f"   ❌ Ошибка с фото {photo_count} в альбоме (ID {msg.id}): {e}", exc_info=True)
                                
                                logger.info(f"   📊 Обработано {photo_count} фото из альбома, всего групп: {len(merged_schedule)}")
                                
                                if success and merged_schedule:
                                    # Определяем schedule_type для альбома (если не определён)
                                    if schedule_type is None:
                                        from gemini_service import check_schedule_post_and_date
                                        logger.info(f"   🤖 Определяю дату графика через Gemini...")
                                        schedule_type = await asyncio.to_thread(check_schedule_post_and_date, text)
                                        if schedule_type is None:
                                            schedule_type = "tomorrow"  # По умолчанию
                                        logger.info(f"   📅 Определена дата: {schedule_type}")
                                    
                                    # Сохраняем
                                    if monitored_channel_obj: # Use the stored channel object
                                        city = self.db.get_city(monitored_channel_obj.city_id) 
                                        if city:
                                            old_schedule = self.db.get_schedule(city.id, schedule_type) or {}
                                            self.db.save_schedule(city.id, merged_schedule, schedule_type)
                                            logger.info(f"   💾 График ({schedule_type}, альбом) сохранен для {city.name}")
                                            
                                            # Отправляем уведомление
                                            await self._notify_subscribers_about_changes(city.id, city.name, old_schedule or {}, merged_schedule, schedule_type)
                            
                            # Если одно фото
                            elif message.photo:
                                logger.info("   📸 Обрабатываю одно фото...")
                                logger.info(f"   📥 Скачиваю фото...")
                                buffer = BytesIO()
                                await message.download_media(file=buffer)
                                photo_bytes = buffer.getvalue()
                                
                                if photo_bytes:
                                    logger.info(f"   ✅ Фото скачано ({len(photo_bytes)} байт)")
                                    logger.info(f"   🤖 Отправляю фото в Gemini Vision для анализа...")
                                    # ВАЖНО: Выполняем анализ в отдельном потоке!
                                    schedule_data = await asyncio.to_thread(analyze_schedule_image, photo_bytes, "image/jpeg")
                                    
                                    if schedule_data:
                                        groups_list = list(schedule_data.keys())
                                        logger.info(f"   ✅ Gemini вернул данные: {len(schedule_data)} групп - {groups_list}")
                                        
                                        # Определяем schedule_type для фото (если не определён)
                                        if schedule_type is None:
                                            from gemini_service import check_schedule_post_and_date
                                            logger.info(f"   🤖 Определяю дату графика через Gemini...")
                                            schedule_type = await asyncio.to_thread(check_schedule_post_and_date, text)
                                            if schedule_type is None:
                                                schedule_type = "tomorrow"  # По умолчанию
                                            logger.info(f"   📅 Определена дата: {schedule_type}")
                                        
                                        if monitored_channel_obj: # Use the stored channel object
                                            city = self.db.get_city(monitored_channel_obj.city_id)
                                            if city:
                                                old_schedule = self.db.get_schedule(city.id, schedule_type) or {}
                                                self.db.save_schedule(city.id, schedule_data, schedule_type)
                                                logger.info(f"   💾 График ({schedule_type}) сохранен для {city.name}")
                                                success = True
                                                
                                                # Отправляем уведомление
                                                await self._notify_subscribers_about_changes(city.id, city.name, old_schedule or {}, schedule_data, schedule_type)
                                    else:
                                        logger.warning(f"   ⚠️ Gemini не вернул данные для фото")
                            
                            if success:
                                logger.info(f"   🎉 График успешно обработан!")
                            else:
                                logger.warning(f"   ❌ Не удалось извлечь данные из фото")

                    else:
                        # Нет фото - проверяем через Gemini если есть текст
                        if text and len(text.strip()) > 0:
                            logger.info(f"   📝 Пост без фото, текст ({len(text)} символов) - проверяю через Gemini...")
                            logger.info(f"   🤖 Отправляю текст в Gemini для проверки (график/игнор + дата)...")
                            
                            from gemini_service import check_schedule_post_and_date, analyze_schedule_text
                            
                            # Проверяем через Gemini (выполняем в отдельном потоке)
                            # Используем новую функцию, которая сразу определяет и график, и дату
                            schedule_type = await asyncio.to_thread(check_schedule_post_and_date, text)
                            if schedule_type:
                                logger.info(f"   ✅ Gemini ответил: это график отключений ({schedule_type})!")
                                logger.info(f"   🤖 ШАГ 3: Извлекаю данные графика из текста через Gemini...")
                                schedule_data = await asyncio.to_thread(analyze_schedule_text, text)
                                if schedule_data and len(schedule_data) > 0:
                                    groups_list = list(schedule_data.keys())
                                    logger.info(f"   ✅ Gemini извлёк данные: {len(schedule_data)} групп - {groups_list}")
                                    if monitored_channel_obj:
                                        city = self.db.get_city(monitored_channel_obj.city_id)
                                        if city:
                                            old_schedule = self.db.get_schedule(city.id, schedule_type) or {}
                                            from gemini_service import is_complete_schedule, merge_schedules
                                            
                                            if is_complete_schedule(schedule_data, old_schedule):
                                                self.db.save_schedule(city.id, schedule_data, schedule_type)
                                            else:
                                                if old_schedule:
                                                    merged_schedule = merge_schedules(old_schedule, schedule_data)
                                                    self.db.save_schedule(city.id, merged_schedule, schedule_type)
                                                else:
                                                    self.db.save_schedule(city.id, schedule_data, schedule_type)
                                            
                                            logger.info(f"   💾 График ({schedule_type}) из текста сохранен для {city.name}")
                                            
                                            # Отправляем уведомление
                                            final_schedule = self.db.get_schedule(city.id, schedule_type) or schedule_data
                                            await self._notify_subscribers_about_changes(city.id, city.name, old_schedule or {}, final_schedule, schedule_type)
                            else:
                                logger.info("   ❌ Gemini: это НЕ график (игнор) - пропускаю пост")
                                logger.debug(f"   💡 Причина: пост не содержит графика с группами и интервалами (возможно, это аварийное сообщение, новость или обновление одной группы)")
                        else:
                            logger.debug(f"   ⏭️ Пост без фото и без текста - пропускаю")
                    
                    # ВАЖНО: Сохраняем ID только после полной обработки поста
                    # Это предотвращает повторную обработку одного и того же поста
                    if chat_username:
                        self.save_last_message_id(message.id, chat_username)
                        logger.debug(f"💾 Сохранён last_message_id={message.id} для канала @{chat_username}")
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка в обработчике сообщений: {e}", exc_info=True)
            
            logger.info("\n🔍 Мониторинг активен! Жду новых постов...\n")
            
            # Держим клиент запущенным
            await self.client.run_until_disconnected()
            
        except Exception as e:
            logger.error(f"Ошибка при запуске мониторинга: {e}")
            self.is_running = False
        finally:
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception as e:
                    logger.warning(f"Ошибка при отключении клиента: {e}")
            self.is_running = False
    
    async def _cleanup_old_albums(self):
        """Периодически очищает метки обработанных альбомов"""
        # Ждем инициализации Lock (он создается в start_monitoring)
        while self._processed_albums_lock is None and self.is_running:
            await asyncio.sleep(0.1)
        
        while self.is_running:
            try:
                await asyncio.sleep(300)  # Каждые 5 минут
                if self._processed_albums_lock is not None:
                    async with self._processed_albums_lock:
                        # Очищаем все метки (альбомы обрабатываются быстро, старые уже не нужны)
                        count = len(self._processed_albums)
                        self._processed_albums.clear()
                        if count > 0:
                            logger.debug(f"🧹 Очищены метки {count} обработанных альбомов")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Ошибка при очистке альбомов: {e}")
    
    async def stop_monitoring(self):
        """Останавливает мониторинг"""
        self.is_running = False
        if self.client:
            await self.client.disconnect()
        logger.info("Мониторинг остановлен")


def start_monitor_task(db: Database, bot_application=None, monitor_instance_ref=None):
    """Запускает мониторинг в отдельном потоке
    
    Args:
        db: База данных
        bot_application: Приложение бота для отправки уведомлений
        monitor_instance_ref: Список для сохранения ссылки на экземпляр monitor (для остановки)
    """
    import threading
    
    def run_monitor():
        monitor = ChannelMonitor(db, bot_application)
        # Сохраняем ссылку на monitor для возможности остановки
        if monitor_instance_ref is not None:
            monitor_instance_ref[0] = monitor
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(monitor.start_monitoring())
        except Exception as e:
            logger.error(f"Ошибка в потоке мониторинга: {e}")
        finally:
            # Корректно закрываем все задачи перед закрытием loop
            try:
                # Отменяем все pending задачи
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                # Ждём завершения отменённых задач
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception as e:
                logger.warning(f"Ошибка при закрытии задач: {e}")
            finally:
                loop.close()
    
    thread = threading.Thread(target=run_monitor, daemon=True)
    thread.start()
    return thread


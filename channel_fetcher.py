"""
Модуль для поиска и обработки графиков из Telegram-канала по запросу пользователя
ИСПРАВЛЕНО: правильная обработка альбомов и извлечение текста
"""
import os
import asyncio
import logging
import re
from io import BytesIO
from typing import Optional, Tuple, List
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto

from database import Database
from gemini_service import analyze_schedule_image
from constants import KEYWORDS

# ВАЖНО: Загружаем переменные окружения из .env файла
load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Конфигурация
CHANNEL_USERNAME = os.getenv("MONITOR_CHANNEL", "dtekoficial")
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")

# KEYWORDS теперь импортируется из constants.py для избежания дублирования


def extract_date_from_text(text: str) -> Optional[str]:
    """
    Извлекает дату из текста поста (например, "17 листопада", "17 ноября", "21.11.2025")
    
    Args:
        text: Текст поста
        
    Returns:
        Строка с датой или None
    """
    if not text:
        return None
    
    # Паттерны для поиска даты
    # Русские месяцы (преобразуем в украинские)
    ru_months = {
        'января': 'січня', 'февраля': 'лютого', 'марта': 'березня',
        'апреля': 'квітня', 'мая': 'травня', 'июня': 'червня',
        'июля': 'липня', 'августа': 'серпня', 'сентября': 'вересня',
        'октября': 'жовтня', 'ноября': 'листопада', 'декабря': 'грудня'
    }
    
    # Ищем паттерн: число + месяц (украинский или русский)
    # Например: "17 листопада", "17 ноября", "на 17 листопада", "на 17 ноября"
    patterns = [
        r'(\d{1,2})\s+(?:листопада|ноября)',
        r'(\d{1,2})\s+(?:січня|января|лютого|февраля|березня|марта|квітня|апреля|травня|мая|червня|июня|липня|июля|серпня|августа|вересня|сентября|жовтня|октября|грудня|декабря)',
        r'на\s+(\d{1,2})\s+(?:листопада|ноября|січня|января|лютого|февраля|березня|марта|квітня|апреля|травня|мая|червня|июня|липня|июля|серпня|августа|вересня|сентября|жовтня|октября|грудня|декабря)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            day = match.group(1)
            # Находим месяц в тексте
            month_text = text[match.end():match.end()+20].lower() if match.end() < len(text) else ""
            month_match = re.search(r'(листопада|ноября|січня|января|лютого|февраля|березня|марта|квітня|апреля|травня|мая|червня|июня|липня|июля|серпня|августа|вересня|сентября|жовтня|октября|грудня|декабря)', month_text, re.IGNORECASE)
            if month_match:
                month = month_match.group(1)
                # Преобразуем русские месяцы в украинские для единообразия
                if month in ru_months:
                    month = ru_months[month]
                return f"{day} {month}"
    
    # ДОПОЛНЕНИЕ: Обработка дат в формате DD.MM.YYYY или DD.MM (например, "21.11.2025", "Зміни на 22:18 21.11.2025")
    # Поиск полной даты в формате DD.MM.YYYY или DD/MM/YYYY
    full_date_match = re.search(r'(\d{1,2})[./](\d{1,2})[./](\d{4})', text)
    if full_date_match:
        day, month, year = full_date_match.groups()
        # Возвращаем в формате "DD MM" для совместимости
        month_names = {
            '01': 'січня', '02': 'лютого', '03': 'березня', '04': 'квітня',
            '05': 'травня', '06': 'червня', '07': 'липня', '08': 'серпня',
            '09': 'вересня', '10': 'жовтня', '11': 'листопада', '12': 'грудня'
        }
        month_name = month_names.get(month, month)
        return f"{day} {month_name}"
    
    # Поиск даты в формате DD.MM или DD/MM (без года)
    date_match = re.search(r'(\d{1,2})[./](\d{1,2})(?![./]\d)', text)
    if date_match:
        day, month = date_match.groups()
        month_names = {
            '01': 'січня', '02': 'лютого', '03': 'березня', '04': 'квітня',
            '05': 'травня', '06': 'червня', '07': 'липня', '08': 'серпня',
            '09': 'вересня', '10': 'жовтня', '11': 'листопада', '12': 'грудня'
        }
        month_name = month_names.get(month, month)
        return f"{day} {month_name}"
    
    return None


def should_download_post(text: str) -> bool:
    """Проверяет, нужно ли обрабатывать этот пост"""
    if not text:
        return False
    
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in KEYWORDS)


async def create_client() -> Optional[TelegramClient]:
    """
    Создаёт и подключает Telegram клиент
    
    Returns:
        TelegramClient или None если не удалось подключиться
    """
    if not API_ID or not API_HASH:
        logger.error("❌ API_ID и API_HASH не установлены в .env файле!")
        return None
    
    try:
        # Используем StringSession если есть, иначе файл сессии
        if SESSION_STRING:
            logger.info("🔑 Использую StringSession из переменной окружения")
            session = StringSession(SESSION_STRING)
        else:
            logger.info("🔑 Использую файл сессии channel_fetcher.session")
            session = 'channel_fetcher'
        
        client = TelegramClient(session, API_ID, API_HASH)
        
        # Подключаемся
        await client.connect()
        
        # Проверяем авторизацию
        if not await client.is_user_authorized():
            logger.error("❌ Сессия не авторизована!")
            logger.error("💡 Запустите: python generate_session.py")
            await client.disconnect()
            return None
        
        logger.info("✅ Успешно подключился к Telegram")
        return client
        
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Telegram: {e}")
        return None


async def get_album_text(client: TelegramClient, message, channel_username: str) -> str:
    """
    Получает текст из альбома (проверяет все сообщения с тем же grouped_id)
    Объединяет текст из всех сообщений альбома для полноты информации
    
    Args:
        client: Telegram клиент
        message: Сообщение из альбома
        channel_username: Username канала
    
    Returns:
        Текст альбома (объединённый из всех сообщений) или пустая строка
    """
    try:
        texts = []
        # Получаем все сообщения вокруг текущего
        async for msg in client.iter_messages(
            channel_username,
            min_id=message.id - 10,
            max_id=message.id + 10
        ):
            if hasattr(msg, 'grouped_id') and msg.grouped_id == message.grouped_id:
                # Проверяем текст в каждом сообщении альбома
                msg_text = (
                    msg.text or 
                    getattr(msg, 'message', None) or 
                    getattr(msg, 'raw_text', None) or 
                    ""
                )
                if msg_text:
                    texts.append(msg_text)
                    logger.debug(f"   📝 Найден текст в сообщении альбома (ID {msg.id}): {msg_text[:100]}...")
        
        # Объединяем все тексты из альбома
        if texts:
            combined_text = "\n".join(texts)
            logger.info(f"   📝 Объединённый текст альбома ({len(texts)} сообщений, {len(combined_text)} символов): {combined_text[:200]}...")
            return combined_text
        
        return ""
    except Exception as e:
        logger.warning(f"⚠️ Ошибка при получении текста альбома: {e}")
        return ""


async def find_latest_schedule_post(client: TelegramClient, channel_username: str) -> Optional[List[Tuple]]:
    """
    Находит посты с графиками в канале и группирует их по дате (today/tomorrow).
    Для каждой даты выбирает самый свежий пост.
    
    Args:
        client: Уже подключённый Telegram клиент
        channel_username: Username канала
    
    Returns:
        List of tuples [(message, text, schedule_type), ...] или None если не найдено
        Список содержит максимум 2 поста: один для today, один для tomorrow (самые свежие)
    """
    try:
        logger.info(f"🔍 Ищу посты в канале @{channel_username}...")
        
        # Берём последние 3 поста для проверки
        messages = await client.get_messages(channel_username, limit=3)
        logger.info(f"📊 Получено {len(messages)} последних постов для проверки")
        
        # Собираем все подходящие посты с их schedule_type
        suitable_posts = []  # [(message, text, schedule_type, date), ...]
        processed_albums = set()  # Чтобы не обрабатывать один альбом несколько раз
        
        # ВАЖНО: Проверяем ВСЕ посты через Gemini для надёжности
        # Это гарантирует, что мы не пропустим обновления графика или посты без keywords
        all_candidates = []  # Все посты для проверки через Gemini
        
        for message in messages:
            # Пропускаем сообщения из уже обработанных альбомов
            if hasattr(message, 'grouped_id') and message.grouped_id:
                if message.grouped_id in processed_albums:
                    logger.debug(f"   ⏭️ Пост ID {message.id} - уже обработан как часть альбома {message.grouped_id}")
                    continue
                processed_albums.add(message.grouped_id)
            
            # Извлекаем текст
            # Для альбомов - ищем текст во всех сообщениях альбома
            if hasattr(message, 'grouped_id') and message.grouped_id:
                logger.debug(f"   📸 Пост ID {message.id} - альбом (grouped_id: {message.grouped_id})")
                text = await get_album_text(client, message, channel_username)
            else:
                # Для одиночных сообщений
                text = (
                    message.text or 
                    getattr(message, 'message', None) or 
                    getattr(message, 'raw_text', None) or 
                    ""
                )
            
            # Проверяем наличие фото (одно или альбом)
            # Для альбомов нужно проверить, есть ли хотя бы одно фото в альбоме
            has_photo = False
            if message.photo:
                has_photo = True
            elif hasattr(message, 'grouped_id') and message.grouped_id:
                # Для альбомов проверяем наличие фото в сообщениях альбома
                try:
                    async for msg in client.iter_messages(
                        message.peer_id,
                        min_id=message.id - 5,
                        max_id=message.id + 5
                    ):
                        if (hasattr(msg, 'grouped_id') and 
                            msg.grouped_id == message.grouped_id and 
                            msg.photo):
                            has_photo = True
                            break
                except Exception as e:
                    logger.debug(f"   ⚠️ Ошибка при проверке фото в альбоме: {e}")
                    # Если не удалось проверить, предполагаем что фото есть (если есть grouped_id)
                    has_photo = True
            
            # Добавляем ВСЕ посты с текстом для проверки через Gemini
            # Это гарантирует, что мы не пропустим обновления графика
            if text and len(text.strip()) > 20:  # Минимум 20 символов текста
                all_candidates.append((message, text, has_photo))
                logger.debug(f"   📝 Пост ID {message.id} добавлен для проверки через Gemini (текст: {len(text)} символов, фото: {'есть' if has_photo else 'нет'})")
            else:
                logger.debug(f"   ⏭️ Пост ID {message.id} без достаточного текста - пропускаю")
        
        # Проверяем ВСЕ кандидаты через Gemini с определением даты
        if all_candidates:
            logger.info(f"🔍 Проверяю {len(all_candidates)} постов через Gemini (с определением даты)...")
            from gemini_service import check_schedule_post_and_date, analyze_schedule_text
            
            for message, text, has_photo in all_candidates:
                # Проверяем через Gemini и сразу определяем дату (выполняем в отдельном потоке)
                try:
                    # Логируем детали поста перед проверкой
                    post_date = message.date.strftime('%Y-%m-%d %H:%M:%S') if message.date else 'Неизвестно'
                    logger.info(f"   🔍 Проверяю пост ID {message.id} через Gemini...")
                    logger.info(f"      Дата: {post_date}")
                    logger.info(f"      Текст (первые 200 символов): {text[:200]}...")
                    logger.info(f"      Есть фото: {has_photo}")
                    
                    # ВАЖНО: Используем новую функцию, которая сразу определяет и график, и дату
                    schedule_type = await asyncio.to_thread(check_schedule_post_and_date, text)
                    
                    if not schedule_type:
                        # Это не график (игнор)
                        logger.warning(f"   ❌ Пост ID {message.id} - Gemini: это НЕ график (игнор)")
                        logger.debug(f"      Текст поста: {text[:300]}...")
                        continue
                    
                    # Это график! Определяем schedule_type
                    logger.info(f"   ✅ Gemini подтвердил: пост ID {message.id} - это график ({schedule_type})!")
                    
                    # Проверяем, можно ли извлечь график (для текста) или есть фото
                    has_keywords = should_download_post(text)
                    
                    if has_photo:
                        # Есть фото - добавляем в подходящие
                        logger.info(f"   ✅ Пост ID {message.id} с графиком ({schedule_type}, есть фото) - добавляю в список")
                        suitable_posts.append((message, text, schedule_type, message.date if message.date else None))
                    elif has_keywords:
                        # Есть keywords, но нет фото - проверяем, можно ли извлечь график из текста
                        logger.info(f"   📝 Пост ID {message.id} - keywords есть, но нет фото - проверяю график в тексте...")
                        schedule_data = await asyncio.to_thread(analyze_schedule_text, text)
                        if schedule_data and len([k for k in schedule_data.keys() if k != '_meta']) >= 3:  # Минимум 3 группы
                            logger.info(f"   ✅ Пост ID {message.id} - график найден в тексте ({len([k for k in schedule_data.keys() if k != '_meta'])} групп, {schedule_type}) - добавляю в список")
                            suitable_posts.append((message, text, schedule_type, message.date if message.date else None))
                        else:
                            logger.warning(f"   ⚠️ Пост ID {message.id} - график в тексте не извлечён или слишком мало групп")
                    else:
                        # Нет keywords, но Gemini подтвердил график - проверяем текст
                        logger.info(f"   📝 Пост ID {message.id} без keywords, но Gemini подтвердил график - проверяю текст...")
                        schedule_data = await asyncio.to_thread(analyze_schedule_text, text)
                        if schedule_data and len([k for k in schedule_data.keys() if k != '_meta']) >= 3:  # Минимум 3 группы
                            logger.info(f"   ✅ Пост ID {message.id} с графиком в тексте ({len([k for k in schedule_data.keys() if k != '_meta'])} групп, {schedule_type}) - добавляю в список")
                            suitable_posts.append((message, text, schedule_type, message.date if message.date else None))
                        else:
                            logger.warning(f"   ⚠️ Пост ID {message.id} - график в тексте, но слишком мало групп")
                            
                except Exception as e:
                    logger.error(f"   ❌ Ошибка при проверке поста ID {message.id} через Gemini: {e}", exc_info=True)
                    continue
        
        if not suitable_posts:
            logger.warning(f"❌ Не найдено постов с графиками в последних {len(messages)} постах")
            logger.info(f"   (Проверено {len(all_candidates)} постов через Gemini)")
            return None
        
        # Группируем посты по schedule_type и выбираем самый свежий для каждой даты
        posts_by_type = {"today": [], "tomorrow": []}
        
        for message, text, schedule_type, date in suitable_posts:
            if schedule_type in posts_by_type:
                posts_by_type[schedule_type].append((message, text, schedule_type, date))
        
        # Сортируем по дате (самый свежий первый) для каждого типа
        for schedule_type in posts_by_type:
            posts_by_type[schedule_type].sort(key=lambda x: x[3] if x[3] else None, reverse=True)
        
        # Выбираем самый свежий пост для каждой даты
        result_posts = []
        
        if posts_by_type["today"]:
            best_today = posts_by_type["today"][0]
            result_posts.append(best_today)
            logger.info(f"")
            logger.info(f"📌 ВЫБРАН САМЫЙ СВЕЖИЙ ПОСТ ДЛЯ СЕГОДНЯ:")
            logger.info(f"   ID: {best_today[0].id}")
            logger.info(f"   Дата: {best_today[3].strftime('%Y-%m-%d %H:%M:%S') if best_today[3] else 'Неизвестно'}")
            logger.info(f"   Всего постов на сегодня: {len(posts_by_type['today'])}")
            if len(posts_by_type["today"]) > 1:
                logger.info(f"   (Остальные {len(posts_by_type['today']) - 1} постов на сегодня пропущены как более старые)")
        
        if posts_by_type["tomorrow"]:
            best_tomorrow = posts_by_type["tomorrow"][0]
            result_posts.append(best_tomorrow)
            logger.info(f"")
            logger.info(f"📌 ВЫБРАН САМЫЙ СВЕЖИЙ ПОСТ ДЛЯ ЗАВТРА:")
            logger.info(f"   ID: {best_tomorrow[0].id}")
            logger.info(f"   Дата: {best_tomorrow[3].strftime('%Y-%m-%d %H:%M:%S') if best_tomorrow[3] else 'Неизвестно'}")
            logger.info(f"   Всего постов на завтра: {len(posts_by_type['tomorrow'])}")
            if len(posts_by_type["tomorrow"]) > 1:
                logger.info(f"   (Остальные {len(posts_by_type['tomorrow']) - 1} постов на завтра пропущены как более старые)")
        
        logger.info(f"")
        logger.info(f"✅ Найдено постов для обработки: {len(result_posts)} (today: {len(posts_by_type['today'])}, tomorrow: {len(posts_by_type['tomorrow'])})")
        
        return result_posts
        
    except Exception as e:
        logger.error(f"❌ Ошибка при поиске поста: {e}", exc_info=True)
        return None


async def download_and_process_photo_from_message(
    message, 
    client: TelegramClient,
    city_id: int, 
    city_name: str, 
    db: Database,
    schedule_type: str = None
) -> Tuple[bool, str]:
    """
    Скачивает и обрабатывает фото из сообщения, или извлекает график из текста
    
    Если в посте есть фото - обрабатывает фото через Gemini Vision.
    Если фото нет, но есть текст - проверяет текст через Gemini и извлекает график.
    
    Args:
        message: Telegram сообщение
        client: Уже подключённый Telegram клиент
        city_id: ID города
        city_name: Название города
        db: База данных
        schedule_type: Тип графика - "today" или "tomorrow" (опционально, определяется автоматически если не указан)
    
    Returns:
        Tuple (success: bool, message: str)
    """
    # Извлекаем текст поста
    text = (
        message.text or 
        getattr(message, 'message', None) or 
        getattr(message, 'raw_text', None) or 
        ""
    )
    
    # Определяем тип графика (сегодня или завтра) через Gemini
    # Если schedule_type не передан, определяем автоматически
    # ВАЖНО: Делаем это один раз в начале функции, чтобы избежать дублирования
    if schedule_type is None and text and len(text.strip()) > 20:
        from gemini_service import check_schedule_post_and_date
        schedule_type = await asyncio.to_thread(check_schedule_post_and_date, text)
        if schedule_type is None:
            schedule_type = "tomorrow"  # По умолчанию
    
    # Если нет медиа, но есть текст - пытаемся извлечь график из текста
    if not message.media:
        logger.info("📝 Пост не содержит фото, проверяю текст...")
        
        if text and len(text) > 50:
            from gemini_service import check_schedule_post_and_date, analyze_schedule_text, is_complete_schedule, merge_schedules
            
            # Проверяем через Gemini, является ли это графиком и определяем дату
            logger.info("🔍 Проверяю текст через Gemini...")
            try:
                schedule_type = await asyncio.to_thread(check_schedule_post_and_date, text)
                
                if schedule_type:
                    logger.info(f"✅ Gemini подтвердил: это график отключений ({schedule_type})!")
                    
                    # Извлекаем график из текста
                    schedule_data = await asyncio.to_thread(analyze_schedule_text, text)
                    
                    if schedule_data and len(schedule_data) > 0:
                        logger.info(f"📊 Извлечено {len(schedule_data)} групп из текста")
                        logger.info(f"   📅 Используется дата: {schedule_type}")
                        
                        # Получаем старый график для сравнения
                        old_schedule = db.get_schedule(city_id, schedule_type) or {}
                        
                        # Проверяем, является ли график полным
                        if is_complete_schedule(schedule_data, old_schedule):
                            # Полный график - полностью заменяем
                            logger.info(f"✅ Полный график - полностью заменяю старый")
                        else:
                            # Частичное обновление - объединяем со старым
                            logger.warning(f"⚠️ Частичное обновление - объединяю со старым графиком")
                            if old_schedule:
                                schedule_data = merge_schedules(old_schedule, schedule_data)
                        
                        # Сохраняем график в правильное поле
                        db.save_schedule(city_id, schedule_data, schedule_type)
                        
                        final_groups = len([k for k in schedule_data.keys() if k != '_meta'])
                        logger.info(f"✅ График из текста сохранён для города '{city_name}'. Всего групп: {final_groups}")
                        
                        return True, f"✅ График загружен из текста!\n\nРаспознано групп: {final_groups}"
                    else:
                        logger.warning("⚠️ Не удалось извлечь график из текста")
                        return False, "❌ Не удалось извлечь график из текста поста"
                else:
                    logger.info("⏭️ Gemini подтвердил: это НЕ график")
                    return False, "❌ Пост не содержит график отключений"
            except Exception as e:
                logger.error(f"❌ Ошибка при обработке текста через Gemini: {e}", exc_info=True)
                return False, f"❌ Ошибка при обработке текста: {str(e)}"
        else:
            logger.warning("Пост не содержит медиа-файлов и текста недостаточно")
            return False, "Пост не содержит фотографий и график не найден в тексте"
    
    try:
        success = False
        processed_groups = 0
        
        # ВАЖНО: Сначала проверяем альбом, потом одно фото
        # Потому что альбом может иметь и photo, и grouped_id
        
        # Если альбом (несколько фото)
        if hasattr(message, 'grouped_id') and message.grouped_id:
            logger.info("📸 Обнаружен альбом, обрабатываю все фото...")
            merged_schedule = {}
            photo_count = 0
            
            # Получаем все сообщения из альбома
            # ВАЖНО: Увеличиваем диапазон для надёжности (альбомы могут приходить не по порядку)
            logger.info(f"   🔍 Ищу все фото альбома (grouped_id: {message.grouped_id})...")
            album_messages_list = []
            async for msg in client.iter_messages(
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
                            
                            # Проверяем пересечения
                            overlapping_groups = []
                            for group in schedule_data.keys():
                                if group in merged_schedule:
                                    overlapping_groups.append(group)
                            
                            if overlapping_groups:
                                logger.warning(f"⚠️ Фото {photo_count}: группы {overlapping_groups} уже есть в предыдущих фото! Значения будут перезаписаны.")
                            
                            # Объединяем данные
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
            
            # Сохраняем объединённый график
            if merged_schedule:
                logger.info(f"   📅 Используется дата: {schedule_type or 'tomorrow'}")
                
                all_groups = sorted(merged_schedule.keys())
                logger.info(f"💾 Сохраняю объединённый график ({schedule_type}) в базу:")
                logger.info(f"   Всего групп: {len(merged_schedule)}")
                logger.info(f"   Группы: {all_groups}")
                logger.info(f"   📋 Распознанные группы: {', '.join(all_groups)}")
                db.save_schedule(city_id, merged_schedule, schedule_type)
                processed_groups = len(merged_schedule)
        
        # Если одно фото (не альбом)
        elif message.photo:
            logger.info("📸 Скачиваю одно фото...")
            buffer = BytesIO()
            await message.download_media(file=buffer)
            photo_bytes = buffer.getvalue()
            logger.info(f"✅ Фото скачано ({len(photo_bytes)} байт)")
            
            if photo_bytes:
                logger.info("🤖 Анализирую через Gemini...")
                # ВАЖНО: Не блокируем event loop
                schedule_data = await asyncio.to_thread(analyze_schedule_image, photo_bytes, "image/jpeg")
                if schedule_data:
                    logger.info(f"   📅 Используется дата: {schedule_type or 'tomorrow'}")
                    
                    groups_list = sorted(schedule_data.keys())
                    logger.info(f"✅ Распознано {len(schedule_data)} групп")
                    logger.info(f"   📋 Распознанные группы: {', '.join(groups_list)}")
                    db.save_schedule(city_id, schedule_data, schedule_type)
                    processed_groups = len(schedule_data)
                    success = True
                else:
                    logger.warning("⚠️ Gemini не вернул данные")
        
        if success:
            return True, f"✅ График загружен!\n\nРаспознано групп: {processed_groups}"
        else:
            return False, "❌ Не удалось обработать фотографии"
            
    except Exception as e:
        logger.error(f"❌ Ошибка при обработке фото: {e}", exc_info=True)
        return False, f"❌ Ошибка: {str(e)}"


async def get_schedule_photos_from_channel(city_id: int = None, channel_username: str = None) -> Tuple[bool, Optional[Tuple], Optional[TelegramClient]]:
    """
    Получает фото из последнего поста с графиком в канале (без обработки)
    
    Args:
        city_id: ID города (если указан, канал берётся из БД)
        channel_username: Username канала (если указан, используется напрямую)
    
    Returns:
        Tuple (success: bool, result: Optional[Tuple[message, text]], client: Optional[TelegramClient])
        Если success=True, result содержит (message, text), client - подключённый клиент
        Если success=False, result=None, client=None
    """
    # Определяем канал
    if city_id:
        from database import Database
        db = Database()
        channel = db.get_channel(city_id)
        if channel:
            channel_username = channel.channel_username
        else:
            logger.warning(f"⚠️ Для города ID {city_id} не настроен канал")
            return False, None, None
    elif not channel_username:
        # Fallback на старый способ
        channel_username = CHANNEL_USERNAME
    
    logger.info(f"🔍 Получение фото графика из канала @{channel_username}")
    
    # Создаём клиент
    client = await create_client()
    if not client:
        return False, None, None
    
    try:
        # Ищем посты с графиками (может быть несколько - для today и tomorrow)
        logger.info("🔍 Ищу посты с графиками...")
        result = await find_latest_schedule_post(client, channel_username)
        
        if not result:
            logger.warning("❌ Посты не найдены")
            await client.disconnect()
            return False, None, None
        
        # result теперь список: [(message, text, schedule_type, date), ...]
        # Берем первый пост (самый свежий для today или tomorrow)
        # Для get_schedule_photos_from_channel достаточно одного поста
        message, text, schedule_type, date = result[0]
        
        # Проверяем наличие фото
        if not message.photo and not (hasattr(message, 'grouped_id') and message.grouped_id):
            logger.warning("❌ В посте нет фото")
            await client.disconnect()
            return False, None, None
        
        logger.info(f"✅ Найден пост ID {message.id} с фото")
        return True, (message, text), client
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении фото: {e}", exc_info=True)
        try:
            await client.disconnect()
        except:
            pass
        return False, None, None


async def find_and_process_schedule_for_user(city_id: int, city_name: str, db: Database) -> Tuple[bool, str]:
    """
    Находит и обрабатывает последний график из канала для конкретного пользователя
    
    Returns:
        Tuple (success: bool, message: str)
    """
    logger.info(f"🔍 Поиск графика для '{city_name}' (ID: {city_id})")
    
    # Получаем канал для города из базы данных
    channel = db.get_channel(city_id)
    if not channel:
        return False, f"❌ Для города '{city_name}' не настроен канал.\n\nДобавьте канал через меню управления городами."
    
    channel_username = channel.channel_username
    logger.info(f"📱 Канал для города '{city_name}': @{channel_username}")
    
    # Создаём клиент
    client = await create_client()
    if not client:
        return False, "❌ Не удалось подключиться к Telegram.\n\nПроверьте настройки авторизации."
    
    try:
        # Ищем последний пост с графиком с таймаутом
        logger.info("🔍 Шаг 1: Ищу пост с графиком...")
        try:
            result = await asyncio.wait_for(
                find_latest_schedule_post(client, channel_username),
                timeout=120.0  # 2 минуты на поиск поста
            )
        except asyncio.TimeoutError:
            logger.error("⏱️ Таймаут при поиске поста (превышено 2 минуты)")
            return False, "⏱️ Поиск поста занял слишком много времени.\n\nПопробуйте ещё раз или загрузите график вручную."
        
        if not result:
            logger.warning("❌ Посты не найдены")
            return False, "❌ Не найдено постов с графиками в канале.\n\nПопробуйте загрузить график вручную."
        
        # result теперь список постов: [(message, text, schedule_type, date), ...]
        # Может быть 1 или 2 поста (для today и tomorrow)
        
        processed_count = 0
        success_messages = []
        errors = []
        
        # Обрабатываем каждый найденный пост
        for message, text, schedule_type, date in result:
            post_date = date.strftime('%Y-%m-%d %H:%M:%S') if date else 'Неизвестно'
            logger.info(f"")
            logger.info(f"📌 Обрабатываю пост для {schedule_type}:")
            logger.info(f"   ID: {message.id}")
            logger.info(f"   📅 Дата: {post_date}")
            logger.info(f"   📝 Текст: {text[:150] if text else 'Нет текста'}...")
            
            # Обрабатываем пост с таймаутом
            logger.info(f"🔍 Обрабатываю пост ({schedule_type})...")
            try:
                success, message_text = await asyncio.wait_for(
                    download_and_process_photo_from_message(
                        message, client, city_id, city_name, db, schedule_type
                    ),
                    timeout=180.0  # 3 минуты на обработку
                )
            except asyncio.TimeoutError:
                logger.error(f"⏱️ Таймаут при обработке поста {message.id} (превышено 3 минуты)")
                errors.append(f"⏱️ Пост {message.id} ({schedule_type}): таймаут")
                continue
            
            if success:
                processed_count += 1
                date_label = "сегодня" if schedule_type == "today" else "завтра"
                success_messages.append(f"✅ График на {date_label} обработан")
                logger.info(f"✅ График ({schedule_type}) обработан для '{city_name}'")
            else:
                date_label = "сегодня" if schedule_type == "today" else "завтра"
                errors.append(f"❌ Пост {message.id} ({date_label}): {message_text}")
        
        if processed_count > 0:
            result_message = "\n".join(success_messages)
            if errors:
                result_message += "\n\n" + "\n".join(errors)
            result_message += f"\n\nГород: {city_name}"
            logger.info(f"✅ Обработано графиков: {processed_count} из {len(result)}")
            return True, result_message
        else:
            error_message = "\n".join(errors) if errors else "❌ Не удалось обработать графики"
            logger.error(f"❌ Ошибка: {error_message}")
            return False, error_message
    
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка при обработке графика: {e}", exc_info=True)
        return False, f"❌ Произошла ошибка: {str(e)}\n\nПопробуйте загрузить график вручную."
    finally:
        # ВАЖНО: Отключаемся только в конце!
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10.0)
            logger.info("🔌 Отключился от Telegram")
        except Exception as e:
            logger.warning(f"Ошибка при отключении клиента: {e}")

import os
import json
import logging
from typing import Dict, List, Optional
from io import BytesIO
from PIL import Image
from dotenv import load_dotenv
import google.generativeai as genai

# ВАЖНО: Загружаем переменные окружения из .env файла
load_dotenv()

logger = logging.getLogger(__name__)

# Инициализация Gemini API
API_KEY = os.getenv("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)

# Модели для разных задач
MODEL_VISION = 'gemini-2.5-flash-lite'  # Для анализа изображений
MODEL_TEXT = 'gemini-2.5-flash-lite'  # Для генерации ответов

def analyze_schedule_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> Dict[str, List[str]]:
    """
    Анализирует изображение графика отключений электричества с помощью Gemini Vision API.
    
    Args:
        image_bytes: Байты изображения
        mime_type: MIME тип изображения (image/jpeg, image/png и т.д.)
    
    Returns:
        Словарь с группами и интервалами отключений
        Пример: {"1.1": ["11:00-15:00", "19:00-22:00"], "1.2": ["08:00-12:00"]}
    """
    if not API_KEY:
        raise ValueError("GEMINI_API_KEY не установлен. Установите переменную окружения GEMINI_API_KEY.")
    
    prompt = """Проанализируй это изображение графика отключений электричества.

ВАЖНО: Извлеки следующую информацию:
- Все группы электричества (формат: X.1 или X.2, где X - число от 1 до 6)
  Например: 1.1, 1.2, 2.1, 2.2, 3.1, 3.2 и т.д.
  ВАЖНО: Каждая группа имеет только .1 или .2 (НЕ .3, .4 и т.д.)
- Для каждой группы найди ВСЕ временные интервалы отключений (формат: ЧЧ:ММ-ЧЧ:ММ)
  Например: "11:00-15:00", "19:00-22:00"

Верни результат ТОЛЬКО в формате JSON. Не включай никакой текст кроме самого JSON объекта. Не используй markdown.

Пример правильного ответа:
{
  "1.1": ["11:00-15:00", "19:00-22:00"],
  "1.2": ["08:00-12:00", "16:00-19:00"],
  "2.1": ["10:00-14:00"],
  "2.2": ["09:00-13:00", "17:00-20:00"]
}

ВАЖНО: 
- Ключи должны быть строками в формате "X.1" или "X.2"
- Значения должны быть массивами строк в формате "ЧЧ:ММ-ЧЧ:ММ"
- Если группа не найдена на изображении, не включай её в результат"""
    
    try:
        model = genai.GenerativeModel(MODEL_VISION)
        
        # Конвертируем байты в PIL Image
        image = Image.open(BytesIO(image_bytes))
        
        # Отправляем запрос
        response = model.generate_content([image, prompt])
        
        # Извлекаем JSON из ответа
        response_text = response.text.strip()
        
        # Убираем markdown форматирование если есть
        response_text = response_text.replace("```json", "").replace("```", "").strip()
        
        # Парсим JSON
        schedule_data = json.loads(response_text)
        
        # Валидация: проверяем что это словарь со строками в качестве ключей и списками строк в качестве значений
        if not isinstance(schedule_data, dict):
            raise ValueError("Ответ от Gemini не является словарем")
        
        # Преобразуем все ключи в строки и значения в списки строк
        result = {}
        for key, value in schedule_data.items():
            if isinstance(value, list):
                result[str(key)] = [str(v) for v in value]
            else:
                result[str(key)] = [str(value)]
        
        return result
        
    except json.JSONDecodeError as e:
        raise ValueError(f"Не удалось распарсить JSON ответ от Gemini: {e}")
    except Exception as e:
        raise ValueError(f"Ошибка при анализе изображения: {str(e)}")


def is_complete_schedule(schedule_data: Dict[str, List[str]], old_schedule: Dict[str, List[str]] = None) -> bool:
    """
    Проверяет, является ли график полным или это частичное обновление.
    
    Args:
        schedule_data: Новый график
        old_schedule: Старый график (опционально)
    
    Returns:
        True если график полный (>= 6 групп), False если подозрительно мало групп
    """
    if not schedule_data:
        return False
    
    groups_count = len([k for k in schedule_data.keys() if k != '_meta'])
    
    # Если меньше 3 групп - это точно не полный график
    if groups_count < 3:
        logger.warning(f"⚠️ Подозрительно мало групп в графике: {groups_count}. Возможно, это частичное обновление.")
        return False
    
    # Если есть старый график и новый содержит значительно меньше групп
    if old_schedule:
        old_groups_count = len([k for k in old_schedule.keys() if k != '_meta'])
        if old_groups_count > 6 and groups_count < old_groups_count * 0.5:
            logger.warning(f"⚠️ Новый график содержит {groups_count} групп, а старый {old_groups_count}. Возможно, это частичное обновление.")
            return False
    
    # Если 6+ групп - скорее всего полный график
    if groups_count >= 6:
        return True
    
    # Если 3-5 групп - проверяем, есть ли пары (1.1 и 1.2, 2.1 и 2.2)
    # Полный график обычно содержит пары групп
    groups = [k for k in schedule_data.keys() if k != '_meta']
    group_numbers = set()
    for g in groups:
        try:
            num = int(g.split('.')[0])
            group_numbers.add(num)
        except:
            pass
    
    # Если есть хотя бы 3 разные номера групп - скорее всего полный
    if len(group_numbers) >= 3:
        return True
    
    # Иначе подозрительно
    return False


def merge_schedules(old_schedule: Dict[str, List[str]], new_schedule: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """
    Умно объединяет старый и новый графики.
    
    Если новый график содержит значительно меньше групп чем старый, это частичное обновление.
    В таком случае объединяем: новые группы обновляются, старые остаются.
    
    Если новый график полный (>= 6 групп) - полностью заменяем старый.
    
    Args:
        old_schedule: Старый график
        new_schedule: Новый график
    
    Returns:
        Объединённый график
    """
    if not old_schedule:
        return new_schedule
    
    if not new_schedule:
        return old_schedule
    
    # Убираем метаданные для подсчёта
    old_groups = {k: v for k, v in old_schedule.items() if k != '_meta'}
    new_groups = {k: v for k, v in new_schedule.items() if k != '_meta'}
    
    old_count = len(old_groups)
    new_count = len(new_groups)
    
    # Если новый график полный (>= 6 групп) или содержит больше групп чем старый - полностью заменяем
    if new_count >= 6 or new_count >= old_count:
        logger.info(f"💾 Новый график полный ({new_count} групп) - полностью заменяю старый ({old_count} групп)")
        return new_schedule
    
    # Если новый график содержит значительно меньше групп - это частичное обновление
    # Объединяем: новые группы обновляются, старые остаются
    if new_count < old_count * 0.7:  # Если новый содержит меньше 70% групп от старого
        logger.info(f"💾 Новый график частичный ({new_count} групп) - объединяю со старым ({old_count} групп)")
        merged = {**old_schedule}
        merged.update(new_groups)  # Обновляем только новые группы
        
        # Сохраняем метаданные из нового графика, если есть
        if '_meta' in new_schedule:
            merged['_meta'] = new_schedule['_meta']
        
        return merged
    
    # В остальных случаях полностью заменяем
    logger.info(f"💾 Заменяю график: старый {old_count} групп, новый {new_count} групп")
    return new_schedule


def analyze_schedule_text(text: str) -> Dict[str, List[str]]:
    """
    Анализирует текст поста и извлекает график отключений электричества.
    
    Используется для постов, где график указан в тексте, а не на изображении.
    
    Args:
        text: Текст поста из Telegram
    
    Returns:
        Словарь с группами и интервалами отключений
        Пример: {"1.1": ["00:00-02:00", "04:00-06:00"], "1.2": ["08:00-10:00"]}
        Или пустой словарь, если график не найден
    """
    if not API_KEY:
        logger.warning("GEMINI_API_KEY не установлен. Не могу анализировать текст.")
        return {}
    
    if not text or len(text.strip()) < 10:
        return {}
    
    prompt = f"""Проанализируй этот текст из Telegram поста и извлеки график отключений электричества.

Текст поста:
{text}

ВАЖНО: Извлеки следующую информацию:
- Все группы электричества (формат: X.1 или X.2, где X - число от 1 до 6)
  Например: 1.1, 1.2, 2.1, 2.2, 3.1, 3.2 и т.д.
  ВАЖНО: Каждая группа имеет только .1 или .2 (НЕ .3, .4 и т.д.)
  Также могут быть варианты: "Черга 1.1", "Группа 1.1", "1.1", "Черга 1,1" - все это группа 1.1
- Для каждой группы найди ВСЕ временные интервалы отключений
  Формат может быть разным:
  - "00-02, 04-06" → означает "00:00-02:00, 04:00-06:00"
  - "00:00-02:00, 04:00-06:00" → уже в правильном формате
  - "00:00–02:00" → длинное тире, тоже валидно
  Преобразуй все в формат "ЧЧ:ММ-ЧЧ:ММ"

Верни результат ТОЛЬКО в формате JSON. Не включай никакой текст кроме самого JSON объекта. Не используй markdown.

Пример правильного ответа:
{{
  "1.1": ["00:00-02:00", "04:00-06:00", "08:00-10:00"],
  "1.2": ["00:00-02:00", "04:00-06:00", "08:00-10:00"],
  "2.1": ["00:00-02:00", "08:00-10:00"]
}}

ВАЖНО: 
- Ключи должны быть строками в формате "X.1" или "X.2"
- Значения должны быть массивами строк в формате "ЧЧ:ММ-ЧЧ:ММ"
- Если график не найден в тексте, верни пустой объект {{}}
- Если интервалы указаны без минут (например "00-02"), добавь ":00" (станет "00:00-02:00")"""
    
    try:
        model = genai.GenerativeModel(MODEL_TEXT)
        
        response = model.generate_content(prompt)
        
        # Извлекаем JSON из ответа
        response_text = response.text.strip()
        
        # Убираем markdown форматирование если есть
        response_text = response_text.replace("```json", "").replace("```", "").strip()
        
        # Парсим JSON
        schedule_data = json.loads(response_text)
        
        # Валидация: проверяем что это словарь
        if not isinstance(schedule_data, dict):
            logger.warning("Ответ от Gemini не является словарем")
            return {}
        
        # Если пустой словарь - график не найден
        if not schedule_data:
            return {}
        
        # Преобразуем все ключи в строки и значения в списки строк
        result = {}
        for key, value in schedule_data.items():
            # Нормализуем ключ (убираем "Черга", "Группа", пробелы)
            normalized_key = str(key).strip().lower()
            # Извлекаем номер группы (например "черга 1.1" → "1.1")
            import re
            match = re.search(r'(\d+)[.,](\d+)', normalized_key)
            if match:
                normalized_key = f"{match.group(1)}.{match.group(2)}"
            
            if normalized_key not in result:
                result[normalized_key] = []
            
            if isinstance(value, list):
                for v in value:
                    # Нормализуем интервалы (добавляем :00 если нужно)
                    interval = str(v).strip()
                    # Если формат "00-02", преобразуем в "00:00-02:00"
                    if re.match(r'^\d{1,2}-\d{1,2}$', interval):
                        parts = interval.split('-')
                        interval = f"{parts[0].zfill(2)}:00-{parts[1].zfill(2)}:00"
                    # Если формат "00:00–02:00" (длинное тире), заменяем на обычное
                    interval = interval.replace('–', '-').replace('—', '-')
                    result[normalized_key].append(interval)
            else:
                interval = str(value).strip()
                if re.match(r'^\d{1,2}-\d{1,2}$', interval):
                    parts = interval.split('-')
                    interval = f"{parts[0].zfill(2)}:00-{parts[1].zfill(2)}:00"
                interval = interval.replace('–', '-').replace('—', '-')
                result[normalized_key].append(interval)
        
        return result
        
    except json.JSONDecodeError as e:
        logger.warning(f"Не удалось распарсить JSON ответ от Gemini при анализе текста: {e}")
        return {}
    except Exception as e:
        logger.warning(f"Ошибка при анализе текста через Gemini: {e}")
        return {}


def is_schedule_post(text: str) -> bool:
    """
    Быстрая проверка через Gemini: является ли этот пост графиком отключений?
    
    DEPRECATED: Используйте check_schedule_post_and_date() для получения даты сразу.
    
    Args:
        text: Текст поста
    
    Returns:
        True если это график, False если нет
    """
    result = check_schedule_post_and_date(text)
    return result is not None


def check_schedule_post_and_date(text: str) -> Optional[str]:
    """
    Проверяет через Gemini: является ли пост графиком и определяет дату (today/tomorrow).
    
    Объединяет проверку "график/игнор" и определение даты в один вызов Gemini.
    Это более точно, чем regex, так как Gemini понимает контекст.
    
    Args:
        text: Текст поста
    
    Returns:
        "today" - если это график на сегодня
        "tomorrow" - если это график на завтра
        None - если это не график (игнор)
    """
    if not API_KEY:
        # Fallback на keywords если Gemini недоступен
        from constants import KEYWORDS
        text_lower = text.lower() if text else ""
        if any(keyword.lower() in text_lower for keyword in KEYWORDS):
            # Если есть keywords, но дата не определена - по умолчанию завтра
            return "tomorrow"
        return None
    
    if not text or len(text.strip()) < 10:
        return None
    
    from helpers import get_kyiv_time
    from datetime import datetime, timedelta
    now = get_kyiv_time()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    today_str = today.strftime("%d.%m.%Y")
    tomorrow_str = tomorrow.strftime("%d.%m.%Y")
    
    system_instruction = f"""Ты фильтруешь посты о графиках отключений электричества.

Сегодня: {today_str}, завтра: {tomorrow_str}.

Определи:

- "today": График/обновление на {today_str} (полный график с >=3 группами, изменения без даты или с датой {today_str}).

- "tomorrow": График на {tomorrow_str} (с датой {tomorrow_str} или новый график без даты).

- "игнор": Аварийные отключения, новости без данных, обновления одной группы, старые даты (< {today_str}), реклама, мусор.

Алгоритм даты (используй ТОЛЬКО дату из текста поста, формат DD.MM.YYYY / DD листопада / DD ноября):

1. Найди конкретную дату графика (игнорируй время вроде HH:MM, относительные слова "сегодня/завтра").

2. Сравни с {today_str}:

   - = {today_str} → "today"

   - = {tomorrow_str} → "tomorrow"

   - < {today_str} или > {tomorrow_str} → "игнор"

3. Если даты нет: "today" для изменений, "tomorrow" для нового графика.

Ответь ОДНИМ словом: "today", "tomorrow" или "игнор"."""

    # Ограничиваем длину для экономии
    text_limited = text[:2000] if len(text) > 2000 else text
    
    user_data = f"""Проанализируй этот пост:

{text_limited}

КРИТИЧЕСКИ ВАЖНО: 
- Текущая дата запроса: {today_str} (сегодня), {tomorrow_str} (завтра)
- НЕ смотри на дату публикации поста - она может быть вчера или позавчера
- Смотри ТОЛЬКО на дату, указанную В ТЕКСТЕ поста (например, "22 листопада", "22.11.2025")
- Сравнивай дату из текста поста с текущей датой ({today_str})

Это график отключений электричества? Если да, то на какую дату относительно СЕГОДНЯ ({today_str})?
- Если дата в посте = {today_str} → "today"
- Если дата в посте = {tomorrow_str} → "tomorrow"  
- Если дата в посте < {today_str} (вчера, позавчера) → "игнор" (старая дата)

Ответь одним словом: "today", "tomorrow" или "игнор"."""

    try:
        model = genai.GenerativeModel(MODEL_TEXT, system_instruction=system_instruction)
        response = model.generate_content(user_data)
        answer = response.text.strip().lower()
        
        logger.debug(f"Gemini ответ: {answer}")
        
        if "today" in answer or "сьогодні" in answer:
            return "today"
        elif "tomorrow" in answer or "завтра" in answer:
            return "tomorrow"
        else:
            # "игнор" или что-то другое - возвращаем None
            logger.debug(f"Ответ не распознан как today/tomorrow, возвращаем None (игнор)")
            return None
        
    except Exception as e:
        logger.warning(f"Ошибка при проверке поста через Gemini: {e}")
        # Fallback на keywords
        from constants import KEYWORDS
        text_lower = text.lower() if text else ""
        if any(keyword.lower() in text_lower for keyword in KEYWORDS):
            # Если есть keywords, но дата не определена - по умолчанию завтра
            return "tomorrow"
        return None


def generate_schedule_response(person_name: str, city_name: str, group: str, 
                               schedule_intervals: List[str], current_time_str: str,
                               status_message: str, next_change: str, time_to_next: str,
                               schedule_intervals_tomorrow: List[str] = None, 
                               date_today: str = None, date_tomorrow: str = None) -> str:
    """
    Генерирует красивый ответ для пользователя о графике отключений с помощью Gemini.
    
    Args:
        person_name: Имя человека
        city_name: Название города
        group: Группа электричества
        schedule_intervals: Список интервалов отключений на сегодня (например: ["11:00-15:00", "19:00-22:00"])
        current_time_str: Текущее время в формате "ЧЧ:ММ"
        status_message: Сообщение о текущем статусе (например: "✅ Сейчас свет есть.")
        next_change: Информация о следующем изменении
        time_to_next: Время до следующего изменения
        schedule_intervals_tomorrow: Список интервалов отключений на завтра (опционально)
        date_today: Дата сегодня в формате "DD.MM" (опционально)
        date_tomorrow: Дата завтра в формате "DD.MM" (опционально)
    
    Returns:
        Отформатированный ответ для пользователя
    """
    if not API_KEY:
        # Если Gemini недоступен, возвращаем простой формат
        intervals_today_text = "\n".join([f"• {interval}" for interval in schedule_intervals]) if schedule_intervals else "График не загружен."
        result = (
            f"👤 {person_name} (группа {group})\n"
            f"🏙️ Город: {city_name}\n\n"
        )
        
        if date_today:
            result += f"📅 {date_today}\n"
        result += f"⚡ График відключень на сьогодні:\n{intervals_today_text}\n\n"
        
        if schedule_intervals_tomorrow:
            intervals_tomorrow_text = "\n".join([f"• {interval}" for interval in schedule_intervals_tomorrow])
            if date_tomorrow:
                result += f"📅 {date_tomorrow}\n"
            result += f"⚡ Графік відключень на завтра:\n{intervals_tomorrow_text}\n\n"
        
        result += (
            f"🕐 Поточний час: {current_time_str}\n\n"
            f"{status_message}\n"
            f"{next_change}\n"
            f"{time_to_next}"
        )
        return result
    
    try:
        # Системные инструкции (роль, формат, правила) - отдельно от данных
        system_instruction = """Ты бот-помощник для информирования о графиках отключений электричества.

Твоя задача — взять данные от пользователя и сформировать из них красивый, понятный ответ.

ПРАВИЛА:
- Ответ всегда должен быть на украинском языке
- Используй эмодзи для лучшей читаемости
- Будь дружелюбным и информативным
- Используй ТОЧНО время, которое указано в данных пользователя
- Не используй свое локальное время или другое время
- Время всегда указано для Киева, Украина (Europe/Kyiv)
- Если есть график на завтра, покажи ОБА графика (сегодня и завтра)

ФОРМАТ ОТВЕТА:
👤 [Имя] (группа [группа])
🏙️ Місто: [Город]

📅 [Дата сегодня, если указана]
⚡ Графік відключень на сьогодні:
[список интервалов в формате • ЧЧ:ММ-ЧЧ:ММ]

[Если есть график на завтра:]
📅 [Дата завтра, если указана]
⚡ Графік відключень на завтра:
[список интервалов в формате • ЧЧ:ММ-ЧЧ:ММ]

🕐 Поточний час: [время из данных]

[статус]
[информация о следующем изменении]

ВАЖНО: Строго следуй формату и используй только данные, которые предоставлены пользователем."""

        # Пользовательские данные - отдельно от инструкций
        schedule_today_str = "\n".join([f"• {interval}" for interval in schedule_intervals]) if schedule_intervals else "График не загружен."
        
        user_data = f"""Сформируй ответ на основе этих данных:

Город: {city_name}
Человек: {person_name}
Группа электричества: {group}

График отключений для группы {group} на СЕГОДНЯ{f" ({date_today})" if date_today else ""}:
{schedule_today_str}
"""
        
        if schedule_intervals_tomorrow:
            schedule_tomorrow_str = "\n".join([f"• {interval}" for interval in schedule_intervals_tomorrow])
            user_data += f"""
График отключений для группы {group} на ЗАВТРА{f" ({date_tomorrow})" if date_tomorrow else ""}:
{schedule_tomorrow_str}
"""
        
        user_data += f"""
Текущее время в Киеве (Украина): {current_time_str}
Это время в часовом поясе Europe/Kyiv (UTC+2 или UTC+3 в зависимости от летнего времени).

Текущий статус: {status_message}
Следующее изменение: {next_change}
Время до изменения: {time_to_next}

ВАЖНО: Используй ТОЧНО время {current_time_str}, которое указано выше."""

        # Создаём модель с системными инструкциями
        model = genai.GenerativeModel(
            MODEL_TEXT,
            system_instruction=system_instruction
        )
        
        # Передаём только пользовательские данные
        response = model.generate_content(user_data)
        return response.text.strip()
        
    except Exception as e:
        # Если ошибка, возвращаем простой формат
        logger.error(f"Ошибка при генерации ответа через Gemini: {e}")
        intervals_today_text = "\n".join([f"• {interval}" for interval in schedule_intervals]) if schedule_intervals else "График не загружен."
        result = (
            f"👤 {person_name} (группа {group})\n"
            f"🏙️ Город: {city_name}\n\n"
        )
        
        if date_today:
            result += f"📅 {date_today}\n"
        result += f"⚡ График відключень на сьогодні:\n{intervals_today_text}\n\n"
        
        if schedule_intervals_tomorrow:
            intervals_tomorrow_text = "\n".join([f"• {interval}" for interval in schedule_intervals_tomorrow])
            if date_tomorrow:
                result += f"📅 {date_tomorrow}\n"
            result += f"⚡ Графік відключень на завтра:\n{intervals_tomorrow_text}\n\n"
        
        result += (
            f"🕐 Поточний час: {current_time_str}\n\n"
            f"{status_message}\n"
            f"{next_change}\n"
            f"{time_to_next}"
        )
        return result


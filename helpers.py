from datetime import datetime, timedelta
from typing import Optional, List, Dict

def get_schedule_status(schedule_intervals: Optional[List[str]], current_time: datetime) -> Dict[str, str]:
    """
    Определяет текущий статус подачи электричества на основе графика.
    
    Args:
        schedule_intervals: Список интервалов отключений в формате ["11:00-15:00", "19:00-22:00"]
        current_time: Текущее время
    
    Returns:
        Словарь с информацией о статусе:
        {
            "status": "ON" или "OFF",
            "message": "Сообщение о текущем статусе",
            "nextChange": "Информация о следующем изменении",
            "timeToNextChange": "Время до следующего изменения"
        }
    """
    if not schedule_intervals or len(schedule_intervals) == 0:
        return {
            "status": "ON",
            "message": "✅ Свет должен быть. График не загружен.",
            "nextChange": "-",
            "timeToNextChange": ""
        }
    
    # Парсим интервалы
    intervals = []
    # Убеждаемся, что работаем с timezone-aware datetime
    if current_time.tzinfo is None:
        # Если время без timezone, создаем naive datetime для сегодня
        today = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        # Если время с timezone, сохраняем timezone
        today = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
    
    for interval_str in schedule_intervals:
        try:
            start_str, end_str = interval_str.split('-')
            start_str = start_str.strip()
            end_str = end_str.strip()
            
            start_hours, start_minutes = map(int, start_str.split(':'))
            end_hours, end_minutes = map(int, end_str.split(':'))
            
            # Создаем интервалы в том же timezone, что и current_time
            start = today.replace(hour=start_hours, minute=start_minutes)
            end = today.replace(hour=end_hours, minute=end_minutes)
            
            # Если current_time имеет timezone, убеждаемся что интервалы тоже имеют timezone
            if current_time.tzinfo is not None:
                # Если start/end не имеют timezone, добавляем его
                if start.tzinfo is None:
                    start = start.replace(tzinfo=current_time.tzinfo)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=current_time.tzinfo)
            
            intervals.append({"start": start, "end": end})
        except (ValueError, AttributeError):
            continue
    
    # Сортируем интервалы по времени начала
    intervals.sort(key=lambda x: x["start"])
    
    # Проверяем, находимся ли мы внутри какого-то интервала отключения
    for interval in intervals:
        if interval["start"] <= current_time < interval["end"]:
            time_diff = interval["end"] - current_time
            return {
                "status": "OFF",
                "message": "❌ Сейчас свет отключён.",
                "nextChange": f"✅ Ближайшее включение: {interval['end'].strftime('%H:%M')}",
                "timeToNextChange": f"(через {format_time_diff(time_diff)})"
            }
    
    # Ищем ближайшее отключение
    next_outage = None
    for interval in intervals:
        if current_time < interval["start"]:
            next_outage = interval
            break
    
    if next_outage:
        time_diff = next_outage["start"] - current_time
        return {
            "status": "ON",
            "message": "✅ Сейчас свет есть.",
            "nextChange": f"❌ Ближайшее отключение: {next_outage['start'].strftime('%H:%M')}",
            "timeToNextChange": f"(через {format_time_diff(time_diff)})"
        }
    
    # Если все отключения прошли
    return {
        "status": "ON",
        "message": "✅ Свет уже есть.",
        "nextChange": "Отключений на сегодня больше нет.",
        "timeToNextChange": ""
    }


def format_time_diff(time_diff: timedelta) -> str:
    """Форматирует разницу времени в читаемый формат"""
    total_seconds = int(time_diff.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    
    result = ""
    if hours > 0:
        result += f"{hours} ч "
    if minutes > 0:
        result += f"{minutes} мин"
    
    return result.strip()


def get_kyiv_time() -> datetime:
    """
    Возвращает текущее время в часовом поясе Киева (Europe/Kyiv).
    Используется для всех операций, связанных с графиками отключений,
    чтобы пользователи видели актуальное время для Украины.
    """
    try:
        # Пробуем использовать zoneinfo (Python 3.9+)
        from zoneinfo import ZoneInfo
        kyiv_tz = ZoneInfo("Europe/Kyiv")
    except (ImportError, ModuleNotFoundError):
        # Fallback на pytz для старых версий Python
        try:
            import pytz
            kyiv_tz = pytz.timezone("Europe/Kyiv")
        except ImportError:
            # Если pytz не установлен, используем UTC+2 (приблизительно)
            # Это не идеально, но лучше чем ничего
            from datetime import timezone, timedelta
            kyiv_tz = timezone(timedelta(hours=2))
    
    return datetime.now(kyiv_tz)


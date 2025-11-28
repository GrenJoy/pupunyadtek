"""
Модуль для работы с графиками группы людей.

Формирует единое сообщение с графиками для всех людей из группы без использования Gemini.
"""

import logging
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from database import Database, Person
from helpers import get_schedule_status, get_kyiv_time

logger = logging.getLogger(__name__)


def format_person_schedule_block(
    person: Person,
    city_name: str,
    schedule_intervals_today: Optional[List[str]],
    schedule_intervals_tomorrow: Optional[List[str]],
    current_time: datetime,
    today_date: Optional[str] = None,
    tomorrow_date: Optional[str] = None
) -> str:
    """
    Формирует блок с графиком для одного человека.
    
    Args:
        person: Объект Person
        city_name: Название города
        schedule_intervals_today: Интервалы отключений на сегодня
        schedule_intervals_tomorrow: Интервалы отключений на завтра
        current_time: Текущее время
        today_date: Дата сегодня в формате "DD.MM" (опционально)
        tomorrow_date: Дата завтра в формате "DD.MM" (опционально)
    
    Returns:
        Отформатированный блок с графиком для человека
    """
    # Получаем статус для сегодня
    status = get_schedule_status(schedule_intervals_today, current_time)
    
    # Формируем блок
    block = f"👤 <b>{person.name}</b> (группа {person.group})\n"
    block += f"🏙️ Місто: {city_name}\n"
    
    # График на сегодня
    if today_date:
        block += f"📅 {today_date}\n"
    
    if schedule_intervals_today:
        intervals_text = "\n".join([f"• {interval}" for interval in schedule_intervals_today])
        block += f"⚡ Графік відключень на сьогодні:\n{intervals_text}\n"
    else:
        block += "⚡ Графік відключень на сьогодні:\nГрафик не загружен.\n"
    
    # График на завтра
    if schedule_intervals_tomorrow:
        if tomorrow_date:
            block += f"📅 {tomorrow_date}\n"
        intervals_tomorrow_text = "\n".join([f"• {interval}" for interval in schedule_intervals_tomorrow])
        block += f"⚡ Графік відключень на завтра:\n{intervals_tomorrow_text}\n"
    
    # Текущее время и статус
    current_time_str = current_time.strftime('%H:%M')
    block += f"🕐 Поточний час: {current_time_str}\n"
    block += f"{status['message']}\n"
    block += f"{status['nextChange']}\n"
    if status['timeToNextChange']:
        block += f"{status['timeToNextChange']}"
    
    return block


def generate_group_schedule_message(db: Database) -> str:
    """
    Генерирует единое сообщение с графиками для всех людей из группы.
    
    Args:
        db: Экземпляр Database
    
    Returns:
        Отформатированное сообщение с графиками группы
    """
    # Получаем всех людей из группы
    group_people = db.get_person_group()
    
    if not group_people:
        return "❌ Группа не создана или пуста.\n\nСоздайте группу через 'Управление людьми' → 'Создать группу людей'."
    
    # Получаем текущее время
    current_time = get_kyiv_time()
    today_date = current_time.strftime('%d.%m')
    tomorrow_date = (current_time + timedelta(days=1)).strftime('%d.%m')
    
    # Формируем сообщение
    message_parts = ["📅 <b>График группы</b>\n"]
    
    for i, person in enumerate(group_people):
        # Получаем город
        city = db.get_city(person.city_id)
        city_name = city.name if city else f"ID {person.city_id}"
        
        # Получаем графики для группы этого человека
        schedule_intervals_today = db.get_schedule_for_group(person.city_id, person.group, "today")
        schedule_intervals_tomorrow = db.get_schedule_for_group(person.city_id, person.group, "tomorrow")
        
        # Формируем блок для этого человека
        person_block = format_person_schedule_block(
            person=person,
            city_name=city_name,
            schedule_intervals_today=schedule_intervals_today,
            schedule_intervals_tomorrow=schedule_intervals_tomorrow,
            current_time=current_time,
            today_date=today_date,
            tomorrow_date=tomorrow_date
        )
        
        message_parts.append(person_block)
        
        # Добавляем пустой абзац между людьми (но не после последнего)
        if i < len(group_people) - 1:
            message_parts.append("")
    
    return "\n".join(message_parts)


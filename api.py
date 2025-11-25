import logging
import asyncio
from typing import List, Optional, Dict
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from datetime import datetime

from database import Database, City, Person
from gemini_service import analyze_schedule_image

# Настройка логирования
logger = logging.getLogger(__name__)

# Инициализация FastAPI
app = FastAPI(title="Power Outage Bot API")

# Настройка CORS (для локальной разработки)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене лучше указать конкретные домены
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Инициализация базы данных
db = Database()

# Pydantic модели для валидации данных
class CityModel(BaseModel):
    id: int
    name: str

class CityCreate(BaseModel):
    name: str

class PersonModel(BaseModel):
    id: int
    name: str
    city_id: int
    group: str

class PersonCreate(BaseModel):
    name: str
    city_id: int
    group: str

class PersonUpdate(BaseModel):
    name: Optional[str] = None
    group: Optional[str] = None

class ScheduleResponse(BaseModel):
    city_id: int
    schedule_data: Dict[str, List[str]]
    updated_at: Optional[str]

# --- Endpoints ---

@app.get("/")
async def root():
    """Корневой endpoint для пинга сервисов"""
    return {
        "status": "ok",
        "service": "power-outage-schedule-bot",
        "message": "Bot is alive and running",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/health")
async def health_check():
    """Проверка работоспособности сервиса"""
    return {"status": "ok", "service": "power-outage-schedule-bot"}

@app.get("/status", response_class=HTMLResponse)
async def status_page():
    """HTML страница со статусом бота"""
    html_content = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Power Outage Bot - Status</title>
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
                padding: 20px;
            }
            .container {
                background: white;
                border-radius: 20px;
                padding: 40px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
                text-align: center;
                max-width: 500px;
                width: 100%;
            }
            .status-icon {
                width: 80px;
                height: 80px;
                margin: 0 auto 20px;
                background: #10b981;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                animation: pulse 2s infinite;
            }
            @keyframes pulse {
                0%, 100% { transform: scale(1); }
                50% { transform: scale(1.1); }
            }
            .status-icon::before {
                content: "✓";
                color: white;
                font-size: 40px;
                font-weight: bold;
            }
            h1 {
                color: #1f2937;
                margin-bottom: 10px;
                font-size: 28px;
            }
            .status-text {
                color: #10b981;
                font-size: 18px;
                font-weight: 600;
                margin-bottom: 20px;
            }
            .info {
                color: #6b7280;
                font-size: 14px;
                line-height: 1.6;
                margin-top: 20px;
            }
            .timestamp {
                color: #9ca3af;
                font-size: 12px;
                margin-top: 30px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="status-icon"></div>
            <h1>Power Outage Bot</h1>
            <div class="status-text">✓ Бот работает</div>
            <div class="info">
                Сервис мониторинга графиков отключений электроэнергии активен и готов к работе.
            </div>
            <div class="timestamp" id="timestamp"></div>
        </div>
        <script>
            document.getElementById('timestamp').textContent = 'Последнее обновление: ' + new Date().toLocaleString('ru-RU');
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# --- Cities ---

@app.get("/api/cities", response_model=List[CityModel])
async def get_cities():
    """Получить список всех городов"""
    try:
        # Используем asyncio.to_thread для неблокирующего вызова синхронной БД
        cities = await asyncio.to_thread(db.get_cities)
        return [CityModel(id=c.id, name=c.name) for c in cities]
    except Exception as e:
        logger.error(f"Ошибка при получении городов: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/cities", response_model=CityModel)
async def add_city(city: CityCreate):
    """Добавить новый город"""
    try:
        # Проверяем существование
        exists = await asyncio.to_thread(db.city_exists, city.name)
        if exists:
            raise HTTPException(status_code=400, detail=f"Город '{city.name}' уже существует")
        
        city_id = await asyncio.to_thread(db.add_city, city.name)
        return CityModel(id=city_id, name=db.normalize_city_name(city.name))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка при добавлении города: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/cities/{city_id}")
async def delete_city(city_id: int):
    """Удалить город"""
    try:
        await asyncio.to_thread(db.delete_city, city_id)
        return {"status": "success", "message": f"Город {city_id} удален"}
    except Exception as e:
        logger.error(f"Ошибка при удалении города: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- People ---

@app.get("/api/people", response_model=List[PersonModel])
async def get_people(city_id: Optional[int] = None):
    """Получить список людей (опционально фильтр по городу)"""
    try:
        people = await asyncio.to_thread(db.get_people, city_id)
        return [PersonModel(id=p.id, name=p.name, city_id=p.city_id, group=p.group) for p in people]
    except Exception as e:
        logger.error(f"Ошибка при получении людей: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/people", response_model=PersonModel)
async def add_person(person: PersonCreate):
    """Добавить человека"""
    try:
        person_id = await asyncio.to_thread(db.add_person, person.name, person.city_id, person.group)
        return PersonModel(id=person_id, name=person.name, city_id=person.city_id, group=person.group)
    except Exception as e:
        logger.error(f"Ошибка при добавлении человека: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/people/{person_id}")
async def update_person(person_id: int, person: PersonUpdate):
    """Обновить данные человека"""
    try:
        await asyncio.to_thread(db.update_person, person_id, person.name, person.group)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Ошибка при обновлении человека: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/people/{person_id}")
async def delete_person(person_id: int):
    """Удалить человека"""
    try:
        await asyncio.to_thread(db.delete_person, person_id)
        return {"status": "success", "message": f"Человек {person_id} удален"}
    except Exception as e:
        logger.error(f"Ошибка при удалении человека: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- Schedules ---

@app.get("/api/schedules/{city_id}")
async def get_schedule(city_id: int):
    """Получить график для города (возвращает оба графика: сегодня и завтра)"""
    try:
        schedules = await asyncio.to_thread(db.get_both_schedules, city_id)
        updated_at = await asyncio.to_thread(db.get_schedule_updated_at, city_id)
        
        return {
            "city_id": city_id, 
            "schedule_today": schedules.get("today") or {}, 
            "schedule_tomorrow": schedules.get("tomorrow") or {},
            "updated_at": updated_at
        }
    except Exception as e:
        logger.error(f"Ошибка при получении графика: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload_schedule")
async def upload_schedule(
    city_id: int = Form(...),
    file: UploadFile = File(...)
):
    """Загрузить и обработать фото графика"""
    try:
        # Читаем файл
        contents = await file.read()
        
        # Анализируем через Gemini (в отдельном потоке, так как это блокирующая операция)
        schedule_data = await asyncio.to_thread(analyze_schedule_image, contents, file.content_type)
        
        # Сохраняем в БД (по умолчанию как завтра, так как дата не определена из файла)
        await asyncio.to_thread(db.save_schedule, city_id, schedule_data, "tomorrow")
        
        return {"status": "success", "schedule_data": schedule_data}
    except ValueError as e:
        # Ошибки валидации или Gemini
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Ошибка при загрузке графика: {e}")
        raise HTTPException(status_code=500, detail=str(e))

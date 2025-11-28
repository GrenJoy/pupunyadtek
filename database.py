import os
import sqlite3
import json
import logging
from typing import List, Optional, Dict, Union
from dataclasses import dataclass
from urllib.parse import urlparse
from datetime import datetime

logger = logging.getLogger(__name__)

@dataclass
class City:
    id: int
    name: str

@dataclass
class Channel:
    id: int
    city_id: int
    channel_username: str
    last_message_id: int = 0

@dataclass
class Person:
    id: int
    name: str
    city_id: int
    group: str

class Database:
    def __init__(self, db_path: str = "bot.db"):
        """
        Инициализация базы данных.
        
        Автоматически определяет тип БД:
        - Если установлена переменная окружения DATABASE_URL (PostgreSQL на Render) - использует PostgreSQL
        - Иначе использует SQLite (для локальной разработки)
        """
        self.db_path = db_path
        self.db_type = self._detect_db_type()
        self.conn_params = None
        
        if self.db_type == "postgresql":
            self._init_postgresql()
        else:
            self._init_sqlite()
        
        self.init_db()
    
    def _detect_db_type(self) -> str:
        """Определяет тип базы данных по переменным окружения"""
        database_url = os.getenv("DATABASE_URL")
        if database_url and (database_url.startswith("postgres://") or database_url.startswith("postgresql://")):
            logger.info("🔍 Обнаружена PostgreSQL база данных (DATABASE_URL)")
            return "postgresql"
        else:
            logger.info("🔍 Используется SQLite база данных (локальная разработка)")
            return "sqlite"
    
    def _init_postgresql(self):
        """Инициализация параметров подключения к PostgreSQL"""
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL не установлен для PostgreSQL")
        
        # Парсим DATABASE_URL
        # Формат: postgres://user:password@host:port/database
        parsed = urlparse(database_url)
        
        # psycopg3 использует dbname вместо database
        self.conn_params = {
            "host": parsed.hostname,
            "port": parsed.port or 5432,
            "dbname": parsed.path.lstrip('/'),  # psycopg3 использует dbname
            "user": parsed.username,
            "password": parsed.password,
            "sslmode": "require"  # Render требует SSL
        }
        
        # Для psycopg2 нужен параметр database
        self.conn_params_psycopg2 = {
            "host": parsed.hostname,
            "port": parsed.port or 5432,
            "database": parsed.path.lstrip('/'),  # psycopg2 использует database
            "user": parsed.username,
            "password": parsed.password,
            "sslmode": "require"  # Render требует SSL
        }
        
        # Пробуем импортировать psycopg (psycopg3) или psycopg2
        try:
            import psycopg
            self.psycopg = psycopg
            self.use_psycopg3 = True
        except ImportError:
            try:
                import psycopg2
                self.psycopg2 = psycopg2
                self.use_psycopg3 = False
            except ImportError:
                raise ImportError("psycopg не установлен. Установите: pip install 'psycopg[binary]'")
    
    def _init_sqlite(self):
        """Инициализация SQLite (ничего не делаем, используется по умолчанию)"""
        pass
    
    def get_connection(self):
        """Получить соединение с базой данных"""
        if self.db_type == "postgresql":
            if hasattr(self, 'use_psycopg3') and self.use_psycopg3:
                # Используем psycopg (psycopg3) - использует dbname
                return self.psycopg.connect(**self.conn_params)
            else:
                # Используем psycopg2 - использует database
                return self.psycopg2.connect(**self.conn_params_psycopg2)
        else:
            return sqlite3.connect(self.db_path)
    
    def _adapt_sql(self, sql: str) -> str:
        """Адаптирует SQL запрос для разных типов БД"""
        if self.db_type == "postgresql":
            # Заменяем ? на %s для PostgreSQL
            sql = sql.replace("?", "%s")
            # Заменяем AUTOINCREMENT на SERIAL (для CREATE TABLE)
            sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            sql = sql.replace("AUTOINCREMENT", "")
            # Заменяем ON CONFLICT для PostgreSQL
            # SQLite: ON CONFLICT(city_id) DO UPDATE SET ...
            # PostgreSQL: ON CONFLICT (city_id) DO UPDATE SET ...
            sql = sql.replace("ON CONFLICT(", "ON CONFLICT (")
            # В PostgreSQL TEXT работает для JSON, но можно использовать JSONB для лучшей производительности
            # Пока оставляем TEXT для совместимости
        return sql
    
    def _get_placeholder(self) -> str:
        """Возвращает placeholder для параметров запроса"""
        return "%s" if self.db_type == "postgresql" else "?"
    
    def _get_lastrowid(self, cursor) -> int:
        """Получить ID последней вставленной записи"""
        if self.db_type == "postgresql":
            # В PostgreSQL используем RETURNING или cursor.fetchone()
            return cursor.lastrowid if hasattr(cursor, 'lastrowid') and cursor.lastrowid else 0
        else:
            return cursor.lastrowid
    
    def init_db(self):
        """Инициализация базы данных с созданием таблиц"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Таблица городов
        cursor.execute(self._adapt_sql("""
            CREATE TABLE IF NOT EXISTS cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """))
        
        # Таблица людей
        cursor.execute(self._adapt_sql("""
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                city_id INTEGER NOT NULL,
                group_name TEXT NOT NULL,
                FOREIGN KEY (city_id) REFERENCES cities(id) ON DELETE CASCADE
            )
        """))
        
        # Таблица графиков (храним JSON с интервалами для каждой группы)
        # Теперь храним два графика: на сегодня и на завтра
        cursor.execute(self._adapt_sql("""
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                schedule_data TEXT,
                schedule_today TEXT,
                schedule_tomorrow TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (city_id) REFERENCES cities(id) ON DELETE CASCADE,
                UNIQUE(city_id)
            )
        """))
        
        # Миграция: переносим старые данные из schedule_data в schedule_today
        # и добавляем новые поля если их нет
        try:
            if self.db_type == "postgresql":
                # Проверяем наличие новых полей
                cursor.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='schedules' AND column_name IN ('schedule_today', 'schedule_tomorrow')
                """)
                existing_columns = [row[0] for row in cursor.fetchall()]
                
                if 'schedule_today' not in existing_columns:
                    logger.info("🔄 Добавляю поле schedule_today в таблицу schedules (PostgreSQL)...")
                    cursor.execute("ALTER TABLE schedules ADD COLUMN schedule_today TEXT")
                
                if 'schedule_tomorrow' not in existing_columns:
                    logger.info("🔄 Добавляю поле schedule_tomorrow в таблицу schedules (PostgreSQL)...")
                    cursor.execute("ALTER TABLE schedules ADD COLUMN schedule_tomorrow TEXT")
                
                # ВАЖНО: Делаем schedule_data nullable, если она имеет NOT NULL constraint
                # Это нужно для совместимости со старой схемой
                try:
                    cursor.execute("""
                        SELECT is_nullable 
                        FROM information_schema.columns 
                        WHERE table_name='schedules' AND column_name='schedule_data'
                    """)
                    result = cursor.fetchone()
                    if result and result[0] == 'NO':
                        logger.info("🔄 Делаю поле schedule_data nullable в таблице schedules (PostgreSQL)...")
                        cursor.execute("ALTER TABLE schedules ALTER COLUMN schedule_data DROP NOT NULL")
                except Exception as e:
                    logger.debug(f"Проверка constraint для schedule_data: {e}")
                
                # Миграция данных: переносим schedule_data в schedule_today
                cursor.execute("""
                    UPDATE schedules 
                    SET schedule_today = schedule_data 
                    WHERE schedule_data IS NOT NULL AND schedule_today IS NULL
                """)
            else:
                # SQLite
                cursor.execute("PRAGMA table_info(schedules)")
                columns = [row[1] for row in cursor.fetchall()]
                
                if 'schedule_today' not in columns:
                    logger.info("🔄 Добавляю поле schedule_today в таблицу schedules (SQLite)...")
                    cursor.execute("ALTER TABLE schedules ADD COLUMN schedule_today TEXT")
                
                if 'schedule_tomorrow' not in columns:
                    logger.info("🔄 Добавляю поле schedule_tomorrow в таблицу schedules (SQLite)...")
                    cursor.execute("ALTER TABLE schedules ADD COLUMN schedule_tomorrow TEXT")
                
                # Миграция данных: переносим schedule_data в schedule_today
                cursor.execute("""
                    UPDATE schedules 
                    SET schedule_today = schedule_data 
                    WHERE schedule_data IS NOT NULL AND schedule_today IS NULL
                """)
        except Exception as e:
            logger.warning(f"⚠️ Ошибка при миграции полей графиков: {e}")
        
        # Таблица подписок на уведомления
        cursor.execute(self._adapt_sql("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE,
                UNIQUE(user_id, person_id)
            )
        """))
        
        # Таблица каналов для городов
        cursor.execute(self._adapt_sql("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                channel_username TEXT NOT NULL,
                last_message_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (city_id) REFERENCES cities(id) ON DELETE CASCADE,
                UNIQUE(city_id)
            )
        """))
        
        # Миграция: добавляем поле last_message_id если его нет (для существующих баз)
        # Проверяем наличие поля через структуру таблицы
        try:
            if self.db_type == "postgresql":
                # Для PostgreSQL проверяем через information_schema
                cursor.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='channels' AND column_name='last_message_id'
                """)
                if not cursor.fetchone():
                    logger.info("🔄 Добавляю поле last_message_id в таблицу channels (PostgreSQL)...")
                    cursor.execute("ALTER TABLE channels ADD COLUMN last_message_id INTEGER DEFAULT 0")
            else:
                # Для SQLite проверяем через PRAGMA table_info
                cursor.execute("PRAGMA table_info(channels)")
                columns = [row[1] for row in cursor.fetchall()]
                if 'last_message_id' not in columns:
                    logger.info("🔄 Добавляю поле last_message_id в таблицу channels (SQLite)...")
                    cursor.execute("ALTER TABLE channels ADD COLUMN last_message_id INTEGER DEFAULT 0")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка при проверке/добавлении поля last_message_id: {e}")
            # Пытаемся добавить поле в любом случае (для совместимости)
            try:
                if self.db_type == "postgresql":
                    cursor.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS last_message_id INTEGER DEFAULT 0")
                else:
                    cursor.execute("ALTER TABLE channels ADD COLUMN last_message_id INTEGER DEFAULT 0")
            except:
                # Поле уже существует или другая ошибка - игнорируем
                pass
        
        # Таблица групп людей (для мониторинга графиков группы)
        cursor.execute(self._adapt_sql("""
            CREATE TABLE IF NOT EXISTS person_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE,
                UNIQUE(person_id)
            )
        """))
        
        conn.commit()
        conn.close()
        logger.info(f"✅ База данных инициализирована ({self.db_type})")
    
    # Работа с городами
    def normalize_city_name(self, name: str) -> str:
        """
        Нормализует название города (убирает лишние пробелы, приводит к правильному регистру)
        Поддерживает украинские символы (і, ї, є, ґ и т.д.)
        """
        # Убираем лишние пробелы
        name = ' '.join(name.split())
        name = name.strip()
        
        # Если название не пустое, делаем первую букву каждого слова заглавной
        if name:
            # Разбиваем на слова и каждое слово с заглавной буквы
            # Используем title() для правильной обработки украинских букв
            words = name.split()
            normalized_words = []
            for word in words:
                if word:
                    # title() правильно обрабатывает украинские символы
                    normalized_words.append(word[0].upper() + word[1:].lower() if len(word) > 1 else word.upper())
                else:
                    normalized_words.append(word)
            return ' '.join(normalized_words)
        
        return name
    
    def city_exists(self, name: str) -> bool:
        """Проверяет, существует ли город (без учета регистра)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(f"SELECT id FROM cities WHERE LOWER(name) = LOWER({placeholder})", (name.strip(),))
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    
    def add_city(self, name: str) -> int:
        """Добавить город, возвращает ID"""
        # Нормализуем название
        normalized_name = self.normalize_city_name(name)
        
        # Проверяем на дубликаты (без учета регистра)
        if self.city_exists(normalized_name):
            raise ValueError(f"Город '{normalized_name}' уже существует")
        
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        try:
            if self.db_type == "postgresql":
                cursor.execute(f"INSERT INTO cities (name) VALUES ({placeholder}) RETURNING id", (normalized_name,))
                city_id = cursor.fetchone()[0]
            else:
                cursor.execute(f"INSERT INTO cities (name) VALUES ({placeholder})", (normalized_name,))
                city_id = cursor.lastrowid
            conn.commit()
            return city_id
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise ValueError(f"Город '{normalized_name}' уже существует")
            raise
        finally:
            conn.close()
    
    def init_default_cities(self):
        """Инициализирует базу предустановленными городами (если их еще нет)"""
        default_cities = ["Дніпро", "Запоріжжя"]
        
        for city_name in default_cities:
            if not self.city_exists(city_name):
                try:
                    self.add_city(city_name)
                    logger.info(f"✅ Добавлен предустановленный город: {city_name}")
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось добавить город {city_name}: {e}")
    
    def get_cities(self) -> List[City]:
        """Получить все города"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM cities ORDER BY name")
        cities = [City(id=row[0], name=row[1]) for row in cursor.fetchall()]
        conn.close()
        return cities
    
    def get_city(self, city_id: int) -> Optional[City]:
        """Получить город по ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(f"SELECT id, name FROM cities WHERE id = {placeholder}", (city_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return City(id=row[0], name=row[1])
        return None
    
    def delete_city(self, city_id: int):
        """Удалить город (каскадно удалит людей и графики)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(f"DELETE FROM cities WHERE id = {placeholder}", (city_id,))
        conn.commit()
        conn.close()
    
    # Работа с людьми
    def add_person(self, name: str, city_id: int, group: str) -> int:
        """Добавить человека, возвращает ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        if self.db_type == "postgresql":
            cursor.execute(
                f"INSERT INTO people (name, city_id, group_name) VALUES ({placeholder}, {placeholder}, {placeholder}) RETURNING id",
                (name, city_id, group)
            )
            person_id = cursor.fetchone()[0]
        else:
            cursor.execute(
                f"INSERT INTO people (name, city_id, group_name) VALUES ({placeholder}, {placeholder}, {placeholder})",
                (name, city_id, group)
            )
            person_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return person_id
    
    def get_people(self, city_id: Optional[int] = None) -> List[Person]:
        """Получить всех людей, опционально отфильтровать по городу"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        if city_id:
            cursor.execute(
                f"SELECT id, name, city_id, group_name FROM people WHERE city_id = {placeholder} ORDER BY name",
                (city_id,)
            )
        else:
            cursor.execute("SELECT id, name, city_id, group_name FROM people ORDER BY name")
        people = [Person(id=row[0], name=row[1], city_id=row[2], group=row[3]) for row in cursor.fetchall()]
        conn.close()
        return people
    
    def get_person(self, person_id: int) -> Optional[Person]:
        """Получить человека по ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(f"SELECT id, name, city_id, group_name FROM people WHERE id = {placeholder}", (person_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return Person(id=row[0], name=row[1], city_id=row[2], group=row[3])
        return None
    
    def update_person(self, person_id: int, name: Optional[str] = None, group: Optional[str] = None):
        """Обновить данные человека"""
        conn = self.get_connection()
        cursor = conn.cursor()
        updates = []
        params = []
        placeholder = self._get_placeholder()
        if name:
            updates.append(f"name = {placeholder}")
            params.append(name)
        if group:
            updates.append(f"group_name = {placeholder}")
            params.append(group)
        if updates:
            params.append(person_id)
            cursor.execute(
                f"UPDATE people SET {', '.join(updates)} WHERE id = {placeholder}",
                params
            )
            conn.commit()
        conn.close()
    
    def delete_person(self, person_id: int):
        """Удалить человека"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(f"DELETE FROM people WHERE id = {placeholder}", (person_id,))
        conn.commit()
        conn.close()

    def get_city_subscribers(self, city_id: int) -> List[Dict[str, Union[int, str]]]:
        """Получить всех подписчиков для города с информацией о людях"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        
        query = f"""
            SELECT s.user_id, p.name, p.group_name
            FROM subscriptions s
            JOIN people p ON s.person_id = p.id
            WHERE p.city_id = {placeholder}
        """
        
        cursor.execute(query, (city_id,))
        rows = cursor.fetchall()
        conn.close()
        
        subscribers = []
        for row in rows:
            subscribers.append({
                "user_id": row[0],
                "person_name": row[1],
                "group": row[2]
            })
        return subscribers
    
    # Работа с графиками
    def save_schedule(self, city_id: int, schedule_data: Dict[str, List[str]], schedule_type: str = "today"):
        """
        Сохранить или обновить график для города
        
        Args:
            city_id: ID города
            schedule_data: Данные графика (словарь с группами и интервалами)
            schedule_type: Тип графика - "today" или "tomorrow" (по умолчанию "today")
        """
        if schedule_type not in ["today", "tomorrow"]:
            raise ValueError(f"schedule_type должен быть 'today' или 'tomorrow', получен: {schedule_type}")
        
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # Сериализуем данные в JSON
            schedule_json = json.dumps(schedule_data, ensure_ascii=False)
            placeholder = self._get_placeholder()
            
            field_name = "schedule_today" if schedule_type == "today" else "schedule_tomorrow"
            
            if self.db_type == "postgresql":
                # PostgreSQL использует другой синтаксис ON CONFLICT
                # ВАЖНО: Также обновляем schedule_data для совместимости со старой схемой (если она ещё существует)
                # Это нужно, чтобы избежать ошибок NOT NULL constraint
                cursor.execute(
                    f"""INSERT INTO schedules (city_id, {field_name}, schedule_data) 
                       VALUES ({placeholder}, {placeholder}, {placeholder}) 
                       ON CONFLICT (city_id) DO UPDATE SET {field_name} = {placeholder}, schedule_data = {placeholder}, updated_at = CURRENT_TIMESTAMP""",
                    (city_id, schedule_json, schedule_json, schedule_json, schedule_json)
                )
            else:
                cursor.execute(
                    f"""INSERT INTO schedules (city_id, {field_name}) 
                       VALUES ({placeholder}, {placeholder}) 
                       ON CONFLICT(city_id) DO UPDATE SET {field_name} = {placeholder}, updated_at = CURRENT_TIMESTAMP""",
                    (city_id, schedule_json, schedule_json)
                )
            conn.commit()
            logger.debug(f"✅ График ({schedule_type}) сохранён для города {city_id}")
        except Exception as e:
            conn.rollback()
            logger.error(f"❌ Ошибка при сохранении графика ({schedule_type}) для города {city_id}: {e}")
            raise
        finally:
            conn.close()
    
    def get_schedule(self, city_id: int, schedule_type: str = "today") -> Optional[Dict[str, List[str]]]:
        """
        Получить график для города
        
        Args:
            city_id: ID города
            schedule_type: Тип графика - "today" или "tomorrow" (по умолчанию "today")
        
        Returns:
            Словарь с графиком или None
        """
        if schedule_type not in ["today", "tomorrow"]:
            raise ValueError(f"schedule_type должен быть 'today' или 'tomorrow', получен: {schedule_type}")
        
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        field_name = "schedule_today" if schedule_type == "today" else "schedule_tomorrow"
        
        try:
            cursor.execute(f"SELECT {field_name} FROM schedules WHERE city_id = {placeholder}", (city_id,))
            row = cursor.fetchone()
            if row and row[0]:
                # Парсим JSON из строки
                schedule_data = json.loads(row[0])
                return schedule_data
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка при парсинге JSON графика ({schedule_type}) для города {city_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Ошибка при получении графика ({schedule_type}) для города {city_id}: {e}")
            return None
        finally:
            conn.close()
    
    def get_both_schedules(self, city_id: int) -> Dict[str, Optional[Dict[str, List[str]]]]:
        """
        Получить оба графика (сегодня и завтра) для города
        
        Returns:
            Словарь с ключами "today" и "tomorrow", значения - графики или None
        """
        return {
            "today": self.get_schedule(city_id, "today"),
            "tomorrow": self.get_schedule(city_id, "tomorrow")
        }
    
    def get_schedule_updated_at(self, city_id: int, schedule_type: str = "today") -> Optional[datetime]:
        """
        Получить дату последнего обновления графика
        
        Args:
            city_id: ID города
            schedule_type: Тип графика - "today" или "tomorrow"
        
        Returns:
            datetime объекта с датой обновления или None
        """
        if schedule_type not in ["today", "tomorrow"]:
            raise ValueError(f"schedule_type должен быть 'today' или 'tomorrow', получен: {schedule_type}")
        
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        field_name = "schedule_today" if schedule_type == "today" else "schedule_tomorrow"
        
        try:
            # Проверяем, что график существует (поле не NULL)
            cursor.execute(
                f"SELECT updated_at FROM schedules WHERE city_id = {placeholder} AND {field_name} IS NOT NULL",
                (city_id,)
            )
            row = cursor.fetchone()
            if row and row[0]:
                # Возвращаем datetime
                if isinstance(row[0], str):
                    # SQLite возвращает строку
                    try:
                        return datetime.fromisoformat(row[0].replace('Z', '+00:00'))
                    except:
                        return datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
                else:
                    # PostgreSQL возвращает datetime объект
                    return row[0]
            return None
        except Exception as e:
            logger.error(f"Ошибка при получении updated_at графика ({schedule_type}) для города {city_id}: {e}")
            return None
        finally:
            conn.close()
    
    def rotate_schedules(self):
        """
        Переносит график на завтра в график на сегодня и очищает график на завтра.
        Вызывается в полночь для автоматического обновления.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.db_type == "postgresql":
                cursor.execute("""
                    UPDATE schedules 
                    SET schedule_today = schedule_tomorrow,
                        schedule_tomorrow = NULL,
                        schedule_data = schedule_tomorrow,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE schedule_tomorrow IS NOT NULL
                """)
            else:
                cursor.execute("""
                    UPDATE schedules 
                    SET schedule_today = schedule_tomorrow,
                        schedule_tomorrow = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE schedule_tomorrow IS NOT NULL
                """)
            conn.commit()
            rotated_count = cursor.rowcount
            logger.info(f"🔄 Перенесено графиков в полночь: {rotated_count}")
        except Exception as e:
            conn.rollback()
            logger.error(f"❌ Ошибка при переносе графиков: {e}")
            raise
        finally:
            conn.close()
    
    def get_schedule_for_group(self, city_id: int, group: str, schedule_type: str = "today") -> Optional[List[str]]:
        """
        Получить интервалы отключений для конкретной группы в городе
        
        Args:
            city_id: ID города
            group: Группа электричества (например, "1.1")
            schedule_type: Тип графика - "today" или "tomorrow" (по умолчанию "today")
        
        Returns:
            Список интервалов или None
        """
        schedule = self.get_schedule(city_id, schedule_type)
        if schedule:
            return schedule.get(group)
        return None
    
    # Работа с подписками на уведомления
    def add_subscription(self, user_id: int, person_id: int) -> bool:
        """Добавить подписку пользователя на уведомления для конкретного человека"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        try:
            cursor.execute(
                f"INSERT INTO subscriptions (user_id, person_id) VALUES ({placeholder}, {placeholder})",
                (user_id, person_id)
            )
            conn.commit()
            return True
        except Exception as e:
            # Подписка уже существует
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                return False
            raise
        finally:
            conn.close()
    
    def remove_subscription(self, user_id: int, person_id: int) -> bool:
        """Удалить подписку пользователя"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(
            f"DELETE FROM subscriptions WHERE user_id = {placeholder} AND person_id = {placeholder}",
            (user_id, person_id)
        )
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted
    
    def get_user_subscriptions(self, user_id: int) -> List[Person]:
        """Получить список людей, на которых подписан пользователь"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(f"""
            SELECT p.id, p.name, p.city_id, p.group_name
            FROM subscriptions s
            JOIN people p ON s.person_id = p.id
            WHERE s.user_id = {placeholder}
            ORDER BY p.name
        """, (user_id,))
        people = [Person(id=row[0], name=row[1], city_id=row[2], group=row[3]) for row in cursor.fetchall()]
        conn.close()
        return people
    
    def get_subscribers_for_person(self, person_id: int) -> List[int]:
        """Получить список user_id пользователей, подписанных на конкретного человека"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(f"SELECT DISTINCT user_id FROM subscriptions WHERE person_id = {placeholder}", (person_id,))
        user_ids = [row[0] for row in cursor.fetchall()]
        conn.close()
        return user_ids
    
    def is_subscribed(self, user_id: int, person_id: int) -> bool:
        """Проверить, подписан ли пользователь на человека"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(
            f"SELECT 1 FROM subscriptions WHERE user_id = {placeholder} AND person_id = {placeholder}",
            (user_id, person_id)
        )
        result = cursor.fetchone() is not None
        conn.close()
        return result
    
    # Работа с каналами
    def add_channel(self, city_id: int, channel_username: str) -> int:
        """Добавить канал для города, возвращает ID"""
        # Убираем @ если есть
        channel_username = channel_username.lstrip('@').strip()
        
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        try:
            if self.db_type == "postgresql":
                cursor.execute(
                    f"""INSERT INTO channels (city_id, channel_username) 
                       VALUES ({placeholder}, {placeholder}) 
                       ON CONFLICT (city_id) DO UPDATE SET channel_username = {placeholder}
                       RETURNING id""",
                    (city_id, channel_username, channel_username)
                )
                result = cursor.fetchone()
                channel_id = result[0] if result else 0
                if channel_id == 0:
                    cursor.execute(f"SELECT id FROM channels WHERE city_id = {placeholder}", (city_id,))
                    row = cursor.fetchone()
                    channel_id = row[0] if row else 0
            else:
                cursor.execute(
                    f"""INSERT INTO channels (city_id, channel_username) 
                       VALUES ({placeholder}, {placeholder}) 
                       ON CONFLICT(city_id) DO UPDATE SET channel_username = {placeholder}""",
                    (city_id, channel_username, channel_username)
                )
                channel_id = cursor.lastrowid
                # Если это UPDATE, получаем существующий ID
                if channel_id == 0:
                    cursor.execute(f"SELECT id FROM channels WHERE city_id = {placeholder}", (city_id,))
                    row = cursor.fetchone()
                    channel_id = row[0] if row else 0
            conn.commit()
            return channel_id
        except Exception as e:
            conn.rollback()
            raise ValueError(f"Ошибка при добавлении канала: {e}")
        finally:
            conn.close()
    
    def get_channel(self, city_id: int) -> Optional[Channel]:
        """Получить канал для города"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(f"SELECT id, city_id, channel_username, COALESCE(last_message_id, 0) FROM channels WHERE city_id = {placeholder}", (city_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return Channel(id=row[0], city_id=row[1], channel_username=row[2], last_message_id=row[3] if len(row) > 3 else 0)
        return None
    
    def get_all_channels(self) -> List[Channel]:
        """Получить все каналы"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, city_id, channel_username, COALESCE(last_message_id, 0) FROM channels ORDER BY city_id")
        channels = [Channel(id=row[0], city_id=row[1], channel_username=row[2], last_message_id=row[3] if len(row) > 3 else 0) for row in cursor.fetchall()]
        conn.close()
        return channels
    
    def update_channel(self, city_id: int, channel_username: str):
        """Обновить канал для города"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        try:
            # Проверяем, существует ли канал
            cursor.execute(f"SELECT id FROM channels WHERE city_id = {placeholder}", (city_id,))
            exists = cursor.fetchone()
            
            if exists:
                # Обновляем существующий канал
                cursor.execute(
                    f"UPDATE channels SET channel_username = {placeholder} WHERE city_id = {placeholder}",
                    (channel_username, city_id)
                )
            else:
                # Создаём новый канал
                cursor.execute(
                    f"INSERT INTO channels (city_id, channel_username) VALUES ({placeholder}, {placeholder})",
                    (city_id, channel_username)
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise ValueError(f"Ошибка при обновлении канала: {e}")
        finally:
            conn.close()
    
    def delete_channel(self, city_id: int):
        """Удалить канал для города"""
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        cursor.execute(f"DELETE FROM channels WHERE city_id = {placeholder}", (city_id,))
        conn.commit()
        conn.close()
    
    def get_channel_username(self, city_id: int) -> Optional[str]:
        """Получить username канала для города (удобный метод)"""
        channel = self.get_channel(city_id)
        return channel.channel_username if channel else None
    
    def save_last_message_id(self, channel_username: str, message_id: int):
        """Сохранить ID последнего обработанного сообщения для канала"""
        # Убираем @ если есть
        channel_username = channel_username.lstrip('@').strip()
        
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        try:
            cursor.execute(
                f"UPDATE channels SET last_message_id = {placeholder} WHERE channel_username = {placeholder}",
                (message_id, channel_username)
            )
            conn.commit()
            logger.debug(f"💾 Сохранён last_message_id={message_id} для канала @{channel_username}")
        except Exception as e:
            logger.error(f"❌ Ошибка при сохранении last_message_id: {e}")
            conn.rollback()
        finally:
            conn.close()
    
    def get_last_message_id(self, channel_username: str) -> int:
        """Получить ID последнего обработанного сообщения для канала"""
        # Убираем @ если есть
        channel_username = channel_username.lstrip('@').strip()
        
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        try:
            cursor.execute(
                f"SELECT COALESCE(last_message_id, 0) FROM channels WHERE channel_username = {placeholder}",
                (channel_username,)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        except Exception as e:
            logger.error(f"❌ Ошибка при получении last_message_id: {e}")
            return 0
        finally:
            conn.close()
    
    # Работа с группами людей
    def create_person_group(self, person_ids: List[int]) -> bool:
        """
        Создает группу людей. При создании новой группы старая автоматически удаляется.
        
        Args:
            person_ids: Список ID людей для добавления в группу
        
        Returns:
            True если успешно, False если ошибка
        """
        if not person_ids:
            logger.warning("⚠️ Попытка создать пустую группу")
            return False
        
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholder = self._get_placeholder()
        
        try:
            # Удаляем старую группу (если есть)
            cursor.execute("DELETE FROM person_groups")
            
            # Добавляем новых людей в группу
            for person_id in person_ids:
                # Проверяем, существует ли человек
                cursor.execute(f"SELECT id FROM people WHERE id = {placeholder}", (person_id,))
                if cursor.fetchone():
                    cursor.execute(
                        f"INSERT INTO person_groups (person_id) VALUES ({placeholder})",
                        (person_id,)
                    )
                else:
                    logger.warning(f"⚠️ Человек с ID {person_id} не найден, пропускаю")
            
            conn.commit()
            logger.info(f"✅ Создана группа из {len(person_ids)} человек")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка при создании группы: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()
    
    def get_person_group(self) -> List[Person]:
        """
        Получает всех людей из текущей группы.
        
        Returns:
            Список людей в группе
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT p.id, p.name, p.city_id, p.group_name
                FROM person_groups pg
                JOIN people p ON pg.person_id = p.id
                ORDER BY p.name
            """)
            people = [Person(id=row[0], name=row[1], city_id=row[2], group=row[3]) for row in cursor.fetchall()]
            return people
        except Exception as e:
            logger.error(f"❌ Ошибка при получении группы: {e}")
            return []
        finally:
            conn.close()
    
    def delete_person_group(self) -> bool:
        """
        Удаляет текущую группу людей (только связку, не самих людей).
        
        Returns:
            True если успешно, False если ошибка
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("DELETE FROM person_groups")
            conn.commit()
            logger.info("✅ Группа людей удалена")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка при удалении группы: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()
    
    def has_person_group(self) -> bool:
        """
        Проверяет, есть ли созданная группа людей.
        
        Returns:
            True если группа существует, False если нет
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT COUNT(*) FROM person_groups")
            count = cursor.fetchone()[0]
            return count > 0
        except Exception as e:
            logger.error(f"❌ Ошибка при проверке группы: {e}")
            return False
        finally:
            conn.close()

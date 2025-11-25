# Power Outage Schedule Bot

Telegram бот для мониторинга графиков отключений электричества в Украине.

## 🚀 Быстрый старт

### 1. Клонируйте репозиторий
```bash
git clone <repository-url>
cd power-outage-schedule-bot
```

### 2. Установите зависимости
```bash
pip install -r requirements.txt
```

### 3. Получите необходимые ключи

**Telegram Bot Token:**
- Зайдите к [@BotFather](https://t.me/BotFather) в Telegram
- Создайте нового бота командой `/newbot`
- Скопируйте полученный токен

**Gemini API Key:**
- Зайдите на [Google AI Studio](https://makersuite.google.com/app/apikey)
- Создайте новый API ключ
- Скопируйте ключ

**Telegram API ID и API Hash:**
- Зайдите на [my.telegram.org](https://my.telegram.org)
- Войдите с вашим номером телефона
- Перейдите в "API development tools"
- Создайте приложение и скопируйте `api_id` и `api_hash`

### 4. Создайте файл `.env`

Создайте файл `.env` в корне проекта со следующим содержимым:

```env
TELEGRAM_BOT_TOKEN=ваш_токен_от_BotFather
GEMINI_API_KEY=ваш_ключ_от_Google_AI_Studio
TELEGRAM_API_ID=ваш_api_id_от_my.telegram.org
TELEGRAM_API_HASH=ваш_api_hash_от_my.telegram.org
```

⚠️ **ВАЖНО:** Файл `.env` уже в `.gitignore` и не будет закоммичен в Git. НИКОГДА не публикуйте эти ключи!

### 5. Создайте Telegram сессию

Сессия нужна для мониторинга Telegram-каналов. Создайте её командой:

```bash
python create_session.py
```

**Что произойдёт:**
1. Скрипт попросит ввести номер телефона (например: +380123456789)
2. Вы получите код в Telegram или SMS
3. Введите код
4. Если включена двухфакторная аутентификация - введите пароль
5. Скрипт сгенерирует строку сессии

**После генерации:**
- Скопируйте полученную строку `TELEGRAM_SESSION_STRING`
- Добавьте её в файл `.env`:
  ```env
  TELEGRAM_SESSION_STRING=скопированная_строка_сессии
  ```

**Альтернатива:** Если хотите более подробные инструкции с предупреждениями:
```bash
python generate_session.py
```

### 6. Запустите бота

```bash
python bot.py
```

Бот запустится и начнёт работать! 🎉

## 📚 Документация

- 📖 [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) - полное описание всех файлов проекта
- 🚀 [DEPLOY.md](DEPLOY.md) - подробные инструкции по деплою на Render
- 🔒 [SECURITY.md](SECURITY.md) - инструкции по безопасности
- 💾 [BACKUP_RESTORE.md](BACKUP_RESTORE.md) - резервное копирование и восстановление

## ⚙️ Переменные окружения

| Переменная | Описание | Где получить | Обязательная |
|-----------|----------|--------------|--------------|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram бота | [@BotFather](https://t.me/BotFather) | ✅ Да |
| `GEMINI_API_KEY` | Ключ API Gemini | [Google AI Studio](https://makersuite.google.com/app/apikey) | ✅ Да |
| `TELEGRAM_API_ID` | ID Telegram API | [my.telegram.org](https://my.telegram.org) | ✅ Да |
| `TELEGRAM_API_HASH` | Hash Telegram API | [my.telegram.org](https://my.telegram.org) | ✅ Да |
| `TELEGRAM_SESSION_STRING` | Строка сессии Telethon | Генерируется через `create_session.py` | ❌ Нет* |
| `DATABASE_URL` | URL PostgreSQL | Автоматически на Render | ❌ Нет** |

\* **Сессия опциональна**, но **рекомендуется** для мониторинга каналов. Без неё мониторинг будет работать через файлы `.session`, которые не сохраняются между перезапусками на сервере.

\** **Для локальной разработки** используется SQLite (`bot.db`). **Для продакшена на Render** обязательно нужен PostgreSQL.

## 🔧 Полезные команды

**Работа с сессиями:**
- `python create_session.py` - создать Telegram сессию (простой вариант)
- `python generate_session.py` - создать сессию с подробными инструкциями

**Работа с базой данных:**
- `python init_database.py` - инициализировать базу данных с предустановленными городами
- `python migrate_channels.py` - мигрировать каналы из `.env` в базу данных

**В боте:**
- `/start` - главное меню
- `/start_mon` - запустить мониторинг каналов вручную (если остановился)

## ⚠️ Безопасность

**НИКОГДА не коммитьте секреты в Git!** См. [SECURITY.md](SECURITY.md) для подробностей.

## 📖 Подробная документация

Все детали установки, деплоя и использования смотрите в соответствующих файлах документации выше.

## Лицензия

MIT

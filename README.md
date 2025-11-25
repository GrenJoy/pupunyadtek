# Power Outage Schedule Bot

Telegram бот для мониторинга графиков отключений электричества в Украине.

## 🚀 Быстрый старт

```bash
# 1. Клонируйте репозиторий
git clone <repository-url>
cd power-outage-schedule-bot

# 2. Установите зависимости
pip install -r requirements.txt

# 3. Создайте .env файл со следующими переменными:
#    TELEGRAM_BOT_TOKEN=ваш_токен
#    GEMINI_API_KEY=ваш_ключ
#    TELEGRAM_API_ID=ваш_id
#    TELEGRAM_API_HASH=ваш_hash

# 4. Создайте сессию (если нужно)
python create_session.py

# 5. Запустите бота
python bot.py
```

## 📚 Документация

- 📖 [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) - полное описание всех файлов проекта
- 🚀 [DEPLOY.md](DEPLOY.md) - подробные инструкции по деплою на Render
- 🔒 [SECURITY.md](SECURITY.md) - инструкции по безопасности
- 💾 [BACKUP_RESTORE.md](BACKUP_RESTORE.md) - резервное копирование и восстановление

## ⚙️ Переменные окружения

| Переменная | Описание | Обязательная |
|-----------|----------|--------------|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram бота | ✅ Да |
| `GEMINI_API_KEY` | Ключ API Gemini | ✅ Да |
| `TELEGRAM_API_ID` | ID Telegram API | ✅ Да |
| `TELEGRAM_API_HASH` | Hash Telegram API | ✅ Да |
| `TELEGRAM_SESSION_STRING` | Строка сессии Telethon | ❌ Нет |
| `DATABASE_URL` | URL PostgreSQL (автоматически на Render) | ❌ Нет |

## 🔧 Полезные команды

- `python create_session.py` - создать Telegram сессию
- `python init_database.py` - инициализировать базу данных
- `python migrate_channels.py` - мигрировать каналы

## ⚠️ Безопасность

**НИКОГДА не коммитьте секреты в Git!** См. [SECURITY.md](SECURITY.md) для подробностей.

## 📖 Подробная документация

Все детали установки, деплоя и использования смотрите в соответствующих файлах документации выше.

## Лицензия

MIT

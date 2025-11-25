# Инструкция по деплою на Render

## Быстрый старт

### 1. Подготовка репозитория

Убедитесь, что все файлы закоммичены:
```bash
git add .
git commit -m "Prepare for Render deployment"
git push origin main
```

### 2. Создание сервиса на Render

#### Вариант A: Через Dashboard (ручной)

1. Зайдите на [render.com](https://render.com) и войдите в аккаунт
2. Нажмите **New** → **Web Service** (важно: Web Service, а не Background Worker!)
3. Подключите ваш Git репозиторий
4. Заполните настройки:
   - **Name**: `power-outage-schedule-bot`
   - **Environment**: `Python 3`
   - **Region**: Выберите ближайший регион
   - **Branch**: `main` (или ваша основная ветка)
   - **Root Directory**: (оставьте пустым)
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
   - **Health Check Path**: `/health` (для предотвращения засыпания)

5. В разделе **Environment Variables** добавьте:
   ```
   TELEGRAM_BOT_TOKEN=ваш_токен
   GEMINI_API_KEY=ваш_ключ
   TELEGRAM_API_ID=ваш_id
   TELEGRAM_API_HASH=ваш_hash
   TELEGRAM_SESSION_STRING=ваша_сессия (опционально)
   DATABASE_URL=postgres://... (автоматически добавляется при создании PostgreSQL)
   ```
   
   **Важно**: `DATABASE_URL` автоматически добавляется при создании PostgreSQL сервиса. Если вы создали PostgreSQL в том же проекте, переменная будет доступна автоматически.

6. Нажмите **Create Web Service**

**Важно**: Используйте **Web Service**, а не Background Worker, чтобы Render не засыпал сервис!

#### Вариант B: Через Blueprint (автоматический)

1. В Render Dashboard выберите **New** → **Blueprint**
2. Подключите ваш Git репозиторий
3. Render автоматически обнаружит `render.yaml` и создаст сервис
4. Добавьте переменные окружения в настройках сервиса

### 3. Генерация сессии Telethon

Если вы не указали `TELEGRAM_SESSION_STRING`:

**Вариант 1: Локально**
```bash
python generate_session.py
```
Скопируйте полученную строку и добавьте как `TELEGRAM_SESSION_STRING` в Render.

**Вариант 2: На Render (через Shell)**
1. В Render Dashboard откройте ваш сервис
2. Перейдите в **Shell**
3. Выполните:
```bash
python generate_session.py
```
4. Скопируйте строку сессии и добавьте в Environment Variables

### 4. Проверка работы

1. Откройте **Logs** в Render Dashboard
2. Убедитесь, что бот запустился без ошибок
3. Проверьте в Telegram, что бот отвечает на команды

## Важные замечания

### База данных

**⚠️ ВАЖНО: Используйте PostgreSQL для продакшена!**

SQLite теряет данные при перезапуске контейнера на Render. Для постоянного хранения данных необходимо использовать PostgreSQL.

#### Настройка PostgreSQL на Render

1. В Render Dashboard создайте **New** → **PostgreSQL**
2. Заполните настройки:
   - **Name**: `power-outage-bot-db`
   - **Database**: `power_outage_bot` (или любое имя)
   - **User**: `power_outage_user` (или любое имя)
   - **Region**: Выберите тот же регион, что и ваш Web Service
   - **PostgreSQL Version**: `16` (или последняя доступная)
   - **Plan**: `Free` (достаточно для начала)

3. После создания PostgreSQL сервиса:
   - Render автоматически создаст переменную окружения `DATABASE_URL`
   - Эта переменная будет доступна всем сервисам в том же проекте

4. В настройках вашего **Web Service**:
   - Убедитесь, что `DATABASE_URL` присутствует в Environment Variables
   - Если нет - добавьте её вручную из настроек PostgreSQL сервиса

5. При первом запуске бота:
   - Бот автоматически определит PostgreSQL по `DATABASE_URL`
   - Создаст все необходимые таблицы
   - Данные будут сохраняться между перезапусками

#### Локальная разработка

Для локальной разработки используйте SQLite (по умолчанию):
- Просто не устанавливайте `DATABASE_URL`
- Бот автоматически использует SQLite (`bot.db` файл)

### Файлы сессий

- Файлы `.session` не сохраняются между перезапусками
- Используйте `TELEGRAM_SESSION_STRING` для постоянной сессии
- Файлы `last_message_id*.txt` создаются автоматически

### Мониторинг

- Render автоматически перезапускает сервис при падении
- Логи доступны в реальном времени в Dashboard
- Настройте уведомления о падении сервиса

### Обновление

При каждом push в основную ветку Render автоматически:
1. Соберёт новый билд
2. Установит зависимости
3. Перезапустит сервис

## Переменные окружения

| Переменная | Где получить | Обязательная |
|-----------|-------------|--------------|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) | ✅ Да |
| `GEMINI_API_KEY` | [Google AI Studio](https://makersuite.google.com/app/apikey) | ✅ Да |
| `TELEGRAM_API_ID` | [my.telegram.org](https://my.telegram.org) | ✅ Да |
| `TELEGRAM_API_HASH` | [my.telegram.org](https://my.telegram.org) | ✅ Да |
| `TELEGRAM_SESSION_STRING` | Генерируется через `generate_session.py` | ❌ Нет |
| `DATABASE_URL` | Автоматически создаётся при создании PostgreSQL на Render | ✅ Да (для продакшена) |

## Устранение проблем

### Бот не запускается

1. Проверьте логи в Render Dashboard
2. Убедитесь, что все переменные окружения установлены
3. Проверьте, что `requirements.txt` содержит все зависимости

### Ошибки подключения к Telegram

1. Проверьте правильность `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`
2. Убедитесь, что сессия сгенерирована и добавлена в переменные окружения
3. Проверьте, что бот имеет доступ к каналам

### Ошибки Gemini API

1. Проверьте правильность `GEMINI_API_KEY`
2. Убедитесь, что у API ключа есть доступ к нужным моделям
3. Проверьте лимиты использования API

### База данных не создаётся

1. **Для PostgreSQL:**
   - Убедитесь, что PostgreSQL сервис создан и запущен
   - Проверьте, что `DATABASE_URL` установлена в Environment Variables
   - Проверьте логи на наличие ошибок подключения
   - Убедитесь, что бот может подключиться к PostgreSQL (проверьте firewall/network настройки)

### Ошибка подключения к PostgreSQL

Если возникают проблемы с подключением к PostgreSQL:

**Решение:**
Проект использует `psycopg[binary]` (psycopg3), который поддерживает все версии Python. Убедитесь, что:
1. PostgreSQL сервис создан и запущен
2. `DATABASE_URL` правильно установлена в Environment Variables
3. Все зависимости установлены (`psycopg[binary]` для PostgreSQL)

**Альтернативное решение (если проблема осталась):**
1. Убедитесь, что в корне проекта есть файл `runtime.txt` с содержимым:
   ```
   python-3.11.0
   ```
2. Или в настройках Web Service на Render:
   - Перейдите в **Settings** → **Environment**
   - Добавьте переменную: `PYTHON_VERSION` = `3.11.0`
3. После этого перезапустите сервис (Manual Deploy → Clear build cache & deploy)

2. **Для SQLite (локально):**
   - Проверьте права доступа (Render должен иметь права на запись)
   - Убедитесь, что `database.py` инициализирует БД при первом запуске
   - Проверьте логи на наличие ошибок инициализации

3. **Общие проблемы:**
   - Проверьте логи бота на наличие ошибок подключения к БД
   - Убедитесь, что все зависимости установлены (`psycopg[binary]` для PostgreSQL)

## Полезные команды

### Просмотр логов
```bash
# В Render Dashboard → Logs
```

### Просмотр метрик
```bash
# В Render Dashboard → Metrics
```

### Shell доступ
```bash
# В Render Dashboard → Shell
# Позволяет выполнять команды в контейнере
```

## Стоимость

- **Free Tier**: До 750 часов работы в месяц (достаточно для одного бота)
- **Starter Plan**: $7/месяц - неограниченное время работы
- Подробнее: [Render Pricing](https://render.com/pricing)

## Поддержка

При возникновении проблем:
1. Проверьте логи в Render Dashboard
2. Проверьте документацию Render: [Render Docs](https://render.com/docs)
3. Создайте issue в репозитории проекта


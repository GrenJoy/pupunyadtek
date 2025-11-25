"""
Скрипт для генерации Telegram сессии для мониторинга канала

⚠️ ВАЖНО: БЕЗОПАСНОСТЬ
- Этот скрипт требует TELEGRAM_API_ID и TELEGRAM_API_HASH из .env файла
- .env файл НЕ должен быть в Git репозитории (должен быть в .gitignore)
- Сгенерированная сессия даёт полный доступ к вашему Telegram аккаунту
- НИКОГДА не коммитьте .env файл или файлы .session в Git!
- НИКОГДА не публикуйте TELEGRAM_SESSION_STRING в открытом доступе!
"""
import os
import sys
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

# Предупреждение о безопасности
print("=" * 70)
print("⚠️  ВНИМАНИЕ: БЕЗОПАСНОСТЬ")
print("=" * 70)
print("Этот скрипт сгенерирует сессию для вашего Telegram аккаунта.")
print("Сгенерированная сессия даёт полный доступ к вашему аккаунту.")
print("\n✅ Убедитесь, что:")
print("   • .env файл НЕ закоммичен в Git (должен быть в .gitignore)")
print("   • Файлы .session НЕ закоммичены в Git")
print("   • TELEGRAM_SESSION_STRING хранится только в переменных окружения")
print("=" * 70)
print()

# Проверяем, что .env не в Git (базовая проверка)
if os.path.exists(".git"):
    try:
        import subprocess
        result = subprocess.run(
            ["git", "check-ignore", ".env"],
            capture_output=True,
            text=True,
            timeout=2
        )
        if result.returncode != 0:
            print("⚠️  ПРЕДУПРЕЖДЕНИЕ: .env файл может быть отслеживаем Git!")
            print("   Убедитесь, что .env добавлен в .gitignore")
            print()
    except:
        pass  # Игнорируем ошибки проверки Git

load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")

if not API_ID or not API_HASH:
    print("❌ Ошибка: TELEGRAM_API_ID и TELEGRAM_API_HASH должны быть в .env файле!")
    print("\n💡 Создайте файл .env в корне проекта со следующим содержимым:")
    print("   TELEGRAM_API_ID=your_api_id")
    print("   TELEGRAM_API_HASH=your_api_hash")
    print("\n⚠️  НЕ коммитьте .env файл в Git!")
    sys.exit(1)

async def generate():
    print("=" * 60)
    print("🔐 ГЕНЕРАЦИЯ TELEGRAM СЕССИИ")
    print("=" * 60)
    print("\n📱 Приготовьтесь ввести:")
    print("   • Номер телефона (например: +380123456789)")
    print("   • Код из SMS/Telegram")
    print("   • Пароль (если включена двухфакторная аутентификация)")
    print("\n" + "=" * 60 + "\n")
    
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        await client.start()
        
        session_string = client.session.save()
        
        print("\n" + "=" * 70)
        print("✅ АВТОРИЗАЦИЯ УСПЕШНА!")
        print("=" * 70)
        print("\n📋 Ваш SESSION_STRING:\n")
        print(session_string)
        print("\n" + "=" * 70)
        print("\n⚠️  ВАЖНО: БЕЗОПАСНОСТЬ")
        print("=" * 70)
        print("• НИКОГДА не публикуйте эту строку в открытом доступе!")
        print("• НИКОГДА не коммитьте её в Git!")
        print("• Храните только в переменных окружения (.env или на сервере)")
        print("=" * 70)
        print("\n💾 Добавьте в .env файл (локально) или в Environment Variables (на сервере):")
        print(f"TELEGRAM_SESSION_STRING={session_string}")
        print("=" * 70)
        print("\n✅ После добавления перезапустите бота!")
        print("\n⚠️  Помните: эта сессия даёт полный доступ к вашему Telegram аккаунту!")
        print("=" * 70)

if __name__ == "__main__":
    asyncio.run(generate())


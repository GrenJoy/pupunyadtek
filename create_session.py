"""
Простой скрипт для создания Telegram сессии
Использование: python create_session.py
"""
import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")

if not API_ID or not API_HASH:
    print("❌ Ошибка: TELEGRAM_API_ID и TELEGRAM_API_HASH должны быть в .env файле!")
    print("\nСоздайте .env файл с содержимым:")
    print("TELEGRAM_API_ID=ваш_id")
    print("TELEGRAM_API_HASH=ваш_hash")
    exit(1)

async def main():
    print("\n🔐 Создание Telegram сессии")
    print("=" * 50)
    print("Введите номер телефона, код из SMS и пароль (если есть 2FA)\n")
    
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        await client.start()
        session_string = client.session.save()
        
        print("\n" + "=" * 50)
        print("✅ Сессия создана!")
        print("=" * 50)
        print("\n📋 Скопируйте эту строку:\n")
        print(session_string)
        print("\n" + "=" * 50)
        print("\n💾 Добавьте в .env файл:")
        print(f"TELEGRAM_SESSION_STRING={session_string}")
        print("\n✅ Готово! Перезапустите бота.\n")

if __name__ == "__main__":
    asyncio.run(main())




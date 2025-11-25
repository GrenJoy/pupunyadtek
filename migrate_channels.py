"""
Скрипт миграции: добавляет каналы из .env в базу данных
Используйте этот скрипт один раз для переноса каналов из переменных окружения в БД
"""
import os
from dotenv import load_dotenv
from database import Database

load_dotenv()

def main():
    print("=" * 60)
    print("🔄 МИГРАЦИЯ КАНАЛОВ В БАЗУ ДАННЫХ")
    print("=" * 60)
    
    db = Database()
    
    # Список заблокированных каналов
    BLOCKED_CHANNELS = ["dtek_ua"]
    
    # Получаем канал из .env (если есть)
    monitor_channel = os.getenv("MONITOR_CHANNEL")
    
    if not monitor_channel:
        print("⚠️ MONITOR_CHANNEL не найден в .env файле")
        print("💡 Добавьте каналы через интерфейс бота или используйте этот скрипт")
        return
    
    # Убираем @ если есть
    monitor_channel = monitor_channel.replace("@", "").strip()
    
    # Проверяем, не заблокирован ли канал
    if monitor_channel.lower() in [ch.lower() for ch in BLOCKED_CHANNELS]:
        print(f"🚫 Канал @{monitor_channel} заблокирован и не может быть добавлен!")
        print("💡 Используйте другой канал или добавьте канал через интерфейс бота")
        return
    
    print(f"\n📱 Найден канал в .env: @{monitor_channel}")
    
    # Получаем все города
    cities = db.get_cities()
    
    if not cities:
        print("\n❌ В базе данных нет городов!")
        print("💡 Сначала добавьте города через бота")
        return
    
    print(f"\n📋 Найдено городов: {len(cities)}")
    for city in cities:
        print(f"   - {city.name} (ID: {city.id})")
    
    # Спрашиваем, для какого города добавить канал
    print("\n" + "=" * 60)
    print("💡 Для какого города добавить этот канал?")
    print("   (Если канал содержит графики для нескольких городов,")
    print("    выберите основной город, например 'Днепр')")
    print("=" * 60)
    
    city_name = input("\nВведите название города: ").strip()
    
    # Ищем город
    city = next((c for c in cities if c.name.lower() == city_name.lower()), None)
    
    if not city:
        print(f"❌ Город '{city_name}' не найден в базе данных")
        return
    
    # Проверяем, есть ли уже канал для этого города
    existing_channel = db.get_channel(city.id)
    if existing_channel:
        print(f"\n⚠️ Для города '{city.name}' уже есть канал: @{existing_channel.channel_username}")
        replace = input("Заменить? (y/n): ").strip().lower()
        if replace != 'y':
            print("❌ Отменено")
            return
    
    # Добавляем канал
    try:
        db.add_channel(city.id, monitor_channel)
        print(f"\n✅ Канал @{monitor_channel} успешно добавлен для города '{city.name}'")
        print("\n💡 Теперь можно удалить MONITOR_CHANNEL из .env файла")
        print("   Каналы теперь хранятся в базе данных")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")

if __name__ == "__main__":
    main()


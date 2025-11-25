"""
Скрипт для инициализации базы данных с предустановленными городами.
Можно запустить отдельно для предзаполнения базы.
"""
from database import Database

def main():
    print("=" * 50)
    print("🗄️ ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ")
    print("=" * 50)
    
    db = Database()
    
    print("\n📋 Добавляю предустановленные города...")
    db.init_default_cities()
    
    print("\n📊 Текущие города в базе:")
    cities = db.get_cities()
    if cities:
        for city in cities:
            print(f"  • {city.name} (ID: {city.id})")
    else:
        print("  Городов пока нет.")
    
    print("\n✅ Инициализация завершена!")

if __name__ == "__main__":
    main()


import os
import psycopg2
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Подключение к базе данных
def create_table():
    connection = None  # Инициализируем переменную
    try:
        # Получаем строку подключения из переменной окружения
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError("Переменная окружения DATABASE_URL не найдена.")

        # Подключаемся к базе данных
        connection = psycopg2.connect(database_url)
        cursor = connection.cursor()
        
        # SQL-запрос для создания таблицы
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS filters (
                id SERIAL PRIMARY KEY,
                filters JSONB NOT NULL
            );
        """)
        connection.commit()
        print("Таблица создана успешно! 🎉")
    
    except Exception as e:
        print(f"Ошибка: {e}")
    
    finally:
        if connection:
            cursor.close()
            connection.close()

if __name__ == "__main__":
    create_table()
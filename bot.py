from dotenv import load_dotenv
import os
import logging
import sqlite3
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    CallbackQueryHandler,
)

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

# Проверка обязательных переменных
TOKEN = os.getenv("TELEGRAM_TOKEN")
SECRET_TOKEN = os.getenv("SECRET_TOKEN") 
WEBHOOK_BASE = os.getenv("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8080))

if not all([TOKEN, SECRET_TOKEN, WEBHOOK_BASE]):
    raise EnvironmentError("Не заданы обязательные переменные окружения!")

WEBHOOK_URL = f"{WEBHOOK_BASE}/{TOKEN}"

# Конфигурация API с обработкой ошибок
API_URLS = {
    "meteora_pools": "https://api.meteora.ag/v2/pools",
    "dexscreener": "https://api.dexscreener.com/latest/dex/pairs/solana/{address}"
}

class Database:
    def __init__(self):
        self.conn = sqlite3.connect("bot_filters.db", check_same_thread=False)
        self._init_db()
        
    def _init_db(self):
        try:
            with self.conn:
                # Исправлено: заменен # на --
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_filters (
                        user_id INTEGER PRIMARY KEY,
                        min_tvl REAL DEFAULT 0,
                        max_bin_step INTEGER DEFAULT 100,
                        token_type TEXT DEFAULT 'ALL'  -- Корректный комментарий
                    )
                """)
                logger.info("Таблицы БД инициализированы")
        except sqlite3.Error as e:
            logger.error(f"Ошибка создания таблиц: {e}")
            raise

    def get_user_filters(self, user_id):
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM user_filters WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            return {
                "min_tvl": result[1] if result else 0,
                "max_bin_step": result[2] if result else 100,
                "token_type": result[3] if result else 'ALL',
            } if result else {}
        except sqlite3.Error as e:
            logger.error(f"Ошибка получения фильтров: {e}")
            return {}

    def update_user_filters(self, user_id, filters):
        try:
            with self.conn:
                self.conn.execute("""
                    INSERT OR REPLACE INTO user_filters 
                    (user_id, min_tvl, max_bin_step, token_type)
                    VALUES (?, ?, ?, ?)
                """, (
                    user_id, 
                    filters.get("min_tvl", 0),
                    filters.get("max_bin_step", 100),
                    filters.get("token_type", "ALL")
                ))
        except sqlite3.Error as e:
            logger.error(f"Ошибка обновления фильтров: {e}")
            raise

db = Database()

async def fetch_pools():
    """Получение пулов с улучшенной обработкой ошибок"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                API_URLS["meteora_pools"],
                headers={"User-Agent": "MeteoraBot/1.0"},
                follow_redirects=True
            )
            
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, list):
                logger.error("Некорректный формат данных от API")
                return []

            return [pool for pool in data if validate_pool(pool)]
            
    except httpx.HTTPError as e:
        logger.error(f"HTTP ошибка: {str(e)}")
    except Exception as e:
        logger.error(f"Общая ошибка: {str(e)}")
    
    return []

def validate_pool(pool: dict) -> bool:
    """Проверка обязательных полей"""
    required = {'address', 'tvl', 'bin_step', 'token_type'}
    return all(field in pool for field in required)

async def track_new_pools(context: CallbackContext):
    logger.info("Запуск проверки новых пулов")
    try:
        all_pools = await fetch_pools()
        
        if not all_pools:
            logger.warning("Нет данных для обработки")
            return
            
        with db.conn:  # Исправлено: использование контекста
            cursor = db.conn.cursor()
            cursor.execute("SELECT user_id FROM user_filters")
            users = [row[0] for row in cursor.fetchall()]
            
        for user_id in users:
            filters = db.get_user_filters(user_id)
            
            filtered = []
            for pool in all_pools:
                # Исправлена логика фильтрации для ALL
                if (
                    pool["tvl"] >= filters["min_tvl"] and
                    pool["bin_step"] <= filters["max_bin_step"] and
                    (filters["token_type"] == "ALL" or 
                     str(pool["token_type"]).upper() == filters["token_type"])
                ):
                    filtered.append(pool)

            for pool in filtered[:5]:
                message = await format_pool_message(pool)
                if message:
                    try:
                        await context.bot.send_message(
                            user_id,
                            message,
                            disable_web_page_preview=True
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки: {e}")

    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")

# Остальные функции остаются без изменений, но добавьте:
# 1. Проверку адреса в get_dexscreener_data()
# 2. Обработку KeyboardInterrupt в main()
# 3. Явное закрытие соединения с БД при завершении

def main():
    try:
        # ... существующий код ...
    except KeyboardInterrupt:
        logger.info("Приложение остановлено пользователем")
    finally:
        if hasattr(db, 'conn'):
            db.conn.close()
            logger.info("Соединение с БД закрыто")

if __name__ == "__main__":
    main()
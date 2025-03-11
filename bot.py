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

# Загрузка переменных окружения
load_dotenv()

# Проверка обязательных переменных
TOKEN = os.getenv("TELEGRAM_TOKEN")
SECRET_TOKEN = os.getenv("SECRET_TOKEN") 
WEBHOOK_BASE = os.getenv("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8080))

if not all([TOKEN, SECRET_TOKEN, WEBHOOK_BASE]):
    raise EnvironmentError("Не заданы обязательные переменные окружения!")

WEBHOOK_URL = f"{WEBHOOK_BASE}/{TOKEN}"

# Конфигурация API (обновленные эндпоинты)
API_URLS = {
    "meteora_pools": "https://api.meteora.ag/v2/pools",  # Актуальный URL
    "dexscreener": "https://api.dexscreener.com/latest/dex/pairs/solana/{address}"
}

class Database:
    def __init__(self):
        self.conn = sqlite3.connect("bot_filters.db", check_same_thread=False)
        self._init_db()
        
    def _init_db(self):
        try:
            with self.conn:
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_filters (
                        user_id INTEGER PRIMARY KEY,
                        min_tvl REAL DEFAULT 0,
                        max_bin_step INTEGER DEFAULT 100,
                        token_type TEXT DEFAULT 'ALL'  # Разрешаем значение ALL
                    )
                """)
                logger.info("Таблица user_filters создана или уже существует")
        except sqlite3.Error as e:
            logger.error(f"Ошибка создания таблицы: {e}")

    # ... остальные методы класса Database без изменений ...

async def fetch_pools():
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                API_URLS["meteora_pools"],
                headers={"User-Agent": "Mozilla/5.0"}
            )
            
            if response.status_code != 200:
                logger.error(f"API Error: {response.status_code} - {response.text[:200]}")
                return []

            data = response.json()
            logger.debug(f"Сырой ответ API: {data}")  # Логируем сырой ответ
            
            if not isinstance(data, list):
                logger.error("Некорректный формат данных от API")
                return []

            logger.info(f"Получено {len(data)} пулов")
            return [pool for pool in data if validate_pool(pool)]  # Валидация данных
            
    except Exception as e:
        logger.error(f"Ошибка получения пулов: {str(e)}")
        return []

def validate_pool(pool: dict) -> bool:
    """Проверяет обязательные поля в данных пула"""
    required_fields = {'address', 'tvl', 'bin_step', 'token_type'}
    return all(field in pool for field in required_fields)

async def format_pool_message(pool):
    try:
        logger.info(f"Форматирование пула {pool['address']}")
        address = pool.get("address", "")
        
        # Получаем дополнительные данные
        dex_data = await get_dexscreener_data(address)
        
        message = (
            f"🔹 {pool.get('base_token', {}).get('symbol', 'N/A')}\n"
            f"• TVL: {pool.get('tvl', 'N/A')}$\n"
            f"• Bin Step: {pool.get('bin_step', 'N/A')}\n"
            f"• Тип: {pool.get('token_type', 'N/A')}\n"
            f"• Адрес: {address[:15]}...\n"
            f"📊 DexScreener: {dex_data.get('url', '#')}"
        )
        return message
    except Exception as e:
        logger.error(f"Ошибка форматирования: {e}")
        return None

async def get_dexscreener_data(address: str) -> dict:
    """Получает данные от DexScreener с обработкой ошибок"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                API_URLS["dexscreener"].format(address=address)
            )
            return response.json().get('pair', {})
    except Exception as e:
        logger.error(f"Ошибка DexScreener: {e}")
        return {}

# ... остальные функции без изменений ...

def main():
    try:
        YOUR_USER_ID = 839443665
        db.update_user_filters(YOUR_USER_ID, {
            "min_tvl": 0,
            "max_bin_step": 100,
            "token_type": "ALL"  # Разрешаем все типы токенов
        })
        
        application = Application.builder().token(TOKEN).build()
        
        # Добавляем обработчик ошибок
        application.add_error_handler(error_handler)
        
        # ... остальная часть main() ...
    except Exception as e:
        logger.critical(f"Фатальная ошибка при запуске: {e}")

async def error_handler(update: object, context: CallbackContext) -> None:
    """Обработчик неотловленных исключений"""
    try:
        logger.error(msg="Исключение в обработчике", exc_info=context.error)
    except Exception as e:
        logger.error(f"Ошибка в обработчике ошибок: {e}")

if __name__ == "__main__":
    main()
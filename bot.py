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

# Конфигурация API
API_URLS = {
    "meteora_pools": "https://app.meteora.ag/api/pools/all",
    "dexscreener": "https://api.dexscreener.com/latest/dex/pairs/solana/{address}",
}

class Database:
    def __init__(self):
        self.conn = sqlite3.connect("bot_filters.db", check_same_thread=False)
        self._init_db()
        
    def _init_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS user_filters (
                    user_id INTEGER PRIMARY KEY,
                    min_tvl REAL DEFAULT 0,
                    max_bin_step INTEGER DEFAULT 100,
                    token_type TEXT DEFAULT 'SOL'
                )
            """)
    
    def get_user_filters(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM user_filters WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return {
            "min_tvl": result[1] if result else 0,
            "max_bin_step": result[2] if result else 100,
            "token_type": result[3] if result else 'SOL',
        } if result else {}
    
    def update_user_filters(self, user_id, filters):
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO user_filters 
                (user_id, min_tvl, max_bin_step, token_type)
                VALUES (?, ?, ?, ?)
            """, (
                user_id, 
                filters.get("min_tvl", 0),
                filters.get("max_bin_step", 100),
                filters.get("token_type", "SOL")
            ))

db = Database()

def get_filter_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Минимальный TVL", callback_data="set_min_tvl")],  # Исправлено: добавлена закрывающая скобка
        [InlineKeyboardButton("Максимальный Bin Step", callback_data="set_max_bin_step")],
        [InlineKeyboardButton("Тип токена", callback_data="set_token_type")],
        [InlineKeyboardButton("Сохранить и выйти", callback_data="save_filters")],
    ])

async def start(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    try:
        if not db.get_user_filters(user_id):
            db.update_user_filters(user_id, {})
            logger.info(f"Новый пользователь зарегистрирован: {user_id}")
        await update.message.reply_text(
            "Привет! Я бот для отслеживания новых пулов Meteora.\n"
            "Используйте /filters для настройки параметров поиска."
        )
    except Exception as e:
        logger.error(f"Ошибка в команде /start: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка при регистрации")

async def filters_command(update: Update, context: CallbackContext):
    try:
        user_id = update.message.from_user.id
        filters = db.get_user_filters(user_id)
        
        text = (
            f"Текущие фильтры:\n"
            f"• Минимальный TVL: {filters['min_tvl']}\n"
            f"• Максимальный Bin Step: {filters['max_bin_step']}\n"
            f"• Тип токена: {filters['token_type']}\n\n"
            "Используйте кнопки ниже для настройки:"
        )
        
        await update.message.reply_text(text, reply_markup=get_filter_keyboard())
    except Exception as e:
        logger.error(f"Ошибка в команде /filters: {e}")
        await update.message.reply_text("⚠️ Не удалось загрузить настройки")

async def track_new_pools(context: CallbackContext):
    try:
        logger.info("Запуск проверки новых пулов")
        
        # Получение пулов
        all_pools = await fetch_pools()
        logger.info(f"Получено {len(all_pools)} пулов от API")
        
        if not all_pools:
            logger.warning("Нет данных для обработки")
            return
            
        # Получение пользователей
        cursor = db.conn.cursor()
        cursor.execute("SELECT user_id FROM user_filters")
        users = [row[0] for row in cursor.fetchall()]
        logger.info(f"Найдено {len(users)} пользователей")
        
        # Отправка уведомлений
        for user_id in users:
            filters = db.get_user_filters(user_id)
            filtered = [
                pool for pool in all_pools
                if pool.get("tvl", 0) >= filters["min_tvl"]
                and pool.get("bin_step", 100) <= filters["max_bin_step"]
                and str(pool.get("token_type", "")).upper() == filters["token_type"]
            ]
            
            logger.info(f"Для {user_id} найдено {len(filtered)} подходящих пулов")
            
            for pool in filtered[:5]:
                message = await format_pool_message(pool)
                if message:
                    try:
                        await context.bot.send_message(
                            user_id,
                            message,
                            disable_web_page_preview=True
                        )
                        logger.info(f"Успешно отправлено сообщение пользователю {user_id}")
                    except Exception as e:
                        logger.error(f"Ошибка отправки для {user_id}: {e}")
                else:
                    logger.warning("Пустое сообщение для пула")
                    
    except Exception as e:
        logger.error(f"Критическая ошибка в track_new_pools: {e}")

def main():
    try:
        # Принудительная регистрация пользователя
        YOUR_USER_ID = 839443665
        db.update_user_filters(YOUR_USER_ID, {
            "min_tvl": 1000,
            "max_bin_step": 5,
            "token_type": "SOL"
        })
        logger.info(f"Основной пользователь {YOUR_USER_ID} зарегистрирован")

        # Инициализация бота
        application = Application.builder().token(TOKEN).build()
        
        # Регистрация обработчиков
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("filters", filters_command))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_filter_value))
        
        # Планировщик задач
        application.job_queue.run_repeating(
            track_new_pools,
            interval=300.0,
            first=10.0
        )
        
        # Запуск вебхука
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
            secret_token=SECRET_TOKEN,
            drop_pending_updates=True
        )
        
    except Exception as e:
        logger.critical(f"Фатальная ошибка при запуске: {e}")

if __name__ == "__main__":
    main()
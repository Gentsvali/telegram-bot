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
        try:
            with self.conn:
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_filters (
                        user_id INTEGER PRIMARY KEY,
                        min_tvl REAL DEFAULT 0,
                        max_bin_step INTEGER DEFAULT 100,
                        token_type TEXT DEFAULT 'SOL'
                    )
                """)
                logger.info("Таблица user_filters создана или уже существует")
        except sqlite3.Error as e:
            logger.error(f"Ошибка создания таблицы: {e}")

    def get_user_filters(self, user_id):
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM user_filters WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            return {
                "min_tvl": result[1] if result else 0,
                "max_bin_step": result[2] if result else 100,
                "token_type": result[3] if result else 'SOL',
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
                    filters.get("token_type", "SOL")
                ))
                logger.info(f"Обновлены фильтры для пользователя {user_id}")
        except sqlite3.Error as e:
            logger.error(f"Ошибка обновления фильтров: {e}")

db = Database()

def get_filter_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Минимальный TVL", callback_data="set_min_tvl")],
        [InlineKeyboardButton("Максимальный Bin Step", callback_data="set_max_bin_step")],
        [InlineKeyboardButton("Тип токена", callback_data="set_token_type")],
        [InlineKeyboardButton("Сохранить и выйти", callback_data="save_filters")],
    ])

async def start(update: Update, context: CallbackContext):
    try:
        user_id = update.message.from_user.id
        logger.info(f"Обработка /start для пользователя {user_id}")
        if not db.get_user_filters(user_id):
            db.update_user_filters(user_id, {})
            logger.info(f"Зарегистрирован новый пользователь: {user_id}")
        await update.message.reply_text(
            "Привет! Я бот для отслеживания новых пулов Meteora.\n"
            "Используйте /filters для настройки параметров поиска."
        )
    except Exception as e:
        logger.error(f"Ошибка в команде /start: {e}")

async def filters_command(update: Update, context: CallbackContext):
    try:
        user_id = update.message.from_user.id
        logger.info(f"Обработка /filters для пользователя {user_id}")
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

async def button_handler(update: Update, context: CallbackContext):
    try:
        query = update.callback_query
        await query.answer()
        logger.info(f"Обработка callback: {query.data}")
        
        if query.data == "save_filters":
            await query.edit_message_text("✅ Фильтры сохранены!")
            return
            
        context.user_data["awaiting_input"] = query.data
        await query.edit_message_text("✏️ Введите новое значение:")
    except Exception as e:
        logger.error(f"Ошибка обработки кнопки: {e}")

async def save_filter_value(update: Update, context: CallbackContext):
    try:
        user_id = update.message.from_user.id
        text = update.message.text.strip()
        logger.info(f"Сохранение значения для {user_id}: {text}")
        
        current_filters = db.get_user_filters(user_id)
        
        converters = {
            "set_min_tvl": ("min_tvl", float),
            "set_max_bin_step": ("max_bin_step", int),
            "set_token_type": ("token_type", str.upper)
        }
        
        key, converter = converters.get(context.user_data.get("awaiting_input"), (None, None))
        
        if key and converter:
            try:
                converted_value = converter(text)
                current_filters[key] = converted_value
                db.update_user_filters(user_id, current_filters)
                await update.message.reply_text("✅ Значение сохранено!", reply_markup=get_filter_keyboard())
                logger.info(f"Успешно сохранено {key} = {converted_value}")
            except ValueError:
                await update.message.reply_text("❌ Некорректный формат значения!")
            except Exception as e:
                logger.error(f"Ошибка сохранения: {e}")
        else:
            await update.message.reply_text("❌ Ошибка обработки запроса")
    except Exception as e:
        logger.error(f"Ошибка сохранения фильтра: {e}")

async def fetch_pools():
    try:
        logger.info("Запрос к API Meteora...")
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                API_URLS["meteora_pools"],
                headers={"User-Agent": "Mozilla/5.0"}
            )
            logger.info(f"Статус ответа API: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Получено {len(data)} пулов")
                logger.debug(f"Первые 3 пула: {data[:3]}")  # Для отладки
                return data
            logger.error(f"Ошибка API: {response.text[:200]}")
            return []
    except Exception as e:
        logger.error(f"Ошибка получения пулов: {str(e)}")
        return []

async def format_pool_message(pool):
    try:
        logger.info(f"Форматирование пула {pool.get('address')}")
        address = pool.get("address", "")
        async with httpx.AsyncClient(timeout=10) as client:
            dex_response = await client.get(
                API_URLS["dexscreener"].format(address=address)
            )
            dex_data = dex_response.json().get("pair", {})
        
        message = (
            f"🔹 {pool.get('base_token', {}).get('symbol', 'N/A')}\n"
            f"• TVL: {dex_data.get('liquidity', {}).get('usd', 'N/A')}$\n"
            f"• Bin Step: {pool.get('bin_step', 'N/A')}\n"
            f"• Тип: {pool.get('token_type', 'N/A')}\n"
            f"• Адрес: {address[:15]}...\n"
            f"📊 DexScreener: {pool.get('links', {}).get('dexscreener', '#')}"
        )
        logger.debug(f"Сформировано сообщение: {message}")
        return message
    except Exception as e:
        logger.error(f"Ошибка форматирования: {e}")
        return None

async def track_new_pools(context: CallbackContext):
    logger.info("Запуск проверки новых пулов")
    try:
        all_pools = await fetch_pools()
        logger.info(f"Получено {len(all_pools)} пулов от API")
        
        if not all_pools:
            logger.warning("Нет данных для обработки")
            return
            
        cursor = db.conn.cursor()
        cursor.execute("SELECT user_id FROM user_filters")
        users = [row[0] for row in cursor.fetchall()]
        logger.info(f"Найдено {len(users)} пользователей")
        
        for user_id in users:
            filters = db.get_user_filters(user_id)
            logger.info(f"Фильтры для {user_id}: {filters}")
            
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
                        logger.info(f"Сообщение отправлено {user_id}")
                    except Exception as e:
                        logger.error(f"Ошибка отправки: {e}")
                else:
                    logger.warning("Пустое сообщение для пула")
    except Exception as e:
        logger.error(f"Критическая ошибка в track_new_pools: {e}")

def main():
    try:
        # Принудительная регистрация и настройка фильтров
        YOUR_USER_ID = 839443665
        db.update_user_filters(YOUR_USER_ID, {
            "min_tvl": 0,        # Временные упрощенные фильтры
            "max_bin_step": 100,
            "token_type": "ALL"
        })
        logger.info(f"Основной пользователь {YOUR_USER_ID} зарегистрирован")

        # Инициализация бота
        application = Application.builder().token(TOKEN).build()
        
        # Регистрация обработчиков
        handlers = [
            CommandHandler("start", start),
            CommandHandler("filters", filters_command),
            CallbackQueryHandler(button_handler),
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_filter_value)
        ]
        
        for handler in handlers:
            application.add_handler(handler)

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
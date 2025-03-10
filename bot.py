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
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

# Загрузка переменных окружения
TOKEN = os.getenv("TELEGRAM_TOKEN")
SECRET_TOKEN = os.getenv("SECRET_TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_URL")

# Проверка наличия всех переменных
if not all([TOKEN, SECRET_TOKEN, WEBHOOK_BASE]):
    raise EnvironmentError("Не заданы обязательные переменные окружения!")

WEBHOOK_URL = f"{WEBHOOK_BASE}/{TOKEN}"

API_URLS = {
    "meteora_pools": "https://app.meteora.ag/api/pools/all",
    "dexscreener": "https://api.dexscreener.com/latest/dex/pairs/solana/{address}",
}

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect("bot_filters.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_filters (
            user_id INTEGER PRIMARY KEY,
            min_tvl REAL DEFAULT 0,
            max_bin_step INTEGER DEFAULT 100,
            token_type TEXT DEFAULT 'SOL'
        )
    """
    )
    conn.commit()
    return conn

# Получение фильтров пользователя из базы данных
def get_user_filters(user_id):
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_filters WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return {
        "min_tvl": result[1] if result else 0,
        "max_bin_step": result[2] if result else 100,
        "token_type": result[3] if result else 'SOL',
    } if result else {}

# Обновление фильтров пользователя в базе данных
def update_user_filters(user_id, filters):
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO user_filters 
        (user_id, min_tvl, max_bin_step, token_type)
        VALUES (?, ?, ?, ?)
    """,
        (user_id, filters.get("min_tvl"), filters.get("max_bin_step"), filters.get("token_type")),
    )
    conn.commit()

# Кнопки для настройки фильтров
def get_filter_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Минимальный TVL", callback_data="set_min_tvl")],
        [InlineKeyboardButton("Максимальный Bin Step", callback_data="set_max_bin_step")],
        [InlineKeyboardButton("Тип токена", callback_data="set_token_type")],
        [InlineKeyboardButton("Сохранить и выйти", callback_data="save_filters")],
    ])

# Команда /start
async def start(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if not get_user_filters(user_id):
        update_user_filters(user_id, {})
    await update.message.reply_text(
        "Привет! Я бот для отслеживания новых пулов Meteora.\n"
        "Используйте /filters для настройки параметров поиска."
    )

# Обработка нажатий на кнопки
async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    if query.data == "save_filters":
        await query.edit_message_text("Фильтры сохранены!")
        return

    context.user_data["awaiting_input"] = query.data
    await query.edit_message_text(
        "Введите новое значение:" if "set_" in query.data else "Выберите опцию:"
    )

# Сохранение введенных значений
async def save_filter_value(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    text = update.message.text
    current_filters = get_user_filters(user_id)
    
    key_map = {
        "set_min_tvl": ("min_tvl", float),
        "set_max_bin_step": ("max_bin_step", int),
        "set_token_type": ("token_type", str.upper)
    }
    
    key, converter = key_map.get(context.user_data.get("awaiting_input"), (None, None))
    
    if key and converter:
        try:
            current_filters[key] = converter(text)
            update_user_filters(user_id, current_filters)
            await update.message.reply_text("✅ Значение сохранено!", reply_markup=get_filter_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректный формат!")

# Асинхронное получение данных
async def fetch_pools(filters):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                API_URLS["meteora_pools"],
                params={
                    "min_tvl": filters.get("min_tvl", 0),
                    "max_bin_step": filters.get("max_bin_step", 100),
                    "token_type": filters.get("token_type", "SOL"),
                },
                headers={"User-Agent": "Mozilla/5.0"}
            )
            
            if response.status_code != 200:
                logger.error(f"Ошибка API Meteora: {response.status_code} - {response.text}")
                return []
                
            return response.json()
            
    except Exception as e:
        logger.error(f"Ошибка в fetch_pools: {e}")
        return []

# Форматирование сообщения о пуле
async def format_pool_message(pool):
    try:
        async with httpx.AsyncClient() as client:
            dex_response = await client.get(
                API_URLS["dexscreener"].format(address=pool["address"])
            )
            dex_data = dex_response.json().get("pair", {})
        
        return (
            f"⭐️ {pool['base_token']['symbol']} ({pool['links']['dexscreener']})-SOL\n"
            f"☄️ Meteora ({pool['links']['meteora']})\n"
            f"🟢 TVL: {dex_data.get('liquidity', {}).get('usd', 'N/A')}$\n"
            f"🟣 Bin Step: {pool['bin_step']}\n"
            f"🔸 24h Fee/TVL: {pool['metrics']['fee24h_tvl']}%\n"
            f"{pool['address']}"
        )
    except Exception as e:
        logger.error(f"Ошибка форматирования пула: {e}")
        return None

# Отслеживание новых пулов
async def track_new_pools(context: CallbackContext):
    logger.info("Запуск задачи track_new_pools")
    try:
        all_pools = await fetch_pools({})
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM user_filters")
        
        async with httpx.AsyncClient() as client:
            for user_id in [row[0] for row in cursor.fetchall()]:
                filters = get_user_filters(user_id)
                filtered_pools = [
                    pool for pool in all_pools
                    if pool.get("tvl", 0) >= filters["min_tvl"]
                    and pool.get("bin_step", 100) <= filters["max_bin_step"]
                    and pool.get("token_type", "").upper() == filters["token_type"]
                ]
                
                for pool in filtered_pools:
                    message = await format_pool_message(pool)
                    if message:
                        await context.bot.send_message(
                            user_id, 
                            message, 
                            disable_web_page_preview=True
                        )
                        
    except Exception as e:
        logger.error(f"Ошибка в track_new_pools: {e}")

def main():
    global conn
    conn = init_db()

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

    # Настройка периодических задач
    application.job_queue.run_repeating(
        track_new_pools, 
        interval=300.0, 
        first=10.0
    )

    # Настройка вебхука
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        secret_token=SECRET_TOKEN
    )

if __name__ == "__main__":
    main()
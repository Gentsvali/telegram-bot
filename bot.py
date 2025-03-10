from dotenv import load_dotenv
import os
import logging
import sqlite3
import requests
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

TOKEN = os.getenv("BOT_TOKEN")
SECRET_TOKEN = os.getenv("SECRET_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

API_URLS = {
    "meteora_pools": "https://app.meteora.ag/api/pools",
    "dexscreener": "https://api.dexscreener.com/latest/dex/pairs/solana/{address}",
}

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect("bot_filters.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_filters (
            user_id INTEGER PRIMARY KEY,
            min_tvl REAL,
            max_bin_step INTEGER,
            token_type TEXT
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
    if result:
        return {
            "min_tvl": result[1],
            "max_bin_step": result[2],
            "token_type": result[3],
        }
    return {}

# Обновление фильтров пользователя в базе данных
def update_user_filters(user_id, filters):
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO user_filters (user_id, min_tvl, max_bin_step, token_type)
        VALUES (?, ?, ?, ?)
    """,
        (user_id, filters.get("min_tvl"), filters.get("max_bin_step"), filters.get("token_type")),
    )
    conn.commit()

# Кнопки для настройки фильтров
def get_filter_keyboard():
    keyboard = [
        [InlineKeyboardButton("Минимальный TVL", callback_data="set_min_tvl")],
        [InlineKeyboardButton("Максимальный Bin Step", callback_data="set_max_bin_step")],
        [InlineKeyboardButton("Тип токена", callback_data="set_token_type")],
        [InlineKeyboardButton("Сохранить и выйти", callback_data="save_filters")],
    ]
    return InlineKeyboardMarkup(keyboard)

# Команда /start
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "Привет! Я бот для отслеживания новых пулов Meteora.\n"
        "Используйте /filters для настройки параметров поиска."
    )

# Команда /filters
async def filters_command(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    current_filters = get_user_filters(user_id)
    await update.message.reply_text(
        f"Текущие фильтры:\n"
        f"Минимальный TVL: {current_filters.get('min_tvl', 'не задан')}\n"
        f"Максимальный Bin Step: {current_filters.get('max_bin_step', 'не задан')}\n"
        f"Тип токена: {current_filters.get('token_type', 'не задан')}\n\n"
        f"Используйте кнопки ниже для настройки:",
        reply_markup=get_filter_keyboard(),
    )

# Обработка нажатий на кнопки
async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    if query.data == "set_min_tvl":
        await query.edit_message_text("Введите минимальный TVL (например, 1000):")
        context.user_data["awaiting_input"] = "min_tvl"
    elif query.data == "set_max_bin_step":
        await query.edit_message_text("Введите максимальный Bin Step (например, 10):")
        context.user_data["awaiting_input"] = "max_bin_step"
    elif query.data == "set_token_type":
        await query.edit_message_text("Введите тип токена (например, SOL):")
        context.user_data["awaiting_input"] = "token_type"
    elif query.data == "save_filters":
        await query.edit_message_text("Фильтры сохранены!")

# Сохранение введенных значений
async def save_filter_value(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    text = update.message.text
    current_filters = get_user_filters(user_id)

    if context.user_data.get("awaiting_input") == "min_tvl":
        current_filters["min_tvl"] = float(text)
    elif context.user_data.get("awaiting_input") == "max_bin_step":
        current_filters["max_bin_step"] = int(text)
    elif context.user_data.get("awaiting_input") == "token_type":
        current_filters["token_type"] = text.upper()

    update_user_filters(user_id, current_filters)
    await update.message.reply_text("Значение сохранено!", reply_markup=get_filter_keyboard())

# Отслеживание новых пулов
async def track_new_pools(context: CallbackContext):
    logger.info("Запуск задачи track_new_pools")
    try:
        all_pools = fetch_pools({})  # Получаем все пулы
        for user_id in get_all_users():
            filters = get_user_filters(user_id)
            filtered_pools = filter_pools(all_pools, filters)
            for pool in filtered_pools:
                message = format_pool_message(pool)
                if message:
                    await context.bot.send_message(user_id, message, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Ошибка в track_new_pools: {e}")

# Получение всех пользователей из базы данных
def get_all_users():
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM user_filters")
    return [row[0] for row in cursor.fetchall()]

# Фильтрация пулов
def filter_pools(pools, filters):
    return [
        pool
        for pool in pools
        if (not filters.get("min_tvl") or pool.get("tvl", 0) >= filters["min_tvl"])
        and (not filters.get("max_bin_step") or pool.get("bin_step", 100) <= filters["max_bin_step"])
        and (not filters.get("token_type") or pool.get("token_type", "").upper() == filters["token_type"])
    ]

# Основная функция
def main():
    global conn
    conn = init_db()

    application = Application.builder().token(TOKEN).build()

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("filters", filters_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_filter_value))

    # Запуск отслеживания новых пулов
    job_queue = application.job_queue
    job_queue.run_repeating(track_new_pools, interval=300.0, first=10.0)  # Проверка каждые 5 минут

    # Запуск бота с обработкой ошибок
    logger.info("Бот запущен и ожидает обновлений")
    try:
        application.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 8080)),
            webhook_url=WEBHOOK_URL,
            secret_token=SECRET_TOKEN,
        )
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")

if __name__ == "__main__":
    main()
import sqlite3

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect("user_settings.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            min_tvl REAL DEFAULT 0,
            max_tvl REAL DEFAULT 1000000,
            min_fees REAL DEFAULT 0,
            max_fees REAL DEFAULT 100
        )
    """)
    conn.commit()
    conn.close()

# Получение настроек пользователя
def get_user_settings(user_id):
    conn = sqlite3.connect("user_settings.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
    settings = cursor.fetchone()
    conn.close()
    if settings:
        return {
            "min_tvl": settings[1],
            "max_tvl": settings[2],
            "min_fees": settings[3],
            "max_fees": settings[4],
        }
    return None

# Обновление настроек пользователя
def update_user_settings(user_id, min_tvl=None, max_tvl=None, min_fees=None, max_fees=None):
    conn = sqlite3.connect("user_settings.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO user_settings (user_id, min_tvl, max_tvl, min_fees, max_fees)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, min_tvl, max_tvl, min_fees, max_fees))
    conn.commit()
    conn.close()

# Инициализация базы данных при запуске
init_db()

import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Конфигурация API
API_URLS = {
    "meteora_pools": "https://api.meteora.ag/v2/pools",
    "dexscreener": "https://api.dexscreener.com/latest/dex/pairs/solana/{address}",
}
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # Токен вашего Telegram-бота
CHAT_ID = os.getenv("CHAT_ID")  # ID вашего чата с ботом

# Проверка наличия обязательных переменных окружения
if not TELEGRAM_TOKEN or not CHAT_ID:
    logger.error("Не указаны обязательные переменные окружения: TELEGRAM_TOKEN или CHAT_ID")
    exit(1)

# Глобальная переменная для хранения последних пулов
last_pools = []

# Функция для получения данных о пулах от API Meteora
async def get_meteora_pools():
    try:
        response = requests.get(API_URLS["meteora_pools"])
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка получения пулов от Meteora: {e}")
        return None

# Функция для получения данных о пуле от DexScreener
async def get_dexscreener_pool(address: str):
    try:
        url = API_URLS["dexscreener"].format(address=address)
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка получения данных от DexScreener: {e}")
        return None

# Функция для проверки новых пулов
async def track_new_pools(user_id):
    global last_pools
    logger.info("Запуск проверки новых пулов")
    
    # Получаем настройки пользователя
    settings = get_user_settings(user_id)
    if not settings:
        settings = {
            "min_tvl": 0,
            "max_tvl": float("inf"),
            "min_fees": 0,
            "max_fees": float("inf"),
        }

    # Получаем текущие пулы
    current_pools = await get_meteora_pools()
    if not current_pools:
        logger.warning("Нет данных для обработки от Meteora")
        return

    # Фильтруем пулы по критериям
    filtered_pools = [
        pool for pool in current_pools
        if settings["min_tvl"] <= pool.get("tvl", 0) <= settings["max_tvl"]
        and settings["min_fees"] <= pool.get("fees", 0) <= settings["max_fees"]
    ]

    # Находим новые пулы
    new_pools = [pool for pool in filtered_pools if pool not in last_pools]

    if new_pools:
        logger.info(f"Найдено {len(new_pools)} новых пулов")
        # Отправляем уведомление в Telegram
        for pool in new_pools:
            message = (
                f"🔥 Обнаружены пулы с высокой доходностью 🔥\n\n"
                f"🔥 {pool.get('pair')} (https://t.me/meteora_pool_tracker_bot/?start=pool_info={pool.get('address')}) | создан ~{pool.get('age')} назад | RugCheck: 🟢1 (https://rugcheck.xyz/tokens/{pool.get('token_address')})\n"
                f"🔗 Meteora (https://app.meteora.ag/dlmm/{pool.get('address')}) | DexScreener (https://dexscreener.com/solana/{pool.get('address')}) | GMGN (https://gmgn.ai/sol/token/{pool.get('token_address')}) | TrenchRadar (https://trench.bot/bundles/{pool.get('token_address')}?all=true)\n"
                f"💎 Market Cap: ${pool.get('market_cap', 'N/A')} 🔹TVL: ${pool.get('tvl', 'N/A')}\n"
                f"📊 Объем: ${pool.get('volume', 'N/A')} 🔸 Bin Step: {pool.get('bin_step', 'N/A')} 💵 Fees: {pool.get('fees', 'N/A')} | {pool.get('dynamic_fee', 'N/A')}\n"
                f"🤑 Принт (5m dynamic fee/TVL): {pool.get('print_rate', 'N/A')}\n"
                f"🪙 Токен (https://t.me/meteora_pool_tracker_bot/?start=pools={pool.get('token_address')}): {pool.get('token_address')}\n"
                f"🤐 Mute 1h (https://t.me/meteora_pool_tracker_bot/?start=mute_token={pool.get('token_address')}_1h) | Mute 24h (https://t.me/meteora_pool_tracker_bot/?start=mute_token={pool.get('token_address')}_24h) | Mute forever (https://t.me/meteora_pool_tracker_bot/?start=mute_token={pool.get('token_address')}_forever)"
            )
            await send_telegram_message(user_id, message)
        
        # Обновляем список последних пулов
        last_pools = filtered_pools
    else:
        logger.info("Новых пулов не обнаружено")

# Функция для отправки сообщений в Telegram
async def send_telegram_message(user_id: int, message: str):
    try:
        application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        await application.bot.send_message(chat_id=user_id, text=message)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения в Telegram: {e}")

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен и отслеживает новые пулы на платформе Meteor!")

# Обработчик для кнопки "Проверить пулы"
async def check_pools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Проверяю новые пулы...")
    await track_new_pools(update.message.from_user.id)

# Обработчик для кнопки "Настройки"
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    keyboard = [
        [InlineKeyboardButton("Минимальный TVL", callback_data="set_min_tvl")],
        [InlineKeyboardButton("Максимальный TVL", callback_data="set_max_tvl")],
        [InlineKeyboardButton("Минимальные Fees", callback_data="set_min_fees")],
        [InlineKeyboardButton("Максимальные Fees", callback_data="set_max_fees")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚙️ Настройки фильтров:", reply_markup=reply_markup)

# Обработчик для callback-запросов
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data.startswith("set_"):
        await query.message.reply_text(f"Введите новое значение для {data[4:]}:")
        context.user_data["awaiting_input"] = data
    await query.answer()

# Обработчик для текстовых сообщений (ввод настроек)
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text

    if "awaiting_input" in context.user_data:
        setting = context.user_data["awaiting_input"]
        try:
            value = float(text)
            update_user_settings(user_id, **{setting: value})
            await update.message.reply_text(f"✅ Настройка {setting} обновлена: {value}")
            del context.user_data["awaiting_input"]
        except ValueError:
            await update.message.reply_text("❌ Ошибка: введите число.")

# Обработчик для команды "Помощь"
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ℹ️ Помощь:\n"
                                   "Используйте кнопки для взаимодействия с ботом.\n"
                                   "Если что-то не работает, напишите /start.")

# Основная функция
def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))

    # Обработчики кнопок
    application.add_handler(MessageHandler(filters.Text("Проверить пулы"), check_pools))
    application.add_handler(MessageHandler(filters.Text("Настройки"), settings))
    application.add_handler(MessageHandler(filters.Text("Помощь"), help_command))

    # Обработчики callback-запросов и текстовых сообщений
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Запуск бота
    application.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
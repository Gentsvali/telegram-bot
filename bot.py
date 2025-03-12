import json
import sqlite3
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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

# Загрузка фильтров
def load_filters():
    with open("filters.json", "r", encoding="utf-8") as file:
        return json.load(file)

# Применение фильтров
def apply_filters(pool, filters):
    for condition in filters["conditions"]:
        param = condition.get("param")
        param1 = condition.get("param1")
        param2 = condition.get("param2")
        operator = condition.get("operator")
        multiplier = condition.get("multiplier", 1.0)

        if condition["type"] == "range":
            value = pool.get(param, 0)
            if not (condition["min"] <= value <= condition["max"]):
                return False
        elif condition["type"] == "comparison":
            value1 = pool.get(param1, 0)
            value2 = pool.get(param2, 0)
            if operator == ">=" and not (value1 >= value2 * multiplier):
                return False
            elif operator == "<=" and not (value1 <= value2 * multiplier):
                return False
            elif operator == ">" and not (value1 > value2 * multiplier):
                return False
            elif operator == "<" and not (value1 < value2 * multiplier):
                return False
            elif operator == "==" and not (value1 == value2 * multiplier):
                return False
    return True

# Функция для получения данных о пулах от API Meteora
async def get_meteora_pools():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(API_URLS["meteora_pools"], timeout=10)
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"Ошибка получения пулов от Meteora: {e}")
        return None

# Функция для проверки новых пулов
async def track_new_pools(application, user_id):
    global last_pools
    logger.info("Запуск проверки новых пулов")
    
    # Загружаем фильтры
    filters = load_filters()

    # Получаем текущие пулы
    current_pools = await get_meteora_pools()
    if not current_pools:
        logger.warning("Нет данных для обработки от Meteora")
        return

    # Фильтруем пулы по условиям
    filtered_pools = [pool for pool in current_pools if apply_filters(pool, filters)]

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
            await send_telegram_message(application, user_id, message)
        
        # Обновляем список последних пулов
        last_pools = filtered_pools
    else:
        logger.info("Новых пулов не обнаружено")

# Функция для отправки сообщений в Telegram
async def send_telegram_message(application, user_id: int, message: str):
    try:
        await application.bot.send_message(chat_id=user_id, text=message)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения в Telegram: {e}")

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Создаем клавиатуру с кнопками
    keyboard = [
        ["Проверить пулы"],
        ["Настройки", "Помощь"],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    # Отправляем сообщение с клавиатурой
    await update.message.reply_text(
        "Бот запущен и отслеживает новые пулы на платформе Meteor!",
        reply_markup=reply_markup
    )

# Обработчик для кнопки "Проверить пулы"
async def check_pools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Проверяю новые пулы...")
    await track_new_pools(context.application, update.message.from_user.id)

# Обработчик для кнопки "Настройки"
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Создаем клавиатуру с кнопками для настроек
    keyboard = [
        ["TVL", "Fees"],
        ["Назад"],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    # Отправляем сообщение с клавиатурой
    await update.message.reply_text(
        "⚙️ Выберите параметр для настройки:",
        reply_markup=reply_markup
    )

# Обработчик для кнопки "TVL"
async def set_tvl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите минимальное значение TVL:")
    context.user_data["awaiting_input"] = "min_tvl"

# Обработчик для кнопки "Fees"
async def set_fees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите минимальное значение Fees:")
    context.user_data["awaiting_input"] = "min_fees"

# Обработчик для текстовых сообщений (ввод настроек)
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text

    if "awaiting_input" in context.user_data:
        setting = context.user_data["awaiting_input"]
        try:
            value = float(text)
            if setting == "min_tvl":
                context.user_data["min_tvl"] = value
                await update.message.reply_text("Теперь введите максимальное значение TVL:")
                context.user_data["awaiting_input"] = "max_tvl"
            elif setting == "max_tvl":
                context.user_data["max_tvl"] = value
                # Сохраняем только TVL
                update_user_settings(
                    user_id, 
                    min_tvl=context.user_data["min_tvl"], 
                    max_tvl=context.user_data["max_tvl"]
                )
                await update.message.reply_text("✅ Настройки TVL успешно сохранены.")
                # Очищаем контекст
                del context.user_data["awaiting_input"]
                del context.user_data["min_tvl"]
                del context.user_data["max_tvl"]
                await show_main_menu(update, context)
            elif setting == "min_fees":
                context.user_data["min_fees"] = value
                await update.message.reply_text("Теперь введите максимальное значение Fees:")
                context.user_data["awaiting_input"] = "max_fees"
            elif setting == "max_fees":
                context.user_data["max_fees"] = value
                # Сохраняем только Fees
                update_user_settings(
                    user_id, 
                    min_fees=context.user_data["min_fees"], 
                    max_fees=context.user_data["max_fees"]
                )
                await update.message.reply_text("✅ Настройки Fees успешно сохранены.")
                # Очищаем контекст
                del context.user_data["awaiting_input"]
                del context.user_data["min_fees"]
                del context.user_data["max_fees"]
                await show_main_menu(update, context)
        except ValueError:
            await update.message.reply_text("❌ Ошибка: введите число.")

# Показ главного меню
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Проверить пулы"],
        ["Настройки", "Помощь"],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Главное меню:",
        reply_markup=reply_markup
    )

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
    application.add_handler(MessageHandler(filters.Text("TVL"), set_tvl))
    application.add_handler(MessageHandler(filters.Text("Fees"), set_fees))
    application.add_handler(MessageHandler(filters.Text("Назад"), show_main_menu))
    application.add_handler(MessageHandler(filters.Text("Помощь"), help_command))

    # Обработчики текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Запуск бота
    application.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
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
async def track_new_pools():
    global last_pools
    logger.info("Запуск проверки новых пулов")
    
    # Получаем текущие пулы от Meteora
    current_pools = await get_meteora_pools()
    if not current_pools:
        logger.warning("Нет данных для обработки от Meteora")
        return

    # Фильтруем пулы, связанные с Solana
    solana_pools = [pool for pool in current_pools if pool.get("chain") == "Solana"]

    # Находим новые пулы
    new_pools = [pool for pool in solana_pools if pool not in last_pools]

    if new_pools:
        logger.info(f"Найдено {len(new_pools)} новых пулов")
        # Отправляем уведомление в Telegram
        for pool in new_pools:
            # Получаем дополнительную информацию о пуле от DexScreener
            dexscreener_data = await get_dexscreener_pool(pool.get("address"))
            if dexscreener_data:
                message = (
                    f"🎉 Новый пул на платформе Meteor!\n"
                    f"🔗 Пара: {pool.get('pair')}\n"
                    f"📊 Объем: {dexscreener_data.get('volume', 'N/A')}\n"
                    f"⏳ Время добавления: {pool.get('timestamp')}"
                )
            else:
                message = (
                    f"🎉 Новый пул на платформе Meteor!\n"
                    f"🔗 Пара: {pool.get('pair')}\n"
                    f"⏳ Время добавления: {pool.get('timestamp')}"
                )
            await send_telegram_message(message)
        
        # Обновляем список последних пулов
        last_pools = solana_pools
    else:
        logger.info("Новых пулов не обнаружено")

# Функция для отправки сообщений в Telegram
async def send_telegram_message(message: str):
    try:
        application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        await application.bot.send_message(chat_id=CHAT_ID, text=message)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения в Telegram: {e}")

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен и отслеживает новые пулы на платформе Meteor!")

# Основная функция
def main():
    # Инициализация бота
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Регистрация команд
    application.add_handler(CommandHandler("start", start))

    # Настройка планировщика
    scheduler = AsyncIOScheduler()
    scheduler.add_job(track_new_pools, "interval", minutes=5)
    scheduler.start()

    # Запуск бота
    application.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
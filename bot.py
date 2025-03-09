from dotenv import load_dotenv
import os
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from bs4 import BeautifulSoup

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

# Получаем токен и секретный токен из переменных окружения
TOKEN = os.environ.get("BOT_TOKEN", "7919326998:AAEStNAdjyL3U6KIg3_P9QefPx3_iUe60jI")  # Ваш токен
SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "my_super_secret_token_mara5555")  # Секретный токен для вебхука
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://telegram-bot-6gec.onrender.com/webhook")  # Ваш URL вебхука

# Фильтры для пулов (настройки по умолчанию)
USER_FILTERS = {}  # Словарь для хранения фильтров каждого пользователя

# Функция для получения пулов
def get_pools():
    url = "https://app.meteora.ag/pools"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            pools = []
            for pool in soup.find_all('div', class_='pool-item'):
                pool_name = pool.find('p', class_='pool-name').text.strip() if pool.find('p', class_='pool-name') else "Название неизвестно"
                pool_volume = pool.find('span', class_='pool-volume').text.strip() if pool.find('span', class_='pool-volume') else "Объем неизвестен"
                pools.append({
                    "name": pool_name,
                    "volume": float(pool_volume.replace('$', '').replace(',', ''))  # Преобразуем объем в число
                })
            return pools
        else:
            logger.error(f"Failed to fetch pools. Status code: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Failed to fetch pools: {e}")
        return None

# Фильтрация пулов по настройкам пользователя
def filter_pools(pools, user_id):
    filters = USER_FILTERS.get(user_id, {})
    filtered_pools = []
    for pool in pools:
        # Фильтр по SOL
        if filters.get("token_type", "SOL") == "SOL" and not pool["name"].endswith("-SOL"):
            continue
        # Фильтр по минимальному объему
        if "min_volume" in filters and pool["volume"] < filters["min_volume"]:
            continue
        # Фильтр по длительности (если нужен)
        if "duration" in filters:
            # Добавьте логику для фильтрации по длительности
            pass
        filtered_pools.append(pool)
    return filtered_pools

# Команда /start
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text("Привет! Я бот для отслеживания пулов. Используй команду /pools для получения списка пулов.")

# Команда /pools
async def pools(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    pools = get_pools()
    if pools:
        filtered_pools = filter_pools(pools, user_id)
        if filtered_pools:
            response = "Свежие пулы:\n"
            for pool in filtered_pools:
                pool_info = f"{pool['name']} - ${pool['volume']}\n"
                # Если добавление нового пула превышает лимит, отправляем текущее сообщение и начинаем новое
                if len(response) + len(pool_info) > 4096:
                    await update.message.reply_text(response)
                    response = "Свежие пулы (продолжение):\n"
                response += pool_info
            # Отправляем оставшуюся часть сообщения
            await update.message.reply_text(response)
        else:
            await update.message.reply_text("Нет пулов, соответствующих вашим фильтрам.")
    else:
        await update.message.reply_text("Не удалось получить данные о пулах.")

# Обработка текстовых сообщений
async def handle_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    text = update.message.text

    # Инициализация фильтров для пользователя, если их еще нет
    if user_id not in USER_FILTERS:
        USER_FILTERS[user_id] = {}

    # Сохранение фильтров
    if text.isdigit():
        USER_FILTERS[user_id]["min_volume"] = float(text)  # Добавляем минимальный объем
        await update.message.reply_text(f"Минимальный объем установлен: {text}")
    elif text.upper() in ["SOL", "USDC", "BTC"]:
        USER_FILTERS[user_id]["token_type"] = text.upper()  # Добавляем тип токена
        await update.message.reply_text(f"Тип токена установлен: {text.upper()}")
    elif text.endswith("h"):
        USER_FILTERS[user_id]["duration"] = text  # Добавляем длительность
        await update.message.reply_text(f"Длительность установлена: {text}")
    else:
        await update.message.reply_text("Неизвестная команда. Используйте команду /pools для получения списка пулов.")

# Запуск бота
def main():
    application = Application.builder().token(TOKEN).build()

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pools", pools))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Установка вебхука
    application.run_webhook(
        listen="0.0.0.0",
        port=10000,  # Порт, который слушает Render
        url_path=TOKEN,
        webhook_url=WEBHOOK_URL,
    )

if __name__ == '__main__':
    main()
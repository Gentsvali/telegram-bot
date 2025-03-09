from flask import Flask, request, jsonify
import os
import logging
import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

# Получаем токен и секретный токен из переменных окружения
TOKEN = os.environ.get("BOT_TOKEN", "7919326998:AAEStNAdjyL3U6KIg3_P9QefPx3_iUe60jI")  # Ваш токен
SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "my_super_secret_token_mara5555")  # Секретный токен для вебхука

# Фильтры для пулов (настройки по умолчанию)
USER_FILTERS = {}  # Словарь для хранения фильтров каждого пользователя

# Клавиатура для фильтров
FILTER_KEYBOARD = ReplyKeyboardMarkup(
    [["/set_min_volume", "/set_token_type"], ["/set_duration", "/show_filters"]],
    resize_keyboard=True,
)

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
    await update.message.reply_text(
        "Привет! Я бот для отслеживания пулов. Используй кнопки ниже для настройки фильтров.",
        reply_markup=FILTER_KEYBOARD,
    )

# Команда /set_min_volume
async def set_min_volume(update: Update, context: CallbackContext):
    await update.message.reply_text("Введите минимальный объем пула (например, 1000):")

# Команда /set_token_type
async def set_token_type(update: Update, context: CallbackContext):
    await update.message.reply_text("Введите тип токена (например, SOL):")

# Команда /set_duration
async def set_duration(update: Update, context: CallbackContext):
    await update.message.reply_text("Введите длительность пула (например, 1h):")

# Команда /show_filters
async def show_filters(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    filters = USER_FILTERS.get(user_id, {})
    await update.message.reply_text(f"Текущие фильтры:\n{filters}")

# Команда /pools
async def pools(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    pools = get_pools()
    if pools:
        filtered_pools = filter_pools(pools, user_id)
        if filtered_pools:
            response = "Свежие пулы:\n"
            for pool in filtered_pools:
                response += f"{pool['name']} - ${pool['volume']}\n"
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
        await update.message.reply_text("Неизвестная команда. Используйте кнопки для настройки фильтров.")

# Запуск бота
def main():
    application = Application.builder().token(TOKEN).build()

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_min_volume", set_min_volume))
    application.add_handler(CommandHandler("set_token_type", set_token_type))
    application.add_handler(CommandHandler("set_duration", set_duration))
    application.add_handler(CommandHandler("show_filters", show_filters))
    application.add_handler(CommandHandler("pools", pools))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Запуск бота
    application.run_polling()

if __name__ == '__main__':
    main()
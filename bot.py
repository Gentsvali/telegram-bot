from flask import Flask, request, jsonify
import os
import logging
import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

app = Flask(__name__)

# Получаем токен и секретный токен из переменных окружения
TOKEN = os.environ.get("BOT_TOKEN", "7919326998:AAEStNAdjyL3U6KIg3_P9QefPx3_iUe60jI")  # Ваш токен
SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "my_super_secret_token_mara5555")  # Секретный токен для вебхука
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://telegram-bot-6gec.onrender.com/webhook")  # Ваш URL вебхука

logger.info(f"TOKEN: {TOKEN}, SECRET_TOKEN: {SECRET_TOKEN}, WEBHOOK_URL: {WEBHOOK_URL}")

# Фильтры для пулов (настройте под свои нужды)
FILTERS = {
    "min_volume": 1000,  # Минимальный объем пула
    "token_type": "SOL",  # Тип токена
    "duration": "1h"      # Срок действия пула
}

def get_pools():
    """
    Получает список пулов с сайта app.meteora.ag.
    """
    url = "https://app.meteora.ag/pools"  # Замените на реальный URL
    try:
        response = requests.get(url)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            pools = []
            # Пример парсинга (замените на реальные селекторы)
            for pool in soup.find_all('div', class_='pool'):  # Замените на реальный селектор
                pool_name = pool.find('h3').text.strip() if pool.find('h3') else "Название неизвестно"
                pool_volume = pool.find('span', class_='volume').text.strip() if pool.find('span', class_='volume') else "Объем неизвестен"
                pools.append({
                    "name": pool_name,
                    "volume": pool_volume
                })
            return pools
        else:
            logger.error(f"Failed to fetch pools. Status code: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Failed to fetch pools: {e}")
        return None

def filter_pools(pools, filters):
    """
    Фильтрует пулы по заданным параметрам.
    """
    filtered_pools = []
    for pool in pools:
        # Пример фильтрации (замените на реальные поля)
        if (float(pool['volume'].replace(',', '')) >= filters['min_volume']:  # Пример фильтра по объему
            filtered_pools.append(pool)
    return filtered_pools

@app.route('/webhook', methods=['POST'])
def webhook():
    logger.info("Webhook called!")  # Лог: вебхук вызван
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != SECRET_TOKEN:
        logger.warning("Unauthorized request")  # Лог: неавторизованный запрос
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        data = request.json
        logger.info(f"Received data: {data}")  # Лог: полученные данные

        message = data.get('message', {})
        text = message.get('text')
        chat_id = message.get('chat', {}).get('id')

        if not text or not chat_id:
            logger.warning("Invalid message format")  # Лог: неверный формат сообщения
            return jsonify({"status": "error", "message": "Invalid message format"}), 400

        if text == "/start":
            logger.info("Handling /start command")  # Лог: обработка команды /start
            send_message(chat_id, "Привет! Я твой бот. Напиши что-нибудь, и я отвечу.")
        elif text == "/help":
            logger.info("Handling /help command")  # Лог: обработка команды /help
            send_message(chat_id, "Список команд:\n/start - Начать диалог\n/help - Показать список команд\n/pools - Показать свежие пулы")
        elif text == "/pools":
            logger.info("Handling /pools command")
            pools = get_pools()
            if pools:
                filtered_pools = filter_pools(pools, FILTERS)
                if filtered_pools:
                    response = "Свежие пулы:\n"
                    for pool in filtered_pools:
                        response += f"{pool['name']} - {pool['volume']}\n"
                    send_message(chat_id, response)
                else:
                    send_message(chat_id, "Нет пулов, соответствующих вашим фильтрам.")
            else:
                send_message(chat_id, "Не удалось получить данные о пулах.")
        else:
            logger.info(f"Handling message: {text}")  # Лог: обработка обычного сообщения
            send_message(chat_id, f"Ты написал: {text}")

        return jsonify({"status": "ok"})

    except Exception as e:
        logger.error(f"Error in webhook: {e}")  # Лог: ошибка в вебхуке
        return jsonify({"status": "error", "message": str(e)}), 500

def send_message(chat_id, text):
    logger.info(f"Sending message: {text} to chat_id: {chat_id}")  # Лог: отправка сообщения
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            logger.error(f"Failed to send message. Response: {response.json()}")  # Лог: ошибка отправки
        else:
            logger.info(f"Message sent successfully! Response: {response.status_code}")  # Лог: успешная отправка
    except Exception as e:
        logger.error(f"Failed to send message: {e}")  # Лог: ошибка при отправке

def set_webhook():
    url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
    payload = {
        "url": WEBHOOK_URL,
        "secret_token": SECRET_TOKEN
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            logger.info(f"Webhook set successfully: {response.json()}")  # Лог: вебхук установлен
        else:
            logger.error(f"Failed to set webhook: {response.json()}")  # Лог: ошибка установки вебхука
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")  # Лог: ошибка при установке вебхука

if __name__ == '__main__':
    set_webhook()  # Устанавливаем вебхук при запуске приложения
    port = int(os.environ.get("PORT", 5000))  # Используем порт из окружения или 5000 по умолчанию
    app.run(host='0.0.0.0', port=port)
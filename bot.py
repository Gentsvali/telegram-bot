from flask import Flask, request, jsonify
import os
import logging
import requests

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Получаем токен из переменных окружения
TOKEN = os.environ.get("BOT_TOKEN", "7919326998:AAEStNAdjyL3U6KIg3_P9QefPx3_iUe60jI")  # Ваш токен
WEBHOOK_URL = "https://telegram-bot-rb7l.onrender.com/webhook"  # Ваш URL вебхука

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json  # Данные от Telegram
        logger.info(f"Received data: {data}")  # Логируем входящие данные

        message = data.get('message', {})
        text = message.get('text')  # Текст сообщения
        chat_id = message.get('chat', {}).get('id')  # ID чата

        if not text or not chat_id:
            logger.warning("Invalid message format")
            return jsonify({"status": "error", "message": "Invalid message format"}), 400

        if text == "/start":
            send_message(chat_id, "Привет! Я твой бот. Напиши что-нибудь, и я отвечу.")
        elif text:
            send_message(chat_id, f"Ты написал: {text}")

        return jsonify({"status": "ok"})

    except Exception as e:
        logger.error(f"Error in webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    response = requests.post(url, json=payload)
    logger.info(f"Sent message to chat_id {chat_id}. Response: {response.status_code}")

def set_webhook():
    url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
    payload = {
        "url": WEBHOOK_URL
    }
    response = requests.post(url, json=payload)
    logger.info(f"Set webhook. Response: {response.json()}")

if __name__ == '__main__':
    set_webhook()  # Устанавливаем вебхук при запуске приложения
    port = int(os.environ.get("PORT", 8080))  # Используем порт из окружения или 8080 по умолчанию
    app.run(host='0.0.0.0', port=port)
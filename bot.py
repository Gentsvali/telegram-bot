from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json  # Данные от Telegram
    message = data.get('message', {})
    text = message.get('text')  # Текст сообщения
    chat_id = message.get('chat', {}).get('id')  # ID чата

    if text:
        print(f"Получено сообщение: {text}")
        # Отправь ответ
        send_message(chat_id, f"Ты написал: {text}")

    return jsonify({"status": "ok"})

def send_message(chat_id, text):
    import requests
    token = "7919326998:AAEStNAdjyL3U6KIg3_P9QefPx3_iUe60jI"  # Замени на токен бота
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    requests.post(url, json=payload)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
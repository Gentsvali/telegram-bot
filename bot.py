from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json  # Данные от Telegram
    print(data)  # Выведем данные в консоль для отладки
    return jsonify({"status": "ok"})  # Ответ для Telegram

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)  # Запуск сервера
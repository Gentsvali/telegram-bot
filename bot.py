import os
import logging
import asyncio
import aiohttp
from quart import Quart, request, jsonify
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)
from solders.pubkey import Pubkey
from hypercorn.config import Config
from hypercorn.asyncio import serve

# --- Конфигурация ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
USER_ID = int(os.getenv("USER_ID")) if os.getenv("USER_ID") else None
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))

# --- Инициализация приложения ---
app = Quart(__name__)
app.bot_app = None
known_pools = set()

# --- Настройка логов ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Основные функции ---
async def init_bot():
    """Инициализация бота Telegram"""
    if not all([TELEGRAM_TOKEN, HELIUS_API_KEY, USER_ID, WEBHOOK_BASE_URL]):
        raise ValueError("Не все обязательные переменные окружения установлены!")

    app.bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    await app.bot_app.initialize()
    
    # Регистрация обработчиков команд
    app.bot_app.add_handler(CommandHandler("start", start))
    app.bot_app.add_handler(CommandHandler("status", status))
    
    # Установка вебхука
    webhook_url = f"{WEBHOOK_BASE_URL}/webhook"
    await app.bot_app.bot.set_webhook(webhook_url)
    logger.info(f"Вебхук установлен: {webhook_url}")

async def fetch_pools():
    """Запрос пулов с Helius API"""
    try:
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        payload = {
            "jsonrpc": "2.0",
            "id": "dlmm-fetcher",
            "method": "getProgramAccounts",
            "params": [
                str(Pubkey.from_string("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")),
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "dataSlice": {"offset": 0, "length": 100},
                    "withContext": True,
                    "limit": 5
                }
            ]
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result", {}).get("value", [])
                logger.error(f"Ошибка Helius API: {resp.status}")
                return []
    except Exception as e:
        logger.error(f"Ошибка при запросе пулов: {e}")
        return []

async def monitor_pools():
    """Фоновая задача мониторинга пулов"""
    logger.info("Запуск мониторинга DLMM пулов...")
    while True:
        try:
            pools = await fetch_pools()
            new_pools = [p["pubkey"] for p in pools if p["pubkey"] not in known_pools]
            
            if new_pools:
                logger.info(f"Найдено {len(new_pools)} новых пулов")
                for pool_id in new_pools:
                    known_pools.add(pool_id)
                    await send_notification(pool_id)
            
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Ошибка мониторинга: {e}")
            await asyncio.sleep(30)

async def send_notification(pool_id):
    """Отправка уведомления в Telegram"""
    try:
        message = (
            "🆕 Новый DLMM пул!\n"
            f"Адрес: {pool_id}\n"
            f"Solscan: https://solscan.io/account/{pool_id}\n"
            f"Meteora: https://app.meteora.ag/pool/{pool_id}"
        )
        await app.bot_app.bot.send_message(chat_id=USER_ID, text=message)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

# --- Обработчики команд Telegram ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен! Мониторинг DLMM пулов активен")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Отслеживается {len(known_pools)} пулов")

# --- Маршруты Quart ---
@app.route('/')
async def health_check():
    return jsonify({"status": "ok", "pools_tracked": len(known_pools)})

@app.route('/webhook', methods=['POST'])
async def webhook():
    if not app.bot_app:
        return jsonify({"status": "error"}), 500
    
    data = await request.get_json()
    update = Update.de_json(data, app.bot_app.bot)
    await app.bot_app.process_update(update)
    return jsonify({"status": "ok"})

async def run_server():
    """Запуск сервера"""
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    config.use_reloader = False
    
    # Инициализация бота перед запуском сервера
    await init_bot()
    
    # Запуск фоновой задачи
    asyncio.create_task(monitor_pools())
    
    # Запуск сервера
    await serve(app, config)

async def shutdown():
    """Корректное завершение работы"""
    if app.bot_app:
        await app.bot_app.bot.delete_webhook()
        await app.bot_app.shutdown()
    logger.info("Бот остановлен")

if __name__ == '__main__':
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Получен сигнал остановки")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
    finally:
        asyncio.run(shutdown())
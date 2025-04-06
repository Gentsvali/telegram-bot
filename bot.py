import os
import logging
from quart import Quart
from telegram.ext import ApplicationBuilder

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Создаем Quart приложение (обязательно должно называться app)
app = Quart(__name__)

# Конфигурация (замените на свои значения)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
USER_ID = int(os.getenv("USER_ID", 0))

@app.before_serving
async def startup():
    """Инициализация при запуске"""
    logger.info("Инициализация бота...")
    
    # Создаем Telegram бота
    app.bot = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    await app.bot.initialize()
    
    logger.info("✅ Бот готов к работе")

@app.route('/')
async def home():
    """Проверочный маршрут"""
    return {"status": "active", "bot": "DLMM Monitor"}

async def fetch_pools():
    """Минимальный запрос к Helius API"""
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": "pool-fetch",
        "method": "getAssetsByAuthority",
        "params": {
            "authorityAddress": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
            "page": 1,
            "limit": 3
        }
    }
    
    async with app.session.get(url, json=payload) as resp:
        return await resp.json()

@app.route('/health')
async def health_check():
    """Проверка работоспособности"""
    try:
        pools = await fetch_pools()
        return {
            "status": "ok",
            "pools": bool(pools.get("result"))
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)}, 500

@app.after_serving
async def shutdown():
    """Корректное завершение"""
    if hasattr(app, 'bot'):
        await app.bot.shutdown()

if __name__ == '__main__':
    # Для локального тестирования
    app.run(host='0.0.0.0', port=10000)
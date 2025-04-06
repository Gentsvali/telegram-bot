import os
import asyncio
import logging
from quart import Quart
from telegram.ext import ApplicationBuilder
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
import aiohttp
from solders.pubkey import Pubkey

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Quart(__name__)
app.running = False
known_pools = set()

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
USER_ID = int(os.getenv("USER_ID", 0))
METEORA_PROGRAM_ID = Pubkey.from_string("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")

async def fetch_dlmm_pools():
    """Получение пулов через Helius API"""
    try:
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        payload = {
            "jsonrpc": "2.0",
            "id": "dlmm-fetcher",
            "method": "getAssetsByAuthority",
            "params": {
                "authorityAddress": str(METEORA_PROGRAM_ID),
                "page": 1,
                "limit": 100
            }
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result", {}).get("items", [])
                logger.error(f"Ошибка {resp.status}: {await resp.text()}")
                return []
    except Exception as e:
        logger.error(f"Ошибка fetch_dlmm_pools: {e}")
        return []

async def monitor_pools():
    """Основной цикл мониторинга"""
    while app.running:
        try:
            pools = await fetch_dlmm_pools()
            new_pools = [p for p in pools if p["id"] not in known_pools]
            
            if new_pools:
                logger.info(f"Найдено новых пулов: {len(new_pools)}")
                for pool in new_pools:
                    pool_id = pool["id"]
                    known_pools.add(pool_id)
                    await send_notification(pool_id)
            
            await asyncio.sleep(300)  # Проверка каждые 5 минут
            
        except Exception as e:
            logger.error(f"Ошибка мониторинга: {e}")
            await asyncio.sleep(60)

async def send_notification(pool_id):
    """Отправка уведомления в Telegram"""
    try:
        message = f"🆕 Новый DLMM пул: {pool_id}\n" \
                  f"🔗 Explorer: https://solscan.io/account/{pool_id}"
        
        await app.bot.bot.send_message(
            chat_id=USER_ID,
            text=message
        )
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

@app.before_serving
async def startup():
    """Инициализация при запуске"""
    app.running = True
    
    # Инициализация Telegram бота
    app.bot = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    await app.bot.initialize()
    
    # Запуск мониторинга
    asyncio.create_task(monitor_pools())
    logger.info("✅ Сервис запущен")

@app.after_serving
async def shutdown():
    """Корректное завершение"""
    app.running = False
    if hasattr(app, 'bot'):
        await app.bot.shutdown()

@app.route('/')
async def home():
    return {"status": "running", "pools_tracking": len(known_pools)}

if __name__ == '__main__':
    try:
        app.run(host='0.0.0.0', port=10000)
    except KeyboardInterrupt:
        logger.info("Завершение работы")
    except Exception as e:
        logger.error(f"Ошибка запуска: {e}")
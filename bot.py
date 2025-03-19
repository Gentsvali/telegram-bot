import os
import logging
import asyncio
import json
import base64
import signal
from datetime import datetime
from quart import Quart, request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler
import httpx
from solana.rpc.websocket_api import connect
from solders.pubkey import Pubkey
from solders.rpc.responses import ProgramNotification

# Загрузка переменных окружения
load_dotenv()

# Конфигурация логгера
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Проверка обязательных переменных
REQUIRED_ENV = ["TELEGRAM_TOKEN", "GITHUB_TOKEN", "USER_ID", "WEBHOOK_URL"]
for var in REQUIRED_ENV:
    if not os.getenv(var):
        raise ValueError(f"Missing required environment variable: {var}")

# Глобальные константы
CONFIG = {
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN"),
    "GITHUB_TOKEN": os.getenv("GITHUB_TOKEN"),
    "USER_ID": int(os.getenv("USER_ID")),
    "WEBHOOK_URL": os.getenv("WEBHOOK_URL"),
    "PORT": int(os.environ.get("PORT", 10000)),
    "API_URL": "https://dlmm-api.meteora.ag/pair/all",
    "WS_URL": "wss://api.mainnet-beta.solana.com",
    "PROGRAM_ID": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
    "DEFAULT_FILTERS": {
        "stable_coin": "USDC",
        "bin_steps": [20, 80, 100, 125, 250],
        "min_tvl": 10000.0,
        "min_volume_1h": 5000.0,
        "max_fee": 1.0,
        "verified_only": True
    }
}

current_filters = CONFIG["DEFAULT_FILTERS"].copy()
tracked_pools = set()

# Инициализация приложений
app = Quart(__name__)
telegram_app = (
    ApplicationBuilder()
    .token(CONFIG["TELEGRAM_TOKEN"])
    .concurrent_updates(True)
    .build()
)

# Вспомогательные функции
def sol_to_lamports(value: float) -> int:
    return int(value * 1e9)

def lamports_to_sol(lamports: int) -> float:
    return lamports / 1e9

# Обработчики Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != CONFIG["USER_ID"]:
        return
    await update.message.reply_text(
        "🚀 Meteora Pool Monitor\n"
        "Команды:\n"
        "/filters - текущие настройки\n"
        "/setfilter - изменить параметры\n"
        "/check - немедленная проверка"
    )

async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filters_text = "\n".join(
        f"• {key}: {value}" 
        for key, value in current_filters.items()
    )
    await update.message.reply_text(f"⚙️ Текущие фильтры:\n{filters_text}")

# Ядро логики
async def fetch_pools():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(CONFIG["API_URL"])
            response.raise_for_status()
            return response.json().get("data", [])
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return []

def filter_pool(pool: dict) -> bool:
    try:
        metrics = {
            "tvl": pool.get("liquidity", 0),
            "volume_1h": pool.get("volume", {}).get("1h", 0),
            "fee": pool.get("fee_percentage", 0),
            "bin_step": pool.get("bin_step", 0),
            "verified": pool.get("is_verified", False)
        }
        
        if current_filters["verified_only"] and not metrics["verified"]:
            return False
            
        return all([
            metrics["bin_step"] in current_filters["bin_steps"],
            metrics["tvl"] >= current_filters["min_tvl"],
            metrics["volume_1h"] >= current_filters["min_volume_1h"],
            metrics["fee"] <= current_filters["max_fee"]
        ])
    except Exception as e:
        logger.error(f"Filter error: {str(e)}")
        return False

async def check_pools(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Starting pool check...")
    pools = await fetch_pools()
    new_pools = [p for p in pools if p["address"] not in tracked_pools and filter_pool(p)]
    
    if new_pools:
        for pool in new_pools:
            await send_pool_alert(context, pool)
            tracked_pools.add(pool["address"])
        logger.info(f"Sent {len(new_pools)} alerts")
    else:
        logger.info("No new pools found")

async def send_pool_alert(context: ContextTypes.DEFAULT_TYPE, pool: dict):
    try:
        message = (
            f"🔥 New Pool Detected!\n"
            f"Pair: {pool['base_symbol']}-{pool['quote_symbol']}\n"
            f"TVL: ${pool['liquidity']:,.2f}\n"
            f"1h Volume: ${pool['volume']['1h']:,.2f}\n"
            f"Fee: {pool['fee_percentage']}%\n"
            f"🔗 DexScreener: https://dexscreener.com/solana/{pool['address']}"
        )
        await context.bot.send_message(
            chat_id=CONFIG["USER_ID"],
            text=message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error sending alert: {str(e)}")

# WebSocket обработчик
async def monitor_pools():
    program_id = Pubkey.from_string(CONFIG["PROGRAM_ID"])
    while True:
        try:
            async with connect(CONFIG["WS_URL"]) as ws:
                await ws.program_subscribe(program_id)
                async for response in ws:
                    if isinstance(response, ProgramNotification):
                        await process_pool_update(response)
        except Exception as e:
            logger.error(f"WebSocket error: {str(e)}")
            await asyncio.sleep(5)

async def process_pool_update(notification: ProgramNotification):
    try:
        pool_data = notification.result.value
        pool_info = {
            "address": str(pool_data.pubkey),
            "liquidity": lamports_to_sol(pool_data.account.lamports),
            "data": json.loads(pool_data.account.data.decode())
        }
        
        if filter_pool(pool_info):
            await send_pool_alert(telegram_app, pool_info)
    except Exception as e:
        logger.error(f"Processing error: {str(e)}")

# Системные функции
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(CONFIG["WEBHOOK_URL"])
    telegram_app.job_queue.run_repeating(check_pools, interval=300)
    asyncio.create_task(monitor_pools())

async def shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()

@app.route(f'/{CONFIG["TELEGRAM_TOKEN"]}', methods=['POST'])
async def webhook():
    data = await request.get_json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return '', 200

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: asyncio.create_task(shutdown()))
    signal.signal(signal.SIGTERM, lambda *_: asyncio.create_task(shutdown()))
    
    app.run(
        host='0.0.0.0',
        port=CONFIG["PORT"],
        use_reloader=False
    )
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

# Инициализация окружения
load_dotenv()

# Конфигурация логгера
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Проверка переменных окружения
REQUIRED_ENV = ["TELEGRAM_TOKEN", "GITHUB_TOKEN", "USER_ID", "WEBHOOK_URL"]
for var in REQUIRED_ENV:
    if not os.getenv(var):
        raise ValueError(f"Отсутствует переменная окружения: {var}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
USER_ID = int(os.getenv("USER_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))

# Конфигурация Meteora
API_URL = "https://dlmm-api.meteora.ag/pair/all"
WS_URL = "wss://api.mainnet-beta.solana.com"
PROGRAM_ID = Pubkey.from_string("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")

DEFAULT_FILTERS = {
    "stable_coin": "USDC",
    "bin_steps": [20, 80, 100, 125, 250],
    "min_tvl": 10000.0,
    "min_volume_1h": 5000.0,
    "max_fee": 1.0,
    "verified_only": True
}
current_filters = DEFAULT_FILTERS.copy()
tracked_pools = set()

# Инициализация приложений
app = Quart(__name__)
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# Вспомогательные функции
def lamports_to_sol(lamports: int) -> float:
    return lamports / 1e9

async def save_filters_to_github():
    """Сохраняет фильтры в GitHub."""
    try:
        url = f"https://api.github.com/repos/Gentsvali/telegram-bot/contents/filters.json"
        headers = {"Authorization": f"token {os.getenv('GITHUB_TOKEN')}"}
        
        content = json.dumps(current_filters, indent=4)
        encoded = base64.b64encode(content.encode()).decode()
        
        data = {
            "message": "Обновление фильтров",
            "content": encoded,
            "sha": requests.get(url, headers=headers).json().get("sha", "")
        }
        
        response = requests.put(url, headers=headers, json=data)
        response.raise_for_status()
        logger.info("Фильтры сохранены в GitHub")
    except Exception as e:
        logger.error(f"Ошибка сохранения: {str(e)}")

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    await update.message.reply_text(
        "🚀 Meteora Pool Monitor\n"
        "Команды:\n"
        "/filters - текущие настройки\n"
        "/setfilter - изменить параметры\n"
        "/check - немедленная проверка"
    )

async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filters_text = "\n".join(f"• {k}: {v}" for k, v in current_filters.items())
    await update.message.reply_text(f"⚙️ Текущие фильтры:\n{filters_text}")

# Логика работы с пулами
async def fetch_pools():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(API_URL)
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
        logger.error(f"Ошибка фильтрации: {str(e)}")
        return False

async def send_alert(context: ContextTypes.DEFAULT_TYPE, pool: dict):
    try:
        message = (
            f"🔥 Новый пул!\n"
            f"Пара: {pool['base_symbol']}-{pool['quote_symbol']}\n"
            f"TVL: ${pool['liquidity']:,.2f}\n"
            f"Объем (1ч): ${pool['volume']['1h']:,.2f}\n"
            f"Комиссия: {pool['fee_percentage']}%\n"
            f"🔗 DexScreener: https://dexscreener.com/solana/{pool['address']}"
        )
        await context.bot.send_message(USER_ID, message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка отправки: {str(e)}")

# WebSocket обработчик
async def monitor_pools():
    while True:
        try:
            async with connect(WS_URL) as ws:
                await ws.program_subscribe(PROGRAM_ID)
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
            await send_alert(application, pool_info)
    except Exception as e:
        logger.error(f"Ошибка обработки: {str(e)}")

# Инициализация приложения Telegram
async def initialize_telegram_app():
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    logger.info("Telegram приложение инициализировано")

# Системные функции
@app.before_serving
async def startup():
    await initialize_telegram_app()
    application.job_queue.run_repeating(check_pools, interval=300)
    asyncio.create_task(monitor_pools())

@app.after_serving
async def shutdown():
    await application.stop()
    await application.shutdown()

@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
async def webhook():
    data = await request.get_json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return '', 200

@app.route('/')
async def home():
    return {"status": "OK"}, 200

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: asyncio.create_task(shutdown()))
    signal.signal(signal.SIGTERM, lambda *_: asyncio.create_task(shutdown()))
    app.run(host='0.0.0.0', port=PORT)
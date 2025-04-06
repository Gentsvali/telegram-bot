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

# --- Конфигурация ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or "ВАШ_ТОКЕН"  # Обязательно!
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY") or "ВАШ_КЛЮЧ"
USER_ID = int(os.getenv("USER_ID", "ВАШ_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or f"https://ваш-домен.xyz/{TELEGRAM_TOKEN}"
PROGRAM_ID = Pubkey.from_string(os.getenv("PROGRAM_ID", "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"))

# --- Настройка логов ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Quart(__name__)
app.bot = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
known_pools = set()

# --- Основные функции ---
async def fetch_pools():
    """Запрос пулов через Helius API"""
    try:
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        payload = {
            "jsonrpc": "2.0",
            "id": "dlmm-fetcher",
            "method": "getAssetsByAuthority",
            "params": {
                "authorityAddress": str(PROGRAM_ID),
                "page": 1,
                "limit": 100
            }
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result", {}).get("items", [])
                logger.error(f"Ошибка API: {resp.status}")
                return []
    except Exception as e:
        logger.error(f"Ошибка fetch_pools: {e}")
        return []

async def monitor_pools():
    """Постоянный мониторинг новых пулов"""
    while True:
        try:
            pools = await fetch_pools()
            new_pools = [p for p in pools if p["id"] not in known_pools]
            
            if new_pools:
                logger.info(f"Найдено новых пулов: {len(new_pools)}")
                for pool in new_pools:
                    pool_id = pool["id"]
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
            f"ID: `{pool_id}`\n"
            f"[Solscan](https://solscan.io/account/{pool_id})\n"
            f"[Meteora](https://app.meteora.ag/pool/{pool_id})"
        )
        await app.bot.bot.send_message(
            chat_id=USER_ID,
            text=message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

# --- Обработчики команд ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот активен! Мониторинг DLMM пулов запущен")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔍 Отслеживается пулов: {len(known_pools)}")

# --- Вебхук ---
@app.before_serving
async def init():
    """Инициализация при запуске"""
    # Регистрация команд
    app.bot.add_handler(CommandHandler("start", start))
    app.bot.add_handler(CommandHandler("status", status))
    
    # Установка вебхука с токеном в URL
    webhook_url = f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}"
    await app.bot.bot.set_webhook(webhook_url)
    logger.info(f"🌍 Вебхук установлен на {webhook_url}")
    
    # Запуск мониторинга
    asyncio.create_task(monitor_pools())

@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
async def webhook():
    """Обработчик вебхука"""
    data = await request.get_json()
    update = Update.de_json(data, app.bot.bot)
    await app.bot.process_update(update)
    return jsonify({"status": "ok"})

@app.route('/')
async def health():
    return jsonify({"status": "active", "pools": len(known_pools)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
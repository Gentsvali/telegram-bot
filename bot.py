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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ВАШ_ТОКЕН")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "ВАШ_КЛЮЧ")
USER_ID = int(os.getenv("USER_ID", "ВАШ_ID"))
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_URL", "https://ваш-домен.xyz")
PROGRAM_ID = Pubkey.from_string(os.getenv("PROGRAM_ID", "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"))

# --- Настройка логов ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Quart(__name__)
app.bot_app = None  # Инициализируется в startup
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
    """Фоновая задача мониторинга"""
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
        await app.bot_app.bot.send_message(
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

# --- Инициализация ---
@app.before_serving
async def startup():
    """Инициализация при запуске"""
    # Создаем и инициализируем бота
    app.bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    await app.bot_app.initialize()
    
    # Регистрируем обработчики команд
    app.bot_app.add_handler(CommandHandler("start", start))
    app.bot_app.add_handler(CommandHandler("status", status))
    
    # Устанавливаем вебхук
    webhook_url = f"{WEBHOOK_BASE_URL}/webhook"
    await app.bot_app.bot.set_webhook(webhook_url)
    logger.info(f"🌍 Вебхук установлен на {webhook_url}")
    
    # Запускаем мониторинг
    asyncio.create_task(monitor_pools())
    logger.info("✅ Сервис запущен")

@app.after_serving
async def shutdown():
    """Корректное завершение"""
    if app.bot_app:
        await app.bot_app.bot.delete_webhook()
        await app.bot_app.shutdown()
        logger.info("🛑 Вебхук удален")

# --- Обработка вебхука ---
@app.route('/webhook', methods=['POST'])
async def webhook():
    """Обработчик вебхука Telegram"""
    if not app.bot_app:
        return jsonify({"status": "error", "reason": "Bot not initialized"}), 500
    
    data = await request.get_json()
    update = Update.de_json(data, app.bot_app.bot)
    await app.bot_app.process_update(update)
    return jsonify({"status": "ok"})

@app.route('/')
async def health():
    return jsonify({"status": "active", "pools": len(known_pools)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
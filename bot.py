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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # Обязательно через переменные окружения!
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")  # Ваш ключ Helius
USER_ID = int(os.getenv("USER_ID"))           # Ваш Telegram ID
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_URL")   # URL вебхука (например, Railway)

# Программа DLMM Meteora (публичный адрес)
PROGRAM_ID = Pubkey.from_string("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")

# --- Настройка логов ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Отключаем логирование HTTP-запросов (чтобы не светить токены)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

app = Quart(__name__)
app.bot_app = None  # Инициализируется в startup
known_pools = set()

# --- Основные функции ---
async def fetch_dlmm_pools():
    """Запрашивает пулы DLMM, относящиеся к PROGRAM_ID"""
    try:
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        payload = {
            "jsonrpc": "2.0",
            "id": "dlmm-fetcher",
            "method": "getProgramAccounts",
            "params": [
                str(PROGRAM_ID),
                {
                    "encoding": "jsonParsed",
                    "filters": [
                        {"dataSize": 324},  # Размер данных для DLMM пула (уточните у Meteora!)
                    ],
                    "withContext": True
                }
            ]
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    if "error" in data:
                        logger.error(f"Helius API error: {data['error']}")
                        return []
                    
                    accounts = data.get("result", {}).get("value", [])
                    logger.info(f"Найдено {len(accounts)} DLMM пулов")
                    return [account["pubkey"] for account in accounts]
                
                logger.error(f"Ошибка Helius API: {resp.status}")
                return []
                
    except Exception as e:
        logger.error(f"Ошибка при запросе пулов: {e}", exc_info=True)
        return []

async def monitor_pools():
    """Мониторинг новых DLMM пулов"""
    logger.info("🔄 Запуск мониторинга DLMM пулов...")
    while True:
        try:
            pools = await fetch_dlmm_pools()
            
            if not pools:
                logger.info("Новых пулов не найдено")
                await asyncio.sleep(60)
                continue
                
            new_pools = [p for p in pools if p not in known_pools]
            
            if not new_pools:
                logger.info("Нет новых пулов в этом цикле")
                await asyncio.sleep(60)
                continue
                
            logger.info(f"Найдено {len(new_pools)} новых пулов")
            for pool_id in new_pools:
                known_pools.add(pool_id)
                await send_notification(pool_id)
            
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"Ошибка мониторинга: {e}", exc_info=True)
            await asyncio.sleep(30)

async def send_notification(pool_id):
    """Отправляет уведомление о новом пуле в Telegram"""
    try:
        message = (
            "🆕 **Обнаружен новый DLMM пул!**\n"
            f"• Адрес: `{pool_id}`\n"
            f"• [Просмотр в Solscan](https://solscan.io/account/{pool_id})\n"
            f"• [Открыть в Meteora](https://app.meteora.ag/pool/{pool_id})"
        )
        await app.bot_app.bot.send_message(
            chat_id=USER_ID,
            text=message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

# --- Telegram команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот запущен! Мониторинг DLMM пулов активен")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔍 Отслеживается {len(known_pools)} пулов")

# --- Инициализация ---
@app.before_serving
async def startup():
    app.bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    await app.bot_app.initialize()
    
    app.bot_app.add_handler(CommandHandler("start", start))
    app.bot_app.add_handler(CommandHandler("status", status))
    
    webhook_url = f"{WEBHOOK_BASE_URL}/webhook"
    await app.bot_app.bot.set_webhook(webhook_url)
    logger.info(f"🌍 Вебхук установлен: {webhook_url}")
    
    asyncio.create_task(monitor_pools())
    logger.info("✅ Бот запущен и мониторит новые пулы")

@app.after_serving
async def shutdown():
    if app.bot_app:
        await app.bot_app.bot.delete_webhook()
        await app.bot_app.shutdown()
    logger.info("🛑 Бот остановлен")

# --- Обработка вебхука ---
@app.route('/webhook', methods=['POST'])
async def webhook():
    if not app.bot_app:
        return jsonify({"status": "error", "reason": "Bot not initialized"}), 500
    
    data = await request.get_json()
    update = Update.de_json(data, app.bot_app.bot)
    await app.bot_app.process_update(update)
    return jsonify({"status": "ok"})

@app.route('/')
async def health():
    return jsonify({"status": "active", "tracked_pools": len(known_pools)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
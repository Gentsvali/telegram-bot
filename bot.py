import os
import logging
import asyncio
import aiohttp
from quart import Quart, request, jsonify
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from solders.pubkey import Pubkey
from signal import signal, SIGINT, SIGTERM

# --- Конфигурация ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or "ВАШ_TELEGRAM_TOKEN"
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY") or "ВАШ_HELIUS_KEY"
USER_ID = int(os.getenv("USER_ID", "ВАШ_USER_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or "https://ваш-домен.xyz/bot"
PROGRAM_ID = Pubkey.from_string(os.getenv("PROGRAM_ID", "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"))

# --- Настройка логов ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Quart(__name__)
app.running = False
known_pools = set()

# Инициализация бота
bot = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# --- Основные функции ---
async def fetch_dlmm_pools():
    """Получение пулов через Helius API"""
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
                logger.error(f"Ошибка {resp.status}: {await resp.text()}")
                return []
    except Exception as e:
        logger.error(f"Ошибка fetch_dlmm_pools: {e}")
        return []

async def monitor_pools():
    """Фоновая задача мониторинга"""
    while app.running:
        try:
            pools = await fetch_dlmm_pools()
            new_pools = [p for p in pools if p["id"] not in known_pools]
            
            if new_pools:
                logger.info(f"Найдено новых пулов: {len(new_pools)}")
                for pool in new_pools:
                    pool_id = pool["id"]
                    known_pools.add(pool_id)
                    await send_telegram_notification(pool_id)
            
            await asyncio.sleep(60)  # Проверка каждую минуту
            
        except Exception as e:
            logger.error(f"Ошибка мониторинга: {e}")
            await asyncio.sleep(30)

async def send_telegram_notification(pool_id):
    """Отправка уведомления в Telegram"""
    try:
        message = (
            "🆕 Новый DLMM пул!\n"
            f"ID: `{pool_id}`\n"
            f"[Solscan](https://solscan.io/account/{pool_id})\n"
            f"[Meteora](https://app.meteora.ag/pool/{pool_id})"
        )
        await bot.bot.send_message(
            chat_id=USER_ID,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

# --- Обработчики команд Telegram ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Бот мониторинга DLMM пулов активен!")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📊 Отслеживается пулов: {len(known_pools)}")

# --- Вебхук и управление приложением ---
@app.before_serving
async def startup():
    """Инициализация при запуске"""
    app.running = True
    
    # Регистрация обработчиков команд
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CommandHandler("status", status))
    
    # Установка вебхука
    await bot.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"🌍 Вебхук установлен на {WEBHOOK_URL}")
    
    # Запуск мониторинга
    asyncio.create_task(monitor_pools())
    logger.info("✅ Сервис запущен")

@app.after_serving
async def shutdown():
    """Корректное завершение"""
    app.running = False
    await bot.bot.delete_webhook()
    logger.info("🛑 Вебхук удален")

@app.route('/bot', methods=['POST'])
async def telegram_webhook():
    """Обработчик вебхука от Telegram"""
    if request.method == "POST":
        data = await request.get_json()
        update = Update.de_json(data, bot.bot)
        await bot.process_update(update)
    return jsonify({"status": "ok"}), 200

@app.route('/')
async def health_check():
    """Проверка состояния сервиса"""
    return jsonify({
        "status": "running",
        "webhook": WEBHOOK_URL is not None,
        "pools_tracked": len(known_pools)
    })

# --- Запуск приложения ---
def handle_signal(signum, frame):
    """Обработчик сигналов завершения"""
    logger.info(f"Получен сигнал {signum}")
    asyncio.create_task(shutdown())
    exit(0)

if __name__ == '__main__':
    # Регистрация обработчиков сигналов
    signal(SIGINT, handle_signal)
    signal(SIGTERM, handle_signal)
    
    try:
        app.run(host='0.0.0.0', port=10000)
    except KeyboardInterrupt:
        logger.info("Принудительное завершение")
    except Exception as e:
        logger.error(f"Ошибка запуска: {e}")
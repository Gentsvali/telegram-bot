import os
import logging
import asyncio
from quart import Quart, request, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes
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
app.keep_running = True
known_pools = set()

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
USER_ID = int(os.getenv("USER_ID", 0))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
METEORA_PROGRAM_ID = Pubkey.from_string("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")

# Инициализация бота
bot = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

async def fetch_dlmm_pools():
    """Получение списка пулов"""
    try:
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        payload = {
            "jsonrpc": "2.0",
            "id": "dlmm-fetch",
            "method": "getAssetsByAuthority",
            "params": {
                "authorityAddress": str(METEORA_PROGRAM_ID),
                "page": 1,
                "limit": 50
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
        logger.error(f"Ошибка fetch_dlmm_pools: {e}")
        return []

async def pool_monitor():
    """Постоянный мониторинг новых пулов"""
    while app.keep_running:
        try:
            pools = await fetch_dlmm_pools()
            new_pools = [p for p in pools if p["id"] not in known_pools]
            
            if new_pools:
                logger.info(f"Найдено новых пулов: {len(new_pools)}")
                for pool in new_pools:
                    pool_id = pool["id"]
                    known_pools.add(pool_id)
                    await send_telegram_notification(pool_id)
            else:
                logger.info("Новых пулов не обнаружено")
            
            await asyncio.sleep(60)  # Проверка каждую минуту
            
        except Exception as e:
            logger.error(f"Ошибка мониторинга: {e}")
            await asyncio.sleep(30)

async def send_telegram_notification(pool_id):
    """Отправка уведомления в Telegram"""
    try:
        message = (
            "🆕 Новый DLMM пул обнаружен!\n"
            f"• Адрес: `{pool_id}`\n"
            f"• [Solscan](https://solscan.io/account/{pool_id})\n"
            f"• [Meteora](https://app.meteora.ag/pool/{pool_id})"
        )
        await bot.bot.send_message(
            chat_id=USER_ID,
            text=message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")

@app.before_serving
async def startup():
    """Инициализация при запуске"""
    # Установка вебхука
    await bot.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Вебхук установлен на {WEBHOOK_URL}")
    
    # Запуск мониторинга
    asyncio.create_task(pool_monitor())
    logger.info("Мониторинг пулов запущен")

@app.after_serving
async def shutdown():
    """Корректное завершение"""
    app.keep_running = False
    await bot.bot.delete_webhook()
    logger.info("Вебхук удален")

@app.route('/bot', methods=['POST'])
async def telegram_webhook():
    """Обработчик вебхука от Telegram"""
    json_data = await request.get_json()
    update = Update.de_json(json_data, bot.bot)
    await bot.process_update(update)
    return jsonify({"status": "ok"})

@app.route('/')
async def health_check():
    """Проверка состояния сервиса"""
    return jsonify({
        "status": "running",
        "monitoring": app.keep_running,
        "pools_tracked": len(known_pools)
    })

if __name__ == '__main__':
    # Для локального тестирования
    app.run(host='0.0.0.0', port=10000)
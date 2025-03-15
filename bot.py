import os
import logging
from datetime import datetime, timedelta
from quart import Quart, request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import httpx
import pytz

# Настройка логгера
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
USER_ID = int(os.getenv("USER_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 10000))

# Конфигурация API Meteora
API_URL = "https://dlmm-api.meteora.ag/pair/all_by_groups"
DEFAULT_FILTERS = {
    "stable_coin": "USDC",  # USDC или SOL
    "bin_steps": [1, 5, 10, 50],
    "min_tvl": 10000.0,
    "min_fdv": 500000.0,
    "base_fee_max": 1.0,
    "fee_tvl_ratio_24h_min": 0.1,
    "volume_1h_min": 5000.0,
    "volume_5m_min": 1000.0,
    "dynamic_fee_tvl_ratio_min": 0.5,
    "verified_only": True
}
current_filters = DEFAULT_FILTERS.copy()
last_checked_pools = set()

# Инициализация приложения Telegram
application = (
    ApplicationBuilder()
    .token(TELEGRAM_TOKEN)
    .concurrent_updates(True)
    .http_version("1.1")
    .get_updates_http_version("1.1")
    .build()
)

app = Quart(__name__)

@app.before_serving
async def startup():
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    logger.info("Приложение и вебхук успешно инициализированы")

@app.after_serving
async def shutdown():
    await application.stop()
    await application.shutdown()

# Основные обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    await update.message.reply_text(
        "🚀 Умный поиск пулов Meteora\n"
        "Команды:\n"
        "/filters - текущие настройки\n"
        "/setfilter - изменить параметры\n"
        "/checkpools - проверить сейчас\n"
        "/help - справка по командам"
    )

async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    response = (
        "⚙️ Текущие фильтры:\n"
        f"• Стабильная монета: {current_filters['stable_coin']}\n"
        f"• Bin Steps: {', '.join(map(str, current_filters['bin_steps']))}\n"
        f"• Мин TVL: ${current_filters['min_tvl']:,.2f}\n"
        f"• Мин FDV: ${current_filters['min_fdv']:,.2f}\n"
        f"• Макс комиссия: {current_filters['base_fee_max']}%\n"
        f"• Мин комиссия/TVL: {current_filters['fee_tvl_ratio_24h_min']}%\n"
        f"• Мин объем (1ч): ${current_filters['volume_1h_min']:,.2f}\n"
        f"• Мин объем (5м): ${current_filters['volume_5m_min']:,.2f}\n"
        f"• Мин динамическая комиссия: {current_filters['dynamic_fee_tvl_ratio_min']}%"
    )
    await update.message.reply_text(response)

async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    
    try:
        args = context.args
        if len(args) < 2:
            raise ValueError("Используйте: /setfilter [параметр] [значение]")

        param = args[0].lower()
        value = args[1]

        # Обработка разных типов параметров
        if param == "stable_coin":
            if value.upper() not in ["USDC", "SOL"]:
                raise ValueError("Допустимые значения: USDC или SOL")
            current_filters[param] = value.upper()
        
        elif param == "bin_steps":
            current_filters[param] = [int(v.strip()) for v in value.split(',')]
        
        elif param in ["min_tvl", "min_fdv", "base_fee_max", 
                      "fee_tvl_ratio_24h_min", "volume_1h_min", 
                      "volume_5m_min", "dynamic_fee_tvl_ratio_min"]:
            current_filters[param] = float(value)
        
        else:
            raise ValueError(f"Неизвестный параметр: {param}")

        await update.message.reply_text(f"✅ {param} обновлен: {value}")
    
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

# Основная логика работы с API
async def fetch_pools():
    try:
        params = {
            "sort_key": "volume",
            "order_by": "desc",
            "limit": 100,
            "include_unknown": not current_filters["verified_only"],
            "include_token_mints": [
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                if current_filters["stable_coin"] == "USDC"
                else "So11111111111111111111111111111111111111112"
            ]
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(API_URL, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("groups", [{}])[0].get("pairs", [])
    
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return []

def filter_pool(pool: dict) -> bool:
    try:
        pool_metrics = {
            "bin_step": pool.get("bin_step", 999),
            "base_fee": float(pool.get("base_fee_percentage", 100)),
            "tvl": float(pool.get("liquidity", 0)),
            "fee_24h": float(pool.get("fees_24h", 0)),
            "volume_1h": float(pool.get("volume", {}).get("hour_1", 0)),
            "volume_5m": float(pool.get("volume", {}).get("min_30", 0)) * 2,
            "dynamic_fee": float(pool.get("fee_tvl_ratio", {}).get("hour_1", 0))
        }

        conditions = [
            pool_metrics["bin_step"] in current_filters["bin_steps"],
            pool_metrics["base_fee"] <= current_filters["base_fee_max"],
            (pool_metrics["fee_24h"] / pool_metrics["tvl"] * 100) >= current_filters["fee_tvl_ratio_24h_min"],
            pool_metrics["volume_1h"] >= current_filters["volume_1h_min"],
            pool_metrics["volume_5m"] >= current_filters["volume_5m_min"],
            pool_metrics["dynamic_fee"] >= current_filters["dynamic_fee_tvl_ratio_min"],
            pool_metrics["tvl"] >= current_filters["min_tvl"]
        ]

        return all(conditions)
    
    except Exception as e:
        logger.error(f"Filter Error: {str(e)}")
        return False

def format_pool_message(pool: dict) -> str:
    metrics = {
        "address": pool.get("address", "N/A"),
        "pair": f"{pool.get('mint_x', '?')}-{pool.get('mint_y', '?')}",
        "tvl": float(pool.get("liquidity", 0)),
        "volume_1h": float(pool.get("volume", {}).get("hour_1", 0)),
        "volume_5m": float(pool.get("volume", {}).get("min_30", 0)) * 2,
        "fee_tvl_ratio": float(pool.get("fees_24h", 0)) / float(pool.get("liquidity", 1)) * 100,
        "dynamic_fee": float(pool.get("fee_tvl_ratio", {}).get("hour_1", 0)),
        "bin_step": pool.get("bin_step", "N/A"),
        "base_fee": pool.get("base_fee_percentage", "N/A")
    }

    return (
        f"🔥 Новый пул по вашим критериям!\n\n"
        f"Пара: {metrics['pair']}\n"
        f"TVL: ${metrics['tvl']:,.2f}\n"
        f"Объем (1ч): ${metrics['volume_1h']:,.2f}\n"
        f"Объем (5м): ${metrics['volume_5m']:,.2f}\n"
        f"Комиссия/TVL: {metrics['fee_tvl_ratio']:.2f}%\n"
        f"Динамическая комиссия: {metrics['dynamic_fee']:.2f}%\n"
        f"Bin Step: {metrics['bin_step']}\n"
        f"Базовая комиссия: {metrics['base_fee']}%\n\n"
        f"🔗 [Meteora](https://app.meteora.ag/dlmm/{metrics['address']}) | "
        f"[DexScreener](https://dexscreener.com/solana/{metrics['address']})"
    )

async def check_new_pools(context: ContextTypes.DEFAULT_TYPE):
    global last_checked_pools
    logger.info("Запуск проверки пулов...")

    try:
        pools = await fetch_pools()
        new_pools = [p for p in pools if p["address"] not in last_checked_pools and filter_pool(p)]

        if new_pools:
            logger.info(f"Найдено {len(new_pools)} новых пулов")
            for pool in new_pools:
                message = format_pool_message(pool)
                await context.bot.send_message(
                    chat_id=USER_ID,
                    text=message,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
            last_checked_pools.update(p["address"] for p in pools)
        else:
            logger.info("Новых подходящих пулов не найдено")
    
    except Exception as e:
        logger.error(f"Ошибка проверки: {str(e)}")
        await context.bot.send_message(
            chat_id=USER_ID,
            text="⚠️ Ошибка при проверке пулов"
        )

# Регистрация обработчиков
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("filters", show_filters))
application.add_handler(CommandHandler("setfilter", set_filter))
application.add_handler(CommandHandler("checkpools", check_new_pools))

# Планировщик задач
application.job_queue.run_repeating(check_new_pools, interval=300, first=10)

# Вебхук
@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
async def webhook():
    try:
        data = await request.get_json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return '', 200
    except Exception as e:
        logger.error(f"Webhook Error: {str(e)}")
        return '', 500

@app.route('/healthcheck')
def healthcheck():
    return {"status": "OK"}, 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)
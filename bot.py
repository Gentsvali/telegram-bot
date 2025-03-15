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
API_URL = "https://dlmm-api.meteora.ag/pair/all_with_pagination"
DEFAULT_FILTERS = {
    "min_tvl": 10000.0,
    "max_age": "3h",
    "min_volume_24h": 5000.0,
    "min_apr": 5.0,
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

# Обработчики событий Quart
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

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    await update.message.reply_text(
        "🚀 Бот для отслеживания новых пулов Meteora!\n"
        "Команды:\n/filters - текущие настройки\n/setfilter [параметр] [значение]\n/checkpools - проверить пулы"
    )

async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    response = "⚙️ Текущие настройки:\n" + "\n".join(
        f"{k}: {v}" for k, v in current_filters.items()
    )
    await update.message.reply_text(response)

async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    try:
        args = context.args
        if len(args) != 2:
            raise ValueError("Формат: /setfilter [параметр] [значение]")
        
        param = args[0].lower()
        value = args[1]
        
        if param not in current_filters:
            raise ValueError("Неизвестный параметр")
        
        # Конвертация значений
        if param in ["min_tvl", "min_volume_24h", "min_apr"]:
            current_filters[param] = float(value)
        elif param == "max_age":
            parse_age(value)  # Проверка формата
            current_filters[param] = value
        elif param == "verified_only":
            current_filters[param] = value.lower() in ["true", "1", "yes"]
        
        await update.message.reply_text(f"✅ {param} обновлен: {current_filters[param]}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

# Вспомогательные функции
def parse_age(age_str: str) -> timedelta:
    units = {'d': 'days', 'h': 'hours', 'm': 'minutes'}
    unit = age_str[-1]
    value = int(age_str[:-1])
    return timedelta(**{units[unit]: value})

# Логика работы с API
async def fetch_pools():
    try:
        params = {
            "sort_key": "volume",
            "order_by": "desc",
            "limit": 50,
            "page": 0,
            "hide_low_tvl": current_filters["min_tvl"],
            "include_unknown": not current_filters["verified_only"]
        }
        async with httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
        ) as client:
            response = await client.get(API_URL, params=params)
            response.raise_for_status()
            data = response.json()
            logger.info(f"Данные от API: {data}")  # Логируем полученные данные
            return data.get("pairs", [])
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return []

def filter_pool(pool: dict) -> bool:
    try:
        created_at = datetime.fromisoformat(pool['created_at'].replace("Z", "+00:00"))
        age = datetime.now(pytz.utc) - created_at
        result = all([
            float(pool.get('liquidity', 0)) >= current_filters['min_tvl'],
            float(pool.get('trade_volume_24h', 0)) >= current_filters['min_volume_24h'],
            float(pool.get('apr', 0)) >= current_filters['min_apr'],
            age <= parse_age(current_filters['max_age'])
        ])
        logger.info(f"Пул {pool.get('address')} прошел фильтрацию: {result}")  # Логируем результат фильтрации
        return result
    except Exception as e:
        logger.error(f"Filter Error: {str(e)}")
        return False

def format_pool_message(pool: dict, created_at: datetime) -> str:
    address = pool.get('address', '')
    mint_x = pool.get('mint_x', '?')
    mint_y = pool.get('mint_y', '?')
    liquidity = float(pool.get('liquidity', 0))
    volume_24h = float(pool.get('trade_volume_24h', 0))
    apr = float(pool.get('apr', 0))
    bin_step = pool.get('bin_step', '?')
    fees = pool.get('fees', {})

    message = (
        f"🔥 Обнаружены пулы с высокой доходностью 🔥\n\n"
        f"🔥 {mint_x}-{mint_y} (https://t.me/meteora_pool_tracker_bot/?start=pool_info={address}) | "
        f"создан ~{created_at.strftime('%d.%m.%Y %H:%M')} | "
        f"RugCheck: 🟢1 (https://rugcheck.xyz/tokens/{mint_x})\n"
        f"🔗 Meteora (https://app.meteora.ag/dlmm/{address}) | "
        f"DexScreener (https://dexscreener.com/solana/{address}) | "
        f"GMGN (https://gmgn.ai/sol/token/{mint_x}) | "
        f"TrenchRadar (https://trench.bot/bundles/{mint_x}?all=true)\n"
        f"💎 Market Cap: ${liquidity / 1e6:.1f}M 🔹TVL: ${liquidity / 1e3:.1f}K\n"
        f"📊 Объем: ${volume_24h / 1e3:.1f}K 🔸 Bin Step: {bin_step} 💵 Fees: {fees.get('min_30', '?')}% | {fees.get('hour_1', '?')}%\n"
        f"🤑 Принт (5m dynamic fee/TVL): {(fees.get('min_30', 0) / liquidity * 100):.2f}%\n"
        f"🪙 Токен (https://t.me/meteora_pool_tracker_bot/?start=pools={mint_x}): {mint_x}\n"
        f"🤐 Mute 1h (https://t.me/meteora_pool_tracker_bot/?start=mute_token={mint_x}_1h) | "
        f"Mute 24h (https://t.me/meteora_pool_tracker_bot/?start=mute_token={mint_x}_24h) | "
        f"Mute forever (https://t.me/meteora_pool_tracker_bot/?start=mute_token={mint_x}_forever)"
    )
    return message

async def check_new_pools(context: ContextTypes.DEFAULT_TYPE):
    global last_checked_pools
    logger.info("Запуск проверки новых пулов...")

    try:
        pools = await fetch_pools()
        logger.info(f"Получено пулов: {len(pools)}")

        current_ids = {p['address'] for p in pools}
        new_pools = [p for p in pools if p['address'] not in last_checked_pools and filter_pool(p)]

        if new_pools:
            logger.info(f"Найдено новых пулов: {len(new_pools)}")
            for pool in new_pools:
                created_at = datetime.fromisoformat(pool['created_at'].replace("Z", "+00:00"))
                moscow_time = created_at.astimezone(pytz.timezone('Europe/Moscow'))
                message = format_pool_message(pool, moscow_time)  # Форматируем сообщение

                await context.bot.send_message(
                    chat_id=USER_ID,
                    text=message,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )
            last_checked_pools = current_ids
        else:
            logger.info("Новых пулов не найдено.")
    except Exception as e:
        logger.error(f"POOL CHECK ERROR: {str(e)}", exc_info=True)
        await context.bot.send_message(
            chat_id=USER_ID,
            text="⚠️ Произошла ошибка при проверке пулов"
        )

# Добавляем глобальный обработчик ошибок
application.add_error_handler(lambda _, __: logger.error("Global error"))

# Регистрация команд
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("filters", show_filters))
application.add_handler(CommandHandler("setfilter", set_filter))
application.add_handler(CommandHandler("checkpools", check_new_pools))

# Планировщик задач
application.job_queue.run_repeating(check_new_pools, interval=300, first=10)  # Проверка каждые 5 минут

# Вебхук
@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
async def webhook():
    try:
        logger.info("Получен вебхук")
        data = await request.get_json()  # Добавлен await
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return '', 200
    except Exception as e:
        logger.error(f"CRITICAL ERROR: {str(e)}", exc_info=True)
        return '', 500

@app.route('/healthcheck', methods=['GET', 'POST'])
def healthcheck():
    return {
        "status": "OK",
        "bot_initialized": application.initialized,
        "last_update": datetime.utcnow().isoformat()
    }, 200

@app.route('/')
async def home():
    return "🤖 Бот активен! Используйте Telegram для управления"

# Запуск приложения
if __name__ == "__main__":
    # Запуск Quart с поддержкой асинхронности
    app.run(host='0.0.0.0', port=PORT)
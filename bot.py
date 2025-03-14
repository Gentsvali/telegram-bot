import os
import logging
import asyncio
from datetime import datetime, timedelta
from flask import Flask, request
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
    .build()
)

app = Flask(__name__)

# Асинхронная инициализация
async def initialize():
    """Инициализация приложения и вебхука"""
    await application.initialize()
    await application.bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    logger.info("Приложение и вебхук успешно инициализированы")

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    await update.message.reply_text(
        "🚀 Бот для отслеживания новых пулов Meteora!\n"
        "Команды:\n/filters - текущие настройки\n/setfilter [параметр] [значение]"
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
            return response.json().get("pairs", [])
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return []

def filter_pool(pool: dict) -> bool:
    try:
        created_at = datetime.fromisoformat(pool['created_at'].replace("Z", "+00:00"))
        age = datetime.now(pytz.utc) - created_at
        return all([
            float(pool.get('liquidity', 0)) >= current_filters['min_tvl'],
            float(pool.get('trade_volume_24h', 0)) >= current_filters['min_volume_24h'],
            float(pool.get('apr', 0)) >= current_filters['min_apr'],
            age <= parse_age(current_filters['max_age'])
        ])
    except Exception as e:
        logger.error(f"Filter Error: {str(e)}")
        return False

async def check_new_pools(context: ContextTypes.DEFAULT_TYPE):
    global last_checked_pools
    try:
        pools = await fetch_pools()
        current_ids = {p['address'] for p in pools}
        new_pools = [p for p in pools if p['address'] not in last_checked_pools and filter_pool(p)]
        
        if new_pools:
            for pool in new_pools:
                created_at = datetime.fromisoformat(pool['created_at'].replace("Z", "+00:00"))
                moscow_time = created_at.astimezone(pytz.timezone('Europe/Moscow'))
                message = (
                    f"🔥 Новый пул: {pool.get('mint_x', '?')}/{pool.get('mint_y', '?')}\n"
                    f"🕒 Создан: {moscow_time.strftime('%d.%m.%Y %H:%M')}\n"
                    f"💎 TVL: ${float(pool.get('liquidity', 0)):,.2f}\n"
                    f"📈 Объем 24ч: ${float(pool.get('trade_volume_24h', 0)):,.2f}\n"
                    f"🎯 APR: {float(pool.get('apr', 0)):.1f}%\n"
                    f"🔗 [Meteora](https://app.meteora.ag/dlmm/{pool.get('address', '')})"
                )
                await context.bot.send_message(
                    chat_id=USER_ID,
                    text=message,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )
            last_checked_pools = current_ids
            logger.info(f"Отправлено уведомлений: {len(new_pools)}")
    except Exception as e:
        logger.error(f"Ошибка проверки пулов: {str(e)}")

# Регистрация команд
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("filters", show_filters))
application.add_handler(CommandHandler("setfilter", set_filter))

# Планировщик задач
application.job_queue.run_repeating(check_new_pools, interval=300, first=10)

# Вебхук
@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        update = Update.de_json(data, application.bot)
        asyncio.run(application.process_update(update))
        return '', 200
    except Exception as e:
        logger.error(f"Webhook Error: {str(e)}")
        return '', 500

@app.route('/')
def home():
    return "🤖 Бот активен! Используйте Telegram для управления"

# Запуск приложения
if __name__ == "__main__":
    # Инициализация асинхронных компонентов
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(initialize())
    
    # Запуск Flask
    app.run(host='0.0.0.0', port=PORT)
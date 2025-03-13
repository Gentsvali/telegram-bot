import os
import logging
from datetime import datetime, timedelta
from flask import Flask
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import httpx
import pytz

# Настройка логгера
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
USER_ID = int(os.getenv("USER_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://telegram-bot-6gec.onrender.com
PORT = int(os.environ.get("PORT", 10000))

# Конфигурация API
API_URL = "https://dlmm-api.meteora.ag/pair/all_with_pagination"

# Фильтры и состояние
DEFAULT_FILTERS = {
    "min_tvl": 10000.0,
    "max_age": "3h",
    "min_volume_24h": 5000.0,
    "min_apr": 5.0,
    "verified_only": True
}
current_filters = DEFAULT_FILTERS.copy()
last_checked_pools = set()

# Flask приложение
app = Flask(__name__)

def parse_age(age_str):
    units = {'d': 'days', 'h': 'hours', 'm': 'minutes'}
    unit = age_str[-1]
    value = int(age_str[:-1])
    return timedelta(**{units[unit]: value})

async def get_meteora_pools():
    try:
        params = {
            "sort_key": "volume",
            "order_by": "desc",
            "limit": 50,
            "page": 0,
            "hide_low_tvl": current_filters["min_tvl"],
            "include_unknown": not current_filters["verified_only"]
        }
        
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(API_URL, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("pairs", [])
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return []

def apply_filters(pool):
    try:
        created_at = datetime.fromisoformat(pool['created_at'].replace("Z", "+00:00"))
        age = datetime.now(pytz.utc) - created_at
        tvl = float(pool.get("liquidity", 0))
        volume = float(pool.get("trade_volume_24h", 0))
        apr = float(pool.get("apr", 0))
        
        return all([
            tvl >= current_filters["min_tvl"],
            volume >= current_filters["min_volume_24h"],
            apr >= current_filters["min_apr"],
            age <= parse_age(current_filters["max_age"])
        ])
    except Exception as e:
        logger.error(f"Filter error: {str(e)}")
        return False

async def send_pool_notification(context, pool):
    try:
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
    except Exception as e:
        logger.error(f"Send message error: {str(e)}")

async def track_new_pools(context: ContextTypes.DEFAULT_TYPE):
    global last_checked_pools
    try:
        current_pools = await get_meteora_pools()
        current_ids = {p['address'] for p in current_pools}
        new_ids = current_ids - last_checked_pools
        
        if new_ids:
            for pool in current_pools:
                if pool['address'] in new_ids and apply_filters(pool):
                    await send_pool_notification(context, pool)
            last_checked_pools = current_ids
            logger.info(f"Sent notifications for {len(new_ids)} new pools")
    except Exception as e:
        logger.error(f"Tracking error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    await update.message.reply_text(
        "🔔 Бот для отслеживания пулов Meteora\n"
        "Используйте /filters для просмотра настроек\n"
        "/setfilter для изменения параметров"
    )

async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    filters_text = "⚙️ Текущие фильтры:\n" + "\n".join(
        f"{k}: {v}" for k, v in current_filters.items()
    )
    await update.message.reply_text(filters_text)

async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    # Реализация изменения фильтров (оставьте предыдущую реализацию)

@app.route('/')
def home():
    return "Meteora Pool Tracker is running!"

def main():
    # Создаем Application
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("filters", show_filters))
    application.add_handler(CommandHandler("setfilter", set_filter))

    # Настраиваем периодическую задачу
    application.job_queue.run_repeating(
        track_new_pools,
        interval=300,
        first=10,
    )

    # Настраиваем вебхук
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
        cert_open=True
    )

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)
    main()
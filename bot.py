import json
import os
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
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

# Конфигурация API
API_URL = "https://dlmm-api.meteora.ag/pair/all_with_pagination"

# Фильтры по умолчанию
DEFAULT_FILTERS = {
    "min_tvl": 10000.0,
    "max_age": "3h",
    "min_volume_24h": 5000.0,
    "min_apr": 5.0,
    "verified_only": True
}

filters = DEFAULT_FILTERS.copy()
last_checked_pools = set()

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
            "hide_low_tvl": filters["min_tvl"],
            "include_unknown": not filters["verified_only"]
        }
        
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(API_URL, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("pairs", [])
            
    except Exception as e:
        logger.error(f"Ошибка получения пулов: {e}")
        return []

def apply_filters(pool):
    try:
        # Проверка TVL
        tvl = float(pool.get("liquidity", 0))
        if tvl < filters["min_tvl"]:
            return False
        
        # Проверка объема
        volume = float(pool.get("trade_volume_24h", 0))
        if volume < filters["min_volume_24h"]:
            return False
        
        # Проверка APR
        apr = float(pool.get("apr", 0))
        if apr < filters["min_apr"]:
            return False
        
        # Проверка времени создания
        created_at_str = pool.get("created_at")
        if created_at_str:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            max_age = parse_age(filters["max_age"])
            if (datetime.now(pytz.utc) - created_at) > max_age:
                return False
        
        return True
    except Exception as e:
        logger.error(f"Ошибка фильтрации пула: {e}")
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
        logger.error(f"Ошибка отправки сообщения: {e}")

async def track_new_pools(context: ContextTypes.DEFAULT_TYPE):
    global last_checked_pools
    
    current_pools = await get_meteora_pools()
    if not current_pools:
        return
    
    current_pool_ids = {pool['address'] for pool in current_pools}
    new_pool_ids = current_pool_ids - last_checked_pools
    
    if new_pool_ids:
        for pool in current_pools:
            if pool['address'] in new_pool_ids and apply_filters(pool):
                await send_pool_notification(context, pool)
        
        last_checked_pools = current_pool_ids

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    
    help_text = (
        "🚀 Бот для отслеживания новых пулов Meteora\n\n"
        "Доступные команды:\n"
        "/filters - текущие настройки фильтров\n"
        "/setfilter [параметр] [значение] - изменить фильтр\n"
        "/help - справка по командам"
    )
    
    await update.message.reply_text(help_text)

async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    
    filters_text = "⚙️ Текущие настройки фильтров:\n"
    filters_text += "\n".join([f"{key}: {value}" for key, value in filters.items()])
    
    await update.message.reply_text(filters_text)

async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return
    
    try:
        args = context.args
        if len(args) != 2:
            raise ValueError("Неправильный формат команды")
        
        param = args[0].lower()
        value = args[1]
        
        if param not in filters:
            raise ValueError("Неизвестный параметр фильтра")
        
        # Преобразование значений
        if param in ["min_tvl", "min_volume_24h"]:
            filters[param] = float(value)
        elif param == "min_apr":
            filters[param] = float(value)
        elif param == "max_age":
            parse_age(value)  # Проверка формата
            filters[param] = value
        elif param == "verified_only":
            filters[param] = value.lower() in ["true", "1", "yes"]
        
        await update.message.reply_text(f"✅ Фильтр {param} обновлен: {filters[param]}")
        
    except Exception as e:
        logger.error(f"Ошибка изменения фильтра: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")

def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("filters", show_filters))
    application.add_handler(CommandHandler("setfilter", set_filter))
    application.add_error_handler(error_handler)
    
    # Настройка периодической проверки
    application.job_queue.run_repeating(
        track_new_pools,
        interval=300,
        first=10,
    )

    application.run_polling()

if __name__ == "__main__":
    main()
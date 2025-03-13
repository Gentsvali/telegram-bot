import json
import os
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
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
USER_ID = int(os.getenv("USER_ID"))  # Ваш Telegram ID

# Конфигурация API
API_URL = "https://dlmm-api.meteora.ag/pools"

# Фильтры (можно менять здесь напрямую)
FILTERS = {
    "minTvl": 10000,
    "maxTvl": None,
    "minMcap": 500000,
    "maxMcap": None,
    "minYieldPercent": 1.0,
    "maxAge": "10d",
    "rugcheck": {
        "maxRagScore": 20000,
        "skipFreezeAuthority": True,
    }
}

last_pools = []

def parse_age(age_str):
    units = {'d': 'days', 'h': 'hours', 'm': 'minutes'}
    value = int(age_str[:-1])
    return timedelta(**{units[age_str[-1]]: value})

async def get_meteora_pools():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                API_URL,
                params={"order": "created_at:desc"}
            )
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"Ошибка получения пулов: {e}")
        return []

def apply_filters(pool):
    now = datetime.now(pytz.timezone('Europe/Moscow'))
    created_at = datetime.fromisoformat(pool['created_at'].replace('Z', '+00:00')).astimezone(pytz.timezone('Europe/Moscow'))
    
    if FILTERS['maxAge'] and (now - created_at) > parse_age(FILTERS['maxAge']):
        return False

    tvl = pool.get('total_liquidity_usd', 0)
    if FILTERS['minTvl'] and tvl < FILTERS['minTvl']:
        return False
    if FILTERS['maxTvl'] and tvl > FILTERS['maxTvl']:
        return False

    rugcheck = pool.get('rugcheck', {})
    if rugcheck.get('ragScore', 0) > FILTERS['rugcheck']['maxRagScore']:
        return False

    return True

async def send_pool_notification(context, pool):
    try:
        created_at = datetime.fromisoformat(pool['created_at'].replace('Z', '+00:00'))
        moscow_time = created_at.astimezone(pytz.timezone('Europe/Moscow'))
        
        message = (
            f"🔥 Новый пул: {pool['token_x']['symbol']}/{pool['token_y']['symbol']}\n"
            f"🕒 Создан: {moscow_time.strftime('%d.%m.%Y %H:%M')}\n"
            f"💎 TVL: ${pool.get('total_liquidity_usd', 0):.2f}\n"
            f"📊 MCap: ${pool.get('market_cap', 0):.2f}\n"
            f"🔗 [Meteora](https://app.meteora.ag/dlmm/{pool['address']}) | "
            f"[DexScreener](https://dexscreener.com/solana/{pool['address']})"
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
    global last_pools
    
    current_pools = await get_meteora_pools()
    if not current_pools:
        return

    filtered_pools = [pool for pool in current_pools if apply_filters(pool)]
    new_pools = [pool for pool in filtered_pools if pool not in last_pools]

    if new_pools:
        for pool in new_pools:
            await send_pool_notification(context, pool)
        last_pools = filtered_pools.copy()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        await update.message.reply_text("⛔ Доступ запрещен")
        return
    
    await update.message.reply_text(
        "🔔 Бот для отслеживания новых пулов Meteora запущен!\n"
        "Автоматическая проверка новых пулов происходит каждые 5 минут."
    )

def main():
    if not TELEGRAM_TOKEN or not USER_ID:
        logger.error("Не заданы TELEGRAM_TOKEN или USER_ID в переменных окружения!")
        return

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Добавляем обработчик старта
    application.add_handler(CommandHandler("start", start))
    
    # Настройка периодической проверки
    application.job_queue.run_repeating(
        track_new_pools,
        interval=300,
        first=10,
    )

    application.run_polling()

if __name__ == "__main__":
    main()
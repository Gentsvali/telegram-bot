import json
import sqlite3
import os
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect("user_settings.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            filters TEXT DEFAULT '{}'
        )
    """)
    conn.commit()
    conn.close()

def get_user_settings(user_id):
    conn = sqlite3.connect("user_settings.db")
    cursor = conn.cursor()
    cursor.execute("SELECT filters FROM user_settings WHERE user_id = ?", (user_id,))
    settings = cursor.fetchone()
    conn.close()
    return json.loads(settings[0]) if settings else None

def update_user_settings(user_id, filters):
    conn = sqlite3.connect("user_settings.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO user_settings (user_id, filters)
        VALUES (?, ?)
    """, (user_id, json.dumps(filters)))
    conn.commit()
    conn.close()

init_db()
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

API_URL = "https://dlmm-api.meteora.ag/pools"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

DEFAULT_FILTERS = {
    "minTvl": 10000,
    "maxTvl": None,
    "minMcap": 500000,
    "maxMcap": None,
    "minBinstep": None,
    "maxBinstep": None,
    "minYieldPercent": 1.0,
    "maxAge": "10d",
    "rugcheck": {
        "maxRagScore": 20000,
        "skipFreezeAuthority": True,
        "skipMintAuthority": False,
        "skipHighHolderCorrelation": True,
        "skipLargeLPUnlocked": False,
        "skipTopHoldersHighOwnership": False
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

def apply_filters(pool, filters):
    now = datetime.now(pytz.timezone('Europe/Moscow'))
    created_at = datetime.fromisoformat(pool['created_at'].replace('Z', '+00:00')).astimezone(pytz.timezone('Europe/Moscow'))
    
    if filters['maxAge'] and (now - created_at) > parse_age(filters['maxAge']):
        return False

    tvl = pool.get('total_liquidity_usd', 0)
    if filters['minTvl'] and tvl < filters['minTvl']:
        return False
    if filters['maxTvl'] and tvl > filters['maxTvl']:
        return False

    mcap = pool.get('market_cap', 0)
    if filters['minMcap'] and mcap < filters['minMcap']:
        return False
    if filters['maxMcap'] and mcap > filters['maxMcap']:
        return False

    rugcheck = pool.get('rugcheck', {})
    if rugcheck.get('ragScore', 0) > filters['rugcheck']['maxRagScore']:
        return False
    if not filters['rugcheck']['skipFreezeAuthority'] and rugcheck.get('freezeAuthority'):
        return False

    return True

async def send_pool_notification(context, pool, user_id):
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
            chat_id=user_id,
            text=message,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

async def track_new_pools(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.user_id
    filters = get_user_settings(user_id) or DEFAULT_FILTERS
    
    current_pools = await get_meteora_pools()
    if not current_pools:
        return

    filtered_pools = [pool for pool in current_pools if apply_filters(pool, filters)]
    new_pools = [pool for pool in filtered_pools if pool not in last_pools]

    if new_pools:
        for pool in new_pools:
            await send_pool_notification(context, pool, user_id)
        last_pools[:] = filtered_pools.copy()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [["Проверить сейчас"], ["Настройки"]]
    
    settings = get_user_settings(user_id) or DEFAULT_FILTERS
    await update.message.reply_text(
        "🔔 Бот для отслеживания новых пулов Meteora\n\n"
        f"Текущие настройки фильтров:\n```{json.dumps(settings, indent=2)}```",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode='MarkdownV2'
    )

async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ Отправьте JSON с новыми настройками фильтров. Пример:\n"
        "```json\n{\n  \"minTvl\": 10000,\n  \"maxAge\": \"3d\"\n}\n```",
        parse_mode='MarkdownV2'
    )

async def handle_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        new_filters = json.loads(update.message.text)
        current_filters = get_user_settings(user_id) or DEFAULT_FILTERS.copy()
        current_filters.update(new_filters)
        update_user_settings(user_id, current_filters)
        await update.message.reply_text("✅ Настройки обновлены!")
    except json.JSONDecodeError:
        await update.message.reply_text("❌ Ошибка формата JSON")
    except Exception as e:
        logger.error(f"Ошибка обновления настроек: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обновлении настроек")

def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Добавление обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Text("Проверить сейчас"), 
                         lambda update, ctx: track_new_pools(ctx)))
    application.add_handler(MessageHandler(filters.Text("Настройки"), handle_settings))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_json))

    # Настройка периодической проверки
    application.job_queue.run_repeating(
        track_new_pools,
        interval=300,
        first=10,
        # Укажите ваш реальный CHAT_ID вместо использования токена
        chat_id=int(os.getenv("839443665"))  
    )

    application.run_polling()

if __name__ == "__main__":
    main()
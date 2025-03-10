from dotenv import load_dotenv
import os
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
SECRET_TOKEN = os.getenv("SECRET_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

API_URLS = {
    "meteora_pools": "https://app.meteora.ag/api/pools",
    "dexscreener": "https://api.dexscreener.com/latest/dex/pairs/solana/{address}",
}

USER_FILTERS = {}

async def format_pool_message(pool):
    try:
        dex_data = requests.get(
            API_URLS["dexscreener"].format(address=pool["address"])
        ).json().get("pair", {})
        
        return (
            f"⭐️ {pool['base_token']['symbol']} ({pool['links']['dexscreener']})-SOL\n"
            f"🐊 gmgn ({pool['links']['gmgn']})\n\n"
            f"☄️ Meteora ({pool['links']['meteora']})\n"
            f"🦅 Dexscreener ({pool['links']['dexscreener']})\n\n"
            f"😼 Bundles ({pool['links']['bundles']})   "
            f"💼 Smart wallets ({pool['links']['smart_wallets']})\n\n"
            f"🟢 TVL - {dex_data.get('liquidity', {}).get('usd', 'Unknown')}$\n"
            f"🟣 Bin Step - {pool['bin_step']}  "
            f"🟡 Base Fee - {pool['fees']['base']} %\n"
            f"💸️ Fees 5min - {dex_data.get('fees5m', 'Unknown')}$  "
            f"▫️ Trade Volume 5min - {dex_data.get('volume5m', 'Unknown')}$\n"
            f"💵️ Fee 1h - {dex_data.get('fees1h', 'Unknown')}  "
            f"▪️ Trade Volume 1h - {dex_data.get('volume1h', 'Unknown')}\n"
            f"🔸 Fee 24h/TVL - {pool['metrics']['fee24h_tvl']}%  "
            f"🔹 Dynamic 1h Fee/TVL - {pool['metrics']['dynamic_fee1h_tvl']}%\n\n"
            f"{pool['address']}"
        )
    except Exception as e:
        logger.error(f"Error formatting pool: {e}")
        return None

async def fetch_pools(filters):
    try:
        response = requests.get(API_URLS["meteora_pools"], params={
            "min_tvl": filters.get("min_tvl", 0),
            "max_bin_step": filters.get("max_bin_step", 100),
            "token_type": filters.get("token_type", "SOL")
        })
        return response.json()["data"] if response.status_code == 200 else []
    except Exception as e:
        logger.error(f"Error fetching pools: {e}")
        return []

async def pools(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    filters = USER_FILTERS.get(user_id, {})
    
    try:
        pools = await fetch_pools(filters)
        if not pools:
            await update.message.reply_text("No pools found with current filters")
            return

        for pool in pools[:5]:  # Ограничение для предотвращения спама
            message = await format_pool_message(pool)
            if message:
                await update.message.reply_text(message, disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error in pools command: {e}")
        await update.message.reply_text("Error fetching pool data")

async def set_filter(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    args = context.args
    
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /setfilter [parameter] [value]")
        return
    
    param = args[0].lower()
    value = args[1]
    
    try:
        if param == "min_tvl":
            USER_FILTERS.setdefault(user_id, {})["min_tvl"] = float(value)
        elif param == "max_bin_step":
            USER_FILTERS.setdefault(user_id, {})["max_bin_step"] = int(value)
        elif param == "token_type":
            USER_FILTERS.setdefault(user_id, {})["token_type"] = value.upper()
        else:
            await update.message.reply_text("Invalid parameter")
            return
        
        await update.message.reply_text(f"Filter updated: {param} = {value}")
    except ValueError:
        await update.message.reply_text("Invalid value type")

def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pools", pools))
    application.add_handler(CommandHandler("setfilter", set_filter))

    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 8080)),
        webhook_url=WEBHOOK_URL,
        secret_token=SECRET_TOKEN
    )

if __name__ == '__main__':
    main()
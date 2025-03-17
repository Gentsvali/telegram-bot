import os
import logging
from datetime import datetime, timedelta
from quart import Quart, request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler
import httpx
import pytz
from json import JSONDecodeError
import json

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
FILTERS_FILE = "filters.json"

# Конфигурация API Meteora
API_URL = "https://dlmm-api.meteora.ag/pair/all_by_groups"
DEFAULT_FILTERS = {
    "disable_filters": False,
    "stable_coin": "USDC",
    "bin_steps": [20, 80, 100, 125, 250],
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

# Добавление обработчика ошибок
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Глобальная ошибка: {context.error}")
application.add_error_handler(error_handler)

app = Quart(__name__)

@app.before_serving
async def startup():
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    
    # Загружаем фильтры при старте
    await load_filters(application)
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

        await save_filters(update, context)  # Сохраняем фильтры в закрепленное сообщение
        await update.message.reply_text(f"✅ {param} обновлен: {value}")
    
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

# Новый обработчик для JSON-сообщений
async def update_filters_via_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return

    try:
        new_filters = json.loads(update.message.text)
        
        if not isinstance(new_filters, dict):
            raise ValueError("Некорректный формат JSON. Ожидается словарь.")

        for key, value in new_filters.items():
            if key in current_filters:
                current_filters[key] = value
            else:
                logger.warning(f"Неизвестный параметр фильтра: {key}")

        await save_filters(update, context)  # Сохраняем фильтры в закрепленное сообщение
        await update.message.reply_text("✅ Фильтры успешно обновлены!")
        await show_filters(update, context)

    except JSONDecodeError:
        await update.message.reply_text("❌ Ошибка: Некорректный JSON. Проверьте формат.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

# Команда для получения текущих настроек в формате JSON
async def get_filters_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return

    try:
        # Формируем JSON с текущими фильтрами
        filters_json = json.dumps(current_filters, indent=4)
        await update.message.reply_text(f"Текущие настройки фильтров:\n```json\n{filters_json}\n```", parse_mode="Markdown")
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
    if current_filters.get("disable_filters", False):
        return True  # Если фильтрация отключена, возвращаем True для всех пулов

    try:
        # Остальная логика фильтрации
        tvl = float(pool.get("liquidity", 0))
        if tvl <= 0:
            return False

        pool_metrics = {
            "bin_step": pool.get("bin_step", 999),
            "base_fee": float(pool.get("base_fee_percentage", 100)),
            "fee_24h": float(pool.get("fees_24h", 0)),
            "volume_1h": float(pool.get("volume", {}).get("hour_1", 0)),
            "volume_5m": float(pool.get("volume", {}).get("min_30", 0)) * 2,
            "dynamic_fee": float(pool.get("fee_tvl_ratio", {}).get("hour_1", 0)),
            "tvl": tvl
        }

        fee_tvl_ratio = (pool_metrics["fee_24h"] / pool_metrics["tvl"] * 100) if pool_metrics["tvl"] > 0 else 0

        conditions = [
            pool_metrics["bin_step"] in current_filters["bin_steps"],
            pool_metrics["base_fee"] <= current_filters["base_fee_max"],
            fee_tvl_ratio >= current_filters["fee_tvl_ratio_24h_min"],
            pool_metrics["volume_1h"] >= current_filters["volume_1h_min"],
            pool_metrics["volume_5m"] >= current_filters["volume_5m_min"],
            pool_metrics["dynamic_fee"] >= current_filters["dynamic_fee_tvl_ratio_min"],
            pool_metrics["tvl"] >= current_filters["min_tvl"]
        ]

        return all(conditions)
    
    except Exception as e:
        logger.error(f"Filter Error: {str(e)}")
        return False

def get_non_sol_token(mint_x: str, mint_y: str) -> str:
    """Возвращает токен, который не является Solana."""
    sol_mint = "So11111111111111111111111111111111111111112"
    if mint_x == sol_mint:
        return mint_y
    elif mint_y == sol_mint:
        return mint_x
    else:
        return mint_x  # Если оба токена не Solana, возвращаем первый

def save_filters_to_file():
    """Сохраняет текущие фильтры в файл."""
    try:
        with open(FILTERS_FILE, "w") as file:
            json.dump(current_filters, file, indent=4)
        logger.info("Фильтры сохранены в файл ✅")
    except Exception as e:
        logger.error(f"Ошибка при сохранении фильтров в файл: {e}")

def load_filters_from_file():
    """Загружает фильтры из файла."""
    global current_filters
    try:
        if os.path.exists(FILTERS_FILE):
            with open(FILTERS_FILE, "r") as file:
                loaded_filters = json.load(file)
                current_filters.update(loaded_filters)
                logger.info("Фильтры загружены из файла ✅")
        else:
            logger.info("Файл с фильтрами не найден. Использую настройки по умолчанию.")
    except Exception as e:
        logger.error(f"Ошибка при загрузке фильтров из файла: {e}")

def format_pool_message(pool: dict) -> str:
    try:
        # Извлекаем данные из пула
        address = pool.get("address", "N/A")
        mint_x = pool.get("mint_x", "?")
        mint_y = pool.get("mint_y", "?")
        tvl = float(pool.get("liquidity", 0))
        volume_1h = float(pool.get("volume", {}).get("hour_1", 0))
        volume_5m = float(pool.get("volume", {}).get("min_30", 0)) * 2
        fee_tvl_ratio = (float(pool.get("fees_24h", 0)) / tvl * 100) if tvl > 0 else 0
        dynamic_fee = float(pool.get("fee_tvl_ratio", {}).get("hour_1", 0))
        bin_step = pool.get("bin_step", "N/A")
        base_fee = pool.get("base_fee_percentage", "N/A")

        # Определяем токен, который не Solana
        non_sol_token = get_non_sol_token(mint_x, mint_y)

        # Формируем сокращённое название токенов (например, "doug-SOL")
        token_pair = f"{non_sol_token[:4]}-{'SOL' if 'So11111111111111111111111111111111111111112' in [mint_x, mint_y] else 'OTHER'}"

        # Формируем сообщение
        message = (
            "🔥 *Обнаружены пулы с высокой доходностью* 🔥\n\n"
            f"🔥 *{token_pair}* ([🕒 ~5h](https://t.me/meteora_pool_tracker_bot/?start=pool_info={address}_5m)) | "
            f"RugCheck: [🟢1](https://rugcheck.xyz/tokens/{non_sol_token})\n"
            f"🔗 [Meteora](https://app.meteora.ag/dlmm/{address}) | "
            f"[DexScreener](https://dexscreener.com/solana/{non_sol_token}) | "
            f"[GMGN](https://gmgn.ai/sol/token/{non_sol_token}) | "
            f"[TrenchRadar](https://trench.bot/bundles/{non_sol_token}?all=true)\n"
            f"💎 *Market Cap*: ${tvl / 1000:,.1f}K 🔹*TVL*: ${tvl:,.1f}K\n"
            f"📊 *Объем*: ${volume_1h:,.1f}K 🔸 *Bin Step*: {bin_step} 💵 *Fees*: {base_fee}% | {dynamic_fee:.2f}%\n"
            f"🤑 *Принт (5m dynamic fee/TVL)*: {fee_tvl_ratio:.2f}%\n"
            f"🪙 *Токен*: [{non_sol_token}](https://t.me/meteora_pool_tracker_bot/?start=pools={non_sol_token})"
        )
        return message
    except Exception as e:
        logger.error(f"Ошибка форматирования: {str(e)}")
        return "⚠️ Ошибка при формировании информации о пуле"

async def check_new_pools(context: ContextTypes.DEFAULT_TYPE):
    global last_checked_pools
    logger.info("Запуск проверки пулов...")

    try:
        if not context or not hasattr(context, 'bot'):
            logger.error("Некорректный контекст")
            return

        pools = await fetch_pools()
        new_pools = []
        
        for pool in pools:
            if pool["address"] not in last_checked_pools:
                try:
                    if filter_pool(pool):
                        new_pools.append(pool)
                except Exception as e:
                    logger.error(f"Ошибка фильтрации пула {pool.get('address')}: {str(e)}")

        if new_pools:
            logger.info(f"Найдено {len(new_pools)} новых пулов")
            for pool in new_pools:
                message = format_pool_message(pool)
                if message:
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
application.add_handler(CommandHandler("getfiltersjson", get_filters_json))
application.add_handler(MessageHandler(filters=None, callback=update_filters_via_json))

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

@app.route('/')
async def home():
    return {"status": "OK"}, 200

# Функции для работы с закрепленными сообщениями
async def save_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет фильтры в закрепленное сообщение и в файл."""
    try:
        # Удаляем предыдущие закрепленные сообщения
        chat = await context.bot.get_chat(chat_id=USER_ID)
        if chat.pinned_message:
            await context.bot.unpin_chat_message(chat_id=USER_ID, message_id=chat.pinned_message.message_id)

        # Преобразуем фильтры в JSON
        filters_json = json.dumps(current_filters, indent=4)
        
        # Отправляем и закрепляем новое сообщение
        message = await context.bot.send_message(
            chat_id=USER_ID,
            text=f"```json\n{filters_json}\n```",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        await context.bot.pin_chat_message(chat_id=USER_ID, message_id=message.message_id)
        logger.info("Фильтры сохранены в закрепленное сообщение ✅")

        # Сохраняем фильтры в файл
        save_filters_to_file()
    except Exception as e:
        logger.error(f"Ошибка при сохранении фильтров: {e}")
        await update.message.reply_text("❌ Не удалось сохранить настройки!")

async def load_filters(context: ContextTypes.DEFAULT_TYPE):
    """Загружает фильтры из закрепленного сообщения или файла."""
    global current_filters
    try:
        # Пытаемся загрузить из закрепленного сообщения
        chat = await context.bot.get_chat(chat_id=USER_ID)
        if not chat.pinned_message:
            logger.info("Закрепленных сообщений не найдено. Пробую загрузить из файла.")
            load_filters_from_file()
            return

        message = chat.pinned_message
        if "```json" in message.text:
            # Извлекаем JSON из сообщения
            json_text = message.text.split("```json\n")[1].split("\n```")[0]
            loaded_filters = json.loads(json_text)
            current_filters.update(loaded_filters)
            logger.info("Фильтры успешно загружены из закрепленного сообщения ✅")
            
            # Сохраняем фильтры в файл
            save_filters_to_file()
        else:
            logger.info("Закрепленное сообщение не содержит JSON. Пробую загрузить из файла.")
            load_filters_from_file()
    except IndexError:
        logger.error("Ошибка: Закрепленное сообщение не содержит JSON. Пробую загрузить из файла.")
        load_filters_from_file()
    except Exception as e:
        logger.error(f"Ошибка при загрузке фильтров: {e}")
        load_filters_from_file()  # Используем файл как резервный вариант 

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)
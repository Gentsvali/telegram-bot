import os
import logging
import asyncio
import json
import base64
from datetime import datetime, timedelta
from quart import Quart, request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler
import httpx
import pytz
from json import JSONDecodeError
import requests
from solana.rpc.websocket_api import connect
from solders.pubkey import Pubkey

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
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_OWNER = "Gentsvali"
REPO_NAME = "telegram-bot"
FILE_PATH = "filters.json"
USER_ID = int(os.getenv("USER_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 10000))

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
    await load_filters(application)
    asyncio.create_task(track_pools())  # Запуск WebSocket
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

# WebSocket для отслеживания пулов
async def track_pools():
    ws_url = "wss://api.mainnet-beta.solana.com"
    program_id = Pubkey.from_string("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")

    while True:
        try:
            async with connect(ws_url) as websocket:
                await websocket.program_subscribe(program_id, encoding="jsonParsed")
                logger.info("WebSocket подключен к Solana")

                async for response in websocket:
                    try:
                        # Логируем сырые данные
                        logger.info(f"Сырые данные: {response}")
                        
                        # Проверяем, что response — это словарь
                        if isinstance(response, dict):
                            pool_data = response.get("result", {}).get("value", {})
                            await handle_pool_change(pool_data)
                        else:
                            logger.error("Ожидался словарь, получен другой тип данных")
                    except KeyError:
                        logger.error("Ошибка формата данных WebSocket")
                    except Exception as e:
                        logger.error(f"Ошибка обработки данных: {e}")
        except Exception as e:
            logger.error(f"Ошибка WebSocket: {e}. Переподключение через 5 секунд...")
            await asyncio.sleep(5)

# Обработка изменений в пулах
async def handle_pool_change(raw_data):
    try:
        # Если данные — это строка, декодируем их
        if isinstance(raw_data, str):
            pool_data = json.loads(raw_data)
        # Если данные — это байты, декодируем их в строку, а затем в JSON
        elif isinstance(raw_data, bytes):
            pool_data = json.loads(raw_data.decode("utf-8"))
        else:
            pool_data = raw_data  # Если данные уже в правильном формате

        # Проверяем, что pool_data — это словарь
        if isinstance(pool_data, dict):
            if filter_pool(pool_data):
                message = format_pool_message(pool_data)
                await application.bot.send_message(
                    chat_id=USER_ID,
                    text=message,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
        else:
            logger.error("Ожидался словарь, получен другой тип данных")
    except json.JSONDecodeError:
        logger.error("Ошибка декодирования JSON")
    except Exception as e:
        logger.error(f"Ошибка обработки данных: {e}")

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

def load_filters_from_github():
    """Загружает фильтры из GitHub."""
    global current_filters
    try:
        url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        content = response.json()["content"]
        decoded_content = base64.b64decode(content).decode("utf-8")
        loaded_filters = json.loads(decoded_content)
        current_filters.update(loaded_filters)
        logger.info("Фильтры успешно загружены из GitHub ✅")
    except Exception as e:
        logger.error(f"Ошибка при загрузке фильтров из GitHub: {e}")

def save_filters_to_github():
    """Сохраняет фильтры в GitHub."""
    try:
        clean_filters = get_clean_filters()
        content = json.dumps(clean_filters, indent=4)
        encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        data = {
            "message": "Обновление фильтров",
            "content": encoded_content,
            "sha": requests.get(url, headers=headers).json().get("sha", "")
        }
        response = requests.put(url, headers=headers, json=data)
        response.raise_for_status()
        logger.info("Фильтры успешно сохранены в GitHub ✅")
    except Exception as e:
        logger.error(f"Ошибка при сохранении фильтров в GitHub: {e}")

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
        with open(FILE_PATH, "w") as file:
            json.dump(current_filters, file, indent=4)
        logger.info("Фильтры сохранены в файл ✅")
    except Exception as e:
        logger.error(f"Ошибка при сохранении фильтров в файл: {e}")

def load_filters_from_file():
    """Загружает фильтры из файла."""
    global current_filters
    try:
        if os.path.exists(FILE_PATH):
            with open(FILE_PATH, "r") as file:
                loaded_filters = json.load(file)
                current_filters.update(loaded_filters)
                logger.info("Фильтры загружены из файла ✅")
        else:
            logger.info("Файл с фильтрами не найден. Использую настройки по умолчанию.")
    except Exception as e:
        logger.error(f"Ошибка при загрузке фильтров из файла: {e}")

def get_clean_filters() -> dict:
    """Возвращает только те поля, которые относятся к настройкам фильтров."""
    return {
        "disable_filters": current_filters.get("disable_filters", False),
        "stable_coin": current_filters.get("stable_coin", "USDC"),
        "bin_steps": current_filters.get("bin_steps", [20, 80, 100, 125, 250]),
        "min_tvl": current_filters.get("min_tvl", 10000.0),
        "min_fdv": current_filters.get("min_fdv", 500000.0),
        "base_fee_max": current_filters.get("base_fee_max", 1.0),
        "fee_tvl_ratio_24h_min": current_filters.get("fee_tvl_ratio_24h_min", 0.1),
        "volume_1h_min": current_filters.get("volume_1h_min", 5000.0),
        "volume_5m_min": current_filters.get("volume_5m_min", 1000.0),
        "dynamic_fee_tvl_ratio_min": current_filters.get("dynamic_fee_tvl_ratio_min", 0.5),
        "verified_only": current_filters.get("verified_only", True)
    }

def format_pool_message(pool: dict) -> str:
    try:
        address = pool.get("address", "N/A")
        mint_x = pool.get("mint_x", "?")
        mint_y = pool.get("mint_y", "?")
        tvl = float(pool.get("liquidity", 0)) if pool.get("liquidity") else 0
        volume_1h = float(pool.get("volume", {}).get("hour_1", 0)) if pool.get("volume", {}).get("hour_1") else 0
        volume_5m = float(pool.get("volume", {}).get("min_30", 0)) * 2 if pool.get("volume", {}).get("min_30") else 0
        fee_tvl_ratio = (float(pool.get("fees_24h", 0)) / tvl * 100) if tvl > 0 else 0
        dynamic_fee = float(pool.get("fee_tvl_ratio", {}).get("hour_1", 0)) if pool.get("fee_tvl_ratio", {}).get("hour_1") else 0
        bin_step = pool.get("bin_step", "N/A")
        base_fee = pool.get("base_fee_percentage", "N/A")

        # Определяем пару токенов
        sol_mint = "So11111111111111111111111111111111111111112"
        usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

        # Определяем, какой токен выбран в фильтрах (SOL или USDC)
        stable_coin = current_filters.get("stable_coin", "USDC")  # По умолчанию USDC

        if stable_coin == "SOL":
            # Если выбран SOL, ищем токен, который идет в паре с SOL
            if mint_x == sol_mint:
                paired_token = mint_y  # Токен, который идет в паре с SOL
            elif mint_y == sol_mint:
                paired_token = mint_x  # Токен, который идет в паре с SOL
            else:
                paired_token = mint_x  # Если SOL нет в паре, берем первый токен
        else:
            # Если выбран USDC, ищем токен, который идет в паре с USDC
            if mint_x == usdc_mint:
                paired_token = mint_y  # Токен, который идет в паре с USDC
            elif mint_y == usdc_mint:
                paired_token = mint_x  # Токен, который идет в паре с USDC
            else:
                paired_token = mint_x  # Если USDC нет в паре, берем первый токен

        # Формируем сообщение
        message = (
            "🔥 *Обнаружены пулы с высокой доходностью* 🔥\n\n"
            f"🔥 *{paired_token[:4]}-{stable_coin}* ([🕒 ~5h](https://t.me/meteora_pool_tracker_bot/?start=pool_info={address}_5m)) | "
            f"RugCheck: [🟢1](https://rugcheck.xyz/tokens/{paired_token})\n"
            f"🔗 [Meteora](https://app.meteora.ag/dlmm/{address}) | "
            f"[DexScreener](https://dexscreener.com/solana/{paired_token}) | "
            f"[GMGN](https://gmgn.ai/sol/token/{paired_token}) | "
            f"[TrenchRadar](https://trench.bot/bundles/{paired_token}?all=true)\n"
            f"💎 *Market Cap*: ${tvl / 1000:,.2f}K 🔹*TVL*: ${tvl:,.2f}K\n"
            f"📊 *Объем*: ${volume_1h:,.2f}K 🔸 *Bin Step*: {bin_step} 💵 *Fees*: {base_fee}% | {dynamic_fee:.2f}%\n"
            f"🤑 *Принт (5m dynamic fee/TVL)*: {fee_tvl_ratio:.2f}%\n"
            f"🪙 *Токен*: {paired_token}"  # Теперь здесь отображается адрес токена
        )
        return message
    except Exception as e:
        logger.error(f"Ошибка форматирования пула {pool.get('address')}: {str(e)}")
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
    """Сохраняет фильтры в GitHub."""
    try:
        save_filters_to_github()
        logger.info("Фильтры сохранены в GitHub ✅")
    except Exception as e:
        logger.error(f"Ошибка при сохранении фильтров: {e}")
        await update.message.reply_text("❌ Не удалось сохранить настройки!")

async def load_filters(context: ContextTypes.DEFAULT_TYPE):
    """Загружает фильтры из GitHub."""
    global current_filters
    try:
        load_filters_from_github()
        logger.info("Фильтры успешно загружены из GitHub ✅")
    except Exception as e:
        logger.error(f"Ошибка при загрузке фильтров: {e}")

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=PORT)
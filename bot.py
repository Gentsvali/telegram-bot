import os
import logging
import sys
import asyncio
import time
import json
import websockets
import httpx
import signal
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union

# Веб-фреймворк
from quart import Quart, request

# Загрузка переменных окружения
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)

# Solana импорты
from solana.rpc.filter import Memcmp, RpcFilterType
from solana.rpc.config import RpcProgramAccountsConfig
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
import base58
import base64
from solana.rpc.commitment import Confirmed
from base58 import b58encode

# Настройка логгера
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Уменьшаем уровень логирования для сторонних библиотек
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("solana").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# Загрузка переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_OWNER = "Gentsvali"
REPO_NAME = "telegram-bot"
FILE_PATH = "filters.json"
USER_ID = int(os.getenv("USER_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 10000))
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL") 
HELIUS_WS_URL = os.getenv("HELIUS_WS_URL")
  
# Проверка наличия обязательных переменных окружения
required_env_vars = [
    "TELEGRAM_TOKEN", 
    "GITHUB_TOKEN", 
    "USER_ID", 
    "WEBHOOK_URL",
    "HELIUS_WS_URL", 
    "HELIUS_RPC_URL",
    "HELIUS_API_KEY"  # Добавляем новую переменную
]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]

if missing_vars:
    error_message = (
        f"Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}. "
        "Пожалуйста, проверьте настройки."
    )
    logger.error(error_message)
    raise ValueError(error_message)

# Инициализация нескольких RPC клиентов для отказоустойчивости
RPC_ENDPOINTS = [
    HELIUS_RPC_URL,
    "https://api.mainnet-beta.solana.com",
    f"https://api.helius.xyz/v0/transactions/?api-key={os.getenv('HELIUS_API_KEY')}"
]
# Настройки Solana
COMMITMENT = "confirmed"
# Инициализация Solana клиента
solana_clients = [AsyncClient(url, commitment=COMMITMENT) for url in RPC_ENDPOINTS]
solana_client = solana_clients[0]

# Дополнительные настройки
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

# Конфигурация фильтров по умолчанию
DEFAULT_FILTERS = {
    "disable_filters": False,
    "bin_steps": [20, 80, 100, 125, 250],  # Допустимые шаги корзин
    "min_tvl": 10.0,  # Минимальный TVL (в SOL)
    "base_fee_min": 0.1,  # Минимальная базовая комиссия (в %)
    "base_fee_max": 10.0,  # Максимальная базовая комиссия (в %)
    "volume_1h_min": 10.0,  # Минимальный объем за 1 час (в SOL)
    "volume_5m_min": 1.0,  # Минимальный объем за 5 минут (в SOL)
    "fee_tvl_ratio_24h_min": 0.1,  # Минимальное отношение комиссии к TVL за 24 часа (в %)
    "dynamic_fee_tvl_ratio_min": 0.5,  # Минимальное отношение динамической комиссии к TVL (в %)
}

# Константы для работы с DLMM
DLMM_CONFIG = {
    "update_interval": 300,  # 5 минут между обновлениями
    "pool_size": 165,  # Размер данных пула в байтах
    "commitment": "confirmed",  # Уровень подтверждения транзакций
    "retry_delay": 120,  # Задержка перед повторной попыткой при ошибке (в секундах)
}

# Проверка корректности фильтров
def validate_filters(filters: dict) -> bool:
    """
    Проверяет корректность фильтров для DLMM пулов.
    """
    required_keys = [
        "disable_filters",
        "bin_steps",
        "min_tvl",
        "base_fee_min",
        "base_fee_max", 
        "volume_1h_min",
        "volume_5m_min",
        "fee_tvl_ratio_24h_min",
        "dynamic_fee_tvl_ratio_min"
    ]
    return all(key in filters for key in required_keys)

if not validate_filters(DEFAULT_FILTERS):
    raise ValueError("Некорректная конфигурация фильтров по умолчанию.")

# Текущие фильтры
current_filters = DEFAULT_FILTERS.copy()

# Хранение пулов и их состояний
class PoolState:
    def __init__(self):
        self.last_checked_pools = set()  # Множество проверенных пулов
        self.pool_data = {}  # Кэш данных пулов
        self.last_update = {}  # Время последнего обновления

pool_state = PoolState()

# Инициализация приложения Telegram
application = (
    ApplicationBuilder()
    .token(TELEGRAM_TOKEN)
    .concurrent_updates(True)
    .http_version("1.1")
    .get_updates_http_version("1.1")
    .build()
)

async def get_working_client():
    """Получает работающий RPC клиент"""
    for client in solana_clients:
        try:
            await client.get_version()
            return client
        except Exception as e:
            logger.warning(f"RPC клиент недоступен: {e}")
            continue
    return None

async def load_filters(app=None):
    """Загружает фильтры из файла или использует значения по умолчанию"""
    global current_filters
    try:
        # Сначала пробуем загрузить из локального файла
        if os.path.exists(FILE_PATH):
            with open(FILE_PATH, 'r') as f:
                loaded = json.load(f)
                if validate_filters(loaded):  # Проверяем валидность
                    current_filters.update(loaded)
                    logger.info("Фильтры загружены из файла")
                    return
        
        # Если локальный файл не валиден, пробуем загрузить из GitHub
        if GITHUB_TOKEN:
            try:
                url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
                headers = {"Authorization": f"token {GITHUB_TOKEN}"}
                
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, headers=headers)
                    if response.status_code == 200:
                        content = base64.b64decode(response.json()["content"]).decode()
                        loaded = json.loads(content)
                        if validate_filters(loaded):
                            current_filters.update(loaded)
                            # Сохраняем локально для будущих загрузок
                            with open(FILE_PATH, 'w') as f:
                                json.dump(loaded, f, indent=4)
                            logger.info("Фильтры загружены из GitHub")
                            return
            except Exception as github_error:
                logger.warning(f"Ошибка загрузки из GitHub: {github_error}")

        # Если ничего не получилось, используем значения по умолчанию
        current_filters = DEFAULT_FILTERS.copy()
        logger.info("Используются фильтры по умолчанию")
        
    except Exception as e:
        current_filters = DEFAULT_FILTERS.copy()
        logger.error(f"Ошибка загрузки фильтров: {e}. Используются значения по умолчанию")

# Инициализация подключения к Solana
async def init_solana() -> bool:
    """Универсальная проверка подключения к Solana"""
    try:
        client = await get_working_client()
        if not client:
            logger.error("❌ Нет доступных RPC клиентов")
            return False
            
        response = await client.get_version()
        if hasattr(response, 'value'):
            version_info = response.value
            version = getattr(version_info, 'solana_core', None) or getattr(version_info, 'version', 'unknown')
            logger.info(f"✅ Подключено к Solana (v{version})")
            return True
            
        logger.error(f"❌ Неподдерживаемый формат ответа RPC: {type(response)}")
        return False
        
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Solana: {str(e)}")
        return False

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает глобальные ошибки, возникающие в боте.
    """
    try:
        error = context.error
        
        # Специфические ошибки Solana RPC
        if "Rate limit exceeded" in str(error):
            logger.warning("Превышен лимит запросов к Solana RPC")
            message = "⚠️ Превышен лимит запросов. Попробуйте через минуту."
        elif "Connection refused" in str(error):
            logger.error("Ошибка подключения к Solana RPC")
            message = "⚠️ Ошибка подключения к сети. Пробуем восстановить..."
            # Пробуем переподключиться
            await init_solana()
        else:
            # Логируем неизвестную ошибку
            logger.error(f"Произошла ошибка: {error}", exc_info=True)
            message = "⚠️ Произошла ошибка. Пожалуйста, попробуйте позже."

        # Отправляем сообщение об ошибке пользователю
        chat_id = update.effective_chat.id if update and update.effective_chat else USER_ID
        await context.bot.send_message(
            chat_id=chat_id,
            text=message
        )

    except Exception as e:
        logger.error(f"Ошибка в обработчике ошибок: {e}")
        try:
            await context.bot.send_message(
                chat_id=USER_ID,
                text="⚠️ Критическая ошибка в обработчике ошибок"
            )
        except:
            pass

# Регистрируем обработчик ошибок
application.add_error_handler(error_handler)

async def get_pool_accounts():
    try:
        client = await get_working_client()
        if not client:
            raise Exception("Нет доступных RPC клиентов")
            
        program_id = Pubkey.from_string("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")
        
        # Используем структуру фильтров согласно документации [(1)](https://solana.com/docs/rpc/http/getprogramaccounts)
        filters = [
            {
                "dataSize": 165  # Фиксированный размер аккаунта
            }
        ]
        
        response = await client.get_program_accounts(
            program_id,
            encoding="base64",
            filters=filters
        )

        if not response:
            logger.warning("Не получено ни одного аккаунта")
            return None

        return response

    except Exception as e:
        logger.error(f"Ошибка получения аккаунтов: {str(e)}", exc_info=True)
        return None

async def monitor_pools():
    while True:
        try:
            accounts = await get_pool_accounts()
            if accounts:
                for acc in accounts:
                    try:
                        pool_data = decode_pool_data(acc.account.data)
                        if pool_data and filter_pool(pool_data):
                            message = format_pool_message(pool_data)
                            if message:
                                await application.bot.send_message(
                                    chat_id=USER_ID,
                                    text=message,
                                    parse_mode="Markdown"
                                )
                    except Exception as e:
                        logger.debug(f"Пропуск аккаунта из-за ошибки: {e}")
                        continue
                        
            # Увеличиваем интервал между запросами
            await asyncio.sleep(300)  # 5 минут между запросами
            
        except Exception as e:
            logger.error(f"Ошибка мониторинга: {e}", exc_info=True)
            await asyncio.sleep(300)

async def check_connection():
    try:
        response = await solana_client.get_version()
        return True
    except Exception as e:
        logger.error(f"Ошибка проверки подключения: {e}")
        return False

async def handle_helius_ws():
    async with websockets.connect(HELIUS_WS_URL) as websocket:
        subscribe_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "programSubscribe",
            "params": [
                "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
                {
                    "encoding": "base64",
                    "commitment": "confirmed"
                }
            ]
        }
        
        await websocket.send(json.dumps(subscribe_msg))
        
        while True:
            try:
                msg = await websocket.recv()
                data = json.loads(msg)
                
                if "method" in data and data["method"] == "programNotification":
                    await handle_pool_change(data["params"]["result"]["value"])
                    
            except Exception as e:
                logger.error(f"Ошибка WebSocket: {e}")
                await asyncio.sleep(5)
                break

# Инициализация Quart приложения
app = Quart(__name__)

@app.before_serving
async def startup_sequence():
    """Выполняет последовательность запуска с проверками."""
    try:
        # 1. Проверка подключения к Solana
        logger.info("🔌 Проверяем подключение к Solana...")
        try:
            response = await solana_client.get_version()
            logger.info("✅ Подключение к Solana работает")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к Solana: {e}")
            return False

        # 2. Загрузка фильтров
        logger.info("📥 Загрузка фильтров...")
        try:
            await load_filters(None)
            logger.info("✅ Фильтры загружены")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки фильтров: {e}")
            return False

        # 3. Инициализация бота
        logger.info("🤖 Инициализация бота...")
        try:
            await application.initialize()
            await application.start()
            await application.bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
            logger.info("✅ Бот успешно инициализирован")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации бота: {e}")
            return False

        # 4. Запуск WebSocket мониторинга
        asyncio.create_task(monitor_pools())
        logger.info("🔌 мониторинг запущен")

        return True

    except Exception as e:
        logger.error(f"💥 Критическая ошибка при запуске: {e}")
        return False

@app.after_serving
async def shutdown_handler():
    """Корректно завершает все соединения"""
    try:
        # Отписываемся от WebSocket
        if 'websocket' in globals():
            await unsubscribe_websocket(websocket)
            
        # Закрываем Solana клиент
        await solana_client.close()
        
        # Останавливаем бота
        await application.stop()
        await application.shutdown()
        
        logger.info("Все соединения успешно закрыты")
    except Exception as e:
        logger.error(f"Ошибка при завершении работы: {e}")

async def shutdown_signal(signal, loop):
    """
    Обрабатывает сигналы завершения.
    """
    logger.info(f"Получен сигнал {signal.name}. Останавливаю приложение...")
    await solana_client.close()
    await application.stop()
    await application.shutdown()
    loop.stop()

def handle_shutdown(signum, frame):
    """Обработчик сигналов завершения"""
    logger.info(f"Получен сигнал {signum}. Останавливаю приложение...")
    
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            shutdown_task = loop.create_task(shutdown_handler())
            loop.run_until_complete(asyncio.wait_for(shutdown_task, timeout=5))
    except Exception as e:
        logger.error(f"Ошибка при завершении: {e}")
    finally:
        if 'loop' in locals() and not loop.is_closed():
            loop.close()

# Регистрируем обработчики сигналов
signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# Основные обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает команду /start и выводит приветственное сообщение.
    """
    if update.effective_user.id != USER_ID:
        logger.warning(f"Попытка доступа от неавторизованного пользователя: {update.effective_user.id}")
        return

    try:
        await update.message.reply_text(
            "🚀 Мониторинг DLMM пулов Meteora\n"
            "Команды:\n"
            "/filters - текущие настройки\n" 
            "/setfilter - изменить параметры\n"
            "/checkpools - проверить сейчас\n"
        )
        logger.info(f"Пользователь {update.effective_user.id} запустил бота")
    except Exception as e:
        logger.error(f"Ошибка при обработке команды /start: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")

async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает команду /filters и выводит текущие настройки фильтров.
    """
    if update.effective_user.id != USER_ID:
        logger.warning(f"Попытка доступа от неавторизованного пользователя: {update.effective_user.id}")
        return

    try:
        response = (
            "⚙️ Текущие фильтры:\n"
            f"• Bin Steps: {', '.join(map(str, current_filters['bin_steps']))}\n"
            f"• Мин TVL: {current_filters['min_tvl']:,.2f} SOL\n"
            f"• Мин базовая комиссия: {current_filters['base_fee_min']}%\n"
            f"• Макс базовая комиссия: {current_filters['base_fee_max']}%\n"
            f"• Мин объем (1ч): {current_filters['volume_1h_min']:,.2f} SOL\n"
            f"• Мин объем (5м): {current_filters['volume_5m_min']:,.2f} SOL\n"
            f"• Мин комиссия/TVL 24ч: {current_filters['fee_tvl_ratio_24h_min']}%\n"
            f"• Мин динамическая комиссия/TVL: {current_filters['dynamic_fee_tvl_ratio_min']}%"
        )
        await update.message.reply_text(response)
        logger.info(f"Пользователь {update.effective_user.id} запросил текущие фильтры")
    except Exception as e:
        logger.error(f"Ошибка при обработке команды /filters: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")

async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает команду /setfilter и обновляет указанный параметр фильтра.
    """
    if update.effective_user.id != USER_ID:
        logger.warning(f"Попытка доступа от неавторизованного пользователя: {update.effective_user.id}")
        return

    try:
        args = context.args
        if len(args) < 2:
            raise ValueError("Используйте: /setfilter [параметр] [значение]")

        param = args[0].lower()
        value = args[1]

        # Обработка параметров
        if param == "bin_steps":
            current_filters[param] = [int(v.strip()) for v in value.split(',')]
        
        elif param in ["min_tvl", "base_fee_min", "base_fee_max", 
                      "fee_tvl_ratio_24h_min", "volume_1h_min", 
                      "volume_5m_min", "dynamic_fee_tvl_ratio_min"]:
            current_filters[param] = float(value)
        
        else:
            raise ValueError(f"Неизвестный параметр: {param}")

        # Сохраняем фильтры
        await save_filters(update, context)
        await update.message.reply_text(f"✅ {param} обновлен: {value}")
        logger.info(f"Пользователь {update.effective_user.id} обновил параметр {param} на {value}")
    
    except ValueError as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        logger.warning(f"Ошибка при обновлении фильтра: {e}")
    except Exception as e:
        await update.message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")
        logger.error(f"Ошибка при обработке команды /setfilter: {e}", exc_info=True)

async def poll_program_accounts():
    while True:
        try:
            accounts = await get_pool_accounts()
            if accounts:
                for acc in accounts:
                    pool_data = decode_pool_data(acc.account.data)
                    if pool_data and filter_pool(pool_data):
                        message = format_pool_message(pool_data)
                        if message:
                            await application.bot.send_message(
                                chat_id=USER_ID,
                                text=message,
                                parse_mode="Markdown"
                            )
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Ошибка poll_program_accounts: {e}")
            await asyncio.sleep(60)

async def get_pool_data_from_log(log: str) -> Optional[dict]:
    """Извлекает данные пула из лога транзакции"""
    try:
        # Изменяем commitment для более быстрого получения данных
        commitment = "confirmed"  # Используем confirmed вместо finalized для скорости [(1)](https://solana.stackexchange.com/questions/18574/speed-up-websocket-connection)
        
        # Ищем адрес пула в логе
        pool_address = None
        if "Instruction: InitializeMint" in log or "Instruction: MintTo" in log:  # [(2)](https://solana.stackexchange.com/questions/13204/helius-webhook-for-get-real-time-information-about-new-tokens-created)
            # Извлекаем адрес из лога
            parts = log.split()
            for part in parts:
                if len(part) == 44:  # Стандартная длина base58 адреса
                    try:
                        pool_address = Pubkey.from_string(part)
                        break
                    except:
                        continue

        if not pool_address:
            return None

        # Получаем данные аккаунта
        try:
            account_info = await solana_client.get_account_info(
                pool_address,
                commitment=commitment,
                encoding="base64"
            )
            
            if account_info and account_info.value:
                return decode_pool_data(account_info.value.data)
            
        except Exception as e:
            logger.error(f"Ошибка получения данных аккаунта: {e}")
            return None

    except Exception as e:
        logger.error(f"Ошибка обработки лога: {e}")
        return None

def decode_pool_data(data: bytes) -> dict:
    try:
        return {
            "mint_x": base58.b58encode(data[:32]).decode(),
            "mint_y": base58.b58encode(data[32:64]).decode(),
            "liquidity": int.from_bytes(data[64:72], "little"),
            "bin_step": int.from_bytes(data[88:90], "little"),
            "base_fee": int.from_bytes(data[90:92], "little") / 10000,
            "tvl_sol": int.from_bytes(data[64:72], "little") / 1e9
        }
    except Exception as e:
        logger.error(f"Ошибка декодирования данных: {e}")
        return None

async def handle_pool_change(pool_data: bytes):
    """Обработка изменений пула с проверкой структуры данных"""
    required_fields = [
        'address', 'mint_x', 'mint_y', 'liquidity',
        'volume_1h', 'volume_5m', 'bin_step', 'base_fee'
    ]
    
    try:
        # Проверка наличия всех обязательных полей
        if not all(field in pool_data for field in required_fields):
            raise ValueError("Отсутствуют обязательные поля в данных пула")
        
        address = pool_data['address']
        
        # Проверка соответствия фильтрам
        if not filter_pool(pool_data):
            logger.debug(f"Пул {address} не соответствует фильтрам")
            return

        # Форматирование сообщения
        message = format_pool_message(pool_data)
        if not message:
            raise ValueError("Не удалось сформировать сообщение")
        
        # Отправка уведомления
        await application.bot.send_message(
            chat_id=USER_ID,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        
        # Обновление кэша
        pool_state.pool_data[address] = pool_data
        pool_state.last_update[address] = int(time.time())
        
    except Exception as e:
        logger.error(f"Ошибка обработки пула {pool_data.get('address', 'unknown')}: {e}")

async def save_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет фильтры в файл"""
    try:
        with open(FILE_PATH, "w") as f:
            json.dump(current_filters, f, indent=4)
        
        # Если настроен GitHub, пробуем сохранить и туда
        if GITHUB_TOKEN:
            try:
                url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
                headers = {"Authorization": f"token {GITHUB_TOKEN}"}
                
                async with httpx.AsyncClient() as client:
                    # Получаем текущий SHA файла
                    response = await client.get(url, headers=headers)
                    sha = response.json().get("sha") if response.status_code == 200 else None
                    
                    # Отправляем обновление
                    with open(FILE_PATH, "rb") as f:
                        content = base64.b64encode(f.read()).decode()
                    
                    data = {
                        "message": "Automatic filters update",
                        "content": content,
                        "sha": sha
                    }
                    await client.put(url, headers=headers, json=data)
            except Exception as e:
                logger.warning(f"Не удалось сохранить в GitHub: {e}")

        await update.message.reply_text("✅ Фильтры успешно сохранены")
        logger.info(f"Фильтры сохранены пользователем {update.effective_user.id}")
    except Exception as e:
        logger.error(f"Ошибка сохранения фильтров: {e}")
        await update.message.reply_text("❌ Ошибка сохранения фильтров")

async def update_filters_via_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновляет фильтры на основе JSON-сообщения."""
    if update.effective_user.id != USER_ID:
        return

    try:
        # Удаляем команду если есть (на случай /command {json})
        text = update.message.text
        if text.startswith('/'):
            text = ' '.join(text.split()[1:])
        
        new_filters = json.loads(text)
        
        if not validate_filters(new_filters):
            raise ValueError("Некорректная структура фильтров")
        
        # Обновляем только разрешенные ключи
        for key in DEFAULT_FILTERS:
            if key in new_filters:
                current_filters[key] = new_filters[key]
        
        # Сохраняем
        await save_filters(update, context)
        await update.message.reply_text("✅ Фильтры успешно обновлены!")
        logger.info(f"Пользователь {update.effective_user.id} обновил фильтры через JSON")
    
    except json.JSONDecodeError:
        example_filters = json.dumps(DEFAULT_FILTERS, indent=4)
        await update.message.reply_text(
            "❌ Ошибка: Некорректный JSON. Проверьте формат.\n"
            f"Пример корректного JSON:\n```json\n{example_filters}\n```",
            parse_mode="Markdown"
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    except Exception as e:
        await update.message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")
        logger.error(f"Ошибка при обработке JSON-сообщения: {e}", exc_info=True)

async def get_filters_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет текущие настройки фильтров в формате JSON.
    """
    if update.effective_user.id != USER_ID:
        logger.warning(f"Попытка доступа от неавторизованного пользователя: {update.effective_user.id}")
        return

    try:
        # Формируем JSON с текущими фильтрами
        filters_json = json.dumps(current_filters, indent=4)
        
        # Отправляем JSON-сообщение
        await update.message.reply_text(
            f"Текущие настройки фильтров:\n```json\n{filters_json}\n```",
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь {update.effective_user.id} запросил текущие фильтры в формате JSON")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        logger.error(f"Ошибка при обработке команды /getfiltersjson: {e}", exc_info=True)

def load_filters_from_github():
    """
    Загружает фильтры из репозитория GitHub.
    """
    global current_filters
    try:
        # Формируем URL для получения содержимого файла
        url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}

        # Выполняем GET-запрос
        response = requests.get(url, headers=headers)
        if response.status_code == 404:
            logger.warning(f"Файл {FILE_PATH} не найден в репозитории.")
            return
        response.raise_for_status()  # Проверяем статус ответа

        # Декодируем содержимое файла
        content = response.json()["content"]
        decoded_content = base64.b64decode(content).decode("utf-8")
        loaded_filters = json.loads(decoded_content)

        # Обновляем текущие фильтры
        current_filters.update(loaded_filters)
        logger.info("Фильтры успешно загружены из GitHub ✅")
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при загрузке фильтров из GitHub: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка декодирования JSON из GitHub: {e}")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при загрузке фильтров из GitHub: {e}", exc_info=True)

def save_filters_to_github():
    """
    Сохраняет фильтры в репозиторий GitHub.
    """
    try:
        # Получаем текущие фильтры
        clean_filters = get_clean_filters()
        content = json.dumps(clean_filters, indent=4)
        encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        # Формируем URL для обновления файла
        url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}

        # Получаем текущий SHA файла (если он существует)
        sha = ""
        try:
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                sha = response.json().get("sha", "")
        except Exception as e:
            logger.warning(f"Не удалось получить SHA файла: {e}")

        # Формируем данные для PUT-запроса
        data = {
            "message": "Обновление фильтров",
            "content": encoded_content,
            "sha": sha  # SHA требуется для обновления существующего файла
        }

        # Выполняем PUT-запрос
        response = requests.put(url, headers=headers, json=data)
        response.raise_for_status()  # Проверяем статус ответа

        logger.info("Фильтры успешно сохранены в GitHub ✅")
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при сохранении фильтров в GitHub: {e}")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при сохранении фильтров в GitHub: {e}", exc_info=True)

def filter_pool(pool: dict) -> bool:
    """
    Фильтрует DLMM пул на основе текущих фильтров.
    """
    if current_filters.get("disable_filters", False):
        return True

    try:
        # Проверяем основные параметры
        conditions = [
            pool.get("bin_step") in current_filters["bin_steps"],
            pool.get("base_fee", 0) <= current_filters["base_fee_max"],
            pool.get("tvl_sol", 0) >= current_filters["min_tvl"],
            pool.get("volume_1h_sol", 0) >= current_filters["volume_1h_min"],
            pool.get("volume_5m_sol", 0) >= current_filters["volume_5m_min"],
            pool.get("fee_tvl_ratio_24h", 0) >= current_filters["fee_tvl_ratio_24h_min"],
            pool.get("dynamic_fee_tvl_ratio", 0) >= current_filters["dynamic_fee_tvl_ratio_min"],
        ]

        return all(conditions)

    except Exception as e:
        logger.error(f"Ошибка фильтрации пула: {e}", exc_info=True)
        return False

def get_non_sol_token(mint_x: str, mint_y: str) -> str:
    """
    Возвращает токен, который не является Solana из пары токенов DLMM пула.
    
    Args:
        mint_x (str): Адрес первого токена в base58
        mint_y (str): Адрес второго токена в base58
    
    Returns:
        str: Адрес не-SOL токена в base58
    """
    SOL_MINT = "So11111111111111111111111111111111111111112"
    WSOL_MINT = "So11111111111111111111111111111111111111111"  # Wrapped SOL
    
    try:
        # Проверяем оба варианта SOL
        if mint_x in (SOL_MINT, WSOL_MINT):
            return mint_y
        elif mint_y in (SOL_MINT, WSOL_MINT):
            return mint_x
        else:
            return mint_x  # Если оба токена не SOL, возвращаем первый
            
    except Exception as e:
        logger.error(f"Ошибка при определении не-SOL токена: {e}")
        return mint_x

def save_filters_to_file():
    """
    Сохраняет текущие фильтры в файл с дополнительными проверками.
    """
    try:
        # Проверяем наличие директории
        directory = os.path.dirname(FILE_PATH)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)
            
        # Проверяем текущие фильтры перед сохранением
        if not validate_filters(current_filters):
            raise ValueError("Некорректная структура фильтров")
            
        # Получаем очищенные фильтры
        clean_filters = get_clean_filters()
            
        # Сохраняем в файл с форматированием
        with open(FILE_PATH, "w", encoding="utf-8") as file:
            json.dump(clean_filters, file, indent=4, ensure_ascii=False)
            
        logger.info(f"Фильтры успешно сохранены в {FILE_PATH} ✅")
        return True
        
    except ValueError as e:
        logger.error(f"Ошибка валидации фильтров: {e}")
        return False
    except IOError as e:
        logger.error(f"Ошибка ввода/вывода при сохранении фильтров: {e}")
        return False
    except Exception as e:
        logger.error(f"Неожиданная ошибка при сохранении фильтров: {e}", exc_info=True)
        return False

def load_filters_from_file():
    """
    Загружает фильтры из файла с дополнительными проверками и валидацией.
    """
    global current_filters
    try:
        # Проверяем существование файла
        if not os.path.exists(FILE_PATH):
            logger.info(f"Файл с фильтрами не найден по пути {FILE_PATH}. Использую настройки по умолчанию.")
            return False
            
        # Проверяем размер файла
        if os.path.getsize(FILE_PATH) == 0:
            logger.warning("Файл фильтров пуст. Использую настройки по умолчанию.")
            return False
            
        # Загружаем и валидируем фильтры
        with open(FILE_PATH, "r", encoding="utf-8") as file:
            loaded_filters = json.load(file)
            
            # Проверяем структуру загруженных фильтров
            if not validate_filters(loaded_filters):
                logger.error("Загруженные фильтры имеют некорректную структуру")
                return False
                
            # Обновляем только валидные поля
            for key, value in loaded_filters.items():
                if key in DEFAULT_FILTERS:
                    # Дополнительная проверка типов данных
                    if isinstance(value, type(DEFAULT_FILTERS[key])):
                        current_filters[key] = value
                    else:
                        logger.warning(f"Пропущено поле {key}: несоответствие типа данных")
                        
            logger.info(f"Фильтры успешно загружены из {FILE_PATH} ✅")
            return True
            
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка декодирования JSON: {e}")
        return False
    except IOError as e:
        logger.error(f"Ошибка чтения файла: {e}")
        return False
    except Exception as e:
        logger.error(f"Неожиданная ошибка при загрузке фильтров: {e}", exc_info=True)
        return False

def get_clean_filters() -> dict:
    """
    Возвращает очищенный словарь с настройками фильтров, проверяя типы данных и границы значений.
    
    Returns:
        dict: Словарь с валидными настройками фильтров
    """
    # Определяем ограничения для числовых значений
    NUMERIC_BOUNDS = {
        "min_tvl": (0.0, 1000000.0),
        "min_fdv": (0.0, 10000000.0),
        "base_fee_max": (0.0, 100.0),
        "fee_tvl_ratio_24h_min": (0.0, 100.0),
        "volume_1h_min": (0.0, 1000000.0),
        "volume_5m_min": (0.0, 1000000.0),
        "dynamic_fee_tvl_ratio_min": (0.0, 100.0),
        "min_listing_time": (0, 365),
        "price_change_1h_min": (-100.0, 100.0),
        "price_change_5m_min": (-100.0, 100.0),
        "fee_change_1h_min": (-100.0, 100.0),
        "fee_change_5m_min": (-100.0, 100.0),
    }

    clean_filters = {}
    
    # Обработка булевых значений
    clean_filters["disable_filters"] = bool(current_filters.get("disable_filters", False))
    clean_filters["verified_only"] = bool(current_filters.get("verified_only", True))
    
    # Обработка строковых значений
    clean_filters["stable_coin"] = str(current_filters.get("stable_coin", "USDC"))
    
    # Обработка списка bin_steps
    bin_steps = current_filters.get("bin_steps", [20, 80, 100, 125, 250])
    if isinstance(bin_steps, list):
        clean_filters["bin_steps"] = [
            step for step in bin_steps 
            if isinstance(step, (int, float)) and 1 <= step <= 1000
        ]
    else:
        clean_filters["bin_steps"] = [20, 80, 100, 125, 250]

    # Обработка числовых значений с проверкой границ
    for key, (min_val, max_val) in NUMERIC_BOUNDS.items():
        value = current_filters.get(key, DEFAULT_FILTERS.get(key, 0.0))
        try:
            value = float(value)
            clean_filters[key] = max(min_val, min(value, max_val))
        except (TypeError, ValueError):
            clean_filters[key] = DEFAULT_FILTERS.get(key, 0.0)
            logger.warning(f"Некорректное значение для {key}, использую значение по умолчанию")

    return clean_filters

def format_pool_message(pool: dict) -> str:
    """
    Форматирует информацию о пуле в сообщение для Telegram с улучшенной обработкой ошибок и форматированием.
    
    Args:
        pool (dict): Словарь с данными пула
        
    Returns:
        str: Отформатированное сообщение
    """
    try:
        # Проверка обязательных полей
        required_fields = ["address", "mint_x", "mint_y", "liquidity", 
                         "volume_1h", "volume_5m", "bin_step", "base_fee"]
        if not all(field in pool for field in required_fields):
            raise ValueError("Отсутствуют обязательные поля в данных пула")

        # Получение и валидация значений
        values = {
            'address': str(pool.get("address", "N/A")),
            'mint_x': str(pool.get("mint_x", "?")),
            'mint_y': str(pool.get("mint_y", "?")),
            'tvl': max(0.0, float(pool.get("liquidity", 0)) / 1e9),
            'volume_1h': max(0.0, float(pool.get("volume_1h", 0)) / 1e9),
            'volume_5m': max(0.0, float(pool.get("volume_5m", 0)) / 1e9),
            'bin_step': max(0, int(pool.get("bin_step", 0))),
            'base_fee': max(0.0, float(pool.get("base_fee", 0))),
            'price_change_1h': float(pool.get("price_change_1h", 0)),
            'price_change_5m': float(pool.get("price_change_5m", 0)),
            'fee_change_1h': float(pool.get("fee_change_1h", 0)),
            'fee_change_5m': float(pool.get("fee_change_5m", 0))
        }

        # Формируем сообщение с улучшенным форматированием
        return (
            f"⭐️ {values['mint_x'][:4]}-{values['mint_y'][:4]} (https://dexscreener.com/solana/{values['address']})\n"
            f"☄️ Метеоры (https://edge.meteora.ag/dlmm/{values['address']})\n"
            f"😼 Наборы (https://trench.bot/bundles/{values['mint_x']}?all=true)\n"
            f"🟢 ТВЛ - {values['tvl']:,.2f} SOL\n"
            f"📊 Объем (1ч) - {values['volume_1h']:,.2f} SOL\n"
            f"📊 Объем (5м) - {values['volume_5m']:,.2f} SOL\n"
            f"⚙️ Шаг корзины - {values['bin_step']}\n"
            f"💸 Базовая комиссия - {values['base_fee']:.2f}%\n"
            f"📈 Изменение цены (1ч) - {values['price_change_1h']:.2f}%\n"
            f"📈 Изменение цены (5м) - {values['price_change_5m']:.2f}%\n"
            f"📊 Изменение комиссии (1ч) - {values['fee_change_1h']:.2f}%\n"
            f"📊 Изменение комиссии (5м) - {values['fee_change_5m']:.2f}%"
        )

    except (ValueError, TypeError) as e:
        logger.error(f"Ошибка валидации данных пула {pool.get('address', 'N/A')}: {e}")
        return None
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при форматировании пула {pool.get('address', 'N/A')}: {e}", exc_info=True)
        return None

def setup_command_handlers(application: ApplicationBuilder):
    """
    Настраивает обработчики команд для бота с группировкой по функциональности.
    """
    try:
        # Основные команды
        application.add_handler(
            CommandHandler(
                "start", 
                start,
                filters=filters.User(user_id=USER_ID)
            )
        )

        # Команды управления фильтрами
        filter_handlers = [
            CommandHandler(
                "filters", 
                show_filters,
                filters=filters.User(user_id=USER_ID)
            ),
            CommandHandler(
                "setfilter", 
                set_filter,
                filters=filters.User(user_id=USER_ID)
            ),
            CommandHandler(
                "getfiltersjson", 
                get_filters_json,
                filters=filters.User(user_id=USER_ID)
            ),
            MessageHandler(
                filters=filters.User(user_id=USER_ID) & filters.TEXT & ~filters.COMMAND,
                callback=update_filters_via_json
            )
        ]
        for handler in filter_handlers:
            application.add_handler(handler)

        logger.info("✅ Обработчики команд успешно зарегистрированы")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при настройке обработчиков команд: {e}", exc_info=True)
        raise

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает неизвестные команды.
    """
    if update.effective_user.id != USER_ID:
        return

    await update.message.reply_text(
        "❌ Неизвестная команда. Доступные команды:\n"
        "/start - начать работу с ботом\n"
        "/filters - показать текущие фильтры\n"
        "/setfilter - установить фильтр\n"
        "/getfiltersjson - получить фильтры в JSON\n"
    )

# Инициализация обработчиков
setup_command_handlers(application)

# Конфигурация веб-хуков и маршрутов
class WebhookConfig:
    """Конфигурация для веб-хуков и маршрутов"""
    WEBHOOK_TIMEOUT = 30  # Тайм-аут для веб-хука в секундах
    MAX_RETRIES = 3      # Максимальное количество попыток
    RETRY_DELAY = 1      # Задержка между попытками в секундах

# Вебхук с улучшенной обработкой ошибок
@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
async def webhook():
    """Обрабатывает входящие запросы от Telegram через вебхук с расширенной валидацией."""
    try:
        # Проверка заголовков
        if not request.is_json:
            logger.error("Получен не JSON запрос")
            return {'error': 'Content-Type должен быть application/json'}, 400

        # Получение и валидация данных
        data = await request.get_json()
        if not data:
            logger.error("Получен пустой JSON")
            return {'error': 'Пустой JSON запрос'}, 400

        # Проверка критических полей
        if 'update_id' not in data:
            logger.error("Отсутствует update_id в запросе")
            return {'error': 'Некорректный формат данных'}, 400

        # Обработка обновления с повторными попытками
        for attempt in range(WebhookConfig.MAX_RETRIES):
            try:
                update = Update.de_json(data, application.bot)
                await asyncio.wait_for(
                    application.process_update(update),
                    timeout=WebhookConfig.WEBHOOK_TIMEOUT
                )
                return '', 200
            except asyncio.TimeoutError:
                if attempt == WebhookConfig.MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(WebhookConfig.RETRY_DELAY)
                continue

    except asyncio.TimeoutError:
        logger.error("Таймаут обработки вебхука")
        return {'error': 'Timeout'}, 504
    except Exception as e:
        logger.error(f"Ошибка в вебхуке: {e}", exc_info=True)
        return {'error': 'Internal server error'}, 500

# Расширенный healthcheck
@app.route('/healthcheck')
async def healthcheck():
    """Расширенная проверка состояния сервиса."""
    try:
        health_status = {
            "status": "ERROR",
            "components": {
                "telegram_bot": False,
                "solana_connection": False,
                "webhook": False
            },
            "timestamp": datetime.utcnow().isoformat()
        }

        # Проверка бота
        if application.running:
            health_status["components"]["telegram_bot"] = True

        # Проверка подключения к Solana
        try:
            await asyncio.wait_for(check_connection(), timeout=5)
            health_status["components"]["solana_connection"] = True
        except Exception as e:
            logger.warning(f"Ошибка проверки подключения к Solana: {e}")

        # Проверка вебхука
        try:
            webhook_info = await application.bot.get_webhook_info()
            health_status["components"]["webhook"] = bool(webhook_info.url)
        except Exception as e:
            logger.warning(f"Ошибка проверки вебхука: {e}")

        # Общий статус
        if all(health_status["components"].values()):
            health_status["status"] = "OK"
            return health_status, 200
        return health_status, 503

    except Exception as e:
        logger.error(f"Ошибка в healthcheck: {e}", exc_info=True)
        return {
            "status": "ERROR",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }, 500

@app.route('/test-solana')
async def test_solana():
    connected = await init_solana()
    return {"solana_connected": connected}, 200

# Главная страница с расширенной информацией
@app.route('/')
async def home():
    """Возвращает расширенную информацию о сервисе."""
    try:
        return {
            "status": "OK",
            "version": "1.0.0",
            "name": "Meteora Pool Monitor",
            "description": "Telegram Bot для отслеживания пулов Meteora на Solana",
            "endpoints": {
                "healthcheck": "/healthcheck",
                "webhook": f"/{TELEGRAM_TOKEN}"
            },
            "documentation": "https://github.com/yourusername/yourrepo",
            "timestamp": datetime.utcnow().isoformat()
        }, 200
    except Exception as e:
        logger.error(f"Ошибка на главной странице: {e}", exc_info=True)
        return {"status": "ERROR", "error": str(e)}, 500

# Улучшенный запуск приложения
async def startup_sequence():
    """Выполняет последовательность запуска с проверками."""
    try:
        # 1. Проверка подключения к Solana
        logger.info("🔌 Проверяем подключение к Solana...")
        try:
            await asyncio.wait_for(check_connection(), timeout=10)
            logger.info("✅ Подключение к Solana работает")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к Solana: {e}")
            return False

        # 2. Загрузка фильтров
        logger.info("📥 Загрузка фильтров...")
        try:
            await load_filters(None)
            logger.info("✅ Фильтры загружены")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки фильтров: {e}")
            return False

        # 3. Инициализация бота
        logger.info("🤖 Инициализация бота...")
        try:
            await application.initialize()
            await application.start()
            await application.bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
            logger.info("✅ Бот успешно инициализирован")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации бота: {e}")
            return False

        return True

    except Exception as e:
        logger.error(f"💥 Критическая ошибка при запуске: {e}")
        return False

if __name__ == "__main__":
    try:
        if asyncio.run(startup_sequence()):
            logger.info(f"🚀 Запускаем мониторинг пулов...")
            asyncio.run(monitor_pools())
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        sys.exit(1)
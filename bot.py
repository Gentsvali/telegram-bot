import os
import logging
import asyncio
import aiohttp
import json
import signal
from datetime import datetime
from typing import Dict, Optional

from quart import Quart, request

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    filters
)

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solana.rpc.commitment import Confirmed
from solana.rpc.types import MemcmpOpts
import base58

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log")  # Добавьте запись в файл
    ]
)

logger = logging.getLogger(__name__)

logging.getLogger("asyncio").setLevel(logging.WARNING)

# Загрузка переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
USER_ID = int(os.getenv("USER_ID"))
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")

# Проверка обязательных переменных
required_env_vars = ["TELEGRAM_TOKEN", "USER_ID", "HELIUS_RPC_URL"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]

if missing_vars:
    error_message = f"Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}"
    logger.error(error_message)
    raise ValueError(error_message)

# Инициализация нескольких RPC клиентов для отказоустойчивости
RPC_ENDPOINTS = [
    HELIUS_RPC_URL,
    "https://api.mainnet-beta.solana.com",
    f"https://api.helius.xyz/v0/transactions/?api-key={os.getenv('HELIUS_API_KEY')}"
]
# Настройки Solana
COMMITMENT = Confirmed
METEORA_PROGRAM_ID = Pubkey.from_string("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")
known_pools = set()

# Инициализация Solana клиента
solana_client = AsyncClient("https://api.mainnet-beta.solana.com", Confirmed)

# Дополнительные настройки
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

# Конфигурация фильтров
DEFAULT_FILTERS = {
    "bin_steps": [20, 80, 100, 125, 250],  # Допустимые шаги корзин
    "min_tvl": 10.0,  # Минимальный TVL (в SOL)
    "base_fee_max": 10.0,  # Максимальная базовая комиссия (в %)
    "volume_1h_min": 10.0,  # Минимальный объем за 1 час (в SOL)
    "volume_5m_min": 5.0  # Минимальный объем за 5 минут (в SOL)
}

current_filters = DEFAULT_FILTERS.copy()

# Инициализация приложения Telegram
application = (
    ApplicationBuilder()
    .token(TELEGRAM_TOKEN)
    .concurrent_updates(True)
    .build()
)

# Базовые настройки
FILE_PATH = "filters.json"  # Путь к файлу с фильтрами

def validate_filters(filters: dict) -> bool:
    """
    Проверяет корректность структуры фильтров
    
    Args:
        filters (dict): Словарь с фильтрами для проверки
        
    Returns:
        bool: True если фильтры валидны, False если нет
    """
    try:
        required_fields = {
            "bin_steps": list,
            "min_tvl": (int, float),
            "base_fee_max": (int, float),
            "volume_1h_min": (int, float),
            "volume_5m_min": (int, float)
        }
        
        # Проверяем наличие всех полей
        if not all(field in filters for field in required_fields):
            logger.error("Отсутствуют обязательные поля в фильтрах")
            return False
            
        # Проверяем типы данных
        for field, expected_type in required_fields.items():
            if not isinstance(filters[field], expected_type):
                logger.error(f"Неверный тип данных для поля {field}")
                return False
                
        # Проверяем значения
        if not all(isinstance(step, (int, float)) for step in filters["bin_steps"]):
            logger.error("Неверный формат bin_steps")
            return False
            
        # Проверяем что числовые значения положительные
        numeric_fields = ["min_tvl", "base_fee_max", "volume_1h_min", "volume_5m_min"]
        if not all(filters[field] >= 0 for field in numeric_fields):
            logger.error("Отрицательные значения в фильтрах")
            return False
            
        return True
        
    except Exception as e:
        logger.error(f"Ошибка валидации фильтров: {e}")
        return False

async def load_filters():
    """Загружает фильтры из файла или использует значения по умолчанию"""
    global current_filters
    try:
        if os.path.exists(FILE_PATH):
            with open(FILE_PATH, 'r') as f:
                loaded = json.load(f)
                if validate_filters(loaded):
                    current_filters.update(loaded)
                    logger.info("Фильтры загружены из файла")
                    return
        
        # Если не удалось загрузить, используем значения по умолчанию
        current_filters = DEFAULT_FILTERS.copy()
        logger.info("Используются фильтры по умолчанию")
        
    except Exception as e:
        current_filters = DEFAULT_FILTERS.copy()
        logger.error(f"Ошибка загрузки фильтров: {e}")

async def init_solana() -> bool:
    """Проверка подключения к Solana"""
    try:
        response = await solana_client.get_version()
        if response.value:
            logger.info("✅ Подключение к Solana работает")
            return True
            
        logger.error("❌ Неподдерживаемый формат ответа RPC")
        return False
        
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Solana: {str(e)}")
        return False

async def get_pool_accounts():
    try:
        # Правильная структура фильтров согласно документации
        filters = [
            {"dataSize": 752}  # Размер данных DLMM пула
        ]

        response = await solana_client.get_program_accounts(
             METEORA_PROGRAM_ID,
             encoding="base64",
             commitment="confirmed",
             filters=filters
        )
        
        return response.value if response else None
        
    except Exception as e:
        logger.error(f"Ошибка получения аккаунтов: {e}")
        return None

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает основные ошибки бота"""
    try:
        # Получаем ошибку
        error = context.error
        
        # Формируем сообщение об ошибке
        if "Rate limit exceeded" in str(error):
            message = "⚠️ Превышен лимит запросов. Попробуйте через минуту."
        elif "Connection refused" in str(error):
            message = "⚠️ Ошибка подключения к сети. Пробуем восстановить..."
            # Пробуем переподключиться
            await init_solana()
        else:
            # Логируем неизвестную ошибку
            logger.error(f"Ошибка: {error}")
            message = "⚠️ Произошла ошибка. Попробуйте позже."

        # Отправляем сообщение
        chat_id = update.effective_chat.id if update and update.effective_chat else USER_ID
        await context.bot.send_message(
            chat_id=chat_id,
            text=message
        )

    except Exception as e:
        logger.error(f"Ошибка в обработчике ошибок: {e}")

# Регистрируем обработчик ошибок
application.add_error_handler(error_handler)

async def monitor_pools():
    """Мониторинг пулов с защитой от дублирования"""
    global known_pools
    
    try:
        logger.info("🔄 Запуск мониторинга DLMM пулов...")
        while True:
            try:
                pools = await fetch_dlmm_pools()
                new_pools = [p for p in pools if p["id"] not in known_pools]
                
                if not new_pools:
                    logger.debug("🔄 Новых пулов не обнаружено")
                    await asyncio.sleep(300)
                    continue
                
                logger.info(f"🆕 Найдено {len(new_pools)} новых пулов")
                for pool in new_pools:
                    try:
                        pool_data = await parse_pool_data(pool)
                        if pool_data and filter_pool(pool_data):
                            known_pools.add(pool["id"])
                            await send_pool_notification(pool_data)
                    except Exception as e:
                        logger.error(f"⚠️ Ошибка обработки пула: {str(e)}")
                
                await asyncio.sleep(300)
                
            except asyncio.CancelledError:
                logger.info("🛑 Мониторинг остановлен по запросу")
                break
            except Exception as e:
                logger.error(f"🔴 Ошибка в цикле мониторинга: {str(e)}")
                await asyncio.sleep(60)
                
    finally:
        logger.info("📴 Мониторинг завершил работу")

async def fetch_dlmm_pools():
    """Корректный запрос пулов DLMM"""
    try:
        logger.info("🔍 Ищем активные DLMM пулы...")
        
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getProgramAccounts", 
            "params": [
                str(METEORA_PROGRAM_ID),
                {
                    "encoding": "base64",  # Используем base64 кодирование [(1)](https://solana.com/developers/guides/javascript/get-program-accounts)
                    "commitment": "confirmed",  # Используем confirmed для быстрого ответа [(2)](https://solana.com/docs/rpc)
                    "filters": [
                        {
                            "dataSize": 752
                        }
                    ]
                }
            ]
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(HELIUS_RPC_URL, json=payload, timeout=30) as resp:  # Добавляем timeout
                if resp.status != 200:
                    logger.error(f"HTTP Error: {resp.status}")
                    return []
                    
                data = await resp.json()
                if "error" in data:
                    logger.error(f"API Error: {data['error']}")
                    return []
                    
                results = data.get("result", [])
                logger.info(f"Найдено {len(results)} пулов")
                return results

    except asyncio.TimeoutError:
        logger.error("Timeout при запросе к API")
        return []
    except Exception as e:
        logger.error(f"Request failed: {str(e)}")
        return []

async def parse_pool_data(pool: dict) -> Optional[dict]:
    """Извлекает ключевые данные из структуры пула"""
    if not isinstance(pool, dict):
        logger.error("Некорректные данные пула: ожидался словарь")
        return None
    try:
        # Основные данные
        metadata = pool.get("content", {}).get("metadata", {})
        return {
            "id": pool["id"],
            "name": metadata.get("name"),
            "symbol": metadata.get("symbol"),
            "tvl": float(metadata.get("tvl", 0)),
            "fee_rate": float(metadata.get("fee_rate", 0)),
            "volume_24h": float(metadata.get("volume_24h", 0)),
            "mint_x": next((a["mint"] for a in pool.get("token_accounts", []) if a.get("type") == "token_x"), ""),
            "mint_y": next((a["mint"] for a in pool.get("token_accounts", []) if a.get("type") == "token_y"), "")
        }
    except Exception as e:
        logger.error(f"Ошибка парсинга пула: {e}")
        return None

async def send_pool_notification(pool: dict):
    """Отправляет сообщение о новом пуле"""
    try:
        message = (
            f"🚀 *Новый DLMM Pool*: {pool['name']} ({pool['symbol']})\n"
            f"• Адрес: `{pool['id']}`\n"
            f"• TVL: {pool['tvl']:.2f} SOL\n"
            f"• Комиссия: {pool['fee_rate']:.2f}%\n"
            f"• Объем (24ч): {pool['volume_24h']:.2f} SOL\n"
            f"• [Meteora](https://app.meteora.ag/pool/{pool['id']}) | "
            f"[DexScreener](https://dexscreener.com/solana/{pool['id']})"
        )
        
        await application.bot.send_message(
            chat_id=USER_ID,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

def filter_pool(pool: dict) -> bool:
    """Применяет пользовательские фильтры"""
    try:
        return all([
            pool["tvl"] >= current_filters["min_tvl"],
            pool["fee_rate"] <= current_filters["base_fee_max"],
            pool["volume_24h"] >= current_filters["volume_1h_min"] / 24  # Конвертация 1ч -> 24ч
        ])
    except Exception as e:
        logger.error(f"Ошибка фильтрации: {e}")
        return False

# Инициализация Quart приложения
app = Quart(__name__)

@app.before_serving
async def startup_sequence():
    """Выполняет последовательность запуска."""
    try:
        # 1. Проверка подключения к Solana
        logger.info("🔌 Проверяем подключение к Solana...")
        if not await init_solana():
            return False

        # 2. Загрузка фильтров
        logger.info("📥 Загрузка фильтров...")
        try:
            if not os.path.exists(FILE_PATH):
                logger.info("Используем фильтры по умолчанию")
            else:
                await load_filters()
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки фильтров: {e}")
            return False
        
        # 3. Инициализация бота
        logger.info("🤖 Инициализация бота...")
        await application.initialize()
        await application.start()
        logger.info("✅ Бот успешно инициализирован")

        # 4. Запуск мониторинга 
        asyncio.create_task(monitor_pools())
        logger.info("DLMM Pool Monitor запущен через DAS API")
        return True

    except Exception as e:
        logger.error(f"💥 Критическая ошибка при запуске: {e}")
        return False

@app.after_serving
async def shutdown_handler():
    """Корректное завершение работы"""
    try:
        logger.info("🛑 Завершение работы...")
        
        # 1. Отменяем задачу мониторинга
        tasks = [t for t in asyncio.all_tasks() 
                if t is not asyncio.current_task()]
                
        # 2. Даем время на корректное завершение
        if tasks:
            await asyncio.wait(tasks, timeout=5.0)
            
        # 3. Принудительно отменяем незавершенные задачи
        for task in tasks:
            if not task.done():
                task.cancel()
                
        # 4. Останавливаем бота
        if application.running:
            await application.stop()
            await application.shutdown()
            
        # 5. Закрываем соединения Solana
        await solana_client.close()
        
        logger.info("✅ Система корректно остановлена")
        
    except Exception as e:
        logger.error(f"⚠️ Ошибка при остановке: {str(e)}")

async def shutdown_signal(signal, loop):
    """
    Обрабатывает сигналы завершения.
    """
    logger.info(f"Получен сигнал {signal.name}. Останавливаю приложение...")
    
    try:
        # Закрываем все соединения
        await solana_client.close()
        
        # Останавливаем бота
        if application.running:
            await application.stop()
            await application.shutdown()
            
        # Останавливаем loop
        if not loop.is_closed():
            loop.stop()
            
    except Exception as e:
        logger.error(f"Ошибка при завершении работы: {e}")
        
    finally:
        # Убеждаемся что loop остановлен
        if not loop.is_closed():
            loop.stop()

def handle_shutdown(signum, frame):
    """
    Обработчик сигналов завершения с корректным закрытием соединений.
    """
    logger.info(f"Получен сигнал {signum}. Останавливаю приложение...")
    
    try:
        # Получаем текущий event loop
        loop = asyncio.get_event_loop()
        
        if loop.is_running():
            # Создаем и запускаем задачу завершения
            shutdown_task = loop.create_task(shutdown_handler())
            
            # Ждем завершения с таймаутом
            try:
                loop.run_until_complete(
                    asyncio.wait_for(shutdown_task, timeout=10.0)
                )
            except asyncio.TimeoutError:
                logger.error("Превышено время ожидания завершения")
            finally:
                # Отменяем незавершенные задачи
                for task in asyncio.all_tasks(loop):
                    task.cancel()
                
                # Запускаем loop еще раз чтобы обработать отмену
                loop.run_until_complete(loop.shutdown_asyncgens())
                
                # Останавливаем loop
                loop.stop()
                loop.close()
                
    except Exception as e:
        logger.error(f"Ошибка при завершении работы: {e}")
        # В случае критической ошибки - принудительно завершаем процесс
        sys.exit(1)

# Регистрация обработчиков сигналов остается как есть, это стандартный Python код
signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик команды /start с улучшенной проверкой авторизации и обработкой ошибок.
    """
    if update.effective_user.id != USER_ID:
        logger.warning(f"Попытка доступа от неавторизованного пользователя: {update.effective_user.id}")
        return

    try:
        welcome_message = (
            "🚀 Мониторинг DLMM пулов Meteora\n\n"
            "Доступные команды:\n"
            "/filters - текущие настройки фильтров\n" 
            "/setfilter - изменить параметры фильтров\n"
            "/checkpools - проверить пулы сейчас\n"
        )
        
        await update.message.reply_text(welcome_message)
        logger.info(f"Пользователь {update.effective_user.id} запустил бота")
        
    except Exception as e:
        logger.error(f"Ошибка при обработке команды /start: {e}", exc_info=True)
        await update.message.reply_text(
            "⚠️ Произошла ошибка при запуске. Пожалуйста, попробуйте позже."
        )

async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает текущие настройки фильтров.
    """
    if update.effective_user.id != USER_ID:
        return

    try:
        response = (
            "⚙️ Текущие фильтры:\n"
            f"• Bin Steps: {', '.join(map(str, current_filters['bin_steps']))}\n"
            f"• Мин TVL: {current_filters['min_tvl']:,.2f} SOL\n"
            f"• Макс базовая комиссия: {current_filters['base_fee_max']}%\n"
            f"• Мин объем (1ч): {current_filters['volume_1h_min']:,.2f} SOL\n"
            f"• Мин объем (5м): {current_filters['volume_5m_min']:,.2f} SOL"
        )
        await update.message.reply_text(response)
        
    except Exception as e:
        await update.message.reply_text("⚠️ Произошла ошибка при отображении фильтров")
        logger.error(f"Ошибка show_filters: {e}")

async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обновляет указанный параметр фильтра.
    """
    if update.effective_user.id != USER_ID:
        return

    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "Использование: /setfilter <параметр> <значение>\n"
                "Параметры:\n"
                "• bin_steps - список (пример: 20,80,100)\n"
                "• min_tvl - минимальный TVL в SOL\n"
                "• base_fee_max - максимальная базовая комиссия в %\n"
                "• volume_1h_min - минимальный объем за 1ч в SOL\n"
                "• volume_5m_min - минимальный объем за 5м в SOL"
            )
            return

        param = context.args[0].lower()
        value = context.args[1]

        # Валидация параметров
        valid_params = {
            "bin_steps": lambda x: [int(v) for v in x.split(',')],
            "min_tvl": float,
            "base_fee_max": float,
            "volume_1h_min": float,
            "volume_5m_min": float
        }

        if param not in valid_params:
            await update.message.reply_text(f"❌ Неизвестный параметр: {param}")
            return

        try:
            # Конвертация значения
            converted_value = valid_params[param](value)
            current_filters[param] = converted_value
            
            # Сохраняем изменения
            with open(FILE_PATH, "w") as f:
                json.dump(current_filters, f, indent=4)
                
            await update.message.reply_text(f"✅ {param} обновлен: {converted_value}")
            
        except (ValueError, TypeError) as e:
            await update.message.reply_text(f"❌ Некорректное значение для {param}")
            
    except Exception as e:
        logger.error(f"Ошибка set_filter: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка при обновлении фильтра")

async def poll_program_accounts():
    """
    Опрашивает аккаунты программы с оптимизированными фильтрами
    """
    try:
        while True:
            try:
                # Создаем фильтры для getProgramAccounts
                filters = [
                    {
                        "dataSize": 752  # Размер данных DLMM пула
                    }
                ]
                
                # Получаем аккаунты с фильтрами
                accounts = await solana_client.get_program_accounts(
                    pubkey=METEORA_PROGRAM_ID,
                    filters=filters,
                    encoding="base64",
                    commitment="confirmed"
                )

                if accounts:
                    for acc in accounts:
                        pool_data = decode_pool_data(acc.account.data)
                        if pool_data and filter_pool(pool_data):
                            message = format_pool_message(pool_data)
                            if message:
                                await application.bot.send_message(
                                    chat_id=USER_ID,
                                    text=message,
                                    parse_mode="Markdown",
                                    disable_web_page_preview=True
                                )
                            
                await asyncio.sleep(60)  # Проверяем раз в минуту
                
            except Exception as e:
                logger.error(f"Ошибка poll_program_accounts: {e}")
                await asyncio.sleep(60)  # Ждем минуту при ошибке
                
    except asyncio.CancelledError:
        logger.info("Мониторинг остановлен")

async def get_pool_data_from_log(log: str) -> Optional[dict]:
    """
    Извлекает данные пула из лога транзакции
    """
    try:
        # Используем commitment level "confirmed" для быстрого получения данных
        commitment = "confirmed" 

        # Ищем адрес пула в логе
        pool_address = None
        # Проверяем сигнатуры для адреса
        if pool_address:
            try:
                account_info = await solana_client.get_account_info(
                    pubkey=pool_address,
                    commitment=commitment,
                    encoding="base64"  # Используем base64 кодирование [(1)](https://solana.com/developers/guides/javascript/get-program-accounts)
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
    """
    Декодирует данные пула из байтов
    """
    try:
        # Используем DATA_OFFSET и DATA_LENGTH из документации [(2)](https://solana.com/developers/courses/native-onchain-development/paging-ordering-filtering-data-frontend)
        DATA_OFFSET = 2  # Skip versioning bytes
        DATA_LENGTH = 18  # Length for comparison

        # Декодируем основные поля согласно документации [(2)](https://solana.com/developers/courses/native-onchain-development/paging-ordering-filtering-data-frontend)
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

async def handle_pool_change(pool_data: dict):
    """
    Обрабатывает изменения в пуле с использованием onAccountChange
    """
    try:
        # Проверяем наличие обязательных полей
        required_fields = [
            'address', 'mint_x', 'mint_y', 'liquidity',
            'volume_1h', 'volume_5m', 'bin_step', 'base_fee'
        ]
        
        if not all(field in pool_data for field in required_fields):
            logger.error("Отсутствуют обязательные поля в данных пула")
            return

        # Проверяем соответствие фильтрам
        if not filter_pool(pool_data):
            return

        # Форматируем сообщение
        message = format_pool_message(pool_data)
        if not message:
            return
            
        # Отправляем уведомление
        await application.bot.send_message(
            chat_id=USER_ID,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Ошибка обработки изменений пула: {e}")

async def save_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Сохраняет фильтры в файл
    """
    try:
        # Сохраняем только необходимые фильтры
        filters_to_save = {
            "bin_steps": current_filters["bin_steps"],
            "min_tvl": current_filters["min_tvl"],
            "base_fee_max": current_filters["base_fee_max"],
            "volume_1h_min": current_filters["volume_1h_min"],
            "volume_5m_min": current_filters["volume_5m_min"]
        }

        # Сохраняем в файл
        with open(FILE_PATH, "w") as f:
            json.dump(filters_to_save, f, indent=4)

        await update.message.reply_text("✅ Фильтры сохранены")
        logger.info("Фильтры успешно сохранены")

    except Exception as e:
        logger.error(f"Ошибка сохранения фильтров: {e}")
        await update.message.reply_text("❌ Ошибка сохранения фильтров")

async def update_filters_via_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обновляет фильтры через JSON-сообщение
    """
    if update.effective_user.id != USER_ID:
        return

    try:
        # Удаляем команду из текста если есть
        text = update.message.text
        if text.startswith('/'):
            text = ' '.join(text.split()[1:])
        
        # Парсим JSON
        new_filters = json.loads(text)
        
        # Проверяем обязательные поля
        required_fields = {
            "bin_steps": list,
            "min_tvl": (int, float),
            "base_fee_max": (int, float),
            "volume_1h_min": (int, float),
            "volume_5m_min": (int, float)
        }

        # Валидация типов данных
        for field, expected_type in required_fields.items():
            if field not in new_filters:
                raise ValueError(f"Отсутствует обязательное поле: {field}")
            if not isinstance(new_filters[field], expected_type):
                raise ValueError(f"Некорректный тип данных для {field}")

        # Обновляем фильтры
        current_filters.update(new_filters)
        
        # Сохраняем
        with open(FILE_PATH, "w") as f:
            json.dump(current_filters, f, indent=4)

        await update.message.reply_text("✅ Фильтры обновлены")
        
    except json.JSONDecodeError:
        await update.message.reply_text("❌ Ошибка: Некорректный JSON формат")
    except ValueError as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    except Exception as e:
        logger.error(f"Ошибка обновления фильтров: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обновлении фильтров")

async def get_filters_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет текущие фильтры в формате JSON
    """
    if update.effective_user.id != USER_ID:
        return

    try:
        # Формируем только необходимые фильтры
        filters_json = {
            "bin_steps": current_filters["bin_steps"],
            "min_tvl": current_filters["min_tvl"],
            "base_fee_max": current_filters["base_fee_max"],
            "volume_1h_min": current_filters["volume_1h_min"],
            "volume_5m_min": current_filters["volume_5m_min"]
        }
        
        # Форматируем JSON
        formatted_json = json.dumps(filters_json, indent=4)
        
        # Отправляем сообщение
        await update.message.reply_text(
            f"Текущие фильтры:\n```json\n{formatted_json}\n```",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Ошибка получения JSON фильтров: {e}")
        await update.message.reply_text("❌ Ошибка при получении фильтров")

def filter_pool(pool: dict) -> bool:
    """
    Фильтрует DLMM пул на основе заданных критериев
    """
    try:
        # Проверяем базовые условия
        conditions = [
            pool.get("bin_step") in current_filters["bin_steps"],
            pool.get("base_fee", 0) <= current_filters["base_fee_max"],
            pool.get("tvl_sol", 0) >= current_filters["min_tvl"],
            pool.get("volume_1h", 0) >= current_filters["volume_1h_min"],
            pool.get("volume_5m", 0) >= current_filters["volume_5m_min"]
        ]

        return all(conditions)

    except Exception as e:
        logger.error(f"Ошибка фильтрации пула: {e}")
        return False

def get_non_sol_token(mint_x: str, mint_y: str) -> str:
    """
    Returns the non-SOL token from a token pair.
    
    Args:
        mint_x (str): First token mint address
        mint_y (str): Second token mint address
    
    Returns:
        str: Address of non-SOL token
    """
    # Using proper Solana token addresses
    SOL_MINT = "So11111111111111111111111111111111111111112"
    
    try:
        if mint_x == SOL_MINT:
            return mint_y
        elif mint_y == SOL_MINT:
            return mint_x
        else:
            return mint_x
            
    except Exception as e:
        logger.error(f"Error determining non-SOL token: {e}")
        return mint_x

def save_filters_to_file():
    """
    Сохраняет текущие фильтры в файл с проверками.
    """
    try:
        # Проверяем наличие директории
        directory = os.path.dirname(FILE_PATH)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)
            
        # Проверяем фильтры перед сохранением    
        if not validate_filters(current_filters):
            raise ValueError("Некорректные фильтры")
            
        # Получаем очищенные фильтры
        clean_filters = get_clean_filters()
            
        # Сохраняем в файл с форматированием
        with open(FILE_PATH, "w", encoding="utf-8") as file:
            json.dump(clean_filters, file, indent=4, ensure_ascii=False)
            
        logger.info(f"Фильтры сохранены в {FILE_PATH} ✅")
        return True
        
    except ValueError as e:
        logger.error(f"Ошибка валидации фильтров: {e}")
        return False
    except IOError as e:
        logger.error(f"Ошибка записи файла: {e}")
        return False
    except Exception as e:
        logger.error(f"Непредвиденная ошибка: {e}")
        return False

def load_filters_from_file():
    """
    Загружает фильтры из файла с проверками.
    """
    global current_filters
    try:
        # Проверяем существование файла
        if not os.path.exists(FILE_PATH):
            logger.info(f"Файл фильтров не найден: {FILE_PATH}")
            return False
            
        # Загружаем и проверяем фильтры    
        with open(FILE_PATH, "r", encoding="utf-8") as file:
            loaded_filters = json.load(file)
            
        # Проверяем структуру
        if not validate_filters(loaded_filters):
            logger.error("Некорректная структура фильтров")
            return False
                
        # Обновляем только валидные поля
        for key, value in loaded_filters.items():
            if key in DEFAULT_FILTERS:
                if isinstance(value, type(DEFAULT_FILTERS[key])):
                    current_filters[key] = value
                else:
                    logger.warning(f"Пропущено поле {key}: неверный тип данных")
                        
        logger.info("Фильтры загружены ✅")
        return True
            
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка JSON: {e}")
        return False
    except IOError as e:
        logger.error(f"Ошибка чтения файла: {e}")
        return False
    except Exception as e:
        logger.error(f"Непредвиденная ошибка: {e}")
        return False

def get_clean_filters() -> dict:
    """
    Возвращает очищенный словарь с настройками фильтров.
    
    Returns:
        dict: Словарь с проверенными настройками фильтров
    """
    # Определяем границы значений
    LIMITS = {
        "min_tvl": (0.0, 1000000.0),
        "base_fee_max": (0.0, 100.0),
        "volume_1h_min": (0.0, 1000000.0),
        "volume_5m_min": (0.0, 1000000.0)
    }

    clean_filters = {}
    
    # Проверяем bin_steps
    bin_steps = current_filters.get("bin_steps", [20, 80, 100, 125, 250])
    if isinstance(bin_steps, list):
        clean_filters["bin_steps"] = [
            step for step in bin_steps 
            if isinstance(step, (int, float)) and 1 <= step <= 1000
        ]
    else:
        clean_filters["bin_steps"] = [20, 80, 100, 125, 250]

    # Проверяем числовые значения
    for key, (min_val, max_val) in LIMITS.items():
        value = current_filters.get(key, DEFAULT_FILTERS.get(key, 0.0))
        try:
            value = float(value)
            clean_filters[key] = max(min_val, min(value, max_val))
        except (TypeError, ValueError):
            clean_filters[key] = DEFAULT_FILTERS.get(key, 0.0)
            logger.warning(f"Неверное значение для {key}, используем значение по умолчанию")

    return clean_filters

def format_pool_message(pool: dict) -> str:
    """
    Форматирует данные пула в сообщение для Telegram.
    
    Args:
        pool (dict): Словарь с данными пула
        
    Returns:
        str: Отформатированное сообщение
    """
    try:
        # Проверяем обязательные поля
        required = ["address", "mint_x", "mint_y", "liquidity", 
                   "volume_1h", "volume_5m", "bin_step", "base_fee"]
        if not all(field in pool for field in required):
            logger.error("Отсутствуют обязательные поля пула")
            return None

        # Получаем и проверяем значения
        tvl = float(pool.get("liquidity", 0)) / 1e9
        volume_1h = float(pool.get("volume_1h", 0)) / 1e9
        volume_5m = float(pool.get("volume_5m", 0)) / 1e9
        fees_1h = volume_1h * (float(pool.get("base_fee", 0)) / 100)
        fees_5m = volume_5m * (float(pool.get("base_fee", 0)) / 100)
        
        # Рассчитываем показатели
        fee_tvl_24h = (fees_1h * 24 / tvl * 100) if tvl > 0 else 0
        dynamic_fee_tvl = (fees_1h / tvl * 100) if tvl > 0 else 0

        # Формируем сообщение
        return (
            f"⭐️Turtle⇆SOL 🔥<3h\n"
            f"☄️Meteora (https://edge.meteora.ag/dlmm/{pool['address']}) "
            f"⟨1 pool (https://meteoranavigator.com/en/pools?sort=liquidity_now&sort_order=desc&search={pool['mint_x']})⟩\n"
            f"🐊gmgn (https://gmgn.ai/sol/token/{pool['mint_x']}) "
            f"🦅Dexscreener (https://dexscreener.com/solana/{pool['address']})\n"
            f"{pool['mint_x']}\n"
            f"╔ TVL ➙ {tvl:.3f}$\n"
            f"╟ Bin step ➙ {pool['bin_step']}\n"
            f"╟ Base fee ➙ {pool['base_fee']:.1f}%\n"
            f"╟ Fees 5min\\1h ➙ {fees_5m:.3f}$\\{fees_1h:.3f}$\n"
            f"╟ Volume 5min\\1h ➙ {volume_5m:.3f}$\\{volume_1h:.3f}$\n"
            f"╟ Fee 24h/TVL ➙ {fee_tvl_24h:.2f}%\n"
            f"╚ Dynamic 1h fee/TVL ➙ {dynamic_fee_tvl:.2f}%"
        )

    except (ValueError, TypeError) as e:
        logger.error(f"Ошибка обработки данных пула: {e}")
        return None
    except Exception as e:
        logger.error(f"Непредвиденная ошибка: {e}")
        return None

def setup_command_handlers(application: ApplicationBuilder):
    """
    Настраивает обработчики команд для бота.
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
            )
        ]
        
        for handler in filter_handlers:
            application.add_handler(handler)

        logger.info("✅ Обработчики команд настроены")
        
    except Exception as e:
        logger.error(f"❌ Ошибка настройки обработчиков: {e}")
        raise

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает неизвестные команды.
    """
    if update.effective_user.id != USER_ID:
        return

    await update.message.reply_text(
        "❌ Неизвестная команда\n\n"
        "Доступные команды:\n"
        "/start - запуск мониторинга\n"
        "/filters - текущие фильтры\n"
        "/setfilter - изменить фильтр\n"
        "/getfiltersjson - фильтры в JSON\n"
    )

# Инициализация обработчиков
setup_command_handlers(application)

class WebhookConfig:
    """
    Конфигурация для веб-хуков
    """
    WEBHOOK_TIMEOUT = 10  # Уменьшаем таймаут для быстрого ответа
    MAX_RETRIES = 2      # Уменьшаем количество попыток
    RETRY_DELAY = 0.5    # Уменьшаем задержку

@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
async def webhook():
    """
    Обрабатывает входящие запросы от Telegram.
    """
    try:
        # Проверка заголовков
        if not request.is_json:
            logger.error("Получен не JSON запрос")
            return {'error': 'Требуется application/json'}, 400

        # Получение данных
        data = await request.get_json()
        if not data:
            logger.error("Пустой JSON")
            return {'error': 'Пустой запрос'}, 400

        # Обработка с повторными попытками
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

    except asyncio.TimeoutError:
        logger.error("Таймаут вебхука")
        return {'error': 'Таймаут'}, 504
    except Exception as e:
        logger.error(f"Ошибка вебхука: {e}")
        return {'error': 'Внутренняя ошибка'}, 500

@app.route('/healthcheck')
async def healthcheck():
    """
    Проверка состояния сервиса.
    """
    try:
        status = {
            "status": "error",
            "components": {
                "bot": False,
                "solana": False
            },
            "timestamp": datetime.utcnow().isoformat()
        }

        # Проверка бота
        if application.running:
            status["components"]["bot"] = True

        # Проверка Solana
        try:
            response = await solana_client.get_version()
            if response.value:
                status["components"]["solana"] = True
        except Exception as e:
            logger.warning(f"Ошибка проверки Solana: {e}")

        # Итоговый статус
        if all(status["components"].values()):
            status["status"] = "ok"
            return status, 200
            
        return status, 503

    except Exception as e:
        logger.error(f"Ошибка проверки состояния: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }, 500

@app.route('/test-solana')
async def test_solana():
    """
    Проверка подключения к Solana.
    """
    try:
        connected = await solana_client.get_version()
        return {"solana_connected": bool(connected.value)}, 200
    except Exception as e:
        logger.error(f"Ошибка проверки Solana: {e}")
        return {"solana_connected": False}, 500

@app.route('/')
async def home():
    """
    Главная страница с основной информацией.
    """
    try:
        return {
            "status": "ok",
            "name": "Meteora Monitor",
            "description": "Мониторинг пулов Meteora на Solana",
            "endpoints": {
                "/healthcheck": "Проверка состояния",
                "/test-solana": "Проверка Solana"
            },
            "timestamp": datetime.utcnow().isoformat()
        }, 200
    except Exception as e:
        logger.error(f"Ошибка на главной странице: {e}")
        return {"status": "error"}, 500

if __name__ == "__main__":
    try:
        # Запускаем основную последовательность
        if asyncio.run(startup_sequence()):
            logger.info("🚀 Запуск мониторинга пулов...")
            
            # Запускаем мониторинг пулов
            app.run(
                host="0.0.0.0",
                port=10000,  # Возвращаем ваш оригинальный порт
                debug=False
            )
        else:
            logger.error("❌ Ошибка запуска приложения")
            sys.exit(1)
            
    except KeyboardInterrupt:
        logger.info("👋 Завершение работы...")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        sys.exit(1)
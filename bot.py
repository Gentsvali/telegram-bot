import os
import logging
import sys
import asyncio
import time
import json
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
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
import base58
import base64
import websockets  # Добавляем websockets

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
    "HELIUS_RPC_URL"
]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]

if missing_vars:
    error_message = (
        f"Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}. "
        "Пожалуйста, проверьте настройки."
    )
    logger.error(error_message)
    raise ValueError(error_message)

# Настройки Solana
COMMITMENT = "confirmed"
DLMM_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
# Инициализация Solana клиента
solana_client = AsyncClient(HELIUS_RPC_URL, commitment="confirmed")

# Добавьте новую константу для WebSocket
WS_RECONNECT_TIMEOUT = 30  # секунды между попытками переподключения

# В начале файла, рядом с другими константами
WEBSOCKET_SUBSCRIBE_MSG = {
  "jsonrpc": "2.0",
  "id": 1,
  "method": "logsSubscribe",
  "params": [
    {
      "mentions": [ "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo" ]
    },
    {
      "commitment": "confirmed"
    }
  ]
}
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

# Инициализация подключения к Solana
async def init_solana() -> bool:
    """Универсальная проверка подключения к Solana"""
    try:
        response = await solana_client.get_version()
        
        # Обработка для новых версий solana-py (solders)
        if hasattr(response, 'value'):
            version_info = response.value
            version = getattr(version_info, 'solana_core', None) or getattr(version_info, 'version', 'unknown')
            logger.info(f"✅ Подключено к Solana (v{version})")
            return True
            
        # Обработка для старых версий
        if hasattr(response, 'to_json'):
            version_data = json.loads(response.to_json())
            version = version_data.get('result', {}).get('version', 'unknown')
            logger.info(f"✅ Подключено к Solana (v{version})")
            return True
            
        # Если ответ в неожиданном формате
        logger.error(f"Неподдерживаемый формат ответа RPC: {type(response)}")
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

async def handle_websocket_connection():
    """Обработчик WebSocket подключения с улучшенной обработкой ошибок"""
    while True:
        try:
            async with websockets.connect(HELIUS_WS_URL) as websocket:
                # Отправляем запрос на подписку с параметром confirmed для скорости
                subscribe_message = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {
                            "mentions": [ "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo" ]
                        },
                        {
                            "commitment": "confirmed"  # Используем confirmed для скорости [(1)](https://solana.stackexchange.com/questions/18574/speed-up-websocket-connection)
                        }
                    ]
                }
                
                await websocket.send(json.dumps(subscribe_message))
                logger.info("✅ WebSocket подписка установлена")

                # Бесконечный цикл обработки сообщений
                while True:
                    try:
                        await handle_websocket_message(websocket)
                    except websockets.exceptions.ConnectionClosed:
                        raise
                    except Exception as e:
                        logger.error(f"Ошибка в цикле обработки: {e}")
                        continue

        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket соединение закрыто, переподключение...")
            await asyncio.sleep(WS_RECONNECT_TIMEOUT)
        except Exception as e:
            logger.error(f"Ошибка WebSocket соединения: {e}")
            await asyncio.sleep(WS_RECONNECT_TIMEOUT)

async def maintain_websocket_connection():
    while True:
        try:
            async with websockets.connect(HELIUS_WS_URL) as websocket:
                await websocket.send(json.dumps(WEBSOCKET_SUBSCRIBE_MSG))
                logger.info("WebSocket подписка установлена")
                
                # Запускаем ping/pong
                ping_task = asyncio.create_task(keep_alive(websocket))
                
                try:
                    while True:
                        message = await websocket.recv()
                        await process_websocket_message(message)
                except Exception as e:
                    logger.error(f"Ошибка в основном цикле websocket: {e}")
                    ping_task.cancel()
                    raise
                    
        except Exception as e:
            logger.error(f"Ошибка websocket соединения: {e}")
            await asyncio.sleep(5)

async def keep_alive(websocket):
    while True:
        try:
            ping_message = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "ping"
            }
            await websocket.send(json.dumps(ping_message))
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Ошибка ping/pong: {e}")
            break

async def unsubscribe_websocket(websocket):
    """Отписывается от WebSocket подписки"""
    try:
        unsubscribe_message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsUnsubscribe",
            "params": [0]
        }
        await websocket.send(json.dumps(unsubscribe_message))
        logger.info("Успешная отписка от WebSocket")
    except Exception as e:
        logger.error(f"Ошибка отписки от WebSocket: {e}")

async def process_transaction_logs(logs: List[str]):
    """Обработка логов транзакций с улучшенной фильтрацией"""
    try:
        # Фильтруем только логи Meteora DLMM
        meteora_logs = []
        instruction_type = None
        pool_data = {}
        
        for log in logs:
            # Ищем только логи программы Meteora DLMM
            if "Program LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo" in log:
                if "Program log: Instruction:" in log:
                    instruction_type = log.split("Instruction: ")[-1].strip()
                    
                    # Обрабатываем только нужные инструкции
                    if instruction_type in ["Initialize", "Swap"]:
                        meteora_logs.append(log)
                        logger.info(f"Найдена инструкция Meteora: {instruction_type}")
                
                # Ищем данные пула
                elif "Program data:" in log:
                    try:
                        data = log.split("Program data: ")[-1].strip()
                        # Здесь можно добавить парсинг данных пула
                        pool_data = decode_pool_data(data)
                        if pool_data and filter_pool(pool_data):
                            message = format_pool_message(pool_data)
                            if message:
                                await application.bot.send_message(
                                    chat_id=USER_ID,
                                    text=message,
                                    parse_mode="Markdown"
                                )
                    except Exception as e:
                        logger.error(f"Ошибка парсинга данных пула: {e}")

    except Exception as e:
        logger.error(f"Ошибка обработки логов: {e}")

async def process_websocket_message(message: str):
    try:
        data = json.loads(message)
        raw_logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])
        await process_dlmm_events(raw_logs)
    except Exception as e:
        logger.error(f"WebSocket error: {str(e)}")

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
        asyncio.create_task(maintain_websocket_connection())
        logger.info("🔌 WebSocket мониторинг запущен")

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
            logger.info(f"🚀 Запускаем сервер на порту {PORT}...")
            
            # Либо запускаем Quart app, либо мониторинг пулов
            # Выберите один вариант:
            
            # Вариант 1: Запуск Quart сервера
            app.run(host='0.0.0.0', port=PORT)
            
            # ИЛИ Вариант 2: Запуск мониторинга
            # asyncio.run(monitor_pools())
            
        else:
            logger.error("❌ Ошибка при запуске приложения")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        sys.exit(1)
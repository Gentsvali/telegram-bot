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

# Solana импорты - обновленные
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Commitment
from solana.rpc.core import RPCException as SolanaRpcException
from solana.rpc.types import MemcmpOpts, DataSliceOpts 
from solders.pubkey import Pubkey
import base58
import base64

# Настройка логгера с ротацией файлов
from logging.handlers import RotatingFileHandler

# Константы RPC и настройки подключения
RPC_CONFIG = {
    "MAX_RETRIES": 3,
    "RETRY_DELAY": 1,
    "DEFAULT_TIMEOUT": 60,
    "MAX_REQUESTS_PER_10_SEC": 40,  # Максимум запросов за 10 секунд
    "MAX_CONCURRENT_REQUESTS": 40,   # Максимум одновременных подключений
}

DEFAULT_FILTERS = {
    "min_tvl": 10.0,
    "volume_5m_min": 1.0
}

# Добавляем недостающие константы
DLMM_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
DLMM_CONFIG = {
    "pool_size": 165,  # Примерный размер данных пула
    "update_interval": 60,  # Интервал обновления в секундах
    "retry_delay": 10  # Задержка при ошибках
}

# Константы для работы с транзакциями [(2)](https://solana.com/developers/guides/advanced/retry)
TX_CONFIG = {
    "retries": 3,
    "timeout": 30,
    "delay_between_retries": 1
}

# Константы для compute budget [(4)](https://solana.com/developers/guides/advanced/how-to-request-optimal-compute)
COMPUTE_BUDGET = {
    "DEFAULT_UNITS": 300,
    "DEFAULT_PRICE": 1
}

# Настройка логгера
def setup_logger():
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    
    # Ротация файлов логов (максимум 5 файлов по 5MB)
    file_handler = RotatingFileHandler(
        "bot.log",
        maxBytes=5*1024*1024,  # 5MB
        backupCount=5,
        encoding="utf-8"
    )
    
    console_handler = logging.StreamHandler()
    
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    # Уменьшаем уровень логирования для сторонних библиотек
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("solana").setLevel(logging.WARNING)
    
    return logger

logger = setup_logger()

# Проверка переменных окружения
load_dotenv()

required_env_vars = [
    "TELEGRAM_TOKEN", 
    "GITHUB_TOKEN", 
    "USER_ID", 
    "WEBHOOK_URL",
    "RPC_URL",
    "HELIUS_API_KEY"  # Добавьте это
]

missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}")

# Определение констант
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_OWNER = "Gentsvali"
REPO_NAME = "telegram-bot"
FILE_PATH = "filters.json"
USER_ID = int(os.getenv("USER_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 10000))
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
application = (
    ApplicationBuilder()
    .token(TELEGRAM_TOKEN)
    .build()
)

# Обновленные RPC эндпоинты с приоритетами
RPC_ENDPOINTS = [
    {
        'url': f'https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}',
        'priority': 1,
        'timeout': 30
    }
]

async def init_monitoring():
    """Инициализация системы мониторинга"""
    try:
        # Инициализация Solana клиента
        if not await solana_client.initialize():
            logger.error("Не удалось инициализировать Solana клиент")
            return False

        # Загрузка фильтров
        if not await filter_manager.load_filters():
            logger.warning("Используются фильтры по умолчанию")

        # Запуск мониторинга
        asyncio.create_task(pool_monitor.start_monitoring())
        logger.info("✅ Мониторинг пулов запущен")
        return True

    except Exception as e:
        logger.error(f"Ошибка инициализации мониторинга: {e}")
        return False

class HeliusClient:
    def __init__(self):
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        self.client = AsyncClient(self.rpc_url, timeout=30)

    async def initialize(self):
        """Инициализация с автоматическим выбором рабочего RPC"""
        for endpoint in sorted(RPC_ENDPOINTS, key=lambda x: x['priority']):
            try:
                self.client = AsyncClient(
                    endpoint['url'],
                    timeout=endpoint['timeout'],
                    commitment=Commitment("confirmed")
                )
                await self.client.get_epoch_info()
                self.current_endpoint = endpoint
                logger.info(f"✅ Подключено к RPC: {endpoint['url']}")
                return True
            except Exception as e:
                logger.warning(f"❌ Ошибка подключения к {endpoint['url']}: {e}")
        return False

    async def switch_endpoint(self):
        """Переключение между двумя нодами"""
        old_url = self.current_endpoint['url']
        new_endpoint = next(
            (ep for ep in RPC_ENDPOINTS if ep['url'] != old_url), 
            RPC_ENDPOINTS[0]
        )
        
        try:
            await self.client.close()
            self.client = AsyncClient(
                new_endpoint['url'],
                timeout=new_endpoint['timeout'],
                commitment=Commitment("confirmed")
            )
            await self.client.get_epoch_info()
            self.current_endpoint = new_endpoint
            logger.info(f"✅ Переключено {old_url} → {new_endpoint['url']}")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка переключения: {e}")
            return False

    async def get_program_accounts(self, program_id: str, filters: list):
        """Унифицированный запрос аккаунтов"""
        try:
            return await self.client.get_program_accounts(
                Pubkey.from_string(program_id),
                encoding="base64",
                filters=filters,
                commitment=Commitment("confirmed")
            )
        except Exception as e:
            logger.error(f"Ошибка get_program_accounts: {e}")
            return None

# Создаем глобальный экземпляр клиента
solana_client = HeliusClient()

async def track_dlmm_pools():
    """Мониторинг пулов с улучшенной обработкой ошибок и rate-limiting"""
    if not await solana_client.initialize():
        logger.error("Не удалось инициализировать Solana клиент")
        return

    while True:
        try:
            filters = [
                MemcmpOpts(
                    offset=0,
                    bytes=base58.b58encode(bytes([1])).decode()
                )
            ]
            
            response = await solana_client.get_program_accounts(DLMM_PROGRAM_ID, filters)
            
            if response and hasattr(response, 'value'):
                for account in response.value:
                    try:
                        if hasattr(account, 'account'):
                            data = account.account.data
                            if isinstance(data, str):
                                decoded = base64.b64decode(data)
                                await handle_pool_data(decoded)
                    except Exception as e:
                        logger.error(f"Ошибка обработки аккаунта: {e}")
                        continue
            
            await asyncio.sleep(DLMM_CONFIG["update_interval"])
            
        except Exception as e:
            logger.error(f"Ошибка в цикле мониторинга: {e}")
            await asyncio.sleep(DLMM_CONFIG["retry_delay"])

class PoolDataDecoder:
    """Класс для декодирования и валидации данных пула"""
    
    @staticmethod
    def decode_pool_data(data: Union[str, bytes]) -> Optional[dict]:
        """Декодирует данные пула с улучшенной валидацией"""
        try:
            # Конвертация в bytes если нужно
            if isinstance(data, str):
                data = base64.b64decode(data)
            elif not isinstance(data, bytes):
                raise ValueError(f"Неподдерживаемый тип данных: {type(data)}")

            # Проверка размера данных
            if len(data) < DLMM_CONFIG["pool_size"]:
                raise ValueError(f"Некорректный размер данных: {len(data)} байт")

            # Декодирование с проверкой каждого поля
            decoded = {
                "mint_x": base58.b58encode(data[:32]).decode(),
                "mint_y": base58.b58encode(data[32:64]).decode(),
                "liquidity": int.from_bytes(data[64:72], "little"),
                "volume_1h": int.from_bytes(data[72:80], "little"),
                "volume_5m": int.from_bytes(data[80:88], "little"),
                "bin_step": int.from_bytes(data[88:90], "little"),
                "base_fee": int.from_bytes(data[90:92], "little") / 10000,
                "fee_tvl_ratio_24h": int.from_bytes(data[92:100], "little") / 10000,
                "dynamic_fee_tvl_ratio": int.from_bytes(data[100:108], "little") / 10000
            }

            # Добавляем рассчитанные значения в SOL
            decoded.update({
                "tvl_sol": decoded["liquidity"] / 1e9,
                "volume_1h_sol": decoded["volume_1h"] / 1e9,
                "volume_5m_sol": decoded["volume_5m"] / 1e9
            })

            # Валидация декодированных данных
            if not all(v is not None for v in decoded.values()):
                raise ValueError("Обнаружены пустые значения")

            return decoded

        except Exception as e:
            logger.error(f"Ошибка декодирования данных пула: {e}", exc_info=True)
            return None

    @staticmethod
    def validate_pool_data(pool_data: dict) -> bool:
        """Проверяет валидность данных пула"""
        required_fields = {
            "mint_x": str,
            "mint_y": str,
            "liquidity": (int, float),
            "volume_1h": (int, float),
            "volume_5m": (int, float),
            "bin_step": int,
            "base_fee": float
        }

        try:
            for field, expected_type in required_fields.items():
                if field not in pool_data:
                    logger.warning(f"Отсутствует поле {field}")
                    return False
                if not isinstance(pool_data[field], expected_type):
                    logger.warning(f"Неверный тип для {field}: {type(pool_data[field])}")
                    return False
            return True
        except Exception as e:
            logger.error(f"Ошибка валидации данных пула: {e}")
            return False

# Обновляем функцию обработки данных пула
async def handle_pool_data(data: bytes):
    """Обработка данных пула с улучшенной валидацией и обработкой ошибок"""
    try:
        # Декодируем данные
        decoder = PoolDataDecoder()
        pool_data = decoder.decode_pool_data(data)
        
        if not pool_data:
            logger.warning("Не удалось декодировать данные пула")
            return

        # Проверяем валидность данных
        if not decoder.validate_pool_data(pool_data):
            logger.warning("Данные пула не прошли валидацию")
            return

        # Проверяем соответствие фильтрам
        if not filter_pool(pool_data):
            logger.debug(f"Пул не соответствует фильтрам")
            return

        # Форматируем и отправляем сообщение
        message = format_pool_message(pool_data)
        if message:
            await send_pool_notification(message)
            
    except Exception as e:
        logger.error(f"Ошибка обработки данных пула: {e}", exc_info=True)

async def send_pool_notification(message: str):
    """Отправка уведомления с повторными попытками"""
    max_retries = 3
    retry_delay = 1
    
    for attempt in range(max_retries):
        try:
            await application.bot.send_message(
                chat_id=USER_ID,
                text=message,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            return
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"Не удалось отправить уведомление после {max_retries} попыток: {e}")
                return
            await asyncio.sleep(retry_delay)
            retry_delay *= 2

class FilterManager:
    """Менеджер фильтров с валидацией и сохранением"""
    
    def __init__(self):
        self.current_filters = DEFAULT_FILTERS.copy()
        self.file_path = FILE_PATH
        self.github_token = GITHUB_TOKEN
        self.repo_owner = REPO_OWNER
        self.repo_name = REPO_NAME

    def validate_filters(self, filters: dict) -> bool:
        """Расширенная валидация фильтров"""
        try:
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

            # Проверка наличия всех ключей
            if not all(key in filters for key in required_keys):
                missing_keys = [key for key in required_keys if key not in filters]
                logger.warning(f"Отсутствуют обязательные ключи: {missing_keys}")
                return False

            # Проверка типов данных и диапазонов значений
            validations = {
                "disable_filters": lambda x: isinstance(x, bool),
                "bin_steps": lambda x: isinstance(x, list) and all(isinstance(i, int) and i > 0 for i in x),
                "min_tvl": lambda x: isinstance(x, (int, float)) and x >= 0,
                "base_fee_min": lambda x: isinstance(x, (int, float)) and 0 <= x <= 100,
                "base_fee_max": lambda x: isinstance(x, (int, float)) and 0 <= x <= 100,
                "volume_1h_min": lambda x: isinstance(x, (int, float)) and x >= 0,
                "volume_5m_min": lambda x: isinstance(x, (int, float)) and x >= 0,
                "fee_tvl_ratio_24h_min": lambda x: isinstance(x, (int, float)) and x >= 0,
                "dynamic_fee_tvl_ratio_min": lambda x: isinstance(x, (int, float)) and x >= 0
            }

            for key, validator in validations.items():
                if not validator(filters[key]):
                    logger.warning(f"Некорректное значение для {key}: {filters[key]}")
                    return False

            return True

        except Exception as e:
            logger.error(f"Ошибка валидации фильтров: {e}")
            return False

    async def load_filters(self) -> bool:
        """Загрузка фильтров с поддержкой GitHub"""
        try:
            # Сначала пробуем загрузить из локального файла
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    loaded = json.load(f)
                    if self.validate_filters(loaded):
                        self.current_filters.update(loaded)
                        logger.info("✅ Фильтры загружены из локального файла")
                        return True

            # Если локальный файл недоступен или невалиден, пробуем GitHub
            if self.github_token:
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(
                            f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/contents/{self.file_path}",
                            headers={"Authorization": f"token {self.github_token}"}
                        )
                        if response.status_code == 200:
                            content = base64.b64decode(response.json()["content"]).decode()
                            loaded = json.loads(content)
                            if self.validate_filters(loaded):
                                self.current_filters.update(loaded)
                                # Сохраняем локально
                                await self.save_filters(loaded)
                                logger.info("✅ Фильтры загружены из GitHub")
                                return True
                except Exception as e:
                    logger.warning(f"Ошибка загрузки из GitHub: {e}")

            logger.info("ℹ️ Используются фильтры по умолчанию")
            return False

        except Exception as e:
            logger.error(f"Ошибка загрузки фильтров: {e}")
            return False

    async def save_filters(self, filters: dict) -> bool:
        """Сохранение фильтров локально и в GitHub"""
        try:
            if not self.validate_filters(filters):
                logger.error("Попытка сохранить невалидные фильтры")
                return False

            # Сохраняем локально
            with open(self.file_path, 'w') as f:
                json.dump(filters, f, indent=4)

            # Сохраняем в GitHub если настроен
            if self.github_token:
                try:
                    async with httpx.AsyncClient() as client:
                        # Получаем текущий SHA файла
                        response = await client.get(
                            f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/contents/{self.file_path}",
                            headers={"Authorization": f"token {self.github_token}"}
                        )
                        sha = response.json()["sha"] if response.status_code == 200 else None

                        # Отправляем обновление
                        content = base64.b64encode(json.dumps(filters, indent=4).encode()).decode()
                        await client.put(
                            f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/contents/{self.file_path}",
                            headers={"Authorization": f"token {self.github_token}"},
                            json={
                                "message": "Update filters",
                                "content": content,
                                "sha": sha
                            }
                        )
                        logger.info("✅ Фильтры сохранены в GitHub")
                except Exception as e:
                    logger.warning(f"Ошибка сохранения в GitHub: {e}")

            logger.info("✅ Фильтры успешно сохранены")
            return True

        except Exception as e:
            logger.error(f"Ошибка сохранения фильтров: {e}")
            return False

# Создаем глобальный экземпляр менеджера фильтров
filter_manager = FilterManager()

def setup_bot_handlers(app, fm):
    """Настройка всех обработчиков бота"""
    
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != USER_ID:
            return
        
        text = (
            "🚀 Бот мониторинга DLMM пулов\n\n"
            "Доступные команды:\n"
            "/start или /начало - это сообщение\n"
            "/filters - текущие настройки\n"
            "/setfilter - изменить фильтры\n"
            "/checkpools - проверить пулы\n"
            "/getfiltersjson - получить фильтры"
        )
        await update.message.reply_text(text)

    async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
        filters = fm.current_filters
        text = (
            f"⚙️ Текущие фильтры:\n\n"
            f"• Bin Steps: {', '.join(map(str, filters['bin_steps']))}\n"
            f"• Мин TVL: {filters['min_tvl']} SOL\n"
            f"• Базовая комиссия: {filters['base_fee_min']}%-{filters['base_fee_max']}%"
        )
        await update.message.reply_text(text)

    async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Установка значения фильтра"""
        if update.effective_user.id != USER_ID:
            return

        try:
            args = context.args
            if len(args) < 2:
                await update.message.reply_text(
                    "❌ Использование: /setfilter [параметр] [значение]\n"
                    "Пример: /setfilter min_tvl 100"
                )
                return

            param = args[0].lower()
            value = args[1]

            if param not in fm.current_filters:
                await update.message.reply_text(f"❌ Неизвестный параметр: {param}")
                return

            # Обработка различных типов параметров
            try:
                if param == "bin_steps":
                    new_value = [int(x.strip()) for x in value.split(',')]
                elif param == "disable_filters":
                    new_value = value.lower() in ('true', '1', 'yes')
                else:
                    new_value = float(value)

                fm.current_filters[param] = new_value
                await fm.save_filters(fm.current_filters)
                await update.message.reply_text(f"✅ {param} обновлен: {new_value}")

            except ValueError:
                await update.message.reply_text("❌ Некорректное значение")

        except Exception as e:
            logger.error(f"Ошибка установки фильтра: {e}")
            await send_error_message(update)

    async def check_pools(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ручная проверка пулов"""
        if update.effective_user.id != USER_ID:
            return

        try:
            message = await update.message.reply_text("🔍 Проверяю пулы...")
            await track_dlmm_pools()
            await message.edit_text("✅ Проверка завершена")
        except Exception as e:
            logger.error(f"Ошибка проверки пулов: {e}")
            await send_error_message(update)

    async def get_filters_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Получение фильтров в формате JSON"""
        if update.effective_user.id != USER_ID:
            return

        try:
            filters_json = json.dumps(fm.current_filters, indent=2)
            await update.message.reply_text(
                f"```json\n{filters_json}\n```",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Ошибка получения JSON фильтров: {e}")
            await send_error_message(update)

    async def handle_json_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка JSON-обновления фильтров"""
        if update.effective_user.id != USER_ID:
            return

        try:
            new_filters = json.loads(update.message.text)
            if fm.validate_filters(new_filters):
                await fm.save_filters(new_filters)
                await update.message.reply_text("✅ Фильтры успешно обновлены")
            else:
                await update.message.reply_text("❌ Некорректный формат фильтров")
        except json.JSONDecodeError:
            await update.message.reply_text("❌ Некорректный JSON формат")
        except Exception as e:
            logger.error(f"Ошибка обработки JSON: {e}")
            await send_error_message(update)

    async def send_error_message(update: Update):
        """Отправка сообщения об ошибке"""
        try:
            await update.message.reply_text(
                "⚠️ Произошла ошибка. Пожалуйста, попробуйте позже."
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения об ошибке: {e}")

    # Добавляем обработчики
    handlers = [
        CommandHandler("start", start),
        CommandHandler("filters", show_filters),
        CommandHandler("setfilter", set_filter),
        CommandHandler("checkpools", check_pools),
        CommandHandler("getfiltersjson", get_filters_json),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_json_update)
    ]
    
    for handler in handlers:
        app.add_handler(handler)

    async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ошибок"""
        logger.error(f"Ошибка при обработке обновления {update}: {context.error}")
        
    # Добавьте обработчик ошибок
    app.add_error_handler(error_handler)

setup_bot_handlers(application, filter_manager)

class PoolMonitor:
    def __init__(self, solana_client):
        self.solana_client = solana_client
        self.pools_cache = {}
        self.last_update = datetime.now()
        self.processing = False
        self.rpc_errors = 0
        self.max_rpc_errors = 5

    async def _get_pools_data(self):
        """Универсальный метод получения данных пулов"""
        try:
            from solana.rpc.types import MemcmpOpts
        
            # Создаем правильные фильтры
            filters = [
                MemcmpOpts(
                    offset=0,
                    bytes=base58.b58encode(bytes([1])).decode()
                )
            ]
        
            # Делаем запрос с правильными параметрами
            response = await self.solana_client.client.get_program_accounts(
                Pubkey.from_string(DLMM_PROGRAM_ID),
                encoding="base64",
                filters=filters,
                commitment=Commitment("confirmed")
            )

            self.rpc_errors = 0
            return response.value if response else None

        except Exception as e:
            self.rpc_errors += 1
            logger.error(f"RPC Error #{self.rpc_errors}: {str(e)}")
        
            # Аварийный вариант без фильтров
            if self.rpc_errors > 2:
                try:
                    response = await self.solana_client.client.get_program_accounts(
                        Pubkey.from_string(DLMM_PROGRAM_ID),
                        encoding="base64",
                        commitment=Commitment("confirmed")
                    )
                    return response.value if response else None
                except Exception as fallback_e:
                    logger.error(f"Fallback RPC Error: {str(fallback_e)}")
        
            return None

    async def refresh_pools(self):
        """Обновление данных пулов с улучшенной обработкой ошибок"""
        try:
            if self.rpc_errors >= self.max_rpc_errors:
                logger.warning("Превышено максимальное количество ошибок RPC")
                if hasattr(self.solana_client, 'switch_endpoint'):
                    await self.solana_client.switch_endpoint()
                    self.rpc_errors = 0
                return False

            self.processing = True
            logger.debug("Начало обновления данных пулов...")
            
            accounts = await self._get_pools_data()
            
            if not accounts:
                logger.warning("Не получены данные пулов")
                return False
                
            new_pools = updated_pools = 0
            for account in accounts:
                try:
                    pool_id = str(account.pubkey)
                    if pool_id not in self.pools_cache:
                        new_pools += 1
                    elif self.pools_cache[pool_id].account.data != account.account.data:
                        updated_pools += 1
                    self.pools_cache[pool_id] = account
                except Exception as e:
                    logger.warning(f"Ошибка обработки пула: {str(e)}")
                    continue

            self.last_update = datetime.now()
            logger.info(
                f"Обновлено пулов: новых {new_pools}, измененных {updated_pools}, "
                f"всего {len(self.pools_cache)}"
            )
            return True
            
        except Exception as e:
            logger.error(f"Ошибка обновления: {str(e)}")
            return False
        finally:
            self.processing = False

    async def start_monitoring(self, interval=60):
        """Запуск мониторинга"""
        while True:
            success = await self.refresh_pools()
            await asyncio.sleep(interval if success else 5)

    async def stop_monitoring(self):
        """Остановка мониторинга"""
        self.processing = False
        logger.info("Мониторинг пулов остановлен")

pool_monitor = PoolMonitor(solana_client)

class WebhookServer:
    def __init__(self, application, pool_monitor, filter_manager):
        self.app = Quart(__name__)
        self.telegram_app = application
        self.pool_monitor = pool_monitor
        self.filter_manager = filter_manager
        self.setup_routes()

    def setup_routes(self):        
        @self.app.before_serving
        async def startup():
            try:
                # Инициализируем Solana клиент
                if not await solana_client.initialize():
                    logger.error("Не удалось инициализировать Solana клиент")
                    raise Exception("Ошибка инициализации Solana клиента")

                # Инициализируем Telegram приложение
                await application.initialize()
        
                # Инициализируем мониторинг
                if not await init_monitoring():
                    raise Exception("Ошибка инициализации мониторинга")
        
                # Устанавливаем webhook
                await application.bot.set_webhook(
                    f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True  # рекомендуется добавить
                )

                logger.info("🚀 Сервер успешно запущен")
            except Exception as e:
                logger.error(f"Критическая ошибка при запуске: {e}")
                sys.exit(1)

        @self.app.after_serving
        async def shutdown():
            """Корректное завершение работы"""
            try:
                await self.pool_monitor.stop_monitoring()
                await self.telegram_app.stop()
                await solana_client.client.close()
                logger.info("👋 Сервер корректно остановлен")
            except Exception as e:
                logger.error(f"Ошибка при остановке: {e}")

        @self.app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
        async def webhook():
            """Обработка webhook от Telegram"""
            try:
                if not request.is_json:
                    return {'error': 'Content-Type должен быть application/json'}, 400

                data = await request.get_json()
                
                # Валидация данных
                if not isinstance(data, dict) or 'update_id' not in data:
                    return {'error': 'Некорректный формат данных'}, 400

                # Обработка обновления
                update = Update.de_json(data, self.telegram_app.bot)
                await self.telegram_app.process_update(update)
                return '', 200

            except Exception as e:
                logger.error(f"Ошибка обработки webhook: {e}")
                return {'error': 'Internal server error'}, 500

        @self.app.route('/healthcheck')
        async def healthcheck():
            """Расширенная проверка здоровья сервиса"""
            try:
                # Проверяем все компоненты
                health_data = {
                    "status": "ERROR",
                    "timestamp": datetime.utcnow().isoformat(),
                    "components": {
                        "telegram_bot": False,
                        "solana_connection": False,
                        "pool_monitor": False,
                        "webhook": False
                    },
                    "stats": {
                        "pools": None,
                        "uptime": None
                    }
                }

                # Проверка бота
                if self.telegram_app.running:
                    health_data["components"]["telegram_bot"] = True

                # Проверка Solana
                try:
                    await solana_client.client.get_epoch_info()
                    health_data["components"]["solana_connection"] = True
                except Exception as e:
                    logger.warning(f"Ошибка проверки Solana: {e}")

                # Проверка монитора пулов
                if self.pool_monitor.processing:
                    health_data["components"]["pool_monitor"] = True
                    health_data["stats"]["pools"] = self.pool_monitor.get_pool_stats()

                # Проверка webhook
                webhook_info = await self.telegram_app.bot.get_webhook_info()
                health_data["components"]["webhook"] = bool(webhook_info.url)

                # Общий статус
                if all(health_data["components"].values()):
                    health_data["status"] = "OK"
                    return health_data, 200
                return health_data, 503

            except Exception as e:
                logger.error(f"Ошибка проверки здоровья: {e}")
                return {
                    "status": "ERROR",
                    "error": str(e),
                    "timestamp": datetime.utcnow().isoformat()
                }, 500

        @self.app.route('/')
        async def home():
            """Информационная страница"""
            return {
                "name": "Meteora Pool Monitor",
                "version": "2.0.0",
                "status": "running",
                "endpoints": {
                    "healthcheck": "/healthcheck",
                    "webhook": f"/{TELEGRAM_TOKEN}"
                }
            }

    async def run(self, host='0.0.0.0', port=PORT):
        """Запуск сервера"""
        try:
            await self.app.run_task(host=host, port=port)
        except Exception as e:
            logger.error(f"Ошибка запуска сервера: {e}")
            raise

webhook_server = WebhookServer(application, pool_monitor, filter_manager)
app = webhook_server.app

if __name__ == "__main__":
    try:
        # Настройка обработки сигналов
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda s, f: asyncio.get_event_loop().stop())

        # Запуск сервера
        asyncio.run(webhook_server.run())
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
        sys.exit(1)
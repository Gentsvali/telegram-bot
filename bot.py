import os
import logging
import asyncio
import time
import json
import signal
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# Веб-фреймворк
from quart import Quart, request

# Загрузка переменных окружения
from dotenv import load_dotenv

# Telegram Bot API
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler

# Асинхронные HTTP-запросы (если вдруг понадобится)
import httpx

# Работа с временными зонами
import pytz

# Solana WebSocket
from solana.rpc.commitment import Confirmed
import base58  # Для кодирования данных в base58 формат
from solana.rpc.websocket_api import connect, Commitment  # Для WebSocket подключения
from solders.pubkey import Pubkey
from solders.account import Account
from solders.rpc.responses import ProgramNotification

# Для работы с JSON
from json import JSONDecodeError

# Для работы с GitHub (если нужно сохранять фильтры в репозиторий)
import requests
import base64  
import websockets.exceptions

# Настройка логгера
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),  # Логирование в файл с указанием кодировки
        logging.StreamHandler()  # Логирование в консоль
    ]
)
logger = logging.getLogger(__name__)

# Уменьшаем уровень логирования для сторонних библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("solana").setLevel(logging.WARNING)  # Добавлено для Solana WebSocket
logging.getLogger("asyncio").setLevel(logging.WARNING)  # Добавлено для asyncio

# Проверка наличия обязательных переменных окружения
required_env_vars = ["TELEGRAM_TOKEN", "GITHUB_TOKEN", "USER_ID", "WEBHOOK_URL"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]

if missing_vars:
    raise ValueError(
        f"Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}. "
        "Пожалуйста, проверьте настройки."
    )

# Загрузка переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_OWNER = "Gentsvali"
REPO_NAME = "telegram-bot"
FILE_PATH = "filters.json"
USER_ID = int(os.getenv("USER_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 10000))  # Порт по умолчанию: 10000

# Дополнительные настройки (если нужно)
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"  # Режим отладки 

# Конфигурация фильтров по умолчанию
DEFAULT_FILTERS = {
    "disable_filters": False,  # Отключение фильтров (для тестирования)
    "stable_coin": "USDC",  # Стабильная монета (USDC или SOL)
    "bin_steps": [20, 80, 100, 125, 250],  # Допустимые шаги корзин
    "min_tvl": 10000.0,  # Минимальный TVL (в $)
    "min_fdv": 500000.0,  # Минимальная FDV (в $)
    "base_fee_max": 1.0,  # Максимальная базовая комиссия (в %)
    "fee_tvl_ratio_24h_min": 0.1,  # Минимальное отношение комиссии к TVL за 24 часа (в %)
    "volume_1h_min": 5000.0,  # Минимальный объем за 1 час (в $)
    "volume_5m_min": 1000.0,  # Минимальный объем за 5 минут (в $)
    "dynamic_fee_tvl_ratio_min": 0.5,  # Минимальное отношение динамической комиссии к TVL (в %)
    "verified_only": True,  # Только проверенные пулы
    "min_listing_time": 7,  # Минимальное время листинга на Meteora (в днях)
    "price_change_1h_min": 0.0,  # Минимальное изменение цены за 1 час (в %)
    "price_change_5m_min": 0.0,  # Минимальное изменение цены за 5 минут (в %)
    "fee_change_1h_min": 0.0,  # Минимальное изменение комиссии за 1 час (в %)
    "fee_change_5m_min": 0.0,  # Минимальное изменение комиссии за 5 минут (в %)
}

# Текущие фильтры (инициализируются значениями по умолчанию)
current_filters = DEFAULT_FILTERS.copy()

# Множество для хранения последних проверенных пулов (чтобы избежать дублирования уведомлений)
last_checked_pools = set()

# Инициализация приложения Telegram
application = (
    ApplicationBuilder()
    .token(TELEGRAM_TOKEN)  # Токен бота
    .concurrent_updates(True)  # Разрешение на параллельную обработку обновлений
    .http_version("1.1")  # Версия HTTP для запросов
    .get_updates_http_version("1.1")  # Версия HTTP для получения обновлений
    .build()
)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает глобальные ошибки, возникающие в боте.
    """
    try:
        # Логируем ошибку
        logger.error(f"Произошла ошибка: {context.error}", exc_info=True)

        # Отправляем сообщение об ошибке пользователю (если возможно)
        if update and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Произошла ошибка. Пожалуйста, попробуйте позже."
            )
    except Exception as e:
        # Если что-то пошло не так при обработке ошибки
        logger.error(f"Ошибка в обработчике ошибок: {e}")

# Регистрируем обработчик ошибок
application.add_error_handler(error_handler)

# Инициализация Quart приложения
app = Quart(__name__)

@app.before_serving
async def startup():
    """
    Запускает бота и инициализирует необходимые компоненты.
    """
    try:
        # Инициализация Telegram бота
        await application.initialize()
        await application.start()
        logger.info("Telegram бот успешно инициализирован ✅")

        # Установка вебхука
        await application.bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
        logger.info(f"Вебхук установлен: {WEBHOOK_URL}/{TELEGRAM_TOKEN} ✅")

        # Загрузка фильтров
        await load_filters(application)
        logger.info("Фильтры успешно загружены ✅")

        # Запуск задачи для отслеживания пулов через WebSocket
        asyncio.create_task(track_pools())
        logger.info("Задача для отслеживания пулов через WebSocket запущена ✅")

        logger.info("Приложение и вебхук успешно инициализированы 🚀")
    except Exception as e:
        logger.error(f"Ошибка при запуске приложения: {e}", exc_info=True)
        raise

@app.after_serving
async def shutdown():
    """
    Корректно завершает работу бота и освобождает ресурсы.
    """
    try:
        logger.info("Завершение работы бота...")

        # Остановка Telegram бота
        await application.stop()
        logger.info("Telegram бот успешно остановлен ✅")

        # Завершение работы Telegram бота
        await application.shutdown()
        logger.info("Telegram бот успешно завершил работу ✅")

        logger.info("Приложение успешно завершило работу 🛑")
    except Exception as e:
        logger.error(f"Ошибка при завершении работы: {e}", exc_info=True)
        raise

# Обработка сигналов для корректного завершения
def handle_shutdown(signum, frame):
    """
    Обрабатывает сигналы завершения (SIGINT, SIGTERM) и корректно останавливает бота.
    """
    logger.info(f"Получен сигнал {signum}. Останавливаю бота...")
    asyncio.create_task(shutdown())

# Регистрируем обработчики сигналов
signal.signal(signal.SIGINT, handle_shutdown)  # Обработка Ctrl+C
signal.signal(signal.SIGTERM, handle_shutdown)  # Обработка сигнала завершения (например, от systemd)

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
            "🚀 Умный поиск пулов Meteora\n"
            "Команды:\n"
            "/filters - текущие настройки\n"
            "/setfilter - изменить параметры\n"
            "/checkpools - проверить сейчас\n"
            "/help - справка по командам"
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
            f"• Стабильная монета: {current_filters['stable_coin']}\n"
            f"• Bin Steps: {', '.join(map(str, current_filters['bin_steps']))}\n"
            f"• Мин TVL: ${current_filters['min_tvl']:,.2f}\n"
            f"• Мин FDV: ${current_filters['min_fdv']:,.2f}\n"
            f"• Макс комиссия: {current_filters['base_fee_max']}%\n"
            f"• Мин комиссия/TVL: {current_filters['fee_tvl_ratio_24h_min']}%\n"
            f"• Мин объем (1ч): ${current_filters['volume_1h_min']:,.2f}\n"
            f"• Мин объем (5м): ${current_filters['volume_5m_min']:,.2f}\n"
            f"• Мин динамическая комиссия: {current_filters['dynamic_fee_tvl_ratio_min']}%\n"
            f"• Только проверенные: {'Да' if current_filters['verified_only'] else 'Нет'}\n"
            f"• Мин время листинга: {current_filters['min_listing_time']} дней\n"
            f"• Мин изменение цены (1ч): {current_filters['price_change_1h_min']}%\n"
            f"• Мин изменение цены (5м): {current_filters['price_change_5m_min']}%\n"
            f"• Мин изменение комиссии (1ч): {current_filters['fee_change_1h_min']}%\n"
            f"• Мин изменение комиссии (5м): {current_filters['fee_change_5m_min']}%"
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
        if param == "stable_coin":
            if value.upper() not in ["USDC", "SOL"]:
                raise ValueError("Допустимые значения: USDC или SOL")
            current_filters[param] = value.upper()
        
        elif param == "bin_steps":
            current_filters[param] = [int(v.strip()) for v in value.split(',')]
        
        elif param in ["min_tvl", "min_fdv", "base_fee_max", 
                      "fee_tvl_ratio_24h_min", "volume_1h_min", 
                      "volume_5m_min", "dynamic_fee_tvl_ratio_min",
                      "min_listing_time", "price_change_1h_min",
                      "price_change_5m_min", "fee_change_1h_min",
                      "fee_change_5m_min"]:
            current_filters[param] = float(value)
        
        elif param == "verified_only":
            if value.lower() not in ["true", "false"]:
                raise ValueError("Допустимые значения: true или false")
            current_filters[param] = value.lower() == "true"
        
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

# Добавьте новые константы для буферизации
BUFFER_TIMEOUT = 300  # 5 минут
MESSAGE_BATCH_SIZE = 5  # Количество сообщений в одной группе

class MessageBuffer:
    def __init__(self):
        self.messages = []
        self.last_process_time = time.time()
    
    async def add_message(self, message):
        self.messages.append(message)
        await self.process_if_needed()
    
    async def process_if_needed(self):
        current_time = time.time()
        if (len(self.messages) >= MESSAGE_BATCH_SIZE or 
            current_time - self.last_process_time >= BUFFER_TIMEOUT):
            await self.process_messages()
    
    async def process_messages(self):
        if not self.messages:
            return
            
        try:
            # Группируем сообщения по пулам
            unique_messages = {}
            for msg in self.messages:
                pool_key = msg.get("pubkey", "unknown")
                unique_messages[pool_key] = msg
            
            # Форматируем и отправляем сообщения
            formatted_messages = []
            for msg in unique_messages.values():
                if filter_pool(msg):  # Используем существующую функцию фильтрации
                    formatted_messages.append(format_pool_message(msg))
            
            if formatted_messages:
                message_text = "\n\n".join(formatted_messages)
                await application.bot.send_message(
                    chat_id=USER_ID,
                    text=message_text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
            
            self.messages.clear()
            self.last_process_time = time.time()
            
        except Exception as e:
            logger.error(f"Ошибка обработки сообщений: {e}", exc_info=True)

# Создаем глобальный буфер сообщений
message_buffer = MessageBuffer()

async def track_pools():
    ws_url = "wss://api.mainnet-beta.solana.com"
    program_id = Pubkey.from_string("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")
    commitment = "confirmed"
    
    last_processed = {}
    RATE_LIMIT = 15  # секунд между обработками одного пула
    MAX_REQUESTS_PER_MINUTE = 60

    while True:
        try:
            async with connect(ws_url) as websocket:
                subscription = await websocket.program_subscribe(
                    program_id,
                    encoding="jsonParsed",
                    commitment=commitment,
                    filters=[{"dataSize": 165}]  # Размер данных пула
                )
                logger.info("WebSocket подключен к Solana ✅")

                request_count = 0
                minute_start = time.time()

                async for msg in websocket:
                    try:
                        current_time = time.time()
                        
                        # Контроль частоты запросов
                        if current_time - minute_start > 60:
                            request_count = 0
                            minute_start = current_time
                        
                        if request_count >= MAX_REQUESTS_PER_MINUTE:
                            await asyncio.sleep(60 - (current_time - minute_start))
                            continue

                        if hasattr(msg, "result") and hasattr(msg.result, "value"):
                            pool_data = msg.result.value
                            pool_key = str(pool_data.get("pubkey", ""))

                            # Проверка частоты обработки пула
                            if pool_key in last_processed:
                                time_since_last = current_time - last_processed[pool_key]
                                if time_since_last < RATE_LIMIT:
                                    continue

                            # Добавляем сообщение в буфер
                            await message_buffer.add_message(pool_data)
                            
                            request_count += 1
                            last_processed[pool_key] = current_time

                    except Exception as e:
                        logger.error(f"Ошибка обработки сообщения: {e}")
                        await asyncio.sleep(1)

        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket соединение закрыто, переподключение...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Ошибка WebSocket: {e}")
            await asyncio.sleep(5)

def decode_pool_data(data: bytes) -> dict:
    """
    Декодирует бинарные данные пула в словарь.
    """
    try:
        # Пример декодирования данных (зависит от структуры данных пула)
        # Здесь нужно использовать документацию Solana или Meteora
        decoded_data = {
            "mint_x": data[:32].hex(),  # Первые 32 байта — mint_x
            "mint_y": data[32:64].hex(),  # Следующие 32 байта — mint_y
            "liquidity": int.from_bytes(data[64:72], byteorder="little"),  # Ликвидность
            "volume_1h": int.from_bytes(data[72:80], byteorder="little"),  # Объем за 1 час
            "volume_5m": int.from_bytes(data[80:88], byteorder="little"),  # Объем за 5 минут
            "bin_step": int.from_bytes(data[88:96], byteorder="little"),  # Шаг корзины
            "base_fee": int.from_bytes(data[96:104], byteorder="little") / 100,  # Базовая комиссия (в %)
            "is_verified": bool(data[104]),  # Проверен ли пул
            "listing_time": int.from_bytes(data[105:113], byteorder="little"),  # Время листинга (в днях)
            "price_change_1h": int.from_bytes(data[113:121], byteorder="little") / 100,  # Изменение цены за 1 час (в %)
            "price_change_5m": int.from_bytes(data[121:129], byteorder="little") / 100,  # Изменение цены за 5 минут (в %)
            "fee_change_1h": int.from_bytes(data[129:137], byteorder="little") / 100,  # Изменение комиссии за 1 час (в %)
            "fee_change_5m": int.from_bytes(data[137:145], byteorder="little") / 100,  # Изменение комиссии за 5 минут (в %)
        }
        return decoded_data
    except Exception as e:
        logger.error(f"Ошибка декодирования данных пула: {e}", exc_info=True)
        return {}

async def handle_pool_change(pool_data):
    try:
        if not pool_data:
            return False

        # Базовая валидация данных
        if not all(key in pool_data.get("account", {}).get("data", {}) 
                  for key in ["bin_step", "liquidity", "volume_1h"]):
            return False

        pool_info = {
            "address": str(pool_data.get("pubkey", "N/A")),
            "bin_step": int(pool_data["account"]["data"]["bin_step"]),
            "tvl": float(pool_data["account"]["data"]["liquidity"]),
            "volume_1h": float(pool_data["account"]["data"]["volume_1h"])
        }

        # Применяем только основные фильтры для экономии ресурсов
        if (pool_info["bin_step"] in current_filters["bin_steps"] and
            pool_info["tvl"] >= current_filters["min_tvl"] and
            pool_info["volume_1h"] >= current_filters["volume_1h_min"]):

            message = format_pool_message(pool_info)
            await application.bot.send_message(
                chat_id=USER_ID,
                text=message,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            return True

        return False

    except Exception as e:
        logger.error(f"Ошибка обработки данных пула: {e}")
        return False

async def update_filters_via_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обновляет фильтры на основе JSON-сообщения.
    """
    if update.effective_user.id != USER_ID:
        logger.warning(f"Попытка доступа от неавторизованного пользователя: {update.effective_user.id}")
        return

    try:
        # Парсим JSON из сообщения
        new_filters = json.loads(update.message.text)
        
        if not isinstance(new_filters, dict):
            raise ValueError("Некорректный формат JSON. Ожидается словарь.")

        # Обновляем текущие фильтры
        for key, value in new_filters.items():
            if key in current_filters:
                # Проверяем тип значения для некоторых параметров
                if key == "bin_steps" and isinstance(value, str):
                    value = [int(v.strip()) for v in value.split(',')]
                elif key == "verified_only" and isinstance(value, str):
                    value = value.lower() == "true"
                current_filters[key] = value
            else:
                logger.warning(f"Неизвестный параметр фильтра: {key}")

        # Сохраняем фильтры
        await save_filters(update, context)
        await update.message.reply_text("✅ Фильтры успешно обновлены!")
        await show_filters(update, context)
        logger.info(f"Пользователь {update.effective_user.id} обновил фильтры через JSON")
    
    except json.JSONDecodeError:
        await update.message.reply_text("❌ Ошибка: Некорректный JSON. Проверьте формат.")
        logger.warning(f"Ошибка декодирования JSON от пользователя {update.effective_user.id}")
    except ValueError as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        logger.warning(f"Ошибка при обновлении фильтров: {e}")
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
    Фильтрует пул на основе текущих фильтров.
    Возвращает True, если пул соответствует критериям.
    """
    # Если фильтрация отключена, возвращаем True для всех пулов
    if current_filters.get("disable_filters", False):
        return True

    try:
        # Получаем данные пула
        tvl = float(pool.get("liquidity", 0))
        if tvl <= 0:
            return False

        # Формируем метрики пула
        pool_metrics = {
            "bin_step": int(pool.get("bin_step", 999)),
            "base_fee": float(pool.get("base_fee_percentage", 100)),
            "fee_24h": float(pool.get("fees_24h", 0)),
            "volume_1h": float(pool.get("volume", {}).get("hour_1", 0)),
            "volume_5m": float(pool.get("volume", {}).get("min_30", 0)) * 2,
            "dynamic_fee": float(pool.get("fee_tvl_ratio", {}).get("hour_1", 0)),
            "tvl": tvl,
            "is_verified": bool(pool.get("is_verified", False)),
            "listing_time": int(pool.get("listing_time", 0)),
            "price_change_1h": float(pool.get("price_change_1h", 0)),
            "price_change_5m": float(pool.get("price_change_5m", 0)),
            "fee_change_1h": float(pool.get("fee_change_1h", 0)),
            "fee_change_5m": float(pool.get("fee_change_5m", 0)),
        }

        # Проверяем стабильную монету
        stable_mints = {
            "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "SOL": "So11111111111111111111111111111111111111112",
        }
        stable_mint = stable_mints[current_filters["stable_coin"]]
        is_stable_pair = (pool.get("mint_x") == stable_mint) or (pool.get("mint_y") == stable_mint)

        # Рассчитываем отношение комиссии к TVL
        fee_tvl_ratio = (pool_metrics["fee_24h"] / pool_metrics["tvl"] * 100) if pool_metrics["tvl"] > 0 else 0

        # Применяем фильтры
        conditions = [
            is_stable_pair,  # Пул должен содержать стабильную монету
            pool_metrics["bin_step"] in current_filters["bin_steps"],
            pool_metrics["base_fee"] <= current_filters["base_fee_max"],
            fee_tvl_ratio >= current_filters["fee_tvl_ratio_24h_min"],
            pool_metrics["volume_1h"] >= current_filters["volume_1h_min"],
            pool_metrics["volume_5m"] >= current_filters["volume_5m_min"],
            pool_metrics["dynamic_fee"] >= current_filters["dynamic_fee_tvl_ratio_min"],
            pool_metrics["tvl"] >= current_filters["min_tvl"],
            (not current_filters["verified_only"]) or pool_metrics["is_verified"],  # Проверенные пулы
            pool_metrics["listing_time"] >= current_filters["min_listing_time"],
            pool_metrics["price_change_1h"] >= current_filters["price_change_1h_min"],
            pool_metrics["price_change_5m"] >= current_filters["price_change_5m_min"],
            pool_metrics["fee_change_1h"] >= current_filters["fee_change_1h_min"],
            pool_metrics["fee_change_5m"] >= current_filters["fee_change_5m_min"],
        ]

        # Возвращаем True, если все условия выполнены
        return all(conditions)
    
    except Exception as e:
        logger.error(f"Ошибка фильтрации пула: {e}", exc_info=True)
        return False

def get_non_sol_token(mint_x: str, mint_y: str) -> str:
    """
    Возвращает токен, который не является Solana.
    """
    sol_mint = "So11111111111111111111111111111111111111112"
    if mint_x == sol_mint:
        return mint_y
    elif mint_y == sol_mint:
        return mint_x
    else:
        return mint_x  # Если оба токена не Solana, возвращаем первый

def save_filters_to_file():
    """
    Сохраняет текущие фильтры в файл.
    """
    try:
        with open(FILE_PATH, "w", encoding="utf-8") as file:
            json.dump(current_filters, file, indent=4, ensure_ascii=False)
        logger.info("Фильтры сохранены в файл ✅")
    except Exception as e:
        logger.error(f"Ошибка при сохранении фильтров в файл: {e}", exc_info=True)

def load_filters_from_file():
    """
    Загружает фильтры из файла.
    """
    global current_filters
    try:
        if os.path.exists(FILE_PATH):
            with open(FILE_PATH, "r", encoding="utf-8") as file:
                loaded_filters = json.load(file)
                current_filters.update(loaded_filters)
                logger.info("Фильтры загружены из файла ✅")
        else:
            logger.info("Файл с фильтрами не найден. Использую настройки по умолчанию.")
    except Exception as e:
        logger.error(f"Ошибка при загрузке фильтров из файла: {e}", exc_info=True)

def get_clean_filters() -> dict:
    """
    Возвращает только те поля, которые относятся к настройкам фильтров.
    """
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
        "verified_only": current_filters.get("verified_only", True),
        "min_listing_time": current_filters.get("min_listing_time", 7),
        "price_change_1h_min": current_filters.get("price_change_1h_min", 0.0),
        "price_change_5m_min": current_filters.get("price_change_5m_min", 0.0),
        "fee_change_1h_min": current_filters.get("fee_change_1h_min", 0.0),
        "fee_change_5m_min": current_filters.get("fee_change_5m_min", 0.0),
    }

def format_pool_message(pool: dict) -> str:
    """
    Форматирует информацию о пуле в сообщение для Telegram.
    """
    try:
        address = pool.get("address", "N/A")
        mint_x = pool.get("mint_x", "?")
        mint_y = pool.get("mint_y", "?")
        tvl = float(pool.get("liquidity", 0)) / 1e9  # Переводим lamports в SOL
        volume_1h = float(pool.get("volume_1h", 0)) / 1e9  # Переводим lamports в SOL
        volume_5m = float(pool.get("volume_5m", 0)) / 1e9  # Переводим lamports в SOL
        bin_step = int(pool.get("bin_step", 0))
        base_fee = float(pool.get("base_fee", 0))
        price_change_1h = float(pool.get("price_change_1h", 0))
        price_change_5m = float(pool.get("price_change_5m", 0))
        fee_change_1h = float(pool.get("fee_change_1h", 0))
        fee_change_5m = float(pool.get("fee_change_5m", 0))

        # Формируем сообщение
        message = (
            f"⭐️ {mint_x[:4]}-{mint_y[:4]} (https://dexscreener.com/solana/{address})\n"
            f"☄️ Метеоры (https://edge.meteora.ag/dlmm/{address})\n"
            f"😼 Наборы (https://trench.bot/bundles/{mint_x}?all=true)\n"
            f"🟢 ТВЛ - {tvl:,.2f} SOL\n"
            f"📊 Объем (1ч) - {volume_1h:,.2f} SOL\n"
            f"📊 Объем (5м) - {volume_5m:,.2f} SOL\n"
            f"⚙️ Шаг корзины - {bin_step}\n"
            f"💸 Базовая комиссия - {base_fee:.2f}%\n"
            f"📈 Изменение цены (1ч) - {price_change_1h:.2f}%\n"
            f"📈 Изменение цены (5м) - {price_change_5m:.2f}%\n"
            f"📊 Изменение комиссии (1ч) - {fee_change_1h:.2f}%\n"
            f"📊 Изменение комиссии (5м) - {fee_change_5m:.2f}%"
        )
        return message
    except Exception as e:
        logger.error(f"Ошибка форматирования пула {pool.get('address')}: {e}", exc_info=True)
        return "⚠️ Ошибка при формировании информации о пуле"

async def check_new_pools(context: ContextTypes.DEFAULT_TYPE):
    """
    Проверяет новые пулы и отправляет уведомления, если они соответствуют фильтрам.
    """
    global last_checked_pools
    logger.info("Запуск проверки пулов...")

    try:
        if not context or not hasattr(context, 'bot'):
            logger.error("Некорректный контекст")
            return

        # Получаем список пулов
        pools = await fetch_pools()
        if not pools:
            logger.info("Нет доступных пулов для проверки")
            return

        new_pools = []
        
        # Проверяем каждый пул
        for pool in pools:
            if pool["address"] not in last_checked_pools:
                try:
                    if filter_pool(pool):
                        new_pools.append(pool)
                except Exception as e:
                    logger.error(f"Ошибка фильтрации пула {pool.get('address')}: {e}", exc_info=True)

        # Если найдены новые пулы, отправляем уведомления
        if new_pools:
            logger.info(f"Найдено {len(new_pools)} новых пулов")
            for pool in new_pools:
                try:
                    message = format_pool_message(pool)
                    if message:
                        await context.bot.send_message(
                            chat_id=USER_ID,
                            text=message,
                            parse_mode="Markdown",
                            disable_web_page_preview=True
                        )
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения о пуле {pool.get('address')}: {e}", exc_info=True)

            # Обновляем список проверенных пулов
            last_checked_pools.update(pool["address"] for pool in pools)
        else:
            logger.info("Новых подходящих пулов не найдено")
    
    except Exception as e:
        logger.error(f"Ошибка проверки пулов: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=USER_ID,
            text="⚠️ Ошибка при проверке пулов"
        )

# Регистрация обработчиков команд
application.add_handler(CommandHandler("start", start))  # Команда /start
application.add_handler(CommandHandler("filters", show_filters))  # Команда /filters
application.add_handler(CommandHandler("setfilter", set_filter))  # Команда /setfilter
application.add_handler(CommandHandler("checkpools", check_new_pools))  # Команда /checkpools
application.add_handler(CommandHandler("getfiltersjson", get_filters_json))  # Команда /getfiltersjson
application.add_handler(MessageHandler(filters=None, callback=update_filters_via_json))  # Обработка JSON-сообщений

# Вебхук
@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
async def webhook():
    """
    Обрабатывает входящие запросы от Telegram через вебхук.
    """
    try:
        # Получаем данные из запроса
        data = await request.get_json()
        if not data:
            logger.error("Получен пустой запрос от Telegram")
            return '', 400

        # Создаем объект Update и обрабатываем его
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return '', 200
    except Exception as e:
        logger.error(f"Ошибка в вебхуке: {e}", exc_info=True)
        return '', 500

# Healthcheck
@app.route('/healthcheck')
def healthcheck():
    """
    Возвращает статус работы сервиса.
    """
    return {"status": "OK"}, 200

# Главная страница
@app.route('/')
async def home():
    """
    Возвращает статус работы сервиса.
    """
    return {"status": "OK"}, 200

# Функции для работы с закрепленными сообщениями
async def save_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Сохраняет фильтры в GitHub.
    """
    try:
        save_filters_to_github()
        logger.info("Фильтры сохранены в GitHub ✅")
    except Exception as e:
        logger.error(f"Ошибка при сохранении фильтров: {e}", exc_info=True)
        await update.message.reply_text("❌ Не удалось сохранить настройки!")

async def load_filters(context: ContextTypes.DEFAULT_TYPE):
    """
    Загружает фильтры из GitHub.
    """
    global current_filters
    try:
        load_filters_from_github()
        logger.info("Фильтры успешно загружены из GitHub ✅")
    except Exception as e:
        logger.error(f"Ошибка при загрузке фильтров: {e}", exc_info=True)

# Обработка сигналов для корректного завершения
def handle_shutdown(signum, frame):
    """
    Обрабатывает сигналы завершения (SIGINT, SIGTERM) и корректно останавливает бота.
    """
    logger.info(f"Получен сигнал {signum}. Останавливаю бота...")
    asyncio.create_task(shutdown())

# Регистрируем обработчики сигналов
signal.signal(signal.SIGINT, handle_shutdown)  # Обработка Ctrl+C
signal.signal(signal.SIGTERM, handle_shutdown)  # Обработка сигнала завершения (например, от systemd)

# Запуск приложения
if __name__ == "__main__":
    logger.info(f"Запуск бота на порту {PORT}...")
    app.run(host='0.0.0.0', port=PORT)
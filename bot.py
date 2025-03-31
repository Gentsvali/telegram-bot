import os
import sys
import logging
import asyncio
import json
import httpx
from datetime import datetime
from typing import Dict, List, Optional
from quart import Quart, request
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.signature import Signature
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import base64

# Загрузка переменных окружения
load_dotenv()

# Настройка логгера
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Уменьшаем уровень логирования для сторонних библиотек
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# Проверка обязательных переменных окружения
required_env_vars = ["TELEGRAM_TOKEN", "GITHUB_TOKEN", "USER_ID", "WEBHOOK_URL"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    error_message = f"Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}"
    logger.error(error_message)
    raise ValueError(error_message)

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_OWNER = "Gentsvali"
REPO_NAME = "telegram-bot"
FILE_PATH = "filters.json"
USER_ID = int(os.getenv("USER_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 10000))
DLMM_PROGRAM_ID = Pubkey.from_string("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
POLL_INTERVAL = 300  # 5 минут

# Фильтры по умолчанию
DEFAULT_FILTERS = {
    "disable_filters": False,
    "bin_steps": [20, 80, 100, 125, 250],
    "min_tvl": 10.0,
    "base_fee_min": 0.1,
    "base_fee_max": 10.0,
    "volume_1h_min": 10.0,
    "volume_5m_min": 1.0,
    "fee_tvl_ratio_24h_min": 0.1,
    "dynamic_fee_tvl_ratio_min": 0.5,
}

# Глобальные переменные
current_filters = DEFAULT_FILTERS.copy()
application = None
pool_tracker = None

class PoolTracker:
    def __init__(self):
        self.last_signature = None
        self.known_pools = set()
        self.running = False

    async def start_tracking(self):
        """Запуск мониторинга новых пулов"""
        if self.running:
            return

        self.running = True
        logger.info("Запуск мониторинга DLMM пулов...")

        while self.running:
            try:
                async with AsyncClient(SOLANA_RPC_URL) as client:
                    signatures = (
                        await client.get_signatures_for_address(
                            DLMM_PROGRAM_ID,
                            before=self.last_signature,
                            limit=5,
                            commitment="confirmed",
                        )
                    ).value

                    if signatures:
                        self.last_signature = signatures[0].signature
                        logger.info(f"Найдено новых транзакций: {len(signatures)}")
                        for sig in signatures:
                            await self.process_transaction(client, sig.signature)

            except Exception as e:
                logger.error(f"Ошибка мониторинга: {str(e)}")
            
            await asyncio.sleep(POLL_INTERVAL)

    async def process_transaction(self, client, signature):
        """Анализ транзакции"""
        try:
            tx = await client.get_transaction(
                signature, 
                encoding="jsonParsed", 
                max_supported_transaction_version=0
            )
            if not tx.value:
                return
            logger.info(f"Обработана транзакция: {signature}")
        except Exception as e:
            logger.error(f"Ошибка обработки транзакции: {e}")

    async def stop_tracking(self):
        """Остановка мониторинга"""
        self.running = False
        logger.info("Мониторинг DLMM пулов остановлен")

# Инициализация Quart приложения
app = Quart(__name__)

async def setup_bot():
    """Инициализация Telegram бота"""
    global application
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    application.add_error_handler(error_handler)
    setup_command_handlers(application)
    return application

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик ошибок бота"""
    logger.error(f"Ошибка при обработке обновления: {context.error}", exc_info=context.error)
    if update and isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")

def setup_command_handlers(application):
    """Настройка обработчиков команд"""
    try:
        # Основные команды
        application.add_handler(
            CommandHandler("start", start, filters=filters.User(user_id=USER_ID))
        )

        # Команды управления фильтрами
        filter_handlers = [
            CommandHandler(
                "filters", show_filters, filters=filters.User(user_id=USER_ID))
            ),
            CommandHandler(
                "setfilter", set_filter, filters=filters.User(user_id=USER_ID))
            ),
            CommandHandler(
                "getfiltersjson", get_filters_json, filters=filters.User(user_id=USER_ID))
            ),
            MessageHandler(
                filters=filters.User(user_id=USER_ID) & filters.TEXT & ~filters.COMMAND,
                callback=update_filters_via_json,
            ),
        ]
        for handler in filter_handlers:
            application.add_handler(handler)

        # Команды мониторинга
        application.add_handler(
            CommandHandler("trackpools", start_pool_tracking, filters=filters.User(user_id=USER_ID))
        )
        application.add_handler(
            CommandHandler("stoptracking", stop_pool_tracking, filters=filters.User(user_id=USER_ID))
        )

        # Обработчик неизвестных команд
        application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

        logger.info("✅ Обработчики команд успешно зарегистрированы")

    except Exception as e:
        logger.error(f"❌ Ошибка при настройке обработчиков команд: {e}", exc_info=True)
        raise

@app.before_serving
async def startup():
    """Инициализация приложения"""
    global application, pool_tracker
    
    logger.info("Starting initialization...")
    
    # Инициализация бота
    application = await setup_bot()
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    logger.info("Bot initialized and webhook set")

    # Инициализация трекера
    pool_tracker = PoolTracker()
    
    # Загрузка фильтров
    await load_filters()
    
    # Запуск трекера
    asyncio.create_task(pool_tracker.start_tracking())
    logger.info("Pool tracker started")

@app.after_serving
async def shutdown_app():
    """Корректное завершение работы"""
    try:
        logger.info("Завершение работы приложения...")
        if application and application.running:
            await application.stop()
            await application.shutdown()
            logger.info("Бот успешно остановлен")
        if pool_tracker and pool_tracker.running:
            await pool_tracker.stop_tracking()
            logger.info("Трекер пулов остановлен")
    except Exception as e:
        logger.error(f"Ошибка при завершении работы: {e}")



async def send_pool_alert(pool: dict):
    """Отправляет уведомление о новом пуле в Telegram."""
    try:
        message = format_pool_message(pool)
        if message:
            await application.bot.send_message(
                chat_id=USER_ID,
                text=message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления: {e}")


def filter_pool(pool: dict) -> bool:
    """Фильтрует DLMM пул на основе текущих фильтров."""
    if current_filters.get("disable_filters", False):
        return True

    try:
        conditions = [
            pool.get("bin_step") in current_filters["bin_steps"],
            current_filters["base_fee_min"]
            <= pool.get("fee", 0)
            <= current_filters["base_fee_max"],
            pool.get("tvl", 0) >= current_filters["min_tvl"],
            pool.get("volume_1h", 0) >= current_filters["volume_1h_min"],
            pool.get("volume_5m", 0) >= current_filters["volume_5m_min"],
            pool.get("fee_tvl_ratio_24h", 0)
            >= current_filters["fee_tvl_ratio_24h_min"],
            pool.get("dynamic_fee_tvl_ratio", 0)
            >= current_filters["dynamic_fee_tvl_ratio_min"],
        ]

        return all(conditions)
    except Exception as e:
        logger.error(f"Ошибка фильтрации пула: {e}")
        return False


def format_pool_message(pool: dict) -> str:
    """Форматирует информацию о пуле в сообщение для Telegram"""
    try:
        return (
            f"⭐️ {pool.get('token_x_symbol', '?')}-{pool.get('token_y_symbol', '?')} "
            f"(https://dexscreener.com/solana/{pool.get('address', '')})\n"
            f"☄️ Метеоры (https://edge.meteora.ag/dlmm/{pool.get('address', '')})\n"
            f"🟢 ТВЛ - {pool.get('tvl', 0):,.2f} SOL\n"
            f"📊 Объем (1ч) - {pool.get('volume_1h', 0):,.2f} SOL\n"
            f"📊 Объем (5м) - {pool.get('volume_5m', 0):,.2f} SOL\n"
            f"⚙️ Шаг корзины - {pool.get('bin_step', 'N/A')}\n"
            f"💸 Комиссия - {pool.get('fee', 0):.2f}%\n"
            f"📈 Изменение цены (1ч) - {pool.get('price_change_1h', 0):.2f}%\n"
            f"📈 Изменение цены (5м) - {pool.get('price_change_5m', 0):.2f}%"
        )
    except Exception as e:
        logger.error(f"Ошибка форматирования сообщения: {e}")
        return None


async def track_dlmm_pools():
    """Основная функция мониторинга пулов"""
    while True:
        try:
            pools = await fetch_dlmm_pools()  # Сначала получаем пулы
            if not pools:
                logger.warning("Не удалось получить данные пулов")
                await asyncio.sleep(60)
                continue

            new_pools_found = 0
            for pool in pools:  # Исправлено: pools вместо pool
                if filter_pool(pool):
                    await send_pool_alert(pool)
                    new_pools_found += 1

            logger.info(
                f"Проверено {len(pools)} пулов, найдено {new_pools_found} новых"
            )

        except Exception as e:
            logger.error(f"Критическая ошибка мониторинга пулов: {e}")
            await asyncio.sleep(300)  # Увеличиваем интервал при ошибках
        else:
            await asyncio.sleep(60)  # Нормальный интервал проверки


async def handle_pool_change(pool_data: bytes):
    """Обработка изменений пула с проверкой структуры данных"""
    required_fields = [
        "address",
        "mint_x",
        "mint_y",
        "liquidity",
        "volume_1h",
        "volume_5m",
        "bin_step",
        "base_fee",
    ]

    try:
        # Проверка наличия всех обязательных полей
        if not all(field in pool_data for field in required_fields):
            raise ValueError("Отсутствуют обязательные поля в данных пула")

        address = pool_data["address"]

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
            disable_web_page_preview=True,
        )

    except Exception as e:
        logger.error(
            f"Ошибка обработки пула {pool_data.get('address', 'unknown')}: {e}"
        )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик неизвестных команд"""
    if update.effective_user.id != USER_ID:
        return

    await update.message.reply_text(
        "❌ Неизвестная команда. Доступные команды:\n"
        "/start - начать работу\n"
        "/filters - показать фильтры\n"
        "/setfilter - изменить фильтры\n"
        "/checkpools - проверить пулы"
    )


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
                    sha = (
                        response.json().get("sha")
                        if response.status_code == 200
                        else None
                    )

                    # Отправляем обновление
                    with open(FILE_PATH, "rb") as f:
                        content = base64.b64encode(f.read()).decode()

                    data = {
                        "message": "Automatic filters update",
                        "content": content,
                        "sha": sha,
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
        if text.startswith("/"):
            text = " ".join(text.split()[1:])

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
        logger.info(
            f"Пользователь {update.effective_user.id} обновил фильтры через JSON"
        )

    except json.JSONDecodeError:
        example_filters = json.dumps(DEFAULT_FILTERS, indent=4)
        await update.message.reply_text(
            "❌ Ошибка: Некорректный JSON. Проверьте формат.\n"
            f"Пример корректного JSON:\n```json\n{example_filters}\n```",
            parse_mode="Markdown",
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    except Exception as e:
        await update.message.reply_text(
            "⚠️ Произошла ошибка. Пожалуйста, попробуйте позже."
        )
        logger.error(f"Ошибка при обработке JSON-сообщения: {e}", exc_info=True)


async def get_filters_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет текущие настройки фильтров в формате JSON.
    """
    if update.effective_user.id != USER_ID:
        logger.warning(
            f"Попытка доступа от неавторизованного пользователя: {update.effective_user.id}"
        )
        return

    try:
        # Формируем JSON с текущими фильтрами
        filters_json = json.dumps(current_filters, indent=4)

        # Отправляем JSON-сообщение
        await update.message.reply_text(
            f"Текущие настройки фильтров:\n```json\n{filters_json}\n```",
            parse_mode="Markdown",
        )
        logger.info(
            f"Пользователь {update.effective_user.id} запросил текущие фильтры в формате JSON"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        logger.error(
            f"Ошибка при обработке команды /getfiltersjson: {e}", exc_info=True
        )


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
            pool.get("fee_tvl_ratio_24h", 0)
            >= current_filters["fee_tvl_ratio_24h_min"],
            pool.get("dynamic_fee_tvl_ratio", 0)
            >= current_filters["dynamic_fee_tvl_ratio_min"],
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
        required_fields = [
            "address",
            "mint_x",
            "mint_y",
            "liquidity",
            "volume_1h",
            "volume_5m",
            "bin_step",
            "base_fee",
        ]
        if not all(field in pool for field in required_fields):
            raise ValueError("Отсутствуют обязательные поля в данных пула")

        # Получение и валидация значений
        values = {
            "address": str(pool.get("address", "N/A")),
            "mint_x": str(pool.get("mint_x", "?")),
            "mint_y": str(pool.get("mint_y", "?")),
            "tvl": max(0.0, float(pool.get("liquidity", 0)) / 1e9),
            "volume_1h": max(0.0, float(pool.get("volume_1h", 0)) / 1e9),
            "volume_5m": max(0.0, float(pool.get("volume_5m", 0)) / 1e9),
            "bin_step": max(0, int(pool.get("bin_step", 0))),
            "base_fee": max(0.0, float(pool.get("base_fee", 0))),
            "price_change_1h": float(pool.get("price_change_1h", 0)),
            "price_change_5m": float(pool.get("price_change_5m", 0)),
            "fee_change_1h": float(pool.get("fee_change_1h", 0)),
            "fee_change_5m": float(pool.get("fee_change_5m", 0)),
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
        logger.error(
            f"Непредвиденная ошибка при форматировании пула {pool.get('address', 'N/A')}: {e}",
            exc_info=True,
        )
        return None


async def check_new_pools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /checkpools"""
    try:
        await track_dlmm_pools()
        await update.message.reply_text("✅ Проверка пулов запущена")
    except Exception as e:
        logger.error(f"Ошибка проверки пулов: {e}")
        await update.message.reply_text("❌ Ошибка при проверке пулов")


class PoolTracker:
    def __init__(self):
        self.last_signature = None
        self.known_pools = set()
        self.running = False

    async def start_tracking(self):
        """Запуск мониторинга новых пулов"""
        if self.running:
            return

        self.running = True
        logger.info("Запуск мониторинга DLMM пулов...")

        while self.running:
            try:
                async with AsyncClient(SOLANA_RPC_URL) as client:
                    # Получаем последние транзакции
                    signatures = (
                        await client.get_signatures_for_address(
                            DLMM_PROGRAM_ID,
                            before=self.last_signature,
                            limit=5,
                            commitment="confirmed",
                        )
                    ).value

                    if signatures:
                        self.last_signature = signatures[0].signature
                        logger.info(f"Найдено новых транзакций: {len(signatures)}")

                        for sig in signatures:
                            await self.process_transaction(client, sig.signature)

            except Exception as e:
                logger.error(f"Ошибка мониторинга: {str(e)}")
            
            await asyncio.sleep(300)

    async def process_transaction(self, client, signature):
        """Анализ транзакции"""
        try:
            tx = await client.get_transaction(
                signature, encoding="jsonParsed", max_supported_transaction_version=0
            )
            if not tx.value:
                return
            logger.info(f"Обработана транзакция: {signature}")
        except Exception as e:
            logger.error(f"Ошибка обработки транзакции: {e}")

    async def stop_tracking(self):
        """Остановка мониторинга"""
        self.running = False
        logger.info("Мониторинг DLMM пулов остановлен")

# Создание экземпляра класса ВНЕ класса
tracker = PoolTracker()

def setup_command_handlers(application):
    """
    Настраивает обработчики команд для бота с группировкой по функциональности.
    """
    try:
        # Основные команды
        application.add_handler(
            CommandHandler("start", start, filters=filters.User(user_id=USER_ID))
        )

        # Команды управления фильтрами
        filter_handlers = [
            CommandHandler(
                "filters", show_filters, filters=filters.User(user_id=USER_ID)
            ),
            CommandHandler(
                "setfilter", set_filter, filters=filters.User(user_id=USER_ID)
            ),
            CommandHandler(
                "getfiltersjson",
                get_filters_json,
                filters=filters.User(user_id=USER_ID),
            ),
            MessageHandler(
                filters=filters.User(user_id=USER_ID) & filters.TEXT & ~filters.COMMAND,
                callback=update_filters_via_json,
            ),
        ]
        for handler in filter_handlers:
            application.add_handler(handler)

        # Команды мониторинга
        application.add_handler(CommandHandler("trackpools", start_pool_tracking))

        application.add_handler(CommandHandler("stoptracking", stop_pool_tracking))

        # Обработчик неизвестных команд
        application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

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
        "/checkpools - проверить пулы"
    )


# Конфигурация веб-хуков и маршрутов
class WebhookConfig:
    """Конфигурация для веб-хуков и маршрутов"""

    WEBHOOK_TIMEOUT = 30  # Тайм-аут для веб-хука в секундах
    MAX_RETRIES = 3  # Максимальное количество попыток
    RETRY_DELAY = 1  # Задержка между попытками в секундах


# Вебхук с улучшенной обработкой ошибок
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
async def webhook():
    if not application.running:
        return "Bot not initialized", 503

    try:
        update = Update.de_json(await request.get_json(), application.bot)
        await application.update_queue.put(update)  # Используем очередь обновлений
        return "", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "Error", 500


# Расширенный healthcheck
@app.route("/healthcheck")
async def healthcheck():
    """Расширенная проверка состояния сервиса."""
    try:
        health_status = {
            "status": "ERROR",
            "components": {
                "telegram_bot": False,
                "solana_connection": False,
                "webhook": False,
            },
            "timestamp": datetime.isoformat(),
        }

        # Проверка бота
        if application.running:
            health_status["components"]["telegram_bot"] = True

        # Проверка подключения к Solana
        try:
            # await asyncio.wait_for(check_connection(), timeout=5)
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
            "timestamp": datetime.utcnow().isoformat(),
        }, 500


# Главная страница с расширенной информацией
@app.route("/")
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
                "webhook": f"/{TELEGRAM_TOKEN}",
            },
            "documentation": "https://github.com/yourusername/yourrepo",
            "timestamp": datetime.utcnow().isoformat(),
        }, 200
    except Exception as e:
        logger.error(f"Ошибка на главной странице: {e}", exc_info=True)
        return {"status": "ERROR", "error": str(e)}, 500


# Улучшенный запуск приложения
async def startup_sequence():
    """Улучшенная последовательность запуска с обработкой ошибок"""
    try:

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
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации бота: {e}")
            return False

    except Exception as e:
        logger.error(f"💥 Критическая ошибка при запуске: {e}")
        return False


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        app.run(host="0.0.0.0", port=PORT, loop=loop)
    except Exception as e:
        logger.error(f"Failed to start application: {e}")
    finally:
        loop.close()
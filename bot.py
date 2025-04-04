import asyncio
from solana.rpc.async_api import AsyncClient
from telegram import Bot
import os

# Загрузка переменных окружения
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

async def monitor_pools():
    bot = Bot(token=TELEGRAM_TOKEN)
    client = AsyncClient(f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}")
    
    last_count = 0
    
    while True:
        try:
            # 1. Запрашиваем пулы DLMM
            response = await client.get_program_accounts(
                "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
                encoding="jsonParsed"
            )
            
            if not response or not hasattr(response, 'value'):
                raise Exception("Не удалось получить данные от RPC")

            current_count = len(response.value)
            
            # 2. Если появились новые пулы
            if current_count > last_count:
                message = f"🔍 Новых пулов: {current_count - last_count}\nВсего: {current_count}"
                await bot.send_message(CHAT_ID, message)
                last_count = current_count
                
        except Exception as e:
            print(f"🚨 Ошибка: {e}")
            await asyncio.sleep(10)  # Пауза при ошибке
        
        await asyncio.sleep(60)  # Проверка каждые 60 секунд

if __name__ == "__main__":
    asyncio.run(monitor_pools())
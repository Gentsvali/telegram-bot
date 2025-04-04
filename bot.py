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
            pools = await client.get_program_accounts(
                "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
                encoding="jsonParsed"
            )
            current_count = len(pools.value)
            
            if current_count > last_count:
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🔍 Новых пулов: {current_count - last_count} | Всего: {current_count}"
                )
                last_count = current_count
                
        except Exception as e:
            print(f"Ошибка: {e}")
        
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(monitor_pools())
import os
import aiohttp
import asyncio
import json

async def test_helius_connection():
    """Тестовый запрос к Helius API"""
    # Получаем API ключ из переменных окружения
    api_key = os.getenv("HELIUS_API_KEY")
    if not api_key:
        print("❌ HELIUS_API_KEY не найден в переменных окружения")
        print("Добавьте его через: export HELIUS_API_KEY='ваш_ключ'")
        return

    # Формируем базовый URL
    helius_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
    
    # Простейший тестовый запрос (не связанный с DLMM)
    payload = {
        "jsonrpc": "2.0",
        "id": "connection-test",
        "method": "getVersion"
    }

    print(f"🔍 Тестируем подключение к Helius API...")
    print(f"URL: {helius_url.split('?')[0]}...")  # Не показываем ключ в логах
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(helius_url, json=payload, timeout=10) as resp:
                data = await resp.json()
                
                if "result" in data:
                    print("✅ Подключение успешно!")
                    print(f"Версия RPC: {data['result']['solana-core']}")
                    return True
                else:
                    print("❌ Ошибка подключения:")
                    print(json.dumps(data, indent=2))
                    return False
                    
    except Exception as e:
        print(f"🚨 Ошибка при подключении: {str(e)}")
        return False

async def test_dlmm_fetch():
    """Тестовый запрос для DLMM пулов"""
    api_key = os.getenv("HELIUS_API_KEY")
    if not api_key:
        return

    helius_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
    
    # Правильный запрос для DLMM пулов
    payload = {
        "jsonrpc": "2.0",
        "id": "dlmm-test",
        "method": "getAssetsByAuthority",
        "params": {
            "authorityAddress": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
            "page": 1,
            "limit": 3
        }
    }

    print("\n🔍 Тестируем запрос DLMM пулов...")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(helius_url, json=payload, timeout=15) as resp:
                data = await resp.json()
                
                if "result" in data:
                    pools = data["result"].get("items", [])
                    print(f"✅ Найдено пулов: {len(pools)}")
                    if pools:
                        print("Пример пула:", json.dumps(pools[0]["id"], indent=2))
                    return True
                else:
                    print("❌ Ошибка при запросе пулов:")
                    print(json.dumps(data.get("error", {}), indent=2))
                    return False
                    
    except Exception as e:
        print(f"🚨 Ошибка: {str(e)}")
        return False

async def main():
    """Точка входа"""
    print("\n=== ТЕСТ HELIUS API ===")
    
    # 1. Проверка базового подключения
    if not await test_helius_connection():
        return
    
    # 2. Проверка запроса пулов
    await test_dlmm_fetch()

if __name__ == "__main__":
    asyncio.run(main())
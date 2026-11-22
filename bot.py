import os
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

CITY = "Ivanovo,ru"
CITY_NAME = "Иваново"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Погода ---
async def get_weather():
    url = f"https://api.openweathermap.org/data/2.5/weather?q={CITY}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return "Не удалось получить погоду."
            data = await resp.json()
            temp = data["main"]["temp"]
            feels = data["main"]["feels_like"]
            desc = data["weather"][0]["description"].capitalize()
            wind = data["wind"]["speed"]
            return (f"🌡 Погода в {CITY_NAME}:\n"
                    f"Температура: {temp:+.0f}°C (ощущается {feels:+.0f}°C)\n"
                    f"{desc}\n"
                    f"Ветер: {wind} м/с")

# --- ДТП и заторы (парсинг новостей) ---
async def get_accidents():
    url = "https://newsivanovo.ru/rss"  # если RSS нет, парсим главную
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    # fallback на HTML
                    async with session.get("https://newsivanovo.ru/") as html_resp:
                        text = await html_resp.text()
                else:
                    text = await resp.text()
    except Exception:
        return "Не удалось получить новости о ДТП."

    soup = BeautifulSoup(text, "html.parser")
    keywords = ["ДТП", "авари", "пробк", "затор", "столкновен"]
    found = []
    for item in soup.find_all(["item", "h2", "h3", "a"]):
        title = item.get_text(strip=True)
        if any(kw.lower() in title.lower() for kw in keywords):
            found.append(title)
    if not found:
        return "Свежих сообщений о ДТП и заторах в Иваново не найдено."
    # Берём последние 5 уникальных
    unique = list(dict.fromkeys(found))[:5]
    return "🚨 Дорожная обстановка (Иваново):\n" + "\n".join(f"— {t}" for t in unique)

# --- Хэндлеры ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Привет! Я слежу за погодой и дорожной обстановкой в {CITY_NAME}.\n"
        "Команды:\n"
        "/weather — погода\n"
        "/traffic — пробки и ДТП\n"
        "/all — всё сразу"
    )

@dp.message(Command("weather"))
async def cmd_weather(message: types.Message):
    await message.answer(await get_weather())

@dp.message(Command("traffic"))
async def cmd_traffic(message: types.Message):
    await message.answer(await get_accidents())

@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    weather = await get_weather()
    traffic = await get_accidents()
    await message.answer(f"{weather}\n\n{traffic}")

# --- Автоматическая рассылка (опционально) ---
# Можно добавить позже через apscheduler, если нужна утренняя сводка.

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
import os
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from datetime import datetime

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

CITY = "Ivanovo,ru"
CITY_NAME = "Иваново"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
SUBSCRIBERS = set()

# --- Погода ---
async def get_weather():
    url = f"https://api.openweathermap.org/data/2.5/weather?q={CITY}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return "⚠️ Не удалось получить погоду."
            data = await resp.json()
            temp = data["main"]["temp"]
            feels = data["main"]["feels_like"]
            desc = data["weather"][0]["description"].capitalize()
            wind = data["wind"]["speed"]
            
            emoji = "☀️" if "ясно" in desc.lower() else "☁️" if "облач" in desc.lower() else "🌧" if "дождь" in desc.lower() else "🌡"
            
            return (f"{emoji} Погода в {CITY_NAME}:\n"
                    f"🌡 Температура: {temp:+.0f}°C (ощущается {feels:+.0f}°C)\n"
                    f"☁️ {desc}\n"
                    f"💨 Ветер: {wind} м/с")

# --- ДТП и заторы ---
def is_duplicate(new_text, existing_texts):
    """Умная проверка на дубли по пересечению важных слов"""
    # Очищаем от знаков препинания и разбиваем на слова
    words_new = set(w.strip(".,!?\"'()-") for w in new_text.lower().split() if len(w) > 3)
    for existing in existing_texts:
        words_ex = set(w.strip(".,!?\"'()-") for w in existing.lower().split() if len(w) > 3)
        # Если совпадает 2 и более значимых слов (например, "дтп" и "погибшими"), это дубль
        if len(words_new.intersection(words_ex)) >= 2:
            return True
    return False

async def fetch_news(session, url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with session.get(url, headers=headers, timeout=8) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            
            keywords = ["дтп", "авария", "аварии", "пробка", "пробки", "затор", "столкновение", "перекрыт"]
            # Стоп-слова, чтобы не тащить новости других регионов
            stop_words = ["украин", "москв", "петербург", "росси", "сша", "европ"]
            
            found = []
            for tag in soup.find_all(["h1", "h2", "h3", "a", "title"]):
                text = tag.get_text(strip=True).replace('\n', ' ').replace('\r', '')
                lower_text = text.lower()
                
                # 1. Фильтр длины (20-90 символов)
                if not (20 <= len(text) <= 90):
                    continue
                    
                # 2. Есть ключевое слово?
                if not any(kw in lower_text for kw in keywords):
                    continue
                    
                # 3. Есть локальный контекст и нет стоп-слов?
                if ("иванов" in lower_text or "город" in lower_text or "област" in lower_text):
                    if not any(sw in lower_text for sw in stop_words):
                        found.append(text)
            return found
    except Exception:
        return []

async def get_accidents():
    sources = [
        "https://newsivanovo.ru/rss",
        "https://ivanovonews.ru/rss/",
        "https://mkivanovo.ru/",
        "https://iv-news.ru/"
    ]
    
    all_news = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_news(session, url) for url in sources]
        results = await asyncio.gather(*tasks)
        for res in results:
            all_news.extend(res)
            
    if not all_news:
        return "✅ Свежих сообщений о ДТП и заторах в Иваново не найдено."
        
    unique_news = []
    for news in all_news:
        if not is_duplicate(news, unique_news):
            unique_news.append(news)
            
    final_news = unique_news[:5]
    
    text = "🚦 <b>Дорожная обстановка (Иваново):</b>\n\n"
    for item in final_news:
        text += f"🔴 {item}\n\n"
    return text

# --- Утренняя рассылка ---
async def morning_broadcast():
    if not SUBSCRIBERS:
        return
    
    weather = await get_weather()
    traffic = await get_accidents()
    text = f"🌅 <b>Доброе утро!</b>\n📅 Сводка на {datetime.now().strftime('%d.%m.%Y')}:\n\n{weather}\n\n{traffic}"
    
    for chat_id in list(SUBSCRIBERS):
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            SUBSCRIBERS.discard(chat_id)

# --- Хэндлеры ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Привет! Я слежу за погодой и дорожной обстановкой в {CITY_NAME}.\n\n"
        "📋 <b>Команды:</b>\n"
        "/weather — погода 🌤\n"
        "/traffic — пробки и ДТП 🚦\n"
        "/all — всё сразу 🗂\n"
        "/subscribe — подписаться на утреннюю сводку (05:00 МСК) ⏰\n"
        "/unsubscribe — отписаться 🔕",
        parse_mode="HTML"
    )

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    SUBSCRIBERS.add(message.chat.id)
    await message.answer("✅ Вы подписаны на утреннюю сводку! Бот пришлет её завтра в 05:00 по Москве.")

@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: types.Message):
    SUBSCRIBERS.discard(message.chat.id)
    await message.answer("❌ Вы отписаны от утренней сводки.")

@dp.message(Command("weather"))
async def cmd_weather(message: types.Message):
    await message.answer(await get_weather())

@dp.message(Command("traffic"))
async def cmd_traffic(message: types.Message):
    await message.answer(await get_accidents(), parse_mode="HTML")

@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    weather = await get_weather()
    traffic = await get_accidents()
    await message.answer(f"{weather}\n\n{traffic}", parse_mode="HTML")


async def main():
    moscow_tz = pytz.timezone("Europe/Moscow")
    scheduler = AsyncIOScheduler(timezone=moscow_tz)
    scheduler.add_job(morning_broadcast, trigger='cron', hour=5, minute=0)
    scheduler.start()
    
    print("Бот запущен. Планировщик активен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

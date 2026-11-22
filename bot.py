import os
import asyncio
import aiohttp
import re
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

# --- Парсинг новостей ---
def extract_street(text):
    """Пытается вытащить улицу из текста для выделения"""
    patterns = [
        r'ул(?:ица|\.)?\s+([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)',
        r'на\s+(?:улице\s+)?([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None

async def fetch_news(session, url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with session.get(url, headers=headers, timeout=8) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            
            keywords = ["дтп", "авария", "аварии", "пробка", "пробки", "затор", "столкновение", "перекрыт", "вылет"]
            stop_words = ["украин", "москв", "петербург", "росси", "сша", "европ"]
            
            found = []
            for tag in soup.find_all(["h1", "h2", "h3", "a", "title"]):
                text = tag.get_text(strip=True).replace('\n', ' ').replace('\r', '')
                lower_text = text.lower()
                
                # Фильтр длины
                if not (25 <= len(text) <= 110):
                    continue
                # Проверка на ключевые слова
                if not any(kw in lower_text for kw in keywords):
                    continue
                # Локальный контекст и отсутствие стоп-слов
                if ("иванов" in lower_text or "город" in lower_text or "област" in lower_text or "лежневск" in lower_text):
                    if not any(sw in lower_text for sw in stop_words):
                        found.append(text)
            return found
    except Exception:
        return []

async def get_traffic_summary():
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
        return "✅ <b>Свежих сообщений о ДТП и заторах в Иваново не найдено.</b>"
        
    # Умная дедупликация
    unique_news = []
    for news in all_news:
        words_new = set(w.strip(".,!?\"'()-") for w in news.lower().split() if len(w) > 3)
        is_dup = False
        for existing in unique_news:
            words_ex = set(w.strip(".,!?\"'()-") for w in existing.lower().split() if len(w) > 3)
            if len(words_new.intersection(words_ex)) >= 2:
                is_dup = True
                break
        if not is_dup:
            unique_news.append(news)
    
    # Формируем красивый список
    text = "🚨 <b>Дорожная обстановка (Иваново):</b>\n\n"
    count = 0
    for news in unique_news:
        if count >= 5: 
            break
            
        # Делаем первую букву заглавной
        news_text = news[0].upper() + news[1:]
        
        # Выделяем улицу жирным, если нашли
        street = extract_street(news_text)
        if street:
            # Заменяем упоминание улицы на жирное
            news_text = re.sub(fr'\b{street}\b', f'<b>{street}</b>', news_text, count=1)
        
        text += f"— {news_text}\n"
        count += 1

    return text

# --- Утренняя рассылка ---
async def morning_broadcast():
    if not SUBSCRIBERS:
        return
    
    weather = await get_weather()
    traffic = await get_traffic_summary()
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
    await message.answer(await get_weather(), parse_mode="HTML")

@dp.message(Command("traffic"))
async def cmd_traffic(message: types.Message):
    await message.answer(await get_traffic_summary(), parse_mode="HTML")

@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    weather = await get_weather()
    traffic = await get_traffic_summary()
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

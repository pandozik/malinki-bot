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
            
            return (f"{emoji} <b>Погода в {CITY_NAME}:</b>\n"
                    f"🌡 Температура: <b>{temp:+.0f}°C</b> (ощущается {feels:+.0f}°C)\n"
                    f"☁️ {desc}\n"
                    f"💨 Ветер: {wind} м/с")

# --- ДТП и заторы ---
def extract_street(text):
    """Пытается вытащить улицу из текста"""
    # Ищем паттерны: "ул. Название", "улица Название", "на Название"
    patterns = [
        r'ул(?:ица|\.)?\s+([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)',
        r'на\s+(?:улице\s+)?([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None

def get_status(text):
    """Определяет статус по ключевым словам"""
    lower = text.lower()
    if "перекрыт" in lower or "закрыт" in lower:
        return "🔴 Перекрыто"
    if "пробка" in lower or "затор" in lower or "стоит" in lower:
        return "🟠 Затруднено"
    if "дтп" in lower or "авари" in lower:
        return "🟡 ДТП"
    return "⚪ Инфо"

async def fetch_news(session, url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with session.get(url, headers=headers, timeout=8) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            
            keywords = ["дтп", "авария", "аварии", "пробка", "пробки", "затор", "столкновение", "перекрыт"]
            stop_words = ["украин", "москв", "петербург", "росси", "сша", "европ"]
            
            found = []
            for tag in soup.find_all(["h1", "h2", "h3", "a", "title"]):
                text = tag.get_text(strip=True).replace('\n', ' ').replace('\r', '')
                lower_text = text.lower()
                
                if not (20 <= len(text) <= 100):
                    continue
                if not any(kw in lower_text for kw in keywords):
                    continue
                if ("иванов" in lower_text or "город" in lower_text or "област" in lower_text):
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
    
    # Дедупликация по пересечению слов
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
    
    # Формируем структурированный список
    incidents = []
    has_major_jam = False
    
    for news in unique_news[:6]:
        street = extract_street(news)
        status = get_status(news)
        if status == "🔴 Перекрыто" or status == "🟠 Затруднено":
            has_major_jam = True
        
        # Очищаем текст от лишнего, оставляем суть
        summary = news
        if len(summary) > 80:
            summary = summary[:77] + "..."
        
        if street:
            incidents.append(f"{status} <b>ул. {street}</b>\n   <i>{summary}</i>")
        else:
            incidents.append(f"{status} <i>{summary}</i>")
    
    # Определяем общий уровень (эвристика)
    if has_major_jam:
        level = "🟠 <b>Затруднено</b> (есть перекрытия/заторы)"
    elif incidents:
        level = "🟡 <b>Небольшие осложнения</b>"
    else:
        level = "🟢 <b>Свободно</b>"
    
    text = "🚦 <b>Дорожная обстановка (Иваново):</b>\n\n"
    text += f"📊 Уровень: {level}\n\n"
    text += "\n\n".join(incidents)
    
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

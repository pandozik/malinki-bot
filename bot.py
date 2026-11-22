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

# --- Оценка влияния погоды на дорогу ---
def get_road_condition(temp, desc, wind):
    """Анализирует погоду и выдает состояние дорог"""
    desc = desc.lower()
    
    if "снег" in desc:
        return "❄️ Снегопад. Дороги скользкие, возможны заносы. Будьте осторожны!"
    elif "дождь" in desc:
        if temp < 2:
            return "🌧 Гололедица. Мокрый асфальт замерзает. Высокий риск ДТП!"
        return "🌧 Мокрый асфальт. Тормозной путь увеличен."
    elif "туман" in desc or "дымка" in desc:
        return "🌫 Плохая видимость. Соблюдайте дистанцию."
    elif temp < -5:
        return "🧊 Сильный мороз. На дорогах возможна наледь."
    elif temp > 25:
        return "☀️ Жарко. Дороги сухие, но возможна усталость за рулем."
    elif "ясно" in desc or "облачно" in desc:
        return "✅ Дороги сухие, видимость хорошая."
    
    return "⚪ Дорожные условия стандартные."

# --- Погода ---
async def get_weather():
    url = f"https://api.openweathermap.org/data/2.5/weather?q={CITY}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return "⚠️ Не удалось получить погоду.", False
            data = await resp.json()
            temp = data["main"]["temp"]
            feels = data["main"]["feels_like"]
            desc = data["weather"][0]["description"].capitalize()
            wind = data["wind"]["speed"]
            
            emoji = "☀️" if "ясно" in desc.lower() else "☁️" if "облач" in desc.lower() else "🌧" if "дождь" in desc.lower() else "🌡"
            road_condition = get_road_condition(temp, desc, wind)
            
            # Возвращаем текст и флаг "плохая ли погода для дорог"
            is_bad_weather = any(w in desc.lower() for w in ["снег", "дождь", "туман"]) or temp < 2
            
            text = (f"{emoji} <b>Погода в {CITY_NAME}:</b>\n"
                    f"🌡 Температура: <b>{temp:+.0f}°C</b> (ощущается {feels:+.0f}°C)\n"
                    f"☁️ {desc}\n"
                    f"💨 Ветер: {wind} м/с\n"
                    f"🛣 <b>Дорога:</b> {road_condition}")
            return text, is_bad_weather

# --- Логика анализа новостей ---
def extract_street(text):
    patterns = [
        r'ул(?:ица|\.)?\s+([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)',
        r'на\s+(?:улице\s+)?([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)',
        r'в\s+([А-ЯЁ][а-яё]+ском)\s+районе',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return "Иваново / Область"

def infer_traffic_impact(text):
    lower = text.lower()
    if any(w in lower for w in ["перекрыт", "смертельн", "погиб", "фура", "грузовик", "трасса", "тройное", "массов", "вылет"]):
        return "🔴 <b>Высокое</b>. Возможна серьезная пробка или полная остановка. Ищите объезд."
    elif any(w in lower for w in ["дтп", "авария", "столкновение", "пострадал", "скорая", "затор"]):
        return "🟠 <b>Среднее</b>. Движение на участке замедлено. Возможно образование затора."
    else:
        return "🟢 <b>Низкое</b>. Существенных затруднений не ожидается."

async def fetch_news(session, url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with session.get(url, headers=headers, timeout=8) as resp:
            if resp.status != 200: return []
            html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            
            keywords = ["дтп", "авария", "аварии", "пробка", "пробки", "затор", "столкновение", "перекрыт", "вылет", "пострадал"]
            stop_words = ["украин", "москв", "петербург", "росси", "сша", "европ"]
            
            found = []
            for tag in soup.find_all(["h1", "h2", "h3", "a", "title"]):
                text = tag.get_text(strip=True).replace('\n', ' ').replace('\r', '')
                lower_text = text.lower()
                if 25 <= len(text) <= 110 and any(kw in lower_text for kw in keywords):
                    if ("иванов" in lower_text or "город" in lower_text or "област" in lower_text):
                        if not any(sw in lower_text for sw in stop_words):
                            found.append(text)
            return found
    except Exception:
        return []

async def get_traffic_summary(is_bad_weather=False):
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
    
    text = "🚨 <b>Дорожная обстановка (Иваново):</b>\n"
    
    # Если погода плохая и есть ДТП - выводим предупреждение
    if is_bad_weather and unique_news:
        text += "⚠️ <i>Погода ухудшает ситуацию. Будьте внимательнее на дорогах!</i>\n"
    text += "\n"
    
    for news in unique_news[:4]:
        news_text = news[0].upper() + news[1:]
        street = extract_street(news_text)
        impact = infer_traffic_impact(news_text)
        
        if news_text.endswith('.'):
            news_text = news_text[:-1]

        text += f"📝 <b>Событие:</b> {news_text}.\n"
        text += f"📍 <b>Локация:</b> {street}.\n"
        text += f"🚦 <b>Влияние:</b> {impact}\n"
        text += "➖➖➖➖➖➖➖➖\n"

    return text

# --- Утренняя рассылка ---
async def morning_broadcast():
    if not SUBSCRIBERS: return
    
    weather_text, is_bad = await get_weather()
    traffic_text = await get_traffic_summary(is_bad_weather=is_bad)
    
    full_text = f"🌅 <b>Доброе утро!</b>\n📅 Сводка на {datetime.now().strftime('%d.%m.%Y')}:\n\n{weather_text}\n\n{traffic_text}"
    
    for chat_id in list(SUBSCRIBERS):
        try:
            await bot.send_message(chat_id, full_text, parse_mode="HTML")
        except Exception:
            SUBSCRIBERS.discard(chat_id)

# --- Хэндлеры ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Привет! Я слежу за погодой и дорожной обстановкой в {CITY_NAME}.\n\n"
        "📋 <b>Команды:</b>\n"
        "/weather — погода и состояние дорог 🌤\n"
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
    weather_text, _ = await get_weather()
    await message.answer(weather_text, parse_mode="HTML")

@dp.message(Command("traffic"))
async def cmd_traffic(message: types.Message):
    _, is_bad = await get_weather()
    traffic_text = await get_traffic_summary(is_bad_weather=is_bad)
    await message.answer(traffic_text, parse_mode="HTML")

@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    weather_text, is_bad = await get_weather()
    traffic_text = await get_traffic_summary(is_bad_weather=is_bad)
    await message.answer(f"{weather_text}\n\n{traffic_text}", parse_mode="HTML")


async def main():
    moscow_tz = pytz.timezone("Europe/Moscow")
    scheduler = AsyncIOScheduler(timezone=moscow_tz)
    scheduler.add_job(morning_broadcast, trigger='cron', hour=5, minute=0)
    scheduler.start()
    
    print("Бот запущен. Планировщик активен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

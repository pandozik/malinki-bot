import os
import asyncio
import aiohttp
import re
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from datetime import datetime
from email.utils import parsedate_to_datetime

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

CITY = "Ivanovo,ru"
CITY_NAME = "Иваново"
MSK_TZ = pytz.timezone("Europe/Moscow")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
SUBSCRIBERS = set()

class RouteState(StatesGroup):
    waiting_for_start = State()
    waiting_for_end = State()

# --- Погода ---
def get_road_condition(temp, desc, wind):
    desc = desc.lower()
    if "снег" in desc: return "❄️ Снегопад. Дороги скользкие.", True
    elif "дождь" in desc:
        if temp < 2: return "🌧 Гололедица. Высокий риск ДТП!", True
        return "🌧 Мокрый асфальт. Тормозной путь увеличен.", True
    elif "туман" in desc: return "🌫 Плохая видимость.", True
    elif temp < -5: return "🧊 Наледь на дорогах.", True
    elif temp > 25: return "☀️ Жарко. Дороги сухие.", False
    elif "ясно" in desc or "облачно" in desc: return "✅ Дороги сухие.", False
    return "⚪ Стандартные условия.", False

async def get_weather():
    url = f"https://api.openweathermap.org/data/2.5/weather?q={CITY}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200: return "⚠️ Ошибка погоды.", False
            data = await resp.json()
            temp = data["main"]["temp"]
            feels = data["main"]["feels_like"]
            desc = data["weather"][0]["description"].capitalize()
            wind = data["wind"]["speed"]
            
            emoji = "☀️" if "ясно" in desc.lower() else "☁️" if "облач" in desc.lower() else "🌧" if "дождь" in desc.lower() else "🌡"
            road, is_bad = get_road_condition(temp, desc, wind)
            
            text = (f"{emoji} <b>Погода в {CITY_NAME}:</b>\n"
                    f"🌡 Температура: <b>{temp:+.0f}°C</b> (ощущается {feels:+.0f}°C)\n"
                    f"☁️ {desc}\n"
                    f"💨 Ветер: {wind} м/с\n"
                    f"🛣 <b>Дорога:</b> {road}")
            return text, is_bad

# --- Маршруты (Карты) ---
async def get_coords(session, address):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": f"Иваново, {address}", "format": "json", "limit": 1}
    headers = {"User-Agent": "MalinkiBot/1.0"}
    try:
        async with session.get(url, params=params, headers=headers, timeout=10) as resp:
            data = await resp.json()
            if data: return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception: pass
    return None

async def get_route_info(session, start_coords, end_coords):
    start_lon, start_lat = start_coords[1], start_coords[0]
    end_lon, end_lat = end_coords[1], end_coords[0]
    url = f"http://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}?overview=false"
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            if data.get("routes"):
                return data["routes"][0]["distance"] / 1000, data["routes"][0]["duration"] / 60
    except Exception: pass
    return None

# --- Логика анализа новостей ---
def extract_street(text):
    patterns = [
        r'(?:ул(?:ица|\.)?|проспект|пр-т|переулок|пер\.|шоссе|пл(?:ощадь|\.)?|мкр(?:орайон|\.)?)\s+([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)',
        r'на\s+(?:улице\s+)?([А-ЯЁ][а-яё]+(?:ой|ий|ая|ое|ые))',
        r'в\s+([А-ЯЁ][а-яё]+ском)\s+районе',
        r'\b([А-ЯЁ][а-яё]+ская)\b'
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            street = match.group(1)
            if street.lower() not in ["ивановская", "областная"]: return street
    return None

def infer_traffic_impact(text):
    lower = text.lower()
    if any(w in lower for w in ["перекрыт", "смертельн", "погиб", "фура", "грузовик", "трасса", "тройное", "массов", "вылет"]):
        return "🔴 <b>Высокое</b>. Возможна серьезная пробка."
    elif any(w in lower for w in ["дтп", "авария", "столкновение", "пострадал", "скорая", "затор"]):
        return "🟠 <b>Среднее</b>. Движение замедлено."
    else:
        return "🟢 <b>Низкое</b>. Затруднений нет."

def format_time(pub_date_str=None, text=""):
    if pub_date_str:
        try:
            dt = parsedate_to_datetime(pub_date_str).astimezone(MSK_TZ)
            now = datetime.now(MSK_TZ)
            if dt.date() == now.date(): return dt.strftime("%H:%M")
            elif (now.date() - dt.date()).days == 1: return f"Вчера в {dt.strftime('%H:%M')}"
            else: return dt.strftime("%d.%m в %H:%M")
        except Exception: pass
    match = re.search(r'\b([01]?[0-9]|2[0-3]):[0-5][0-9]\b', text)
    if match: return match.group(0)
    return "Недавно"

def clean_html(raw_html):
    """Убирает HTML-теги из описания новости и лишние пробелы"""
    if not raw_html: return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    text = soup.get_text(separator=' ')
    return re.sub(r'\s+', ' ', text).strip()

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

            items = soup.find_all('item')
            if items:
                for item in items:
                    title_tag = item.find('title')
                    if not title_tag: continue
                    text = title_tag.get_text(strip=True)
                    lower_text = text.lower()
                    
                    if 25 <= len(text) <= 110 and any(kw in lower_text for kw in keywords):
                        if ("иванов" in lower_text or "город" in lower_text or "област" in lower_text):
                            if not any(sw in lower_text for sw in stop_words):
                                pub_date = item.find('pubDate')
                                # Ищем описание
                                desc_tag = item.find('description') or item.find('summary')
                                raw_desc = desc_tag.get_text(strip=True) if desc_tag else ""
                                clean_desc = clean_html(raw_desc)
                                
                                found.append({
                                    "title": text, 
                                    "time": format_time(pub_date.text if pub_date else None, text),
                                    "desc": clean_desc
                                })
            else:
                # Если нет RSS, берем только заголовки (без описаний)
                for tag in soup.find_all(["h1", "h2", "h3", "a", "title"]):
                    text = tag.get_text(strip=True).replace('\n', ' ').replace('\r', '')
                    lower_text = text.lower()
                    if 25 <= len(text) <= 110 and any(kw in lower_text for kw in keywords):
                        if ("иванов" in lower_text or "город" in lower_text or "област" in lower_text):
                            if not any(sw in lower_text for sw in stop_words):
                                found.append({"title": text, "time": format_time(None, text), "desc": ""})
            return found
    except Exception:
        return []

async def get_traffic_summary(is_bad_weather=False):
    sources = ["https://newsivanovo.ru/rss", "https://ivanovonews.ru/rss/", "https://mkivanovo.ru/", "https://iv-news.ru/"]
    all_news = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_news(session, url) for url in sources]
        results = await asyncio.gather(*tasks)
        for res in results: all_news.extend(res)
            
    if not all_news: return "✅ <b>Свежих сообщений о ДТП нет.</b>"
        
    unique_news = []
    for news in all_news:
        words_new = set(w.strip(".,!?\"'()-") for w in news["title"].lower().split() if len(w) > 3)
        if not any(len(words_new.intersection(set(w.strip(".,!?\"'()-") for w in ex["title"].lower().split() if len(w) > 3))) >= 2 for ex in unique_news):
            unique_news.append(news)
    
    text = "🚨 <b>Дорожная обстановка (Иваново):</b>\n"
    if is_bad_weather and unique_news: text += "⚠️ <i>Погода ухудшает ситуацию!</i>\n"
    text += "\n"
    
    for news in unique_news[:4]:
        news_text = news["title"][0].upper() + news["title"][1:]
        if news_text.endswith('.'): news_text = news_text[:-1]
        
        street = extract_street(news_text + " " + news["desc"]) # Ищем улицу и в описании тоже
        impact = infer_traffic_impact(news_text + " " + news["desc"])
        time_str = news["time"]

        text += f"📝 <b>Событие:</b> {news_text}.\n"
        
        # Если есть описание, выводим его подробности
        if news["desc"] and len(news["desc"]) > 20:
            short_desc = news["desc"][:200] + "..." if len(news["desc"]) > 200 else news["desc"]
            text += f"📄 <b>Детали:</b> <i>{short_desc}</i>\n"

        text += f"🕒 <b>Время:</b> {time_str}.\n"
        if street: text += f"📍 <b>Локация:</b> {street}.\n"
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
        try: await bot.send_message(chat_id, full_text, parse_mode="HTML")
        except Exception: SUBSCRIBERS.discard(chat_id)

# --- Хэндлеры ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Привет! Я слежу за погодой и дорожной обстановкой в {CITY_NAME}.\n\n"
        "📋 <b>Команды:</b>\n"
        "/weather — погода 🌤\n"
        "/traffic — пробки и ДТП 🚦\n"
        "/all — всё сразу 🗂\n"
        "/route — построить маршрут 🗺\n"
        "/subscribe — утренняя сводка ⏰\n"
        "/unsubscribe — отписаться 🔕",
        parse_mode="HTML"
    )

@dp.message(Command("route"))
async def cmd_route(message: types.Message, state: FSMContext):
    await state.set_state(RouteState.waiting_for_start)
    await message.answer("🗺 <b>Откуда выезжаете?</b>\nНапишите адрес или улицу (например: <i>Площадь Ленина</i>).", parse_mode="HTML")

@dp.message(RouteState.waiting_for_start)
async def process_route_start(message: types.Message, state: FSMContext):
    await state.update_data(start_point=message.text)
    await state.set_state(RouteState.waiting_for_end)
    await message.answer("🏁 <b>Куда едем?</b>\nНапишите конечный адрес.")

@dp.message(RouteState.waiting_for_end)
async def process_route_end(message: types.Message, state: FSMContext):
    end_point = message.text
    data = await state.get_data()
    start_point = data.get("start_point")
    await state.clear()

    wait_msg = await message.answer("⏳ Строю маршрут и проверяю обстановку...")

    async with aiohttp.ClientSession() as session:
        start_coords = await get_coords(session, start_point)
        end_coords = await get_coords(session, end_point)
        
        if not start_coords or not end_coords:
            await wait_msg.edit_text("❌ Не удалось найти один из адресов. Попробуйте указать точнее.")
            return

        route_data = await get_route_info(session, start_coords, end_coords)
        
        if not route_data:
            await wait_msg.edit_text("❌ Не удалось проложить маршрут между этими точками.")
            return

        dist_km, base_min = route_data

    _, is_bad_weather = await get_weather()
    traffic_text = await get_traffic_summary(is_bad_weather=is_bad_weather)
    
    time_penalty = 0
    warning = ""
    if "🚨" in traffic_text:
        time_penalty = 10
        warning = "\n⚠️ <i>В городе есть ДТП (см. /traffic). Возможны задержки в пути.</i>"

    total_min = int(base_min + time_penalty)
    hours = total_min // 60
    mins = total_min % 60
    time_str = f"{hours} ч {mins} мин" if hours > 0 else f"{mins} мин"

    result_text = (
        f"🗺 <b>Маршрут построен:</b>\n"
        f"📍 <b>От:</b> {start_point.capitalize()}\n"
        f"🏁 <b>До:</b> {end_point.capitalize()}\n\n"
        f"📏 <b>Расстояние:</b> {dist_km:.1f} км\n"
        f"⏱ <b>Примерное время:</b> {time_str}\n"
        f"{warning}\n\n"
        f"🛣 <i>Расчет по разрешенной скорости.</i>"
    )
    
    await wait_msg.edit_text(result_text, parse_mode="HTML")

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
    scheduler = AsyncIOScheduler(timezone=MSK_TZ)
    scheduler.add_job(morning_broadcast, trigger='cron', hour=5, minute=0)
    scheduler.start()
    
    print("Бот запущен. Планировщик активен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

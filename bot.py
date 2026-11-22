import os
import asyncio
import aiohttp
import re
import sqlite3
import json
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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

# --- База данных ---
def init_db():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            cities TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fuel_history (
            date TEXT PRIMARY KEY,
            ai92 REAL,
            ai95 REAL,
            dt REAL
        )
    """)
    conn.commit()
    conn.close()

def get_user_cities(user_id: int) -> list:
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT cities FROM user_settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return json.loads(row[0])
    return ["Иваново"]

def save_user_cities(user_id: int, cities: list):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_settings (user_id, cities) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET cities = excluded.cities
    """, (user_id, json.dumps(cities, ensure_ascii=False)))
    conn.commit()
    conn.close()

def get_subscribers() -> list:
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM user_settings")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

def get_yesterday_fuel():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT ai92, ai95, dt FROM fuel_history ORDER BY date DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"АИ-92": row[0], "АИ-95": row[1], "ДТ": row[2]}
    return None

def save_today_fuel(ai92, ai95, dt):
    today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO fuel_history (date, ai92, ai95, dt) VALUES (?, ?, ?, ?)
    """, (today, ai92, ai95, dt))
    conn.commit()
    conn.close()

init_db()

# --- Список городов ---
REGION_CITIES = {
    "Иваново": (56.9972, 40.9714), "Шуя": (56.8487, 41.3883), "Кинешма": (57.4391, 42.1289),
    "Вичуга": (57.2128, 41.9301), "Тейково": (56.858, 40.5369), "Фурманов": (57.2536, 41.1085),
    "Кохма": (56.9349, 41.0915), "Родники": (57.1051, 41.7305), "Приволжск": (57.387, 41.2867),
    "Заволжск": (57.4823, 42.1378), "Наволоки": (57.4657, 41.9634), "Юрьевец": (57.3201, 43.1041),
    "Южа": (56.5831, 42.0122), "Гаврилов Посад": (56.5585, 40.1217), "Комсомольск": (57.0292, 40.3787),
    "Плес": (57.4606, 41.5158), "Пучеж": (56.9833, 43.1667),
}
IVANOVO_AGGLOMERATION = ["Иваново", "Кохма", "Тейково", "Фурманов", "Шуя", "Приволжск"]

class RouteState(StatesGroup):
    waiting_for_start = State()
    waiting_for_end = State()

# --- Получение цен на бензин (Российский источник) ---
def format_price_with_delta(name, current, previous):
    if not previous or current is None:
        return f"🔹 <b>{name}:</b> {current} ₽/л\n" if current else ""
    diff = current - previous
    percent = (diff / previous) * 100
    if diff > 0:
        return f"🔹 <b>{name}:</b> {current} ₽/л 📈 <i>(+{percent:.2f}%)</i>\n"
    elif diff < 0:
        return f"🔹 <b>{name}:</b> {current} ₽/л 📉 <i>({percent:.2f}%)</i>\n"
    else:
        return f"🔹 <b>{name}:</b> {current} ₽/л ➡️ <i>(0%)</i>\n"

async def get_fuel_prices(show_delta=False):
    # Используем российский агрегатор gdebenz.ru (регион Ивановская область)
    url = "https://gdebenz.ru/region/ivanovskaya-oblast/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    return "⚠️ Не удалось получить данные о ценах (сайт недоступен)."
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                
                # Ищем средние цены на странице
                prices = {}
                text_html = soup.get_text(separator=' ')
                
                # Регулярные выражения для поиска цен (АИ-92, АИ-95, ДТ)
                ai92_match = re.search(r'АИ-92.*?(\d{2}[.,]\d{2})', text_html)
                ai95_match = re.search(r'АИ-95.*?(\d{2}[.,]\d{2})', text_html)
                dt_match = re.search(r'ДТ.*?(\d{2}[.,]\d{2})', text_html)

                if ai92_match: prices["АИ-92"] = float(ai92_match.group(1).replace(',', '.'))
                if ai95_match: prices["АИ-95"] = float(ai95_match.group(1).replace(',', '.'))
                if dt_match: prices["ДТ"] = float(dt_match.group(1).replace(',', '.'))

                if not prices:
                    return "⚠️ Не удалось распознать цены на сайте. Возможно, изменилась верстка."

                text = "⛽ <b>Средние цены на топливо (Ивановская обл.):</b>\n\n"
                
                if show_delta:
                    yesterday_prices = get_yesterday_fuel()
                    if yesterday_prices:
                        text += format_price_with_delta("АИ-92", prices.get("АИ-92"), yesterday_prices.get("АИ-92"))
                        text += format_price_with_delta("АИ-95", prices.get("АИ-95"), yesterday_prices.get("АИ-95"))
                        text += format_price_with_delta("ДТ", prices.get("ДТ"), yesterday_prices.get("ДТ"))
                    else:
                        text += f"🔹 <b>АИ-92:</b> {prices.get('АИ-92', '—')} ₽/л\n"
                        text += f"🔹 <b>АИ-95:</b> {prices.get('АИ-95', '—')} ₽/л\n"
                        text += f"🔹 <b>ДТ:</b> {prices.get('ДТ', '—')} ₽/л\n"
                    
                    save_today_fuel(prices.get("АИ-92"), prices.get("АИ-95"), prices.get("ДТ"))
                else:
                    if "АИ-92" in prices: text += f"🔹 <b>АИ-92:</b> {prices['АИ-92']} ₽/л\n"
                    if "АИ-95" in prices: text += f"🔹 <b>АИ-95:</b> {prices['АИ-95']} ₽/л\n"
                    if "ДТ" in prices: text += f"🔹 <b>ДТ:</b> {prices['ДТ']} ₽/л\n"

                text += "\n<i>Данные агрегируются по региону.</i>"
                return text
        except Exception as e:
            print(f"Ошибка топлива: {e}")
            return "⚠️ Ошибка при запросе цен на топливо."

# --- Погода ---
def get_weather_emoji(code):
    if code in [0, 1]: return "☀️"
    if code in [2, 3]: return "☁️"
    if code in [45, 48]: return "🌫"
    if code in [51, 53, 55, 56, 57]: return "🌦"
    if code in [61, 63, 65, 66, 67]: return "🌧"
    if code in [71, 73, 75, 77]: return "❄️"
    if code in [80, 81, 82]: return "🌧"
    if code in [85, 86]: return "❄️"
    if code in [95, 96, 99]: return "⛈"
    return "🌡"

async def get_region_weather(user_cities: list):
    selected_cities = {city: REGION_CITIES[city] for city in user_cities if city in REGION_CITIES}
    if not selected_cities:
        return "⚠️ Вы не выбрали ни одного города. Зайдите в /settings."

    lats = ",".join(str(lat) for lat, lon in selected_cities.values())
    lons = ",".join(str(lon) for lat, lon in selected_cities.values())
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lons}&current=temperature_2m,precipitation_probability,weather_code&timezone=Europe/Moscow"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200: return f"⚠️ Ошибка сервера погоды."
                data = await resp.json()
                if isinstance(data, dict): data = [data]

                lines = []
                for i, city in enumerate(selected_cities.keys()):
                    if i < len(data):
                        current = data[i].get("current", {})
                        temp = current.get("temperature_2m", 0)
                        prob = current.get("precipitation_probability", 0)
                        code = current.get("weather_code", 0)
                        lines.append(f"{get_weather_emoji(code)} <b>{city}</b>: {temp:+.0f}°C, 💧{prob}%")
                return "🌍 <b>Погода в выбранных городах:</b>\n" + "\n".join(lines)
        except Exception:
            return "⚠️ Ошибка при запросе к сервису."

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

async def get_weather_ivanovo():
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
                    f"☁️ {desc}\n💨 Ветер: {wind} м/с\n🛣 <b>Дорога:</b> {road}")
            return text, is_bad

def get_cities_keyboard(user_cities: list):
    keyboard = []
    row = []
    for city in REGION_CITIES.keys():
        btn_text = f"✅ {city}" if city in user_cities else city
        row.append(InlineKeyboardButton(text=btn_text, callback_data=f"toggle_{city}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="💾 Сохранить и выйти", callback_data="save_settings")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- Маршруты ---
async def get_coords(session, address):
    query = address
    if not any(city.lower() in address.lower() for city in REGION_CITIES.keys()):
        query = f"Иваново, {address}"
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 1}
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

# --- Анализ новостей ---
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
            if street.lower() not in ["ивановская", "областная", "кинешемская"]: return street
    return None

def infer_traffic_impact(text):
    lower = text.lower()
    if any(w in lower for w in ["перекрыт", "смертельн", "погиб", "фура", "грузовик", "трасса", "тройное", "массов", "вылет"]):
        return "🔴 <b>Высокое</b>. Возможна серьезная пробка.", True
    elif any(w in lower for w in ["дтп", "авария", "столкновение", "пострадал", "скорая", "затор"]):
        return "🟠 <b>Среднее</b>. Движение замедлено.", False
    else:
        return "🟢 <b>Низкое</b>. Затруднений нет.", False

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
    if not raw_html: return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    text = soup.get_text(separator=' ')
    return re.sub(r'\s+', ' ', text).strip()

def find_city(text):
    lower_text = text.lower()
    for city in REGION_CITIES.keys():
        if city.lower() in lower_text:
            return city
    return "Ивановская область"

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
                    desc_tag = item.find('description') or item.find('summary')
                    raw_desc = desc_tag.get_text(strip=True) if desc_tag else ""
                    clean_desc = clean_html(raw_desc)
                    
                    full_text = text + " " + clean_desc
                    lower_text = full_text.lower()
                    
                    if 25 <= len(text) <= 110 and any(kw in lower_text for kw in keywords):
                        if ("иванов" in lower_text or "город" in lower_text or "област" in lower_text or any(c.lower() in lower_text for c in REGION_CITIES)):
                            if not any(sw in lower_text for sw in stop_words):
                                pub_date = item.find('pubDate')
                                found.append({
                                    "title": text, 
                                    "time": format_time(pub_date.text if pub_date else None, text),
                                    "desc": clean_desc,
                                    "city": find_city(full_text)
                                })
            else:
                for tag in soup.find_all(["h1", "h2", "h3", "a", "title"]):
                    text = tag.get_text(strip=True).replace('\n', ' ').replace('\r', '')
                    lower_text = text.lower()
                    if 25 <= len(text) <= 110 and any(kw in lower_text for kw in keywords):
                        if ("иванов" in lower_text or "город" in lower_text or "област" in lower_text or any(c.lower() in lower_text for c in REGION_CITIES)):
                            if not any(sw in lower_text for sw in stop_words):
                                found.append({"title": text, "time": format_time(None, text), "desc": "", "city": find_city(text)})
            return found
    except Exception:
        return []

async def get_traffic_summary(is_bad_weather, user_cities):
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
            
    filtered_news = []
    for news in unique_news:
        impact_text, is_high = infer_traffic_impact(news["title"] + " " + news["desc"])
        news["impact_text"] = impact_text
        news["is_high"] = is_high
        
        if is_high or news["city"] == "Ивановская область":
            filtered_news.append(news)
            continue
            
        if news["city"] in user_cities:
            filtered_news.append(news)
            continue

        if "Иваново" in user_cities and news["city"] in IVANOVO_AGGLOMERATION:
            filtered_news.append(news)
            continue

        if "Иваново" not in user_cities and news["city"] == "Иваново":
            filtered_news.append(news)
            continue
            
    if not filtered_news:
        return "✅ <b>Свежих сообщений о ДТП в выбранных городах нет.</b>"
    
    text = "🚨 <b>Дорожная обстановка:</b>\n"
    if is_bad_weather: text += "⚠️ <i>Погода ухудшает ситуацию!</i>\n"
    text += "\n"
    
    for news in filtered_news[:5]:
        news_text = news["title"][0].upper() + news["title"][1:]
        if news_text.endswith('.'): news_text = news_text[:-1]
        
        street = extract_street(news_text + " " + news["desc"])
        time_str = news["time"]

        text += f"📝 <b>Событие:</b> {news_text}.\n"
        if news["desc"] and len(news["desc"]) > 20:
            short_desc = news["desc"][:200] + "..." if len(news["desc"]) > 200 else news["desc"]
            text += f"📄 <b>Детали:</b> <i>{short_desc}</i>\n"
        text += f"🕒 <b>Время:</b> {time_str}.\n"
        text += f"🏙 <b>Город:</b> {news['city']}.\n"
        if street: text += f"📍 <b>Локация:</b> {street}.\n"
        text += f"🚦 <b>Влияние:</b> {news['impact_text']}\n"
        text += "➖➖➖➖➖➖➖➖\n"

    return text

# --- Утренняя рассылка ---
async def morning_broadcast():
    subscribers = get_subscribers()
    if not subscribers: return
    
    weather_text, is_bad = await get_weather_ivanovo()
    fuel_text = await get_fuel_prices(show_delta=True)
    
    for user_id in subscribers:
        user_cities = get_user_cities(user_id)
        region_text = await get_region_weather(user_cities)
        traffic_text = await get_traffic_summary(is_bad, user_cities)
        
        full_text = (f"🌅 <b>Доброе утро!</b>\n📅 Сводка на {datetime.now().strftime('%d.%m.%Y')}:\n\n"
                     f"{weather_text}\n\n{region_text}\n\n{fuel_text}\n\n{traffic_text}")
        try: await bot.send_message(user_id, full_text, parse_mode="HTML")
        except Exception: pass

# --- Хэндлеры ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"👋 Привет! Я слежу за погодой и дорожной обстановкой.\n\n"
        "📋 <b>Команды:</b>\n"
        "/weather — погода в Иваново 🌤\n"
        "/region — погода в выбранных городах 🌍\n"
        "/fuel — цены на топливо ⛽\n"
        "/settings — выбрать города ⚙️\n"
        "/traffic — ДТП и пробки 🚦\n"
        "/all — всё сразу 🗂\n"
        "/route — маршрут 🗺\n"
        "/subscribe — рассылка в 05:00 ⏰\n"
        "/unsubscribe — отписаться 🔕",
        parse_mode="HTML"
    )

@dp.message(Command("fuel"))
async def cmd_fuel(message: types.Message):
    await message.answer(await get_fuel_prices(show_delta=False), parse_mode="HTML")

@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    user_cities = get_user_cities(message.from_user.id)
    await message.answer("⚙️ <b>Настройка городов</b>\nВыберите города для отслеживания:", reply_markup=get_cities_keyboard(user_cities), parse_mode="HTML")

@dp.callback_query(F.data.startswith("toggle_"))
async def process_toggle_city(callback: CallbackQuery):
    city = callback.data.split("_")[1]
    user_cities = get_user_cities(callback.from_user.id)
    if city in user_cities: user_cities.remove(city)
    else: user_cities.append(city)
    await callback.message.edit_reply_markup(reply_markup=get_cities_keyboard(user_cities))
    await callback.answer(f"{city} {'добавлен' if city in user_cities else 'убран'}")

@dp.callback_query(F.data == "save_settings")
async def process_save_settings(callback: CallbackQuery):
    keyboard = callback.message.reply_markup.inline_keyboard
    selected_cities = [btn.text.replace("✅ ", "") for row in keyboard for btn in row if btn.text.startswith("✅")]
    save_user_cities(callback.from_user.id, selected_cities)
    await callback.message.edit_text("✅ Настройки сохранены! Теперь бот учтет ваши предпочтения.")
    await callback.answer()

@dp.message(Command("region"))
async def cmd_region(message: types.Message):
    user_cities = get_user_cities(message.from_user.id)
    await message.answer(await get_region_weather(user_cities), parse_mode="HTML")

@dp.message(Command("route"))
async def cmd_route(message: types.Message, state: FSMContext):
    await state.set_state(RouteState.waiting_for_start)
    await message.answer("🗺 <b>Откуда выезжаете?</b>\nНапишите адрес или город.", parse_mode="HTML")

@dp.message(RouteState.waiting_for_start)
async def process_route_start(message: types.Message, state: FSMContext):
    await state.update_data(start_point=message.text)
    await state.set_state(RouteState.waiting_for_end)
    await message.answer("🏁 <b>Куда едем?</b>\nНапишите конечный адрес или город.")

@dp.message(RouteState.waiting_for_end)
async def process_route_end(message: types.Message, state: FSMContext):
    end_point = message.text
    data = await state.get_data()
    start_point = data.get("start_point")
    await state.clear()

    wait_msg = await message.answer("⏳ Строю маршрут...")
    async with aiohttp.ClientSession() as session:
        start_coords = await get_coords(session, start_point)
        end_coords = await get_coords(session, end_point)
        if not start_coords or not end_coords:
            await wait_msg.edit_text("❌ Не удалось найти один из адресов.")
            return
        route_data = await get_route_info(session, start_coords, end_coords)
        if not route_data:
            await wait_msg.edit_text("❌ Не удалось проложить маршрут.")
            return
        dist_km, base_min = route_data

    _, is_bad_weather = await get_weather_ivanovo()
    user_cities = get_user_cities(message.from_user.id)
    traffic_text = await get_traffic_summary(is_bad_weather, user_cities)
    time_penalty = 10 if "🚨" in traffic_text else 0
    warning = "\n⚠️ <i>На дорогах есть ДТП. Возможны задержки.</i>" if time_penalty else ""

    total_min = int(base_min + time_penalty)
    hours, mins = total_min // 60, total_min % 60
    time_str = f"{hours} ч {mins} мин" if hours > 0 else f"{mins} мин"

    result_text = (f"🗺 <b>Маршрут построен:</b>\n📍 <b>От:</b> {start_point.capitalize()}\n"
                   f"🏁 <b>До:</b> {end_point.capitalize()}\n\n📏 <b>Расстояние:</b> {dist_km:.1f} км\n"
                   f"⏱ <b>Время:</b> {time_str}\n{warning}")
    await wait_msg.edit_text(result_text, parse_mode="HTML")

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    user_cities = get_user_cities(message.from_user.id)
    save_user_cities(message.from_user.id, user_cities)
    await message.answer("✅ Вы подписаны на утреннюю сводку в 05:00!")

@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: types.Message):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_settings WHERE user_id = ?", (message.from_user.id,))
    conn.commit()
    conn.close()
    await message.answer("❌ Вы отписаны от рассылки.")

@dp.message(Command("weather"))
async def cmd_weather(message: types.Message):
    weather_text, _ = await get_weather_ivanovo()
    await message.answer(weather_text, parse_mode="HTML")

@dp.message(Command("traffic"))
async def cmd_traffic(message: types.Message):
    _, is_bad = await get_weather_ivanovo()
    user_cities = get_user_cities(message.from_user.id)
    await message.answer(await get_traffic_summary(is_bad, user_cities), parse_mode="HTML")

@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    weather_text, is_bad = await get_weather_ivanovo()
    user_cities = get_user_cities(message.from_user.id)
    region_text = await get_region_weather(user_cities)
    fuel_text = await get_fuel_prices(show_delta=True)
    traffic_text = await get_traffic_summary(is_bad, user_cities)
    await message.answer(f"{weather_text}\n\n{region_text}\n\n{fuel_text}\n\n{traffic_text}", parse_mode="HTML")

async def main():
    scheduler = AsyncIOScheduler(timezone=MSK_TZ)
    scheduler.add_job(morning_broadcast, trigger='cron', hour=5, minute=0)
    scheduler.start()
    print("Бот запущен. Планировщик активен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

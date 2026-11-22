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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from datetime import datetime
from email.utils import parsedate_to_datetime

# --- Инициализация и настройки ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY") # Оставил, если захочешь вернуть OWM, но сейчас не используется

CITY_NAME = "Иваново"
MSK_TZ = pytz.timezone("Europe/Moscow")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- База данных ---
def init_db():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, cities TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS fuel_history (date TEXT PRIMARY KEY, ai92 REAL, ai95 REAL, dt REAL)")
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
    cursor.execute("INSERT INTO user_settings (user_id, cities) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET cities = excluded.cities", (user_id, json.dumps(cities, ensure_ascii=False)))
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
    cursor.execute("INSERT OR REPLACE INTO fuel_history (date, ai92, ai95, dt) VALUES (?, ?, ?, ?)", (today, ai92, ai95, dt))
    conn.commit()
    conn.close()

init_db()

# --- Справочники ---
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

# --- Меню (Клавиатура) ---
def get_main_menu():
    keyboard = [
        [KeyboardButton(text="🌤 Погода"), KeyboardButton(text="🌍 Города")],
        [KeyboardButton(text="🚦 ДТП и Заторы"), KeyboardButton(text="⛽ Бензин")],
        [KeyboardButton(text="🗺 Маршрут"), KeyboardButton(text="🗂 Всё сразу")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="⏰ Рассылка")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# --- Погода (Open-Meteo) ---
def get_weather_emoji(code):
    if code in [0, 1]: return "☀️"
    if code in [2]: return "🌤"
    if code in [3]: return "☁️"
    if code in [45, 48]: return "🌫"
    if code in [51, 53, 55, 56, 57]: return "🌦"
    if code in [61, 63, 65, 66, 67]: return "🌧"
    if code in [71, 73, 75, 77]: return "❄️"
    if code in [80, 81, 82]: return "🌧"
    if code in [85, 86]: return "❄️"
    if code in [95, 96, 99]: return "⛈"
    return "🌡"

def get_wmo_description(code):
    if code in [0, 1]: return "Ясно"
    if code in [2]: return "Переменная облачность"
    if code in [3]: return "Пасмурно"
    if code in [45, 48]: return "Туман"
    if code in [51, 53, 55, 56, 57]: return "Морось"
    if code in [61, 63, 65, 66, 67]: return "Дождь"
    if code in [71, 73, 75, 77]: return "Снег"
    if code in [80, 81, 82]: return "Ливень"
    if code in [85, 86]: return "Снегопад"
    if code in [95, 96, 99]: return "Гроза"
    return "Облачно"

def get_road_condition_wmo(code, temp):
    if code in [71, 73, 75, 77, 85, 86]: return "❄️ Снегопад. Дороги скользкие, возможны заносы.", True
    elif code in [51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82]:
        if temp < 2: return "🌧 Гололедица. Мокрый асфальт замерзает. Высокий риск ДТП!", True
        return "🌧 Мокрый асфальт. Тормозной путь увеличен.", True
    elif code in [45, 48]: return "🌫 Плохая видимость. Соблюдайте дистанцию.", True
    elif temp < -5: return "🧊 Сильный мороз. На дорогах возможна наледь.", True
    elif temp > 25: return "☀️ Жарко. Дороги сухие.", False
    return "✅ Дороги сухие, видимость хорошая.", False

async def get_weather_ivanovo():
    lat, lon = REGION_CITIES["Иваново"]
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,apparent_temperature,precipitation_probability,weather_code&timezone=Europe/Moscow"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200: return "⚠️ Ошибка сервера погоды.", False
                data = await resp.json()
                current = data.get("current", {})
                temp = current.get("temperature_2m", 0)
                feels = current.get("apparent_temperature", 0)
                code = current.get("weather_code", 0)
                prob = current.get("precipitation_probability", 0)
                
                emoji = get_weather_emoji(code)
                desc = get_wmo_description(code)
                road, is_bad = get_road_condition_wmo(code, temp)
                
                text = (f"{emoji} <b>Погода в Иваново:</b>\n"
                        f"🌡 Температура: <b>{temp:+.1f}°C</b> (ощущается {feels:+.1f}°C)\n"
                        f"☁️ {desc}\n"
                        f"💧 Вероятность осадков: {prob}%\n"
                        f"🛣 <b>Дорога:</b> {road}")
                return text, is_bad
        except Exception:
            return "⚠️ Ошибка сети при запросе погоды.", False

async def get_region_weather(user_cities: list):
    selected_cities = {city: REGION_CITIES[city] for city in user_cities if city in REGION_CITIES}
    if not selected_cities: return "⚠️ Вы не выбрали ни одного города. Зайдите в /settings."

    lats = ",".join(str(lat) for lat, lon in selected_cities.values())
    lons = ",".join(str(lon) for lat, lon in selected_cities.values())
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lons}&current=temperature_2m,precipitation_probability,weather_code&timezone=Europe/Moscow"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                if isinstance(data, dict): data = [data]
                lines = []
                for i, city in enumerate(selected_cities.keys()):
                    if i < len(data):
                        current = data[i].get("current", {})
                        lines.append(f"{get_weather_emoji(current.get('weather_code', 0))} <b>{city}</b>: {current.get('temperature_2m', 0):+.1f}°C, 💧{current.get('precipitation_probability', 0)}%")
                return "🌍 <b>Погода в выбранных городах:</b>\n" + "\n".join(lines)
        except Exception:
            return "⚠️ Ошибка при запросе к сервису."

# --- Топливо (Цены и Новости) ---
def format_price_with_delta(name, current, previous):
    if not previous or current is None: return f"🔹 <b>{name}:</b> {current} ₽/л\n" if current else ""
    diff = current - previous
    percent = (diff / previous) * 100
    if diff > 0: return f"🔹 <b>{name}:</b> {current} ₽/л 📈 <i>(+{percent:.2f}%)</i>\n"
    elif diff < 0: return f"🔹 <b>{name}:</b> {current} ₽/л 📉 <i>({percent:.2f}%)</i>\n"
    else: return f"🔹 <b>{name}:</b> {current} ₽/л ➡️ <i>(0%)</i>\n"

async def get_fuel_prices(show_delta=False):
    url = "https://benzinstatus.ru/ivanovo/ceny/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "ru-RU,ru;q=0.9"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=8) as resp:
                if resp.status != 200: return "⚠️ Сайт с ценами временно недоступен."
                soup = BeautifulSoup(await resp.text(), "html.parser")
                prices = {}
                for row in soup.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        fuel_text = cells[0].get_text(strip=True).upper()
                        match = re.search(r'(\d+[.,]\d+)', cells[1].get_text(strip=True))
                        if match:
                            val = float(match.group(1).replace(',', '.'))
                            if "АИ-92" in fuel_text: prices["АИ-92"] = val
                            elif "АИ-95" in fuel_text: prices["АИ-95"] = val
                            elif "ДТ" in fuel_text: prices["ДТ"] = val
                if not prices: return "⚠️ Не удалось распознать цены."

                text = "⛽ <b>Цены на топливо (Иваново):</b>\n\n"
                if show_delta:
                    yesterday = get_yesterday_fuel()
                    if yesterday:
                        text += format_price_with_delta("АИ-92", prices.get("АИ-92"), yesterday.get("АИ-92"))
                        text += format_price_with_delta("АИ-95", prices.get("АИ-95"), yesterday.get("АИ-95"))
                        text += format_price_with_delta("ДТ", prices.get("ДТ"), yesterday.get("ДТ"))
                    else:
                        text += f"🔹 <b>АИ-92:</b> {prices.get('АИ-92', '—')} ₽/л\n🔹 <b>АИ-95:</b> {prices.get('АИ-95', '—')} ₽/л\n🔹 <b>ДТ:</b> {prices.get('ДТ', '—')} ₽/л\n"
                    save_today_fuel(prices.get("АИ-92"), prices.get("АИ-95"), prices.get("ДТ"))
                else:
                    if "АИ-92" in prices: text += f"🔹 <b>АИ-92:</b> {prices['АИ-92']:.2f} ₽/л\n"
                    if "АИ-95" in prices: text += f"🔹 <b>АИ-95:</b> {prices['АИ-95']:.2f} ₽/л\n"
                    if "ДТ" in prices: text += f"🔹 <b>ДТ:</b> {prices['ДТ']:.2f} ₽/л\n"
                return text
    except Exception:
        return "⚠️ Ошибка сети при запросе цен."

async def get_fuel_situation_news():
    sources = ["https://newsivanovo.ru/rss", "https://ivanovonews.ru/rss/", "https://mkivanovo.ru/", "https://iv-news.ru/"]
    keywords = ["дефицит", "бензин", "топливо", "азс", "заправк", "очеред", "лимит", "ограничен"]
    stop_words = ["украин", "москв", "петербург", "росси", "сша", "европ"]
    found_news = []
    
    async with aiohttp.ClientSession() as session:
        for url in sources:
            try:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8) as resp:
                    if resp.status != 200: continue
                    soup = BeautifulSoup(await resp.text(), "html.parser")
                    for item in soup.find_all('item'):
                        title_tag = item.find('title')
                        if not title_tag: continue
                        text = title_tag.get_text(strip=True)
                        lower_text = text.lower()
                        if any(kw in lower_text for kw in keywords) and ("иванов" in lower_text or "област" in lower_text):
                            if not any(sw in lower_text for sw in stop_words):
                                if not any(text.lower() in ex.lower() or ex.lower() in text.lower() for ex in found_news):
                                    found_news.append(text)
            except Exception: continue
    
    if not found_news: return None
    return "📰 <b>Ситуация с топливом (новости):</b>\n\n" + "".join(f"🔸 {n}\n\n" for n in found_news[:3])

# --- Маршруты ---
async def get_coords(session, address):
    query = address if any(city.lower() in address.lower() for city in REGION_CITIES.keys()) else f"Иваново, {address}"
    url = "https://nominatim.openstreetmap.org/search"
    try:
        async with session.get(url, params={"q": query, "format": "json", "limit": 1}, headers={"User-Agent": "MalinkiBot/1.0"}, timeout=10) as resp:
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
            data = await resp.json()
            if data.get("routes"): return data["routes"][0]["distance"] / 1000, data["routes"][0]["duration"] / 60
    except Exception: pass
    return None

# --- Анализ ДТП и Заторов ---
def extract_street(text):
    patterns = [
        r'(?:ул(?:ица|\.)?|проспект|пр-т|переулок|пер\.|шоссе|пл(?:ощадь|\.)?|мкр(?:орайон|\.)?)\s+([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)',
        r'на\s+(?:улице\s+)?([А-ЯЁ][а-яё]+(?:ой|ий|ая|ое|ые))',
        r'в\s+([А-ЯЁ][а-яё]+ском)\s+районе',
        r'\b([А-ЯЁ][а-яё]+ская)\b',
        r'(?:перекрёст(?:ок|ке)|развязк[ае])\s+(?:улиц\s+)?([А-ЯЁ][а-яё]+)',
        r'(?:мост[ау]?|путепровод[ау]?)\s+(?:на\s+)?([А-ЯЁ][а-яё]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            street = match.group(1)
            if street.lower() not in ["ивановская", "областная", "кинешемская", "шуйская"]: return street
    return None

def infer_traffic_impact(text):
    lower = text.lower()
    if any(w in lower for w in ["перекрыт", "смертельн", "погиб", "фура", "грузовик", "трасса", "тройное", "массов", "вылет", "коллапс"]): return "🔴 <b>Высокое</b>. Возможна пробка.", True
    elif any(w in lower for w in ["дтп", "авария", "столкновение", "пострадал", "скорая", "затор", "пробка", "скопление"]): return "🟠 <b>Среднее</b>. Замедление.", False
    return "🟢 <b>Низкое</b>. Затруднений нет.", False

def format_time(pub_date_str=None, text=""):
    if pub_date_str:
        try:
            dt = parsedate_to_datetime(pub_date_str).astimezone(MSK_TZ)
            if dt.date() == datetime.now(MSK_TZ).date(): return dt.strftime("%H:%M")
            return f"Вчера в {dt.strftime('%H:%M')}"
        except Exception: pass
    match = re.search(r'\b([01]?[0-9]|2[0-3]):[0-5][0-9]\b', text)
    if match: return match.group(0)
    return "Недавно"

def clean_html(raw_html):
    if not raw_html: return ""
    return re.sub(r'\s+', ' ', BeautifulSoup(raw_html, "html.parser").get_text(separator=' ')).strip()

def find_city(text):
    lower_text = text.lower()
    for city in REGION_CITIES.keys():
        if city.lower() in lower_text: return city
    return "Ивановская область"

async def fetch_news(session, url):
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8) as resp:
            soup = BeautifulSoup(await resp.text(), "html.parser")
            keywords = ["дтп", "авария", "пробка", "затор", "столкновение", "перекрыт", "пострадал", "скопление"]
            stop_words = ["украин", "москв", "петербург", "росси", "сша", "европ"]
            found = []
            for item in soup.find_all('item'):
                title_tag = item.find('title')
                if not title_tag: continue
                text = title_tag.get_text(strip=True)
                clean_desc = clean_html(item.find('description').get_text(strip=True) if item.find('description') else "")
                lower_text = (text + " " + clean_desc).lower()
                if 25 <= len(text) <= 110 and any(kw in lower_text for kw in keywords):
                    if ("иванов" in lower_text or "област" in lower_text or any(c.lower() in lower_text for c in REGION_CITIES)):
                        if not any(sw in lower_text for sw in stop_words):
                            found.append({"title": text, "time": format_time(item.find('pubDate').text if item.find('pubDate') else None, text), "desc": clean_desc, "city": find_city(lower_text)})
            return found
    except Exception: return []

async def fetch_telegram_channel(session, channel_name):
    url = f"https://t.me/s/{channel_name}"
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10) as resp:
            if resp.status != 200: return []
            soup = BeautifulSoup(await resp.text(), "html.parser")
            keywords = ["затор", "пробка", "скопление", "затруднено", "перекрыт", "стоит"]
            found = []
            for msg in soup.find_all("div", class_="tgme_widget_message_text"):
                text = msg.get_text(separator=' ', strip=True)
                lower_text = text.lower()
                if any(kw in lower_text for kw in keywords) and ("иванов" in lower_text or any(c.lower() in lower_text for c in REGION_CITIES)):
                    found.append({"title": text[:100] + "...", "desc": text, "city": find_city(text), "time": "Недавно"})
            return found
    except Exception: return []

async def fetch_vk_group(session, group_url):
    try:
        async with session.get(group_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10) as resp:
            if resp.status != 200: return []
            soup = BeautifulSoup(await resp.text(), "html.parser")
            keywords = ["затор", "пробка", "скопление", "затруднено", "перекрыт"]
            found = []
            for post in soup.find_all("div", class_="wall_post_text"):
                text = post.get_text(separator=' ', strip=True)
                lower_text = text.lower()
                if any(kw in lower_text for kw in keywords):
                    found.append({"title": text[:100] + "...", "desc": text, "city": find_city(text), "time": "Недавно"})
            return found
    except Exception: return []

async def get_traffic_summary(is_bad_weather, user_cities):
    sources = ["https://newsivanovo.ru/rss", "https://ivanovonews.ru/rss/", "https://mkivanovo.ru/", "https://iv-news.ru/"]
    all_news = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_news(session, url) for url in sources]
        for res in await asyncio.gather(*tasks): all_news.extend(res)
        
        tg_channels = ["ivanovonewsBars", "typical_kineshma", "kineshemec_ru"]
        tg_tasks = [fetch_telegram_channel(session, ch) for ch in tg_channels]
        for res in await asyncio.gather(*tg_tasks): all_news.extend(res)
        
        vk_urls = ["https://m.vk.ru/avtopartner_ivanovo"]
        vk_tasks = [fetch_vk_group(session, url) for url in vk_urls]
        for res in await asyncio.gather(*vk_tasks): all_news.extend(res)

    if not all_news: return "✅ <b>Свежих сообщений о ДТП нет.</b>"
        
    unique_news = []
    for news in all_news:
        words_new = set(w.strip(".,!?\"'()-") for w in news["title"].lower().split() if len(w) > 3)
        if not any(len(words_new.intersection(set(w.strip(".,!?\"'()-") for w in ex["title"].lower().split() if len(w) > 3))) >= 3 for ex in unique_news):
            unique_news.append(news)
            
    filtered_news = []
    for news in unique_news:
        impact_text, is_high = infer_traffic_impact(news["title"] + " " + news.get("desc", ""))
        news["impact_text"] = impact_text
        if is_high or news["city"] == "Ивановская область" or news["city"] in user_cities or ("Иваново" in user_cities and news["city"] in IVANOVO_AGGLOMERATION) or ("Иваново" not in user_cities and news["city"] == "Иваново"):
            filtered_news.append(news)
            
    if not filtered_news: return "✅ <b>Свежих сообщений о заторах в выбранных городах нет.</b>"
    
    text = "🚨 <b>Дорожная обстановка:</b>\n"
    if is_bad_weather: text += "⚠️ <i>Погода ухудшает ситуацию!</i>\n"
    text += "\n"
    
    for news in filtered_news[:5]:
        news_text = news["title"][0].upper() + news["title"][1:]
        if news_text.endswith('.'): news_text = news_text[:-1]
        street = extract_street(news_text + " " + news.get("desc", ""))
        
        text += f"📝 <b>Событие:</b> {news_text}.\n"
        if street: text += f"📍 <b>Улица:</b> {street}.\n"
        text += f"🏙 <b>Город:</b> {news['city']}.\n"
        text += f"🚦 <b>Влияние:</b> {news['impact_text']}\n➖➖➖➖➖➖➖➖\n"
    return text

# --- Утренняя рассылка ---
async def morning_broadcast():
    subscribers = get_subscribers()
    if not subscribers: return
    weather_text, is_bad = await get_weather_ivanovo()
    fuel_text = await get_fuel_prices(show_delta=True)
    fuel_news = await get_fuel_situation_news()
    
    for user_id in subscribers:
        user_cities = get_user_cities(user_id)
        full_text = (f"🌅 <b>Доброе утро!</b>\n📅 Сводка на {datetime.now(MSK_TZ).strftime('%d.%m.%Y')}:\n\n"
                     f"{weather_text}\n\n{await get_region_weather(user_cities)}\n\n{fuel_text}\n\n")
        if fuel_news: full_text += f"{fuel_news}\n"
        full_text += f"{await get_traffic_summary(is_bad, user_cities)}"
        try: await bot.send_message(user_id, full_text, parse_mode="HTML")
        except Exception: pass

# --- Клавиатура настроек ---
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

# --- Хэндлеры кнопок и команд ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я слежу за обстановкой в регионе.\n"
        "Пользуйся кнопками внизу экрана для управления 👇",
        reply_markup=get_main_menu()
    )

@dp.message(F.text == "🌤 Погода")
@dp.message(Command("weather"))
async def cmd_weather(message: types.Message): await message.answer((await get_weather_ivanovo())[0], parse_mode="HTML")

@dp.message(F.text == "🌍 Города")
@dp.message(Command("region"))
async def cmd_region(message: types.Message): await message.answer(await get_region_weather(get_user_cities(message.from_user.id)), parse_mode="HTML")

@dp.message(F.text == "⛽ Бензин")
@dp.message(Command("fuel"))
async def cmd_fuel(message: types.Message):
    text = await get_fuel_prices(False)
    news = await get_fuel_situation_news()
    if news: text += f"\n\n{news}"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "🚦 ДТП и Заторы")
@dp.message(Command("traffic"))
async def cmd_traffic(message: types.Message): await message.answer(await get_traffic_summary((await get_weather_ivanovo())[1], get_user_cities(message.from_user.id)), parse_mode="HTML")

@dp.message(F.text == "🗂 Всё сразу")
@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    weather_text, is_bad = await get_weather_ivanovo()
    fuel_news = await get_fuel_situation_news()
    text = (f"{weather_text}\n\n{await get_region_weather(get_user_cities(message.from_user.id))}\n\n{await get_fuel_prices(True)}\n\n")
    if fuel_news: text += f"{fuel_news}\n"
    text += f"{await get_traffic_summary(is_bad, get_user_cities(message.from_user.id))}"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "⚙️ Настройки")
@dp.message(Command("settings"))
async def cmd_settings(message: types.Message): await message.answer("⚙️ <b>Настройка городов</b>\nВыберите города для отслеживания:", reply_markup=get_cities_keyboard(get_user_cities(message.from_user.id)), parse_mode="HTML")

@dp.message(F.text == "⏰ Рассылка")
async def menu_subscribe(message: types.Message):
    if message.from_user.id in get_subscribers():
        conn = sqlite3.connect("users.db")
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (message.from_user.id,))
        conn.commit(); conn.close()
        await message.answer("❌ Вы отписались от утренней рассылки в 05:00.")
    else:
        save_user_cities(message.from_user.id, get_user_cities(message.from_user.id))
        await message.answer("✅ Вы подписались на утреннюю сводку в 05:00 по МСК!")

@dp.message(F.text == "🗺 Маршрут")
async def menu_route(message: types.Message, state: FSMContext):
    await state.set_state(RouteState.waiting_for_start)
    await message.answer("🗺 <b>Откуда выезжаете?</b>\nНапишите адрес или город.", parse_mode="HTML")

@dp.message(RouteState.waiting_for_start)
async def process_route_start(message: types.Message, state: FSMContext):
    await state.update_data(start_point=message.text)
    await state.set_state(RouteState.waiting_for_end)
    await message.answer("🏁 <b>Куда едем?</b>\nНапишите конечный адрес или город.")

@dp.message(RouteState.waiting_for_end)
async def process_route_end(message: types.Message, state: FSMContext):
    end_point, data = message.text, await state.get_data()
    start_point = data.get("start_point")
    await state.clear()
    wait_msg = await message.answer("⏳ Строю маршрут...")
    async with aiohttp.ClientSession() as session:
        start_coords, end_coords = await get_coords(session, start_point), await get_coords(session, end_point)
        if not start_coords or not end_coords: return await wait_msg.edit_text("❌ Адрес не найден.")
        route_data = await get_route_info(session, start_coords, end_coords)
        if not route_data: return await wait_msg.edit_text("❌ Ошибка маршрута.")
        dist_km, base_min = route_data
    _, is_bad = await get_weather_ivanovo()
    time_penalty = 10 if "🚨" in await get_traffic_summary(is_bad, get_user_cities(message.from_user.id)) else 0
    total_min = int(base_min + time_penalty)
    time_str = f"{total_min // 60} ч {total_min % 60} мин"
    await wait_msg.edit_text(f"🗺 <b>Маршрут:</b>\n{start_point.capitalize()} ➡️ {end_point.capitalize()}\n📏 {dist_km:.1f} км\n⏱ {time_str}", parse_mode="HTML")

# --- Обработчики инлайн-клавиатуры ---
@dp.callback_query(F.data.startswith("toggle_"))
async def process_toggle_city(callback: CallbackQuery):
    city = callback.data.split("_")[1]
    user_cities = get_user_cities(callback.from_user.id)
    if city in user_cities: user_cities.remove(city)
    else: user_cities.append(city)
    await callback.message.edit_reply_markup(reply_markup=get_cities_keyboard(user_cities))
    await callback.answer()

@dp.callback_query(F.data == "save_settings")
async def process_save_settings(callback: CallbackQuery):
    selected_cities = [btn.text.replace("✅ ", "") for row in callback.message.reply_markup.inline_keyboard for btn in row if btn.text.startswith("✅")]
    save_user_cities(callback.from_user.id, selected_cities)
    await callback.message.edit_text("✅ Настройки сохранены!")
    await callback.answer()

# --- Запуск ---
async def main():
    scheduler = AsyncIOScheduler(timezone=MSK_TZ)
    scheduler.add_job(morning_broadcast, trigger='cron', hour=5, minute=0)
    scheduler.start()
    print("Бот запущен. Планировщик активен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

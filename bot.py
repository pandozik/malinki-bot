import os
import asyncio
import aiohttp
import re
import sqlite3
import json
import hashlib
import math
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 5007806242
CITY_NAME = "Иваново"
MSK_TZ = pytz.timezone("Europe/Moscow")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- База данных ---
def init_db():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, cities TEXT, broadcast_times TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS fuel_history (date TEXT PRIMARY KEY, ai92 REAL, ai95 REAL, dt REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS seen_news (user_id INTEGER, news_hash TEXT, seen_date TEXT, PRIMARY KEY (user_id, news_hash))")
    cursor.execute("CREATE TABLE IF NOT EXISTS saved_routes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, start_point TEXT, end_point TEXT)")
    conn.commit()
    conn.close()

def get_user_cities(user_id: int) -> list:
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT cities FROM user_settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone(); conn.close()
    return json.loads(row[0]) if row and row[0] else ["Иваново"]

def save_user_cities(user_id: int, cities: list):
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("INSERT INTO user_settings (user_id, cities) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET cities = excluded.cities", (user_id, json.dumps(cities, ensure_ascii=False)))
    conn.commit(); conn.close()

def get_user_times(user_id: int) -> list:
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT broadcast_times FROM user_settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone(); conn.close()
    return json.loads(row[0]) if row and row[0] else []

def save_user_times(user_id: int, times_list: list):
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("INSERT INTO user_settings (user_id, broadcast_times) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET broadcast_times = excluded.broadcast_times", (user_id, json.dumps(times_list, ensure_ascii=False)))
    conn.commit(); conn.close()

def get_users_for_broadcast(current_time: str) -> list:
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT user_id, broadcast_times FROM user_settings")
    rows = cursor.fetchall(); conn.close()
    users = []
    for row in rows:
        if row[1]:
            if current_time in json.loads(row[1]): users.append(row[0])
    return users

def get_saved_routes(user_id: int) -> list:
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT id, name, start_point, end_point FROM saved_routes WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall(); conn.close()
    return rows

def save_new_route(user_id: int, name: str, start: str, end: str):
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("INSERT INTO saved_routes (user_id, name, start_point, end_point) VALUES (?, ?, ?, ?)", (user_id, name, start, end))
    conn.commit(); conn.close()

def generate_hash(text): return hashlib.md5(text.encode('utf-8')).hexdigest()

def is_news_seen(user_id, news_hash):
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_news WHERE user_id = ? AND news_hash = ?", (user_id, news_hash))
    row = cursor.fetchone(); conn.close()
    return bool(row)

def mark_news_as_seen(user_id, news_hash):
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO seen_news (user_id, news_hash, seen_date) VALUES (?, ?, ?)", (user_id, news_hash, datetime.now(MSK_TZ).strftime("%Y-%m-%d")))
    conn.commit(); conn.close()

def cleanup_old_news():
    cutoff = (datetime.now(MSK_TZ) - timedelta(days=3)).strftime("%Y-%m-%d")
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("DELETE FROM seen_news WHERE seen_date < ?", (cutoff,))
    conn.commit(); conn.close()

def get_bot_stats():
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM user_settings"); total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM user_settings WHERE broadcast_times IS NOT NULL AND broadcast_times != '[]'"); subs = cursor.fetchone()[0]
    conn.close(); return total, subs

def get_yesterday_fuel():
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT ai92, ai95, dt FROM fuel_history ORDER BY date DESC LIMIT 1")
    row = cursor.fetchone(); conn.close()
    return {"АИ-92": row[0], "АИ-95": row[1], "ДТ": row[2]} if row else None

def save_today_fuel(ai92, ai95, dt):
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO fuel_history (date, ai92, ai95, dt) VALUES (?, ?, ?, ?)", (datetime.now(MSK_TZ).strftime("%Y-%m-%d"), ai92, ai95, dt))
    conn.commit(); conn.close()

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
CAMERAS = [
    {"name": "Перекресток Ленина / Багаева", "lat": 56.9986, "lon": 40.9742}, {"name": "Проспект Строителей / Шубиных", "lat": 57.0021, "lon": 40.9613},
    {"name": "Лежневская / Громобоя", "lat": 56.9789, "lon": 40.9712}, {"name": "Уводьский мост", "lat": 57.0011, "lon": 40.9841},
    {"name": "Шереметевский проспект", "lat": 57.0015, "lon": 40.9758}, {"name": "Улица Куконковых", "lat": 56.9923, "lon": 40.9582},
    {"name": "Московское шоссе (въезд)", "lat": 56.9321, "lon": 40.9210}, {"name": "Кохомское шоссе", "lat": 56.9448, "lon": 41.0112},
    {"name": "Тейковское шоссе", "lat": 56.9811, "lon": 40.9105}, {"name": "Улица Некрасова", "lat": 56.9995, "lon": 40.9851}
]

CITY_PATTERNS = {
    "Иваново": r'\bиванов(?:о|е|а|у|ым)\b',
    "Шуя": r'\bшу(?:я|е|ю|и|ей)\b',
    "Кинешма": r'кинешм(?:а|е|у|ы|ой)\b',
    "Вичуга": r'вичуг(?:а|е|у|и|ой)\b',
    "Тейково": r'тейков(?:о|е|а|у)\b',
    "Фурманов": r'фурманов(?:е|а|у|ом)?\b',
    "Кохма": r'кохм(?:а|е|у|ы|ой)\b',
    "Родники": r'родник(?:и|ах|ов|ам)\b',
    "Приволжск": r'приволжск(?:е|а|у|ом)?\b',
    "Заволжск": r'заволжск(?:е|а|у|ом)?\b',
    "Наволоки": r'наволок(?:и|ах|ов|ам)\b',
    "Юрьевец": r'юрьев(?:ец|це|ца|цу)\b',
    "Южа": r'\bюж(?:а|е|у|ей)\b',
    "Гаврилов Посад": r'гаврилов[- ]посад',
    "Комсомольск": r'комсомольск(?:е|а|у|ом)?\b',
    "Плес": r'\bпл[её]с(?:е|а|у|ом)?\b',
    "Пучеж": r'пучеж(?:е|а|у|ом)?\b',
}

OBLAST_DISTRICTS = {
    "Ивановский район": r'ивановск(?:ом|ого|ий|ому)\s+район',
    "Шуйский район": r'шуйск(?:ом|ого|ий|ому)\s+район',
    "Кинешемский район": r'кинешемск(?:ом|ого|ий|ому)\s+район',
    "Вичугский район": r'вичугск(?:ом|ого|ий|ому)\s+район',
    "Тейковский район": r'тейковск(?:ом|ого|ий|ому)\s+район',
    "Фурмановский район": r'фурмановск(?:ом|ого|ий|ому)\s+район',
    "Родниковский район": r'родниковск(?:ом|ого|ий|ому)\s+район',
    "Приволжский район": r'приволжск(?:ом|ого|ий|ому)\s+район',
    "Заволжский район": r'заволжск(?:ом|ого|ий|ому)\s+район',
    "Юрьевецкий район": r'юрьевецк(?:ом|ого|ий|ому)\s+район',
    "Южский район": r'южск(?:ом|ого|ий|ому)\s+район',
    "Гаврилово-Посадский район": r'гаврилово[- ]посадск(?:ом|ого|ий|ому)\s+район',
    "Комсомольский район": r'комсомольск(?:ом|ого|ий|ому)\s+район',
    "Пучежский район": r'пучежск(?:ом|ого|ий|ому)\s+район',
    "Палехский район": r'палехск(?:ом|ого|ий|ому)\s+район',
    "Пестяковский район": r'пестяковск(?:ом|ого|ий|ому)\s+район',
    "Лухский район": r'лухск(?:ом|ого|ий|ому)\s+район',
    "Верхнеландеховский район": r'верхнеландеховск(?:ом|ого|ий|ому)\s+район',
    "Ильинский район": r'ильинск(?:ом|ого|ий|ому)\s+район',
    "Лежневский район": r'лежневск(?:ом|ого|ий|ому)\s+район',
    "Савинский район": r'савинск(?:ом|ого|ий|ому)\s+район',
}

CITY_DISTRICTS = {
    "Ленинский район": r'ленинск(?:ом|ого|ий|ому)\s+район',
    "Октябрьский район": r'октябрьск(?:ом|ого|ий|ому)\s+район',
    "Советский район": r'советск(?:ом|ого|ий|ому)\s+район',
    "Фрунзенский район": r'фрунзенск(?:ом|ого|ий|ому)\s+район',
    "мкр. Авдотьино": r'авдотьин[оеа]',
    "мкр. Сортировка": r'сортировк[аеу]',
    "мкр. Московский": r'московск(?:ий|ом)\s+микрорайон',
    "мкр. Суховка": r'суховк[аеу]',
    "Местечко Пустошь-Бор": r'пустошь[- ]бор',
    "Местечко Балино": r'балин[ое]',
    "Местечко Минеево": r'минеев[ое]',
    "Местечко Курьяново": r'курьянов[ое]',
}

THEFT_PATTERN = r'\b(угон\w*|угнал\w*|похищ\w*\s+(?:авто|машин|транспорт|ваз|лада|иномарк)|похитил\w*\s+(?:авто|машин|транспорт)|розыск\w*\s+(?:авто|машин|транспорт)|краж\w*\s+(?:авто|машин))\b'

class RouteState(StatesGroup):
    waiting_for_start = State(); waiting_for_end = State(); waiting_for_route_name = State()
class BroadcastState(StatesGroup):
    waiting_for_time = State()

def get_main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🌤 Погода"), KeyboardButton(text="🌍 Города")],
        [KeyboardButton(text="🚦 ДТП, Заторы, Угоны"), KeyboardButton(text="⛽ Бензин")],
        [KeyboardButton(text="🗺 Маршрут"), KeyboardButton(text="🗂 Всё сразу")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="⏰ Время рассылки")]
    ], resize_keyboard=True)

# --- Погода ---
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

def get_road_condition(code, temp, humidity, precip_now, precip24, snow24, min24, visibility_m):
    snow_codes = [71, 73, 75, 77, 85, 86]
    heavy_rain = [65, 82]
    rain_codes = [51, 53, 55, 56, 57, 61, 63, 66, 67, 80, 81]
    fog = code in [45, 48] or (visibility_m is not None and visibility_m < 1000)

    if code in snow_codes: return "❄️ Идёт снег. Скользко, возможны заносы.", True
    if code in heavy_rain: return "🌧 Сильный ливень. Плохая видимость, риск аквапланирования.", True
    if code in rain_codes or precip_now > 0.1:
        if temp <= 2: return "🧊 Дождь при температуре около нуля: высокий риск гололедицы!", True
        return "🌧 Идёт дождь. Асфальт мокрый, тормозной путь больше.", True
    if fog: return "🌫 Туман или дымка, плохая видимость. Держите дистанцию.", True
    if precip24 >= 0.3:
        if snow24 >= 0.5 and temp <= 2: return "❄️ Недавно выпал снег: возможны снежный накат и наледь.", True
        if temp <= 2 or min24 <= 0: return "🧊 После осадков температура около нуля: возможна гололедица.", True
        return "💧 После недавних осадков асфальт, скорее всего, мокрый.", True
    if temp <= 1 and humidity >= 85: return "🧊 Около нуля и высокая влажность: возможна гололедица.", True
    if temp < -5: return "🧊 Сильный мороз. На дорогах возможна наледь.", True
    if visibility_m is not None and visibility_m < 5000: return "☁️ Осадков нет, но видимость снижена (дымка).", False
    return "✅ Осадков нет, дороги, вероятно, сухие.", False

def _num(x, default=0.0): return default if x is None else x

async def get_weather_ivanovo():
    lat, lon = REGION_CITIES["Иваново"]
    base = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    url = (base + "&current=temperature_2m,apparent_temperature,precipitation_probability,weather_code,relative_humidity_2m,precipitation"
           "&hourly=precipitation,snowfall,rain,temperature_2m,visibility&past_hours=24&forecast_hours=12&timezone=Europe/Moscow")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200: return "⚠️ Ошибка при запросе погоды.", False
                data = await resp.json()
        except Exception: return "⚠️ Ошибка сети при запросе погоды.", False

    current = data.get("current", {})
    hourly = data.get("hourly", {})
    temp = _num(current.get("temperature_2m"))
    feels = _num(current.get("apparent_temperature"))
    code = current.get("weather_code", 0)
    prob = _num(current.get("precipitation_probability"))
    humidity = _num(current.get("relative_humidity_2m"), 0)
    precip_now = _num(current.get("precipitation"))

    precip_list = hourly.get("precipitation", [])
    snow_list = hourly.get("snowfall", [])
    
    precip24 = sum(_num(v) for v in precip_list[0:24])
    snow24 = sum(_num(v) for v in snow_list[0:24])
    temps = [v for v in hourly.get("temperature_2m", [])[0:24] if v is not None]
    min24 = min(temps) if temps else temp
    vis_list = [v for v in hourly.get("visibility", [])[0:24] if v is not None]
    visibility = vis_list[-1] if vis_list else None

    duration_text = ""
    if precip_now > 0:
        hours_left = 0
        for i in range(24, len(precip_list)):
            if _num(precip_list[i]) > 0.05:
                hours_left += 1
            else:
                break
        if hours_left > 0: duration_text = f"⏳ Осадки еще ~{hours_left} ч.\n"

    emoji = get_weather_emoji(code)
    desc = get_wmo_description(code)
    road, is_bad = get_road_condition(code, temp, humidity, precip_now, precip24, snow24, min24, visibility)

    text = (f"{emoji} <b>Погода в Иваново:</b>\n"
            f"🌡 Температура: <b>{temp:+.1f}°C</b> (ощущается {feels:+.1f}°C)\n"
            f"☁️ {desc}\n"
            f"💧 Вероятность осадков: {prob:.0f}%\n{duration_text}")
    if visibility is not None: text += f"👁 Видимость: ~{visibility / 1000:.1f} км\n"
    text += f"🛣 <b>Дорога:</b> {road}\n<i>Оценка по погоде за сутки.</i>"
    return text, is_bad

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
        except Exception: return "⚠️ Ошибка при запросе к сервису."

# --- Топливо ---
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
    except Exception: return "⚠️ Ошибка сети при запросе цен."

# --- Свежесть новостей ---
def parse_rss_date(s):
    if not s: return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=pytz.utc)
        return dt
    except Exception: return None

def is_fresh(dt) -> bool:
    if dt is None: return False
    today = datetime.now(MSK_TZ).date()
    return (today - dt.astimezone(MSK_TZ).date()).days in (0, 1)

def vk_post_is_fresh(text_div) -> bool:
    node = text_div
    for _ in range(6):
        node = node.parent
        if node is None: return False
        d = node.find(class_="rel_date") or node.find(class_="wi_date")
        if d:
            return any(w in d.get_text(" ", strip=True).lower() for w in ("сегодня", "вчера", "назад", "только что"))
    return False

async def get_fuel_situation_news():
    sources = ["https://newsivanovo.ru/rss", "https://ivanovonews.ru/rss/", "https://mkivanovo.ru/", "https://iv-news.ru/"]
    keywords = ["дефицит", "бензин", "топливо", "азс", "заправк", "очеред", "лимит", "ограничен"]
    stop_words = ["украин", "москв", "петербург", "росси", "сша", "европ", "пробаци", "проблем"]
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
                        date_tag = item.find('pubdate')
                        if not is_fresh(parse_rss_date(date_tag.get_text(strip=True) if date_tag else None)): continue
                        text = title_tag.get_text(strip=True)
                        lower_text = text.lower()
                        if any(re.search(rf'\b{kw}\b', lower_text) for kw in keywords) and ("иванов" in lower_text or "област" in lower_text):
                            if not any(re.search(rf'\b{sw}\b', lower_text) for sw in stop_words):
                                if not any(text.lower() in ex.lower() or ex.lower() in text.lower() for ex in found_news):
                                    found_news.append(text)
            except Exception: continue
    if not found_news: return None
    return "📰 <b>Топливо (новости):</b>\n" + "".join(f"🔸 {n}\n\n" for n in found_news[:3])

async def get_weather_news():
    sources = ["https://newsivanovo.ru/rss", "https://ivanovonews.ru/rss/", "https://mkivanovo.ru/", "https://iv-news.ru/"]
    keywords = ["снегопад", "метель", "гололед", "ураган", "мчс", "циклон", "штормовое", "мороз", "занос", "подтоплен", "паводок", "гололедица"]
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
                        date_tag = item.find('pubdate')
                        if not is_fresh(parse_rss_date(date_tag.get_text(strip=True) if date_tag else None)): continue
                        text = title_tag.get_text(strip=True)
                        lower_text = text.lower()
                        if any(re.search(rf'\b{kw}\b', lower_text) for kw in keywords):
                            if not any(text.lower() in ex.lower() or ex.lower() in text.lower() for ex in found_news):
                                found_news.append(text)
            except Exception: continue
    if not found_news: return None
    return "🌪 <b>Погодные предупреждения:</b>\n" + "".join(f"⚠️ {n}\n\n" for n in found_news[:3])

# --- Маршруты и локации ---
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0; dlat = math.radians(lat2 - lat1); dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

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
    url = f"http://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}?overview=full&geometries=geojson"
    try:
        async with session.get(url, timeout=10) as resp:
            data = await resp.json()
            if data.get("routes"):
                route = data["routes"][0]
                return route["distance"] / 1000, route["duration"] / 60, route["geometry"]["coordinates"]
    except Exception: pass
    return None

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
            if street.lower() not in ["ивановская", "областная", "кинешемская", "шуйская"]:
                return street
    return None

def extract_location(text):
    lower_text = text.lower()
    city = None
    district = None

    for c_name, pattern in CITY_PATTERNS.items():
        if re.search(pattern, lower_text):
            city = c_name
            break

    for d_name, pattern in OBLAST_DISTRICTS.items():
        if re.search(pattern, lower_text):
            district = d_name
            if not city:
                base_city = d_name.split()[0].rstrip("ий").rstrip("ск")
                for c_name in REGION_CITIES:
                    if c_name.lower().startswith(base_city[:4]):
                        city = c_name
                        break
            break

    if not district:
        for cd_name, pattern in CITY_DISTRICTS.items():
            if re.search(pattern, lower_text):
                district = cd_name
                if not city:
                    city = "Иваново"
                break

    if not district:
        village_match = re.search(r'(?:дер(?:евне|\.)?|с(?:еле|\.)?|пос(?:ёлке|елке|\.)?|пгт\.?)\s+([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)', text)
        if village_match:
            district = f"н.п. {village_match.group(1)}"

    if not city:
        city = "Ивановская область"

    return city, district

def infer_traffic_impact(text, is_theft=False):
    if is_theft:
        return "🚓 <b>Угон / Розыск авто.</b> Будьте внимательны!", False
    lower = text.lower()
    if any(w in lower for w in ["перекрыт", "смертельн", "погиб", "фура", "грузовик", "трасса", "тройное", "массов", "вылет", "коллапс", "пострадал", "пьян", "встречк", "сбил"]):
        return "🔴 <b>Высокое</b>. Возможна пробка.", True
    elif any(w in lower for w in ["дтп", "авари", "столкн", "скорая", "затор", "пробка", "скопление"]):
        return "🟠 <b>Среднее</b>. Замедление.", False
    return "🟢 <b>Низкое</b>. Затруднений нет.", False

def format_time(dt=None):
    if dt:
        dt = dt.astimezone(MSK_TZ)
        if dt.date() == datetime.now(MSK_TZ).date(): return f"Сегодня в {dt.strftime('%H:%M')}"
        return f"Вчера в {dt.strftime('%H:%M')}"
    return "Недавно"

def clean_html(raw_html):
    if not raw_html: return ""
    return re.sub(r'\s+', ' ', BeautifulSoup(raw_html, "html.parser").get_text(separator=' ')).strip()

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
DATE_RE = re.compile(r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\b')

def event_date_ok(text: str) -> bool:
    today = datetime.now(MSK_TZ).date(); dates = []
    for d, mon in DATE_RE.findall(text.lower()):
        try: dt = datetime(today.year, MONTHS[mon], int(d)).date()
        except ValueError: continue
        if (dt - today).days > 7: dt = dt.replace(year=today.year - 1)
        dates.append(dt)
    return True if not dates else (today - max(dates)).days <= 1

def clean_telegram_text(text):
    if not text: return ""
    for phrase in [r"Иллюстрация создана ИИ", r"Фото:.*", r"Подписывайтесь.*", r"Проголосовать за канал.*", r"Прислать новость.*"]:
        text = re.sub(phrase, "", text, flags=re.IGNORECASE)
    return re.sub(r'\n{3,}', '\n\n', text).strip()

async def fetch_news(session, url):
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8) as resp:
            soup = BeautifulSoup(await resp.text(), "html.parser")
            traffic_pattern = r'\b(дтп|авари|пробк|затор|столкн|перекрыт|пострадал|скоплен)\b'
            stop_words = ["украин", "москв", "петербург", "росси", "сша", "европ", "пробаци", "проблем"]
            found = []
            for item in soup.find_all('item'):
                title_tag = item.find('title')
                if not title_tag: continue
                date_tag = item.find('pubdate')
                dt = parse_rss_date(date_tag.get_text(strip=True) if date_tag else None)
                if not is_fresh(dt): continue
                text = title_tag.get_text(strip=True)
                clean_desc = clean_html(item.find('description').get_text(strip=True) if item.find('description') else "")
                full_str = text + " " + clean_desc
                if not event_date_ok(full_str): continue
                lower_text = full_str.lower()

                is_traffic = bool(re.search(traffic_pattern, lower_text))
                is_theft = bool(re.search(THEFT_PATTERN, lower_text))

                if is_traffic or is_theft:
                    if ("иванов" in lower_text or "област" in lower_text or any(c.lower() in lower_text for c in REGION_CITIES)):
                        if not any(re.search(rf'\b{sw}\b', lower_text) for sw in stop_words):
                            city, district = extract_location(full_str)
                            found.append({
                                "title": text,
                                "full_text": clean_desc or text,
                                "time": format_time(dt),
                                "city": city,
                                "district": district,
                                "is_theft": is_theft
                            })
            return found
    except Exception: return []

async def fetch_telegram_channel(session, channel_name):
    url = f"https://t.me/s/{channel_name}"
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10) as resp:
            if resp.status != 200: return []
            soup = BeautifulSoup(await resp.text(), "html.parser")
            traffic_pattern = r'\b(затор|пробк|скоплен|затруднен|перекрыт|стоит|дтп|авари|столкн|сбил|вылет|опрокид|наезд)\b'
            found = []
            for wrap in soup.find_all("div", class_="tgme_widget_message_wrap"):
                msg_divs = wrap.find_all("div", class_="tgme_widget_message_text")
                if not msg_divs: continue
                time_tag = wrap.find("time")
                dt = None
                if time_tag and time_tag.get("datetime"):
                    try: dt = datetime.fromisoformat(time_tag["datetime"])
                    except ValueError: dt = None
                if not is_fresh(dt): continue
                raw_text = "\n".join([div.get_text(separator='\n', strip=True) for div in msg_divs])
                clean_text = clean_telegram_text(raw_text)
                if not event_date_ok(clean_text): continue
                lower_text = clean_text.lower()

                is_traffic = bool(re.search(traffic_pattern, lower_text))
                is_theft = bool(re.search(THEFT_PATTERN, lower_text))

                if (is_traffic or is_theft) and ("иванов" in lower_text or any(c.lower() in lower_text for c in REGION_CITIES) or "юрьев" in lower_text):
                    city, district = extract_location(clean_text)
                    found.append({
                        "title": clean_text[:60] + "...",
                        "full_text": clean_text,
                        "city": city,
                        "district": district,
                        "time": format_time(dt),
                        "is_theft": is_theft
                    })
            return found
    except Exception: return []

async def fetch_vk_group(session, group_url):
    try:
        async with session.get(group_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10) as resp:
            if resp.status != 200: return []
            soup = BeautifulSoup(await resp.text(), "html.parser")
            traffic_pattern = r'\b(затор|пробка|пробки|скопление|затруднено|перекрыт|дтп|авария|столкновение|сбил|вылет)\b'
            found = []
            for post in soup.find_all("div", class_="wall_post_text"):
                if not vk_post_is_fresh(post): continue
                raw_text = post.get_text(separator='\n', strip=True)
                clean_text = clean_telegram_text(raw_text)
                if not event_date_ok(clean_text): continue
                lower_text = clean_text.lower()

                is_traffic = bool(re.search(traffic_pattern, lower_text))
                is_theft = bool(re.search(THEFT_PATTERN, lower_text))

                if is_traffic or is_theft:
                    city, district = extract_location(clean_text)
                    found.append({
                        "title": clean_text[:60] + "...",
                        "full_text": clean_text,
                        "city": city,
                        "district": district,
                        "time": "Сегодня/вчера",
                        "is_theft": is_theft
                    })
            return found
    except Exception: return []

async def get_traffic_summary(user_id, is_bad_weather, user_cities, mark_seen=True):
    cleanup_old_news()
    sources = ["https://newsivanovo.ru/rss", "https://ivanovonews.ru/rss/", "https://mkivanovo.ru/", "https://iv-news.ru/"]
    all_news = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_news(session, url) for url in sources]
        for res in await asyncio.gather(*tasks): all_news.extend(res)
        tg_channels = ["ivanovonewsBars", "typical_kineshma", "kineshemec_ru", "ivanovo_proisshestvia"]
        tg_tasks = [fetch_telegram_channel(session, ch) for ch in tg_channels]
        for res in await asyncio.gather(*tg_tasks): all_news.extend(res)
        vk_urls = ["https://m.vk.ru/avtopartner_ivanovo"]
        vk_tasks = [fetch_vk_group(session, url) for url in vk_urls]
        for res in await asyncio.gather(*vk_tasks): all_news.extend(res)

    if not all_news:
        return "✅ <b>Свежих сообщений о ДТП и угонах нет (за сегодня и вчера).</b>"

    unique_news = []
    for news in all_news:
        words_new = set(w.strip(".,!?\"'()-").lower() for w in news["full_text"].split() if len(w) > 4)
        if not any(len(words_new.intersection(set(w.strip(".,!?\"'()-").lower() for w in ex["full_text"].split() if len(w) > 4))) >= 4 for ex in unique_news):
            unique_news.append(news)

    filtered_news = []
    jam_streets = set()
    traffic_events_count = 0

    for news in unique_news:
        is_theft = news.get("is_theft", False)
        impact_text, is_high = infer_traffic_impact(news["title"] + " " + news.get("full_text", ""), is_theft=is_theft)
        news["impact_text"] = impact_text

        if is_theft or is_high or news["city"] == "Ивановская область" or news["city"] in user_cities or ("Иваново" in user_cities and news["city"] in IVANOVO_AGGLOMERATION) or ("Иваново" not in user_cities and news["city"] == "Иваново"):
            news_hash = generate_hash(news["full_text"])
            if not is_news_seen(user_id, news_hash):
                filtered_news.append(news)
                if not is_theft:
                    traffic_events_count += 1
                    if is_high or "затор" in news["full_text"].lower() or "пробка" in news["full_text"].lower():
                        street = extract_street(news["full_text"])
                        if street: jam_streets.add(street)
                if mark_seen:
                    mark_news_as_seen(user_id, news_hash)

    if not filtered_news:
        return "✅ <b>Свежих сообщений о заторах и угонах нет (все прочитаны).</b>"

    jam_level = min(10, 2 + traffic_events_count * 2) if traffic_events_count > 0 else 1
    text = "🚨 <b>Дорожная обстановка и происшествия:</b>\n"
    if jam_streets:
        text += f"📊 Условный уровень пробок: <b>{jam_level}/10</b>\n📍 Улицы: <b>{', '.join(list(jam_streets)[:5])}</b>\n\n"
    else:
        text += f"📊 Уровень пробок: <b>Низкий</b>\n\n"
    if is_bad_weather:
        text += "⚠️ <i>Погода ухудшает ситуацию!</i>\n"
    text += "\n"

    for news in filtered_news[:10]:
        body_text = news.get("full_text", news["title"])
        if body_text: body_text = body_text[0].upper() + body_text[1:]
        street = extract_street(body_text)
        header_icon = "🚓 <b>Угон / Розыск:</b>" if news.get("is_theft") else "📝 <b>Событие:</b>"

        text += f"{header_icon}\n{body_text}\n\n"
        text += f"🏙 <b>Город:</b> {news['city']}\n"
        if news.get("district"):
            text += f"🏘 <b>Район:</b> {news['district']}\n"
        if street:
            text += f"📍 <b>Улица:</b> {street}\n"
        text += f"🕒 <b>Опубликовано:</b> {news['time']}\n🚦 <b>Статус:</b> {news['impact_text']}\n➖➖➖➖➖➖➖➖\n\n"

    return text

async def calculate_and_send_route(user_id, start_point, end_point, send_func):
    wait_msg = await send_func("⏳ Строю маршрут, проверяю камеры и погоду...")
    async with aiohttp.ClientSession() as session:
        start_coords, end_coords = await get_coords(session, start_point), await get_coords(session, end_point)
        if not start_coords or not end_coords: return await wait_msg.edit_text("❌ Адрес не найден. Попробуйте указать точнее.")
        route_data = await get_route_info(session, start_coords, end_coords)
        if not route_data: return await wait_msg.edit_text("❌ Ошибка маршрута.")
        dist_km, base_min, geom = route_data

    cameras_on_route = []
    for i in range(0, len(geom), 10):
        lon, lat = geom[i]
        for cam in CAMERAS:
            if haversine(lat, lon, cam["lat"], cam["lon"]) < 0.2:
                if cam["name"] not in cameras_on_route: cameras_on_route.append(cam["name"])

    camera_warning = ""
    if cameras_on_route:
        camera_warning = "\n📸 <b>Камеры на пути:</b>\n" + "\n".join(f"— {c}" for c in cameras_on_route[:5]) + "\n"

    weather_text, is_bad = await get_weather_ivanovo()
    traffic_text = await get_traffic_summary(user_id, is_bad, get_user_cities(user_id), mark_seen=False)
    time_penalty = 10 if "🚨" in traffic_text else 0
    total_min = int(base_min + time_penalty)
    hours, mins = total_min // 60, total_min % 60
    time_str = f"{hours} ч {mins} мин" if hours > 0 else f"{mins} мин"
    weather_short = "✅ Дороги сухие" if not is_bad else "⚠️ Дороги скользкие/мокрые"

    result_text = (
        f"🗺 <b>Маршрут:</b> {start_point.capitalize()} ➡️ {end_point.capitalize()}\n"
        f"📏 <b>Расстояние:</b> {dist_km:.1f} км\n⏱ <b>Время в пути:</b> {time_str}\n\n"
        f"🌤 <b>Условия:</b> {weather_short}\n"
        f"🚦 <b>Трафик:</b> {'Есть ДТП, возможны задержки (+10 мин)' if time_penalty else 'Свободно'}\n"
        f"{camera_warning}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💾 Сохранить маршрут", callback_data=f"save_route_{start_point[:10]}_{end_point[:10]}")]])
    await wait_msg.edit_text(result_text, parse_mode="HTML", reply_markup=keyboard)

async def broadcast_job():
    current_time = datetime.now(MSK_TZ).strftime("%H:%M")
    users = get_users_for_broadcast(current_time)
    if not users: return

    weather_text, is_bad = await get_weather_ivanovo()
    fuel_text = await get_fuel_prices(show_delta=True)
    fuel_news = await get_fuel_situation_news()
    weather_news = await get_weather_news()
    
    for user_id in users:
        user_cities = get_user_cities(user_id)
        full_text = (f"🌅 <b>Сводка на {datetime.now(MSK_TZ).strftime('%d.%m.%Y')}:</b>\n\n"
                     f"{weather_text}\n\n{await get_region_weather(user_cities)}\n\n{fuel_text}\n\n")
        if fuel_news: full_text += f"{fuel_news}\n"
        if weather_news: full_text += f"{weather_news}\n"
        full_text += f"{await get_traffic_summary(user_id, is_bad, user_cities)}"
        try: await bot.send_message(user_id, full_text, parse_mode="HTML")
        except Exception: pass

def get_cities_keyboard(user_cities: list):
    keyboard = [[InlineKeyboardButton(text="✅ Выбрать ВСЕ", callback_data="select_all"), InlineKeyboardButton(text="❌ Очистить", callback_data="clear_all")]]
    row = []
    for city in REGION_CITIES.keys():
        row.append(InlineKeyboardButton(text=f"✅ {city}" if city in user_cities else city, callback_data=f"toggle_{city}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="💾 Сохранить и выйти", callback_data="save_settings")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_routes_keyboard(user_id):
    routes = get_saved_routes(user_id)
    keyboard = [[InlineKeyboardButton(text=f"⭐ {name} ({start} ➡️ {end})", callback_data=f"run_route_{r_id}")] for r_id, name, start, end in routes]
    keyboard.append([InlineKeyboardButton(text="➕ Построить новый маршрут", callback_data="new_route")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- Хэндлеры ---

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    total, subs = get_bot_stats()
    await message.answer(f"📊 <b>Статистика:</b>\n\n👥 Всего юзеров: <b>{total}</b>\n⏰ Активных подписок: <b>{subs}</b>", parse_mode="HTML")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    save_user_cities(message.from_user.id, get_user_cities(message.from_user.id))
    intro_text = (
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        "Я — твой персональный помощник по <b>Ивановской области</b>.\n\n"
        "📌 <b>Что я умею:</b>\n"
        "🌤 <b>Погода</b> — прогноз, видимость, длительность осадков и состояние дорог.\n"
        "🌍 <b>Города</b> — погода в выбранных городах.\n"
        "🚦 <b>ДТП, Заторы, Угоны</b> — оперативные новости, точный район, балл пробок и угоны.\n"
        "⛽ <b>Бензин</b> — цены и новости.\n"
        "🗺 <b>Маршрут</b> — расчет времени, камеры и погодные условия.\n\n"
        "⚙️ <b>Настройки:</b> Выбери города и настрой до 3-х времен для рассылки.\n\n"
        "👇 <i>Используй кнопки меню внизу.</i>"
    )
    await message.answer(intro_text, reply_markup=get_main_menu(), parse_mode="HTML")

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

@dp.message(F.text == "🚦 ДТП, Заторы, Угоны")
@dp.message(Command("traffic"))
async def cmd_traffic(message: types.Message):
    uid = message.from_user.id
    await message.answer(await get_traffic_summary(uid, (await get_weather_ivanovo())[1], get_user_cities(uid)), parse_mode="HTML")

@dp.message(F.text == "🗂 Всё сразу")
@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    user_id = message.from_user.id
    weather_text, is_bad = await get_weather_ivanovo()
    fuel_news = await get_fuel_situation_news()
    weather_news = await get_weather_news()
    text = (f"{weather_text}\n\n{await get_region_weather(get_user_cities(user_id))}\n\n{await get_fuel_prices(True)}\n\n")
    if fuel_news: text += f"{fuel_news}\n"
    if weather_news: text += f"{weather_news}\n"
    text += f"{await get_traffic_summary(user_id, is_bad, get_user_cities(user_id))}"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "⚙️ Настройки")
@dp.message(Command("settings"))
async def cmd_settings(message: types.Message): 
    await message.answer("⚙️ <b>Настройка городов</b>\nВыберите города:", reply_markup=get_cities_keyboard(get_user_cities(message.from_user.id)), parse_mode="HTML")

@dp.message(F.text == "⏰ Время рассылки")
async def menu_broadcast_time(message: types.Message, state: FSMContext):
    times = get_user_times(message.from_user.id)
    text = f"⏰ Ваши времена: {', '.join(times)} (МСК).\n\n" if times else "⏰ У вас пока не настроена рассылка.\n\n"
    text += ("Напишите время в формате <code>ЧЧ:ММ</code> (например, <code>07:30</code>).\nДоступно до <b>3-х</b> слотов.\n\nЧтобы <b>удалить все</b>, напишите <code>0</code>.")
    await state.set_state(BroadcastState.waiting_for_time)
    await message.answer(text, parse_mode="HTML")

@dp.message(BroadcastState.waiting_for_time)
async def process_broadcast_time(message: types.Message, state: FSMContext):
    text = message.text.strip().lower()
    user_id = message.from_user.id
    if text in ["0", "отмена", "стоп"]:
        save_user_times(user_id, [])
        await state.clear()
        return await message.answer("🔕 Все рассылки отключены.")
    if re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', text):
        if len(text) == 4: text = "0" + text
        times = get_user_times(user_id)
        if len(times) >= 3: return await message.answer("❌ Уже 3 времени. Напишите <code>0</code>, чтобы очистить.", parse_mode="HTML")
        if text in times: return await message.answer("⚠️ Это время уже добавлено.")
        times.append(text); times.sort()
        save_user_times(user_id, times)
        await state.clear()
        await message.answer(f"✅ Время <b>{text}</b> добавлено!\nРассылки: {', '.join(times)}.", parse_mode="HTML")
    else: await message.answer("❌ Неверный формат. Напишите <code>ЧЧ:ММ</code> или <code>0</code>.", parse_mode="HTML")

@dp.message(F.text == "🗺 Маршрут")
async def menu_route(message: types.Message, state: FSMContext):
    await state.clear()
    routes = get_saved_routes(message.from_user.id)
    text = "🗺 <b>Управление маршрутами</b>\n\n"
    if routes: text += "Выберите сохраненный маршрут или постройте новый:"
    else: text += "У вас пока нет сохраненных маршрутов. Постройте первый!"
    await message.answer(text, reply_markup=get_routes_keyboard(message.from_user.id), parse_mode="HTML")

@dp.callback_query(F.data == "new_route")
async def start_new_route(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🗺 <b>Откуда выезжаете?</b>\nНапишите адрес или город.", parse_mode="HTML")
    await state.set_state(RouteState.waiting_for_start)

@dp.message(RouteState.waiting_for_start)
async def process_route_start(message: types.Message, state: FSMContext):
    await state.update_data(start_point=message.text)
    await state.set_state(RouteState.waiting_for_end)
    await message.answer("🏁 <b>Куда едем?</b>\nНапишите конечный адрес или город.")

@dp.message(RouteState.waiting_for_end)
async def process_route_end(message: types.Message, state: FSMContext):
    end_point, data = message.text, await state.get_data()
    start_point = data.get("start_point")
    await state.update_data(end_point=end_point)
    await calculate_and_send_route(message.from_user.id, start_point, end_point, message.answer)

@dp.callback_query(F.data.startswith("run_route_"))
async def run_saved_route(callback: CallbackQuery):
    route_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT start_point, end_point FROM saved_routes WHERE id = ?", (route_id,))
    row = cursor.fetchone(); conn.close()
    if row: await calculate_and_send_route(callback.from_user.id, row[0], row[1], callback.message.edit_text)
    else: await callback.answer("Маршрут не найден", show_alert=True)

@dp.callback_query(F.data.startswith("save_route_"))
async def ask_route_name(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("💾 <b>Как назовем этот маршрут?</b>\nНапример: «Работа», «Домой», «Дача».")
    await state.set_state(RouteState.waiting_for_route_name)
    await callback.answer()

@dp.message(RouteState.waiting_for_route_name)
async def save_route_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    data = await state.get_data()
    start = data.get("start_point"); end = data.get("end_point")
    if start and end:
        save_new_route(message.from_user.id, name, start, end)
        await message.answer(f"✅ Маршрут <b>«{name}»</b> сохранен!")
    else: await message.answer("⚠️ Не удалось сохранить маршрут. Попробуйте построить заново.")
    await state.clear()

@dp.callback_query(F.data == "select_all")
async def process_select_all(callback: CallbackQuery):
    all_cities = list(REGION_CITIES.keys())
    save_user_cities(callback.from_user.id, all_cities)
    await callback.message.edit_reply_markup(reply_markup=get_cities_keyboard(all_cities))
    await callback.answer("Выбраны все города")

@dp.callback_query(F.data == "clear_all")
async def process_clear_all(callback: CallbackQuery):
    save_user_cities(callback.from_user.id, [])
    await callback.message.edit_reply_markup(reply_markup=get_cities_keyboard([]))
    await callback.answer("Список городов очищен")

@dp.callback_query(F.data.startswith("toggle_"))
async def process_toggle_city(callback: CallbackQuery):
    city = callback.data.split("_")[1]
    user_cities = get_user_cities(callback.from_user.id)
    if city in user_cities: user_cities.remove(city)
    else: user_cities.append(city)
    save_user_cities(callback.from_user.id, user_cities)
    await callback.message.edit_reply_markup(reply_markup=get_cities_keyboard(user_cities))
    await callback.answer()

@dp.callback_query(F.data == "save_settings")
async def process_save_settings(callback: CallbackQuery):
    await callback.message.edit_text("✅ Настройки городов сохранены!")
    await callback.answer()

async def main():
    scheduler = AsyncIOScheduler(timezone=MSK_TZ)
    scheduler.add_job(broadcast_job, trigger='cron', minute='*')
    scheduler.start()
    print("Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

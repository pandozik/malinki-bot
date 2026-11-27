import os
import asyncio
import aiohttp
import re
import sqlite3
import json
import hashlib
import math
import time
import html
import difflib
from urllib.parse import urlparse
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
import warnings

try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 5007806242
CITY_NAME = "Иваново"
MSK_TZ = pytz.timezone("Europe/Moscow")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
def tmo(seconds=10): return aiohttp.ClientTimeout(total=seconds)

# =====================================================================
#                              БАЗА ДАННЫХ
# =====================================================================
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
    return [row[0] for row in rows if row[1] and current_time in json.loads(row[1])]

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

def slug(text: str) -> str:
    """Безопасный кусок callback_data: без пробелов, до 24 символов."""
    return re.sub(r'\s+', '_', text.strip())[:24]

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

# =====================================================================
#                              СПРАВОЧНИКИ
# =====================================================================
REGION_CITIES = {
    "Иваново": (56.9972, 40.9714), "Шуя": (56.8487, 41.3883), "Кинешма": (57.4391, 42.1289),
    "Вичуга": (57.2128, 41.9301), "Тейково": (56.858, 40.5369), "Фурманов": (57.2536, 41.1085),
    "Кохма": (56.9349, 41.0915), "Родники": (57.1051, 41.7305), "Приволжск": (57.387, 41.2867),
    "Заволжск": (57.4823, 42.1378), "Наволоки": (57.4657, 41.9634), "Юрьевец": (57.3201, 43.1041),
    "Южа": (56.5831, 42.0122), "Гаврилов Посад": (56.5585, 40.1217), "Комсомольск": (57.0292, 40.3787),
    "Плес": (57.4606, 41.5158), "Пучеж": (56.9833, 43.1667),
}

CAMERAS = [
    {"name": "Перекресток Ленина / Багаева", "lat": 56.9986, "lon": 40.9742}, {"name": "Проспект Строителей / Шубиных", "lat": 57.0021, "lon": 40.9613},
    {"name": "Лежневская / Громобоя", "lat": 56.9789, "lon": 40.9712}, {"name": "Уводьский мост", "lat": 57.0011, "lon": 40.9841},
    {"name": "Шереметевский проспект", "lat": 57.0015, "lon": 40.9758}, {"name": "Улица Куконковых", "lat": 56.9923, "lon": 40.9582},
    {"name": "Московское шоссе (въезд)", "lat": 56.9321, "lon": 40.9210}, {"name": "Кохомское шоссе", "lat": 56.9448, "lon": 41.0112},
    {"name": "Тейковское шоссе", "lat": 56.9811, "lon": 40.9105}, {"name": "Улица Некрасова", "lat": 56.9995, "lon": 40.9851}
]

CITY_PATTERNS = {
    "Иваново": r'\b(?:в|из|по|до|под|г\.|город[еау]?)\s+иванов(?:о|е|а|у|ым)\b|\bиванов(?:о|е)\b(?!\s+(?:район|област))',
    "Шуя": r'\b(?:в|из|по|до|под|г\.|город[еау]?)\s+шу(?:я|е|ю|и|ей)\b|\bшу(?:я|е|ю)\b',
    "Кинешма": r'\bкинешм(?:а|е|у|ы|ой)\b', "Вичуга": r'\bвичуг(?:а|е|у|и|ой)\b',
    "Тейково": r'\bтейков(?:о|е|а|у)\b(?!\s+район)', "Фурманов": r'\bфурманов(?:е|а|у|ом)?\b(?!\s+район)',
    "Кохма": r'\bкохм(?:а|е|у|ы|ой)\b', "Родники": r'\bродник(?:и|ах|ов|ам)\b',
    "Приволжск": r'\bприволжск(?:е|а|у|ом)?\b(?!\s+район)', "Заволжск": r'\bзаволжск(?:е|а|у|ом)?\b(?!\s+район)',
    "Наволоки": r'\bнаволок(?:и|ах|ов|ам)\b', "Юрьевец": r'\bюрьев(?:ец|це|ца|цу)\b',
    "Южа": r'\bюж(?:а|е|у|ей)\b', "Гаврилов Посад": r'\bгаврилов[- ]посад(?:е|а|у|ом)?\b',
    "Комсомольск": r'\bкомсомольск(?:е|а|у|ом)?\b(?!\s+район)', "Плес": r'\bпл[её]с(?:е|а|у|ом)?\b',
    "Пучеж": r'\bпучеж(?:е|а|у|ом)?\b(?!\s+район)',
}

TOWNS = {
    "Палех": (r'\bпалех\w{0,2}\b', "Палехский район"), "Пестяки": (r'\bпестяк(?:и|ах|ов|ам)\b', "Пестяковский район"),
    "Лух": (r'\bлух(?:а|е|у|ом)?\b', "Лухский район"), "Лежнево": (r'\bлежнев(?:о|е)\b', "Лежневский район"),
    "Савино": (r'\bсавин(?:о|е)\b', "Савинский район"), "Верхний Ландех": (r'\bверхн(?:ий|ем|его)\s+ландех\w{0,2}\b', "Верхнеландеховский район"),
    "Ильинское-Хованское": (r'\bильинск(?:ое|ом|ого)[- ]хованск\w+', "Ильинский район"),
}

# Расширенные паттерны для поиска ДТП
THEFT_PATTERN = r'\b(угон\w*|угнал\w*|похищ\w*\s+(?:авто|машин|транспорт|ваз|лада|иномарк)|похитил\w*\s+(?:авто|машин|транспорт)|розыск\w*\s+(?:авто|машин|транспорт)|краж\w*\s+(?:авто|машин))\b'
PEDESTRIAN_PATTERN = r'\b(сбил\w*\s+(?:пешеход|человек|мужчин|женщин|ребенк|девочк|мальчик|пенсионер|подростк)|наезд\w*\s+на\s+пешеход|пешеход\w*\s+(?:попал|оказался)\s+под\s+колес)\b'
ROADWORK_PATTERN = r'\b(перекрыт\w*|перекро\w*|перекрыва\w*|ограничен\w*\s+движен|ремонт\w*\s+дорог|закрыт\w*\s+движен|объезд\w*|светофор\w*\s+не\s+работа|отключ\w*\s+светофор|ямочн\w*\s+ремонт|реверсивн\w*\s+движен)\b'
TRAFFIC_PATTERN = r'\b(дтп|авария|аварии|аварий|пробка|пробки|затор|столкновение|пострадал|скопление|затруднено|вылет|опрокидывание|перевертыш|лобовое|врезался|протаранил|снесло|фура|эвакуатор|парализовало|наезд|сбил)\b'
WEATHER_ROAD_PATTERN = r'\b(гололед\w*|гололёд\w*|скользк\w+\s+дорог\w*|снежн\w+\s+накат\w*|наледь\w*)\b'

STOP_WORDS = ["украин", "москв", "петербург", "росси", "сша", "европ", "пробаци", "проблем", "политика", "спецоперац"]

RSS_SOURCES = [
    "https://newsivanovo.ru/rss", "https://ivanovonews.ru/rss/", "https://mkivanovo.ru/rss/",
    "https://iv-news.ru/feed/", "https://37.ru/rss/", "https://cursiv.ru/rss/",
    "https://kineshemec.ru/rss/", "https://mspros.ru/rss/",
]
# Расширенный список TG каналов
TG_CHANNELS = [
    "ivanovonewsBars", "ivanovo_proisshestvia", "chp_ivanovo", "dtp_ivanovo",
    "typical_kineshma", "kineshemec_ru", "shuya_news", "vichuga_news",
    "gibdd37", "ivoblast", "ivanovo_news", "ivanovo_tipichny", "ivanovo_life", 
    "ivanovo_chp", "dtp37", "avtopartner_ivanovo"
]
# Эти каналы считаются локальными, бот не требует явного упоминания "Иваново"
LOCAL_TG = ["ivanovonewsBars", "ivanovo_proisshestvia", "chp_ivanovo", "dtp_ivanovo", "gibdd37", "ivoblast", "ivanovo_news", "ivanovo_life", "ivanovo_chp", "dtp37", "avtopartner_ivanovo"]

VK_GROUPS = [
    "https://m.vk.ru/avtopartner_ivanovo", "https://m.vk.ru/chp_ivanovo",
    "https://m.vk.ru/dtp_ivanovo", "https://m.vk.ru/ivanovonews",
    "https://m.vk.ru/kineshma_chp", "https://m.vk.ru/shuya_chp"
]

# =====================================================================
#                         ВСПОМОГАТЕЛЬНОЕ
# =====================================================================
def split_message(text: str, limit: int = 4000) -> list:
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            if cur: chunks.append(cur); cur = ""
            chunks.append(para[:limit]); para = para[limit:]
        add = para if not cur else cur + "\n\n" + para
        if len(add) > limit:
            chunks.append(cur); cur = para
        else:
            cur = add
    if cur: chunks.append(cur)
    return chunks or [""]

async def answer_long(send_func, text: str):
    for chunk in split_message(text):
        await send_func(chunk, parse_mode="HTML", disable_web_page_preview=True)

async def http_get(session, url, timeout=10):
    async with session.get(url, headers=UA, timeout=tmo(timeout)) as resp:
        if resp.status != 200: raise RuntimeError(f"HTTP {resp.status}")
        return await resp.text()

def clean_html(raw_html):
    if not raw_html: return ""
    return re.sub(r'\s+', ' ', BeautifulSoup(raw_html, "html.parser").get_text(separator=' ')).strip()

def clean_telegram_text(text):
    if not text: return ""
    for phrase in [r"Иллюстрация создана ИИ", r"Фото:.*", r"Подписывайтесь.*", r"Проголосовать за канал.*", r"Прислать новость.*", r"Предложить новость.*", r"Источник:.*"]:
        text = re.sub(phrase, "", text, flags=re.IGNORECASE)
    return re.sub(r'\n{3,}', '\n\n', text).strip()

def format_time(dt=None):
    if dt:
        dt = dt.astimezone(MSK_TZ)
        if dt.date() == datetime.now(MSK_TZ).date(): return f"Сегодня в {dt.strftime('%H:%M')}"
        if (datetime.now(MSK_TZ).date() - dt.date()).days == 1: return f"Вчера в {dt.strftime('%H:%M')}"
        return dt.strftime("%d.%m в %H:%M")
    return "Недавно"

# =====================================================================
#                               ПОГОДА
# =====================================================================
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

def _num(x, default=0.0): return default if x is None else x

async def get_weather_ivanovo():
    lat, lon = REGION_CITIES["Иваново"]
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,apparent_temperature,precipitation_probability,weather_code,relative_humidity_2m,precipitation&hourly=precipitation,snowfall,temperature_2m,visibility&past_hours=24&forecast_hours=12&timezone=Europe/Moscow"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=tmo(10)) as resp:
                if resp.status != 200: return "⚠️ Ошибка при запросе погоды.", False
                data = await resp.json()
        except Exception: return "⚠️ Ошибка сети при запросе погоды.", False

    current = data.get("current", {})
    hourly = data.get("hourly", {})
    temp = _num(current.get("temperature_2m"))
    feels = _num(current.get("apparent_temperature"))
    code = current.get("weather_code", 0)
    prob = _num(current.get("precipitation_probability"))
    precip_now = _num(current.get("precipitation"))

    precip_list = hourly.get("precipitation", [])
    duration_text = ""  # ИСПРАВЛЕНИЕ 1: создаётся всегда, а не только внутри if precip_now > 0
    if precip_now > 0:
        hours_left = 0
        for i in range(24, len(precip_list)):
            if _num(precip_list[i]) > 0.05: hours_left += 1
            else: break
        if hours_left > 0: duration_text = f"⏳ Осадки еще ~{hours_left} ч.\n"

    emoji = get_weather_emoji(code)
    desc = get_wmo_description(code)
    
    road = "✅ Дороги сухие"
    is_bad = False
    if code in [71, 73, 75, 77, 85, 86]: road = "❄️ Снег, скользко"; is_bad = True
    elif code in [61, 63, 65, 80, 81, 82]: 
        if temp <= 2: road = "🧊 Гололедица!"; is_bad = True
        else: road = "🌧 Мокрый асфальт"; is_bad = True
    elif temp < -5: road = "🧊 Наледь"; is_bad = True

    text = (f"{emoji} <b>Погода в Иваново:</b>\n🌡 Температура: <b>{temp:+.1f}°C</b> (ощущается {feels:+.1f}°C)\n☁️ {desc}\n💧 Вероятность осадков: {prob:.0f}%\n{duration_text}🛣 <b>Дорога:</b> {road}")
    return text, is_bad

async def get_region_weather(user_cities: list):
    selected_cities = {city: REGION_CITIES[city] for city in user_cities if city in REGION_CITIES}
    if not selected_cities: return "⚠️ Вы не выбрали ни одного города."
    lats = ",".join(str(lat) for lat, lon in selected_cities.values())
    lons = ",".join(str(lon) for lat, lon in selected_cities.values())
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lons}&current=temperature_2m,precipitation_probability,weather_code&timezone=Europe/Moscow"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=tmo(10)) as resp:
                data = await resp.json()
                if isinstance(data, dict): data = [data]
                lines = []
                for i, city in enumerate(selected_cities.keys()):
                    if i < len(data):
                        # ИСПРАВЛЕНИЕ 2: current может быть массивом (2+ города) или объектом (1 город)
                        cur = data[i].get("current")
                        if isinstance(cur, list):
                            cur = cur[i] if i < len(cur) else {}
                        cur = cur or {}
                        lines.append(f"{get_weather_emoji(cur.get('weather_code', 0))} <b>{city}</b>: {cur.get('temperature_2m', 0):+.1f}°C, 💧{cur.get('precipitation_probability', 0)}%")
                return "🌍 <b>Погода в выбранных городах:</b>\n" + "\n".join(lines)
        except Exception: return "⚠️ Ошибка при запросе к сервису."

# =====================================================================
#                               ТОПЛИВО
# =====================================================================
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
            async with session.get(url, headers=headers, timeout=tmo(8)) as resp:
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
                else:
                    if "АИ-92" in prices: text += f"🔹 <b>АИ-92:</b> {prices['АИ-92']:.2f} ₽/л\n"
                    if "АИ-95" in prices: text += f"🔹 <b>АИ-95:</b> {prices['АИ-95']:.2f} ₽/л\n"
                    if "ДТ" in prices: text += f"🔹 <b>ДТ:</b> {prices['ДТ']:.2f} ₽/л\n"

                # ИСПРАВЛЕНИЕ 3: история пишется в ОБЕИХ ветках (раньше — только при show_delta=True)
                save_today_fuel(prices.get("АИ-92"), prices.get("АИ-95"), prices.get("ДТ"))
                return text
    except Exception: return "⚠️ Ошибка сети при запросе цен."

# =====================================================================
#                       ДАТЫ И СВЕЖЕСТЬ НОВОСТЕЙ
# =====================================================================
def parse_any_date(s):
    if not s: return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=pytz.utc)
        return dt
    except Exception: pass
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=pytz.utc)
        return dt
    except Exception: return None

def is_fresh(dt) -> bool:
    if dt is None: return True 
    today = datetime.now(MSK_TZ).date()
    return (today - dt.astimezone(MSK_TZ).date()).days in (0, 1, 2)

def vk_post_is_fresh(text_div) -> bool:
    node = text_div
    for _ in range(6):
        node = node.parent
        if node is None: return True
        d = node.find(class_="rel_date") or node.find(class_="wi_date")
        if d:
            return any(w in d.get_text(" ", strip=True).lower() for w in ("сегодня", "вчера", "назад", "только что"))
    return True

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
DATE_RE = re.compile(r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\b')

def event_date_ok(text: str) -> bool:
    today = datetime.now(MSK_TZ).date(); dates = []
    for d, mon in DATE_RE.findall(text.lower()):
        try: dt = datetime(today.year, MONTHS[mon], int(d)).date()
        except ValueError: continue
        if (dt - today).days > 7: dt = dt.replace(year=today.year - 1)
        dates.append(dt)
    return True if not dates else (today - max(dates)).days <= 2

# =====================================================================
#                    АНАЛИЗ ТЕКСТА И ЛОКАЦИИ
# =====================================================================
def extract_street(text):
    patterns = [
        r'(?:ул(?:ица|\.)?|проспект|пр-т|переулок|пер\.|шоссе|пл(?:ощадь|\.)?|мкр(?:орайон|\.)?)\s+([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)',
        r'на\s+(?:улице\s+)?([А-ЯЁ][а-яё]+(?:ой|ий|ая|ое|ые))',
        r'\b([А-ЯЁ][а-яё]+ская)\b',
        r'(?:перекрёст(?:ок|ке)|развязк[ае])\s+(?:улиц\s+)?([А-ЯЁ][а-яё]+)',
        r'(?:мост[ау]?|путепровод[ау]?)\s+(?:на\s+)?([А-ЯЁ][а-яё]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            street = match.group(1)
            if street.lower() not in ["ивановская", "областная", "кинешемская", "шуйская", "городская", "районная"]:
                return street
    return None

def extract_location(text):
    lower_text = text.lower()
    city = district = settlement = None
    filter_city = "Ивановская область"

    for c_name, pattern in CITY_PATTERNS.items():
        if re.search(pattern, lower_text):
            city = c_name; filter_city = c_name
            break

    if not city:
        for t_name, (pattern, t_district) in TOWNS.items():
            if re.search(pattern, lower_text):
                settlement = t_name; district = t_district; filter_city = district
                break

    return {"city": city, "district": district, "settlement": settlement, "filter_city": filter_city}

def location_lines(n) -> str:
    city, district, settlement = n.get("city"), n.get("district"), n.get("settlement")
    if city and district: return f"🏙 <b>Город:</b> {city}, {district}\n"
    if city: return f"🏙 <b>Город:</b> {city}\n"
    if district or settlement:
        parts = [p for p in (district, settlement) if p]
        return f"🗺 <b>Район:</b> {', '.join(parts)} (Ивановская область)\n"
    return "🗺 <b>Локация:</b> Ивановская область\n"

def classify_event_and_impact(text, city=None):
    lower = text.lower()
    
    if re.search(THEFT_PATTERN, lower): return ("🚓 <b>Угон / Розыск:</b>", "🚓 <b>Внимание:</b> Угон / Розыск авто.", False)
    if re.search(PEDESTRIAN_PATTERN, lower): return ("🚶‍♂️ <b>Наезд на пешехода:</b>", "🔴 <b>Критично!</b> Наезд на пешехода.", True)
    if re.search(ROADWORK_PATTERN, lower): return ("🚧 <b>Перекрытие:</b>", "🟡 <b>Внимание:</b> Дорожные работы.", True)

    high_words = ["перекрыт", "смертельн", "погиб", "фура", "грузовик", "трасса", "тройное", "массов", "вылет", "коллапс", "пострадал", "пьян", "встречк", "сбил", "скорая"]
    if any(w in lower for w in high_words): return "💥 <b>Серьезное ДТП:</b>", "🔴 <b>Высокое.</b> Возможна пробка.", True

    if any(w in lower for w in ["дтп", "авари", "столкн", "затор", "пробка", "скопление", "наезд"]):
        return "🚗 <b>ДТП / Затор:</b>", "🟠 <b>Среднее.</b> Замедление.", False

    return "📝 <b>Дорожная информация:</b>", "🟢 <b>Низкое.</b> Примите к сведению.", False

def get_news_core(text):
    """Создает уникальный отпечаток события (город + улица + тип события)"""
    lower = text.lower()
    loc = extract_location(lower)
    street = extract_street(lower)
    header = classify_event_and_impact(lower)[0]
    
    core = []
    if loc["city"]: core.append(loc["city"])
    if loc["district"]: core.append(loc["district"])
    if street: core.append(street)
    core.append(header)
    
    # Если не удалось вытащить улицу/город, берем первые 5 значимых слов
    if len(core) < 2:
        words = re.findall(r'\b[а-яА-ЯёЁ]{5,}\b', lower)
        core.extend(words[:5])
        
    return generate_hash("_".join(core))

# =====================================================================
#                     ПАРСИНГ НОВОСТЕЙ
# =====================================================================
def is_auto_related(lower_text: str) -> bool:
    return bool(re.search(TRAFFIC_PATTERN, lower_text) or re.search(THEFT_PATTERN, lower_text) or 
                re.search(PEDESTRIAN_PATTERN, lower_text) or re.search(ROADWORK_PATTERN, lower_text) or 
                re.search(WEATHER_ROAD_PATTERN, lower_text))

def is_region_related(lower_text: str) -> bool:
    return ("иванов" in lower_text or "област" in lower_text or 
            any(c.lower() in lower_text for c in REGION_CITIES) or 
            any(re.search(p, lower_text) for p, _ in TOWNS.values()))

async def fetch_rss(session, url):
    soup = BeautifulSoup(await http_get(session, url, 10), "html.parser")
    items = []
    for item in soup.find_all(["item", "entry"]):
        title_tag = item.find("title")
        if not title_tag: continue
        title = title_tag.get_text(strip=True)
        desc_tag = item.find("description") or item.find("summary") or item.find("content")
        desc = clean_html(desc_tag.get_text(strip=True)) if desc_tag else ""
        date_tag = item.find("pubdate") or item.find("published") or item.find("updated")
        dt = parse_any_date(date_tag.get_text(strip=True)) if date_tag else None
        text = f"{title}. {desc}" if desc and desc.lower() not in title.lower() else title
        
        if is_fresh(dt) and event_date_ok(text):
            lower = text.lower()
            if is_auto_related(lower) and is_region_related(lower) and not any(sw in lower for sw in STOP_WORDS):
                loc = extract_location(text)
                items.append({"full_text": text, "dt": dt, "time": format_time(dt), "source": "RSS", **loc})
    return items

async def fetch_tg(session, channel):
    soup = BeautifulSoup(await http_get(session, f"https://t.me/s/{channel}", 12), "html.parser")
    items = []
    for wrap in soup.find_all("div", class_="tgme_widget_message_wrap"):
        divs = wrap.find_all("div", class_="tgme_widget_message_text")
        if not divs: continue
        dt = None
        time_tag = wrap.find("time")
        if time_tag and time_tag.get("datetime"):
            try: dt = datetime.fromisoformat(time_tag["datetime"])
            except ValueError: pass
        
        if not is_fresh(dt): continue
        
        text = clean_telegram_text("\n".join(d.get_text(separator="\n", strip=True) for d in divs))
        if event_date_ok(text):
            lower = text.lower()
            local = channel in LOCAL_TG
            if is_auto_related(lower) and (local or is_region_related(lower)) and not any(sw in lower for sw in STOP_WORDS):
                loc = extract_location(text)
                items.append({"full_text": text, "dt": dt, "time": format_time(dt), "source": f"t.me/{channel}", **loc})
    return items

async def fetch_vk(session, group_url):
    soup = BeautifulSoup(await http_get(session, group_url, 12), "html.parser")
    items = []
    for post in soup.find_all("div", class_="wall_post_text"):
        if not vk_post_is_fresh(post): continue
        text = clean_telegram_text(post.get_text(separator="\n", strip=True))
        if event_date_ok(text):
            lower = text.lower()
            if is_auto_related(lower) and is_region_related(lower) and not any(sw in lower for sw in STOP_WORDS):
                loc = extract_location(text)
                items.append({"full_text": text, "dt": None, "time": "Сегодня/вчера", "source": "VK", **loc})
    return items

# =====================================================================
#                           СВОДКА ПО ДОРОГАМ
# =====================================================================
def is_duplicate_text(text1, text2, threshold=0.65):
    """Умная проверка на дубли с использованием SequenceMatcher"""
    t1 = re.sub(r'[^\w\s]', '', text1.lower())
    t2 = re.sub(r'[^\w\s]', '', text2.lower())
    
    # Быстрая проверка на точное совпадение или полное вхождение
    if t1 in t2 or t2 in t1: return True
    
    # Сравнение схожести текста
    seq = difflib.SequenceMatcher(None, t1, t2)
    return seq.ratio() > threshold

async def get_traffic_summary(user_id, user_cities, mark_seen=True):
    cleanup_old_news()
    all_news = []
    
    async with aiohttp.ClientSession() as session:
        tasks = []
        for url in RSS_SOURCES: tasks.append(fetch_rss(session, url))
        for ch in TG_CHANNELS: tasks.append(fetch_tg(session, ch))
        for grp in VK_GROUPS: tasks.append(fetch_vk(session, grp))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, list): all_news.extend(res)

    if not all_news: 
        return "✅ <b>Свежих сообщений о ДТП нет (за сегодня и вчера).</b>"
        
    # 1. Дедупликация внутри пачки (Fuzzy)
    unique_news = []
    for news in all_news:
        is_dup = False
        for ex in unique_news:
            if is_duplicate_text(news["full_text"], ex["full_text"], threshold=0.65):
                is_dup = True
                break
        if not is_dup: unique_news.append(news)
            
    filtered_news = []
    jam_streets = set()
    
    for news in unique_news:
        header, impact_text, is_high = classify_event_and_impact(news["full_text"], city=news["city"])
        news["header"] = header
        news["impact_text"] = impact_text
        
        f_city = news["filter_city"]
        show = False
        
        if is_high: show = True
        elif f_city == "Ивановская область": show = True
        elif f_city in user_cities: show = True
        elif "Иваново" in user_cities and f_city == "Ивановская область": show = True
            
        if show:
            # Используем "ядро" новости для проверки в БД
            news_core_hash = get_news_core(news["full_text"])
            
            if not is_news_seen(user_id, news_core_hash):
                filtered_news.append(news)
                if is_high or "затор" in news["full_text"].lower() or "пробка" in news["full_text"].lower():
                    street = extract_street(news["full_text"])
                    if street: jam_streets.add(street)
                if mark_seen: mark_news_as_seen(user_id, news_core_hash)
            
    if not filtered_news: return "✅ <b>Свежих сообщений о ДТП в выбранных городах нет (все прочитаны).</b>"
    
    text = "🚨 <b>Дорожная обстановка:</b>\n"
    if jam_streets:
        text += f"📊 Улицы с затруднениями: <b>{', '.join(list(jam_streets)[:5])}</b>\n\n"
    else:
        text += f"📊 Уровень пробок: <b>Свободно</b>\n\n"
    
    for news in filtered_news[:15]: # Увеличили до 15 новостей
        body_text = news["full_text"]
        if body_text: body_text = body_text[0].upper() + body_text[1:]
        street = extract_street(body_text)
        
        text += f"{news['header']}\n{body_text}\n\n"
        text += location_lines(news)
        if street: text += f"📍 <b>Улица:</b> {street}\n"
        text += f"🕒 <b>Время:</b> {news['time']}\n"
        text += f"🚦 <b>Влияние:</b> {news['impact_text']}\n"
        text += "➖➖➖➖➖➖➖➖\n\n"
    return text

# =====================================================================
#                           МАРШРУТЫ
# =====================================================================
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

    _, is_bad = await get_weather_ivanovo()
    traffic_text = await get_traffic_summary(user_id, get_user_cities(user_id), mark_seen=False)
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
    # ИСПРАВЛЕНИЕ 4: слаг вместо сырых адресов в callback_data (пробелы/запятые ломали кнопку)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💾 Сохранить маршрут", callback_data=f"save_route_{slug(start_point)}_{slug(end_point)}")]])
    await wait_msg.edit_text(result_text, parse_mode="HTML", reply_markup=keyboard)

# =====================================================================
#                           УТРЕННЯЯ РАССЫЛКА
# =====================================================================
async def broadcast_job():
    current_time = datetime.now(MSK_TZ).strftime("%H:%M")
    users = get_users_for_broadcast(current_time)
    if not users: return

    weather_text, is_bad = await get_weather_ivanovo()
    fuel_text = await get_fuel_prices(show_delta=True)
    
    for user_id in users:
        user_cities = get_user_cities(user_id)
        traffic_text = await get_traffic_summary(user_id, user_cities)
        
        full_text = (f"🌅 <b>Сводка на {datetime.now(MSK_TZ).strftime('%d.%m.%Y')}:</b>\n\n"
                     f"{weather_text}\n\n{await get_region_weather(user_cities)}\n\n{fuel_text}\n\n{traffic_text}")
        try: 
            for chunk in split_message(full_text):
                await bot.send_message(user_id, chunk, parse_mode="HTML", disable_web_page_preview=True)
        except Exception: 
            pass

# =====================================================================
#                           КЛАВИАТУРЫ И ХЭНДЛЕРЫ
# =====================================================================
class RouteState(StatesGroup):
    waiting_for_start = State(); waiting_for_end = State(); waiting_for_route_name = State()
class BroadcastState(StatesGroup):
    waiting_for_time = State()

def get_main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🌤 Погода"), KeyboardButton(text="🌍 Города")],
        [KeyboardButton(text="🚦 ДТП и Заторы"), KeyboardButton(text="⛽ Бензин")],
        [KeyboardButton(text="🗺 Маршрут"), KeyboardButton(text="🗂 Всё сразу")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="⏰ Время рассылки")]
    ], resize_keyboard=True)

def get_cities_keyboard(user_cities: list):
    keyboard = [[InlineKeyboardButton(text="✅ Выбрать ВСЕ", callback_data="select_all"), InlineKeyboardButton(text="❌ Очистить", callback_data="clear_all")]]
    row = []
    for city in REGION_CITIES.keys():
        row.append(InlineKeyboardButton(text=f"✅ {city}" if city in user_cities else city, callback_data=f"toggle_{slug(city)}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="💾 Сохранить и выйти", callback_data="save_settings")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_routes_keyboard(user_id):
    routes = get_saved_routes(user_id)
    keyboard = [[InlineKeyboardButton(text=f"⭐ {name} ({start} ➡️ {end})", callback_data=f"run_route_{r_id}")] for r_id, name, start, end in routes]
    keyboard.append([InlineKeyboardButton(text="➕ Построить новый маршрут", callback_data="new_route")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

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
        "🌤 <b>Погода</b> — прогноз, видимость, длительность осадков.\n"
        "🌍 <b>Города</b> — погода в выбранных городах.\n"
        "🚦 <b>ДТП и Заторы</b> — оперативные новости без повторов.\n"
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
    await message.answer(await get_fuel_prices(False), parse_mode="HTML")

@dp.message(F.text == "🚦 ДТП и Заторы")
@dp.message(Command("traffic"))
async def cmd_traffic(message: types.Message):
    uid = message.from_user.id
    await message.answer(await get_traffic_summary(uid, get_user_cities(uid)), parse_mode="HTML")

@dp.message(F.text == "🗂 Всё сразу")
@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    user_id = message.from_user.id
    weather_text, is_bad = await get_weather_ivanovo()
    text = (f"{weather_text}\n\n{await get_region_weather(get_user_cities(user_id))}\n\n{await get_fuel_prices(True)}\n\n")
    text += f"{await get_traffic_summary(user_id, get_user_cities(user_id))}"
    await answer_long(message.answer, text)

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
    # ИСПРАВЛЕНИЕ 4: город целиком после префикса, а не split("_")[1] (иначе «Посад» терялся)
    city = callback.data[7:].replace("_", " ")
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

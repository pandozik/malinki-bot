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
import logging
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
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)  # RSS читаем html-парсером, предупреждение не нужно
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

def tmo(seconds=10):
    return aiohttp.ClientTimeout(total=seconds)

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
    # Источники, добавленные администратором командами /addtg /addvk /addrss
    cursor.execute("CREATE TABLE IF NOT EXISTS extra_sources (kind TEXT, value TEXT, PRIMARY KEY (kind, value))")
    # Чаты, которым администратор разрешил присылать сообщения боту (/addchat), и их сообщения
    cursor.execute("CREATE TABLE IF NOT EXISTS approved_chats (chat_id INTEGER PRIMARY KEY, title TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS chat_events (hash TEXT PRIMARY KEY, text TEXT, ts REAL, chat_title TEXT)")
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
    cursor.execute("DELETE FROM chat_events WHERE ts < ?", (time.time() - 7 * 86400,))
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

# --- Дополнительные источники и чаты ---
def get_extra_sources() -> list:
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT kind, value FROM extra_sources")
    rows = cursor.fetchall(); conn.close()
    return rows

def add_extra_source(kind: str, value: str) -> bool:
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO extra_sources (kind, value) VALUES (?, ?)", (kind, value))
    added = cursor.rowcount > 0
    conn.commit(); conn.close()
    return added

def del_extra_source(value: str) -> int:
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("DELETE FROM extra_sources WHERE value = ?", (value,))
    n = cursor.rowcount
    conn.commit(); conn.close()
    return n

def get_approved_chats() -> set:
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM approved_chats")
    rows = cursor.fetchall(); conn.close()
    return {r[0] for r in rows}

def approve_chat(chat_id: int, title: str):
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO approved_chats (chat_id, title) VALUES (?, ?)", (chat_id, title))
    conn.commit(); conn.close()

def remove_chat(chat_id: int):
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("DELETE FROM approved_chats WHERE chat_id = ?", (chat_id,))
    conn.commit(); conn.close()

def save_chat_event(text: str, title: str):
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO chat_events (hash, text, ts, chat_title) VALUES (?, ?, ?, ?)", (generate_hash(text), text, time.time(), title))
    conn.commit(); conn.close()

def get_recent_chat_events(days: int = 3) -> list:
    conn = sqlite3.connect("users.db"); cursor = conn.cursor()
    cursor.execute("SELECT text, ts, chat_title FROM chat_events WHERE ts > ? ORDER BY ts DESC", (time.time() - days * 86400,))
    rows = cursor.fetchall(); conn.close()
    return rows

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
IVANOVO_AGGLOMERATION = ["Иваново", "Кохма", "Тейково", "Фурманов", "Шуя", "Приволжск"]
CAMERAS = [
    {"name": "Перекресток Ленина / Багаева", "lat": 56.9986, "lon": 40.9742}, {"name": "Проспект Строителей / Шубиных", "lat": 57.0021, "lon": 40.9613},
    {"name": "Лежневская / Громобоя", "lat": 56.9789, "lon": 40.9712}, {"name": "Уводьский мост", "lat": 57.0011, "lon": 40.9841},
    {"name": "Шереметевский проспект", "lat": 57.0015, "lon": 40.9758}, {"name": "Улица Куконковых", "lat": 56.9923, "lon": 40.9582},
    {"name": "Московское шоссе (въезд)", "lat": 56.9321, "lon": 40.9210}, {"name": "Кохомское шоссе", "lat": 56.9448, "lon": 41.0112},
    {"name": "Тейковское шоссе", "lat": 56.9811, "lon": 40.9105}, {"name": "Улица Некрасова", "lat": 56.9995, "lon": 40.9851}
]

# Города (исключаем совпадения с прилагательными вроде "Ивановская область", "Шуйский район")
CITY_PATTERNS = {
    "Иваново": r'\b(?:в|из|по|до|под|г\.|город[еау]?)\s+иванов(?:о|е|а|у|ым)\b|\bиванов(?:о|е)\b(?!\s+(?:район|област))',
    "Шуя": r'\b(?:в|из|по|до|под|г\.|город[еау]?)\s+шу(?:я|е|ю|и|ей)\b|\bшу(?:я|е|ю)\b',
    "Кинешма": r'\bкинешм(?:а|е|у|ы|ой)\b',
    "Вичуга": r'\bвичуг(?:а|е|у|и|ой)\b',
    "Тейково": r'\bтейков(?:о|е|а|у)\b(?!\s+район)',
    "Фурманов": r'\bфурманов(?:е|а|у|ом)?\b(?!\s+район)',
    "Кохма": r'\bкохм(?:а|е|у|ы|ой)\b',
    "Родники": r'\bродник(?:и|ах|ов|ам)\b',
    "Приволжск": r'\bприволжск(?:е|а|у|ом)?\b(?!\s+район)',
    "Заволжск": r'\bзаволжск(?:е|а|у|ом)?\b(?!\s+район)',
    "Наволоки": r'\bнаволок(?:и|ах|ов|ам)\b',
    "Юрьевец": r'\bюрьев(?:ец|це|ца|цу)\b',
    "Южа": r'\bюж(?:а|е|у|ей)\b',
    "Гаврилов Посад": r'\bгаврилов[- ]посад(?:е|а|у|ом)?\b',
    "Комсомольск": r'\bкомсомольск(?:е|а|у|ом)?\b(?!\s+район)',
    "Плес": r'\bпл[её]с(?:е|а|у|ом)?\b',
    "Пучеж": r'\bпучеж(?:е|а|у|ом)?\b(?!\s+район)',
}

# Районы области: (корень прилагательного, райцентр из списка городов бота или None)
DISTRICT_STEMS = {
    "Ивановский район": ("ивановск", "Иваново"),
    "Шуйский район": ("шуйск", "Шуя"),
    "Кинешемский район": ("кинешемск", "Кинешма"),
    "Вичугский район": ("вичугск", "Вичуга"),
    "Тейковский район": ("тейковск", "Тейково"),
    "Фурмановский район": ("фурмановск", "Фурманов"),
    "Родниковский район": ("родниковск", "Родники"),
    "Приволжский район": ("приволжск", "Приволжск"),
    "Заволжский район": ("заволжск", "Заволжск"),
    "Юрьевецкий район": ("юрьевецк", "Юрьевец"),
    "Южский район": ("южск", "Южа"),
    "Гаврилово-Посадский район": ("гаврилово[- ]посадск", "Гаврилов Посад"),
    "Комсомольский район": ("комсомольск", "Комсомольск"),
    "Пучежский район": ("пучежск", "Пучеж"),
    "Палехский район": ("палехск", None),
    "Пестяковский район": ("пестяковск", None),
    "Лухский район": ("лухск", None),
    "Верхнеландеховский район": ("верхнеландеховск", None),
    "Ильинский район": ("ильинск", None),
    "Лежневский район": ("лежневск", None),
    "Савинский район": ("савинск", None),
}
OBLAST_DISTRICTS = {
    name: re.compile(stem + r'(?:ий|ого|ом|ому|им|ие|их)\s+(?:муниципальн\w+\s+)?(?:район|округ)\w*')
    for name, (stem, _) in DISTRICT_STEMS.items()
}
DISTRICT_CENTER = {name: center for name, (_, center) in DISTRICT_STEMS.items()}

# Райцентры и посёлки, которых нет в списке городов бота: (шаблон, район)
TOWNS = {
    "Палех": (r'\bпалех\w{0,2}\b', "Палехский район"),
    "Пестяки": (r'\bпестяк(?:и|ах|ов|ам)\b', "Пестяковский район"),
    "Лух": (r'\bлух(?:а|е|у|ом)?\b', "Лухский район"),
    "Лежнево": (r'\bлежнев(?:о|е)\b', "Лежневский район"),
    "Савино": (r'\bсавин(?:о|е)\b', "Савинский район"),
    "Верхний Ландех": (r'\bверхн(?:ий|ем|его)\s+ландех\w{0,2}\b', "Верхнеландеховский район"),
    "Ильинское-Хованское": (r'\bильинск(?:ое|ом|ого)[- ]хованск\w+', "Ильинский район"),
    "Новописцово": (r'\bновописцов(?:о|е)\b', "Вичугский район"),
}

# Городские районы и крупные микрорайоны Иванова
CITY_DISTRICTS = {
    "Ленинский район": r'ленинск(?:ом|ого|ий|ому|им)\s+район',
    "Октябрьский район": r'октябрьск(?:ом|ого|ий|ому|им)\s+район',
    "Советский район": r'советск(?:ом|ого|ий|ому|им)\s+район',
    "Фрунзенский район": r'фрунзенск(?:ом|ого|ий|ому|им)\s+район',
    "мкр. Авдотьино": r'авдотьин[оеа]',
    "мкр. Сортировка": r'сортировк[аеу]',
    "мкр. Московский": r'московск(?:ий|ом)\s+микрорайон',
    "мкр. Суховка": r'суховк[аеу]',
    "Местечко Пустошь-Бор": r'пустошь[- ]бор',
    "Местечко Балино": r'балин[ое]',
    "Местечко Минеево": r'минеев[ое]',
    "Местечко Курьяново": r'курьянов[ое]',
}

# Основные трассы и направления региона
HIGHWAYS = {
    "Трасса Р-132 «Золотое кольцо»": r'(?:р-?132|золотое\s+кольцо)',
    "Трасса М-7 «Волга» (подъезд к Иванову)": r'(?:м-?7|подъезд\s+к\s+иванов)',
    "Трасса Иваново — Кинешма": r'(?:иваново\s*[-—]\s*кинешм|кинешемск(?:ая|ой|ую)\s+трасс)',
    "Трасса Иваново — Шуя": r'(?:иваново\s*[-—]\s*шу|шуйск(?:ая|ой|ую)\s+трасс)',
    "Трасса Иваново — Ярославль": r'(?:иваново\s*[-—]\s*ярославл|ярославск(?:ая|ой|ую)\s+трасс)',
    "Трасса Иваново — Владимир": r'(?:иваново\s*[-—]\s*владимир|владимирск(?:ая|ой|ую)\s+трасс)',
    "Трасса Иваново — Кострома": r'(?:иваново\s*[-—]\s*костром|костромск(?:ая|ой|ую)\s+трасс)',
    "Трасса Иваново — Тейково": r'(?:иваново\s*[-—]\s*тейков|тейковск(?:ая|ой|ую)\s+трасс)',
    "Трасса Иваново — Родники": r'(?:иваново\s*[-—]\s*родник|родниковск(?:ая|ой|ую)\s+трасс)',
}

# Ключевые слова для мониторинга автодвижения (текст всегда приводится к нижнему регистру)
THEFT_PATTERN = r'\b(угон\w*|угнал\w*|похищ\w*\s+(?:авто|машин|транспорт|ваз|лада|иномарк)|похитил\w*\s+(?:авто|машин|транспорт)|розыск\w*\s+(?:авто|машин|транспорт)|краж\w*\s+(?:авто|машин))\b'
PEDESTRIAN_PATTERN = r'\b(сбил\w*\s+(?:пешеход|человек|мужчин|женщин|ребенк|девочк|мальчик|пенсионер|подростк)|наезд\w*\s+на\s+пешеход|пешеход\w*\s+(?:попал|оказался)\s+под\s+колес|на\s+пешеходном\s+переходе\s+сбил)\b'
ROADWORK_PATTERN = r'\b(перекрыт\w*|перекро\w*|перекрыва\w*|ограничен\w*\s+движен|ремонт\w*\s+дорог|закрыт\w*\s+движен|объезд\w*|светофор\w*\s+не\s+работа|отключ\w*\s+светофор|ямочн\w*\s+ремонт|реверсивн\w*\s+движен|нет\s+проезда)\b'
TRAFFIC_PATTERN = r'\b(дтп|авари\w*|пробк\w*|затор\w*|столкн\w*|пострадал\w*|скоплен\w*|затруднен\w*|стоит\s+движен|вылет\w*\s+(?:в\s+кювет|с\s+дороги|на\s+встречк)|опрокид\w*|перевернул\w*|лобов\w*|врезал\w*|снес\w*\s+(?:столб|остановк|огражден)|фура\s+(?:перегородил|развернул|застрял)|эвакуатор\w*|паралич\w*\s+движени\w*)\b'
WEATHER_ROAD_PATTERN = r'\b(гололед\w*|гололёд\w*|скользк\w+\s+дорог\w*|снежн\w+\s+накат\w*|наледь\w*|гибдд\s+(?:предупрежд|призыва|рекоменд)\w*)\b'

# Источники: сайты (RSS/Atom), публичные Telegram-каналы, паблики ВК.
# Недоступные источники бот просто пропускает. Проверить, какие работают: команда /sources (для админа).
# Свои источники можно добавлять без правки кода: /addtg, /addvk, /addrss.
RSS_SOURCES = [
    "https://newsivanovo.ru/rss",
    "https://ivanovonews.ru/rss/",
    "https://mkivanovo.ru/rss/",
    "https://iv-news.ru/feed/",
    "https://37.ru/rss/",
    "https://cursiv.ru/rss/",
    "https://kineshemec.ru/rss/",
    "https://mspros.ru/rss/",
]

TG_CHANNELS = [
    "ivanovonewsBars",
    "ivanovo_proisshestvia",
    "chp_ivanovo",
    "dtp_ivanovo",
    "typical_kineshma",
    "kineshemec_ru",
    "shuya_news",
    "vichuga_news",
    "teykovo_news",
    "furmanov_news",
    "gibdd37",
    "ivoblast",
]
# Профильные ивановские каналы: из них берём сообщения, даже если слово «Иваново» не названо
LOCAL_TG = {"ivanovonewsBars", "ivanovo_proisshestvia", "chp_ivanovo", "dtp_ivanovo", "gibdd37", "ivoblast"}

VK_GROUPS = [
    "https://m.vk.ru/avtopartner_ivanovo",
    "https://m.vk.ru/chp_ivanovo",
    "https://m.vk.ru/dtp_ivanovo",
    "https://m.vk.ru/ivanovonews",
    "https://m.vk.ru/kineshma_chp",
    "https://m.vk.ru/shuya_chp",
]

STOP_WORDS = ["украин", "москв", "петербург", "росси", "сша", "европ", "пробаци"]

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

# =====================================================================
#                         ВСПОМОГАТЕЛЬНОЕ
# =====================================================================
def esc(s) -> str:
    return html.escape(str(s), quote=False)

def split_message(text: str, limit: int = 4000) -> list:
    """Режет длинный текст на части по абзацам (лимит Telegram - 4096 символов)."""
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
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.text()

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

WEATHER_QUERY = ("&current=temperature_2m,apparent_temperature,precipitation_probability,weather_code,relative_humidity_2m,precipitation"
                 "&hourly=precipitation,snowfall,rain,temperature_2m,visibility&past_hours=24&forecast_hours=12&timezone=Europe/Moscow")

async def get_weather_ivanovo():
    lat, lon = REGION_CITIES["Иваново"]
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}" + WEATHER_QUERY
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
            async with session.get(url, timeout=tmo(10)) as resp:
                data = await resp.json()
                if isinstance(data, dict): data = [data]
                lines = []
                for i, city in enumerate(selected_cities.keys()):
                    if i < len(data):
                        current = data[i].get("current", {})
                        lines.append(f"{get_weather_emoji(current.get('weather_code', 0))} <b>{city}</b>: {current.get('temperature_2m', 0):+.1f}°C, 💧{current.get('precipitation_probability', 0)}%")
                return "🌍 <b>Погода в выбранных городах:</b>\n" + "\n".join(lines)
        except Exception: return "⚠️ Ошибка при запросе к сервису."

# --- Риски на дорогах по погоде для каждого выбранного города ---
def analyze_city_weather(d: dict) -> dict:
    current = d.get("current", {}) or {}
    hourly = d.get("hourly", {}) or {}
    temp = _num(current.get("temperature_2m"))
    code = current.get("weather_code", 0)
    humidity = _num(current.get("relative_humidity_2m"), 0)
    precip_now = _num(current.get("precipitation"))

    prec = hourly.get("precipitation", []) or []
    snow = hourly.get("snowfall", []) or []
    temps_h = hourly.get("temperature_2m", []) or []
    times = hourly.get("time", []) or []

    precip24 = sum(_num(v) for v in prec[0:24])
    snow24 = sum(_num(v) for v in snow[0:24])
    past_temps = [v for v in temps_h[0:24] if v is not None]
    min24 = min(past_temps) if past_temps else temp
    vis = [v for v in (hourly.get("visibility", []) or [])[0:24] if v is not None]
    visibility = vis[-1] if vis else None
    road, bad = get_road_condition(code, temp, humidity, precip_now, precip24, snow24, min24, visibility)

    # Прогноз на ближайшие 12 часов (индекс 24 = текущий час)
    fut_prec = prec[24:36]
    fut_temps = [v for v in temps_h[24:36] if v is not None]
    fut_times = times[24:36]
    wet = [i for i, v in enumerate(fut_prec) if _num(v) > 0.05]
    upcoming = None
    if wet and len(fut_times) > wet[-1]:
        start = "сейчас" if wet[0] == 0 else fut_times[wet[0]][-5:]
        try:
            end_h = (int(fut_times[wet[-1]][-5:-3]) + 1) % 24
            end = f"{end_h:02d}:00"
        except ValueError:
            end = "?"
        upcoming = {
            "start": start, "end": end,
            "tmin": min(fut_temps) if fut_temps else temp,
            "snow": sum(_num(v) for v in snow[24:36]) >= 0.3,
        }
    return {"temp": temp, "road": road, "bad": bad, "upcoming": upcoming, "code": code}

_risk_cache = {}

async def get_cities_weather_risk(user_cities: list) -> dict:
    cities = [c for c in user_cities if c in REGION_CITIES]
    if not cities: return {}
    key = tuple(sorted(cities))
    cached = _risk_cache.get(key)
    if cached and time.time() - cached[0] < 600: return cached[1]

    lats = ",".join(str(REGION_CITIES[c][0]) for c in cities)
    lons = ",".join(str(REGION_CITIES[c][1]) for c in cities)
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lons}"
           "&current=temperature_2m,weather_code,relative_humidity_2m,precipitation"
           "&hourly=precipitation,snowfall,temperature_2m,visibility&past_hours=24&forecast_hours=12&timezone=Europe/Moscow")
    risks = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=tmo(15)) as resp:
                if resp.status != 200: return {}
                data = await resp.json()
        if isinstance(data, dict): data = [data]
        for i, city in enumerate(cities):
            if i < len(data): risks[city] = analyze_city_weather(data[i])
    except Exception:
        return {}
    _risk_cache[key] = (time.time(), risks)
    return risks

RUSH_HOURS = "07:30–09:30 и 16:30–19:00"

def build_weather_block(risks: dict, news_list: list) -> str:
    """Погода + новости -> где и когда на дорогах могут быть проблемы. Только выбранные пользователем города."""
    if not risks: return ""
    streets = {}
    for n in news_list:
        c = n.get("city") or n.get("filter_city")
        st = extract_street(n["full_text"])
        if c and st: streets.setdefault(c, set()).add(st)

    cards, calm, shown, any_bad = [], [], 0, False
    for city, r in risks.items():
        up = r["upcoming"]
        if not (r["bad"] or up):
            calm.append(city); continue
        if shown >= 8:
            calm.append(city + " (есть риск)"); continue
        shown += 1
        any_bad = any_bad or r["bad"]
        lines = [f"📍 <b>{city}</b> · {r['temp']:+.0f}°C"]
        if r["bad"]: lines.append(r["road"])
        if up:
            kind = "снег" if up["snow"] else "осадки"
            when = f"сейчас и до {up['end']}" if up["start"] == "сейчас" else f"с {up['start']} до {up['end']}"
            tail = ", будет скользко" if up["tmin"] <= 1 else ", асфальт будет мокрым"
            lines.append(f"🕗 {when.capitalize()}: {kind} при {up['tmin']:+.0f}°C{tail}")
        if city == "Иваново":
            spots = ", ".join(c["name"] for c in CAMERAS[:3])
            lines.append(f"⚠️ Осторожнее: {esc(spots)}")
        else:
            lines.append("⚠️ Осторожнее на мостах, спусках и выездах на трассу")
        if streets.get(city):
            lines.append(f"📰 В новостях: {esc(', '.join('ул. ' + x for x in sorted(streets[city])[:3]))}")
        cards.append("\n".join(lines))

    if not cards: return ""
    text = "🌦 <b>Погода и дороги в ваших городах</b>\n\n" + "\n\n".join(cards)
    if any_bad:
        text += f"\n\n⏱ В часы пик ({RUSH_HOURS}) заторы вероятнее."
    if calm:
        text += f"\n✅ Без осложнений: {', '.join(calm)}"
    text += "\n<i>Оценка по прогнозу погоды и новостям, не данные дорожных служб.</i>"
    return text

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
                    save_today_fuel(prices.get("АИ-92"), prices.get("АИ-95"), prices.get("ДТ"))
                else:
                    if "АИ-92" in prices: text += f"🔹 <b>АИ-92:</b> {prices['АИ-92']:.2f} ₽/л\n"
                    if "АИ-95" in prices: text += f"🔹 <b>АИ-95:</b> {prices['АИ-95']:.2f} ₽/л\n"
                    if "ДТ" in prices: text += f"🔹 <b>ДТ:</b> {prices['ДТ']:.2f} ₽/л\n"
                return text
    except Exception: return "⚠️ Ошибка сети при запросе цен."

# =====================================================================
#                       ДАТЫ И СВЕЖЕСТЬ НОВОСТЕЙ
# =====================================================================
def parse_rss_date(s):
    if not s: return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=pytz.utc)
        return dt
    except Exception: return None

def parse_any_date(s):
    """RSS-дата или ISO (Atom). Возвращает datetime с часовым поясом либо None."""
    dt = parse_rss_date(s)
    if dt: return dt
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=pytz.utc)
        return dt
    except Exception: return None

def is_fresh(dt) -> bool:
    if dt is None: return False
    today = datetime.now(MSK_TZ).date()
    return (today - dt.astimezone(MSK_TZ).date()).days in (0, 1, 2)

def vk_post_is_fresh(text_div) -> bool:
    node = text_div
    for _ in range(6):
        node = node.parent
        if node is None: return False
        d = node.find(class_="rel_date") or node.find(class_="wi_date")
        if d:
            return any(w in d.get_text(" ", strip=True).lower() for w in ("сегодня", "вчера", "назад", "только что"))
    return False

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

def format_time(dt=None):
    if dt:
        dt = dt.astimezone(MSK_TZ)
        if dt.date() == datetime.now(MSK_TZ).date(): return f"Сегодня в {dt.strftime('%H:%M')}"
        if (datetime.now(MSK_TZ).date() - dt.date()).days == 1: return f"Вчера в {dt.strftime('%H:%M')}"
        return dt.strftime("%d.%m в %H:%M")
    return "Недавно"

def clean_html(raw_html):
    if not raw_html: return ""
    return re.sub(r'\s+', ' ', BeautifulSoup(raw_html, "html.parser").get_text(separator=' ')).strip()

def clean_telegram_text(text):
    if not text: return ""
    for phrase in [r"Иллюстрация создана ИИ", r"Фото:.*", r"Подписывайтесь.*", r"Проголосовать за канал.*", r"Прислать новость.*", r"Предложить новость.*", r"Источник:.*"]:
        text = re.sub(phrase, "", text, flags=re.IGNORECASE)
    return re.sub(r'\n{3,}', '\n\n', text).strip()

# =====================================================================
#          ВРЕМЕННОЕ ОКНО: когда из-за события возможны проблемы
# =====================================================================
RANGE_HM = re.compile(r'\bс\s+(\d{1,2})[:.](\d{2})\s*(?:до|по|-|—|–)\s*(\d{1,2})[:.](\d{2})')
RANGE_H = re.compile(r'\bс\s+(\d{1,2})\s*(?:до|по)\s*(\d{1,2})\s*(?:ч\b|час)')
UNTIL_HM = re.compile(r'\b(?:до|по)\s+(\d{1,2})[:.](\d{2})')
AT_HM = re.compile(r'\b(?:в|около|примерно|ближе\s+к)\s+(\d{1,2})[:.](\d{2})')

def _event_base_date(text, dt):
    today = datetime.now(MSK_TZ).date(); dates = []
    for d, mon in DATE_RE.findall(text.lower()):
        try: dd = datetime(today.year, MONTHS[mon], int(d)).date()
        except ValueError: continue
        if (dd - today).days > 7: dd = dd.replace(year=today.year - 1)
        dates.append(dd)
    if dates: return max(dates)
    if dt: return dt.astimezone(MSK_TZ).date()
    return today

def _mk(base_date, h, m):
    h, m = int(h), int(m)
    if h > 23 or m > 59: return None
    return MSK_TZ.localize(datetime(base_date.year, base_date.month, base_date.day, h, m))

def traffic_window(text, ev_type, dt=None, is_high=False):
    """Возвращает строку про время, когда возможны затруднения, или ''."""
    if ev_type in ("theft", "info", "weather_road"): return ""
    lower = text.lower()
    now = datetime.now(MSK_TZ)
    base = _event_base_date(text, dt)
    start = end = None
    planned = False

    m = RANGE_HM.search(lower)
    if m:
        start, end = _mk(base, m.group(1), m.group(2)), _mk(base, m.group(3), m.group(4)); planned = True
    else:
        m = RANGE_H.search(lower)
        if m:
            start, end = _mk(base, m.group(1), 0), _mk(base, m.group(2), 0); planned = True
    if planned and start and end and end < start:
        end += timedelta(days=1)

    if not planned:
        at = AT_HM.search(lower)
        until = UNTIL_HM.search(lower) if ev_type == "roadwork" else None
        if until:
            end = _mk(base, until.group(1), until.group(2))
            start = _mk(base, at.group(1), at.group(2)) if at else (dt.astimezone(MSK_TZ) if dt else None)
        else:
            if at: start = _mk(base, at.group(1), at.group(2))
            elif dt: start = dt.astimezone(MSK_TZ)
            if start:
                hours = 4 if ev_type == "roadwork" else 3 if is_high else 2 if ev_type == "pedestrian" else 1.5
                end = start + timedelta(hours=hours)

    if not start or not end or end <= start: return ""
    s, e = start.strftime("%H:%M"), end.strftime("%H:%M")
    if end < now: status = "по описанию, уже закончилось"
    elif start > now: status = "ещё впереди"
    else: status = "возможно, идёт сейчас"
    if planned or ev_type == "roadwork":
        return f"⏱ Ограничения: <b>{s}–{e}</b> · {status}"
    return f"⏱ Затруднения: <b>~{s}–{e}</b> · {status}"

# =====================================================================
#                    ТОЧНОЕ ОПРЕДЕЛЕНИЕ МЕСТА СОБЫТИЯ
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

VILLAGE_RE = re.compile(
    r'(?:\b[Дд]ер(?:евн[еяю]|\.)|\b[Сс]ел[оеа]\b|\b[Сс]\.|\b[Пп]ос[её]л(?:ок|ке|ка)\b|\b[Пп]ос\.|\bпгт\.?)\s*'
    r'([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)'
)

def extract_location(text):
    """
    Возвращает словарь:
      city        - город (только если назван в тексте)
      district    - район области / район города
      settlement  - посёлок, деревня, село
      highway     - трасса или километр автодороги
      filter_city - город для сверки с выбранными пользователем городами
    Если в тексте назван только район, город не подставляется: бот покажет район.
    """
    lower_text = text.lower()
    city = district = settlement = highway = None
    filter_city = "Ивановская область"

    # 0. Трасса (её название, например «Иваново — Кинешма», не должно считаться городом события)
    lower_wo_highway = lower_text
    for h_name, pattern in HIGHWAYS.items():
        if re.search(pattern, lower_text):
            highway = h_name
            lower_wo_highway = re.sub(pattern, " ", lower_wo_highway)
            break

    # 1. Город
    for c_name, pattern in CITY_PATTERNS.items():
        if re.search(pattern, lower_wo_highway):
            city = c_name; filter_city = c_name
            break

    # 2. Муниципальный район области
    for d_name, pattern in OBLAST_DISTRICTS.items():
        if pattern.search(lower_text):
            district = d_name
            if not city:
                center = DISTRICT_CENTER.get(d_name)
                if center: filter_city = center
            break

    # 3. Внутригородской район или микрорайон Иванова
    if not district:
        for cd_name, pattern in CITY_DISTRICTS.items():
            if re.search(pattern, lower_text):
                district = cd_name
                if not city:
                    city = "Иваново"; filter_city = "Иваново"
                break

    # 4. Райцентры и посёлки, которых нет в списке городов
    if not city:
        for t_name, (pattern, t_district) in TOWNS.items():
            if re.search(pattern, lower_text):
                settlement = t_name
                if not district: district = t_district
                break

    # 5. Деревня, село, посёлок
    if not settlement:
        m = VILLAGE_RE.search(text)
        if m and m.group(1).lower() not in [c.lower() for c in REGION_CITIES]:
            settlement = f"н.п. {m.group(1)}"

    # 6. Километр автодороги, если трасса по названию не определилась
    if not highway:
        km = re.search(r'(\d{1,3}[- ]?[йм]?\s*км\s+(?:автодороги|трассы|шоссе)\s+[А-ЯЁа-яё0-9\s\-—«»]+?)(?=[.,;\n]|$)', text)
        if km: highway = km.group(1).strip()

    return {"city": city, "district": district, "settlement": settlement, "highway": highway, "filter_city": filter_city}

def location_lines(n) -> str:
    city, district, settlement = n.get("city"), n.get("district"), n.get("settlement")
    if city and district: return f"🏙 <b>Город:</b> {esc(city)}, {esc(district)}\n"
    if city: return f"🏙 <b>Город:</b> {esc(city)}\n"
    if district or settlement:
        parts = [p for p in (district, settlement) if p]
        return f"🗺 <b>Район:</b> {esc(', '.join(parts))} (Ивановская область)\n"
    return "🗺 <b>Локация:</b> точное место в новости не указано (Ивановская область)\n"

def classify_event_and_impact(text, city=None, district=None, highway=None):
    lower = text.lower()
    off_city = bool(highway or (district and not city))

    if re.search(THEFT_PATTERN, lower):
        return "🚓 <b>Угон / розыск авто</b>", "🚓 Внимание", False, "theft"

    if re.search(PEDESTRIAN_PATTERN, lower):
        return "🚶 <b>Наезд на пешехода</b>", "🔴 Критично", True, "pedestrian"

    if re.search(ROADWORK_PATTERN, lower):
        return "🚧 <b>Перекрытие / ограничение движения</b>", "🟠 Ищите объезд", True, "roadwork"

    high_words = ["перекрыт", "смертельн", "погиб", "фура", "грузовик", "трасса", "тройное", "массов", "вылет", "коллапс", "пострадал", "пьян", "встречк", "реанимац", "госпитализ"]
    if any(w in lower for w in high_words):
        impact = "🔴 Высокое: возможны заторы на направлении" if off_city else "🔴 Высокое: возможна пробка"
        return "💥 <b>Серьёзное ДТП</b>", impact, True, "accident"

    if any(w in lower for w in ["дтп", "авари", "столкн", "скорая", "затор", "пробка", "скопление", "затруднен"]):
        impact = "🟠 Среднее: возможны затруднения на участке" if off_city else "🟠 Среднее: возможно замедление"
        return "🚗 <b>ДТП / затор</b>", impact, False, "traffic"

    if re.search(WEATHER_ROAD_PATTERN, lower):
        return "🧊 <b>Опасные дорожные условия</b>", "🟠 Снизьте скорость", False, "weather_road"

    return "📝 <b>Дорожная информация</b>", "🟢 Низкое", False, "info"

def short_text(text, limit=260):
    """Сокращает длинный текст до 1-3 предложений."""
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'#\w+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    sentences = re.split(r'(?<=[.!?])(?<!\bул\.)(?<!\bпр\.)(?<!\bг\.)(?<!\bд\.)(?<!\bкм\.)(?<!\bпер\.)\s+(?=[А-ЯЁ«"])', text)
    out = ""
    for sent in sentences:
        if not out:
            out = sent
        elif len(out) + 1 + len(sent) <= limit:
            out += " " + sent
        else:
            break
    if len(out) > limit + 60:
        out = out[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    elif len(out) < len(text):
        out = out.rstrip() + " …"
    return out

def place_line(n) -> str:
    parts = []
    if n.get("city"): parts.append(n["city"])
    if n.get("district"): parts.append(n["district"])
    if n.get("settlement"): parts.append(n["settlement"])
    street = extract_street(n["full_text"])
    if street: parts.append(f"ул. {street}")
    if not parts:
        return "📍 Место не указано (Ивановская область)"
    if not n.get("city"):
        parts.append("Ивановская обл.")
    return "📍 " + " · ".join(esc(p) for p in parts)

def format_event(n) -> str:
    lines = [n["header"], place_line(n)]
    if n.get("highway"): lines.append(f"🛣 {esc(n['highway'])}")
    lines.append(f"<blockquote>{esc(short_text(n['full_text']))}</blockquote>")
    window = traffic_window(n["full_text"], n["ev_type"], n.get("dt"), n["is_high"])
    if window: lines.append(window)
    foot = [n["impact_text"], f"🕒 {n['time']}"]
    if n.get("link"):
        foot.append(f'<a href="{html.escape(n["link"], quote=True)}">{esc(n["source"])}</a>')
    elif n.get("source"):
        foot.append(esc(n["source"]))
    lines.append(" · ".join(foot))
    return "\n".join(lines)

# =====================================================================
#                     ПОИСК НОВОСТЕЙ: САЙТЫ, КАНАЛЫ, ПАБЛИКИ, ЧАТЫ
# =====================================================================
def is_auto_related(lower_text: str) -> bool:
    return bool(
        re.search(TRAFFIC_PATTERN, lower_text)
        or re.search(THEFT_PATTERN, lower_text)
        or re.search(PEDESTRIAN_PATTERN, lower_text)
        or re.search(ROADWORK_PATTERN, lower_text)
        or re.search(WEATHER_ROAD_PATTERN, lower_text)
    )

def is_region_related(lower_text: str) -> bool:
    return (
        "иванов" in lower_text
        or "област" in lower_text
        or any(c.lower() in lower_text for c in REGION_CITIES)
        or any(p.search(lower_text) for p in OBLAST_DISTRICTS.values())
        or any(re.search(p, lower_text) for p in CITY_DISTRICTS.values())
        or any(re.search(p, lower_text) for p, _ in TOWNS.values())
    )

def build_event(text, dt=None, link=None, source="", time_label=None, hint_city=None):
    loc = extract_location(text)
    if hint_city and not loc["city"] and not loc["district"]:
        loc["city"] = loc["filter_city"] = hint_city
    return {
        "title": text[:60] + "...", "full_text": text, "dt": dt,
        "time": time_label or format_time(dt), "link": link, "source": source, **loc,
    }

# Объявления, графики уборки, конкурсы и т.п. - это не события на дорогах
NOISE_PATTERN = r'(оставить\s+заявк|принима\w+\s+обращен|единой\s+дежурн|график\w*\s+уборк|уборк\w*\s+снега|очищ\w+\s+от\s+снега|песко-?солев|розыгрыш|конкурс|скидк|вакансии|акци[яи]\b)'
STRONG_PATTERN = r'\b(дтп|авари\w*|столкн\w*|сбил\w*|пострадал\w*|погиб\w*|угон\w*|угнал\w*|опрокин\w*|перевернул\w*|затор\w*|пробк\w*|перекрыт\w*|перекро\w*)\b'

def event_from_raw(raw, source, local=False, hint_city=None):
    text = raw.get("text") or ""
    if not text: return None
    if not event_date_ok(text): return None
    lower = text.lower()
    lead = lower[:400]  # суть события должна быть в начале текста, а не где-то в середине длинной статьи
    if not is_auto_related(lead): return None
    if re.search(NOISE_PATTERN, lower) and not re.search(STRONG_PATTERN, lead): return None
    if not (local or is_region_related(lower)): return None
    if any(re.search(rf'\b{sw}\b', lower) for sw in STOP_WORDS): return None
    return build_event(text, dt=raw.get("dt"), link=raw.get("link"), source=source,
                       time_label=raw.get("time_label"), hint_city=hint_city)

def item_is_fresh(item) -> bool:
    if item.get("fresh") is not None: return item["fresh"]
    return is_fresh(item.get("dt"))

def get_item_link(item):
    lt = item.find("link")
    if lt is not None:
        if lt.get("href"): return lt["href"]
        sib = lt.next_sibling
        if isinstance(sib, str) and sib.strip().startswith("http"): return sib.strip()
        if lt.get_text(strip=True).startswith("http"): return lt.get_text(strip=True)
    guid = item.find("guid")
    if guid is not None and guid.get_text(strip=True).startswith("http"): return guid.get_text(strip=True)
    return None

# --- "Сырые" данные из источников (до фильтрации) ---
async def raw_rss(session, url):
    soup = BeautifulSoup(await http_get(session, url, 10), "html.parser")
    items = []
    for item in soup.find_all(["item", "entry"]):  # RSS и Atom
        title_tag = item.find("title")
        if not title_tag: continue
        title = title_tag.get_text(strip=True)
        desc_tag = item.find("description") or item.find("summary") or item.find("content")
        desc = clean_html(desc_tag.get_text(strip=True)) if desc_tag else ""
        date_tag = item.find("pubdate") or item.find("published") or item.find("updated") or item.find("dc:date")
        dt = parse_any_date(date_tag.get_text(strip=True)) if date_tag else None
        text = f"{title}. {desc}" if desc and desc.lower() not in title.lower() else title
        items.append({"text": text, "dt": dt, "link": get_item_link(item), "fresh": None})
    return items

async def raw_tg(session, channel):
    soup = BeautifulSoup(await http_get(session, f"https://t.me/s/{channel}", 12), "html.parser")
    items = []
    for wrap in soup.find_all("div", class_="tgme_widget_message_wrap"):
        divs = wrap.find_all("div", class_="tgme_widget_message_text")
        if not divs: continue
        dt = None
        time_tag = wrap.find("time")
        if time_tag and time_tag.get("datetime"):
            try: dt = datetime.fromisoformat(time_tag["datetime"])
            except ValueError: dt = None
        a = wrap.find("a", class_="tgme_widget_message_date")
        text = clean_telegram_text("\n".join(d.get_text(separator="\n", strip=True) for d in divs))
        items.append({"text": text, "dt": dt, "link": a.get("href") if a else None, "fresh": None})
    return items

async def raw_vk(session, group_url):
    soup = BeautifulSoup(await http_get(session, group_url, 12), "html.parser")
    items = []
    for post in soup.find_all("div", class_="wall_post_text"):
        text = clean_telegram_text(post.get_text(separator="\n", strip=True))
        items.append({"text": text, "dt": None, "link": group_url, "fresh": vk_post_is_fresh(post), "time_label": "Сегодня/вчера"})
    return items

# --- Источники: встроенные + добавленные командами ---
def get_all_sources():
    extra = get_extra_sources()
    rss = list(RSS_SOURCES) + [v for k, v in extra if k == "rss" and v not in RSS_SOURCES]
    tg = list(TG_CHANNELS) + [v for k, v in extra if k == "tg" and v not in TG_CHANNELS]
    vk = list(VK_GROUPS) + [v for k, v in extra if k == "vk" and v not in VK_GROUPS]
    extra_local = {v for k, v in extra if k in ("tg", "vk")}
    return rss, tg, vk, extra_local

def source_label(kind, value):
    if kind == "rss": return urlparse(value).netloc or value
    if kind == "tg": return f"t.me/{value}"
    return value.replace("https://", "").replace("http://", "")

# --- События из источников (после фильтров) ---
async def events_rss(session, url):
    try: items = await raw_rss(session, url)
    except Exception: return []
    label = source_label("rss", url)
    return [e for e in (event_from_raw(i, label) for i in items if item_is_fresh(i)) if e]

async def events_tg(session, channel, local=False):
    try: items = await raw_tg(session, channel)
    except Exception: return []
    label = source_label("tg", channel)
    return [e for e in (event_from_raw(i, label, local=local) for i in items if item_is_fresh(i)) if e]

async def events_vk(session, group_url):
    try: items = await raw_vk(session, group_url)
    except Exception: return []
    label = source_label("vk", group_url)
    hint = "Кинешма" if "kineshma" in group_url else "Шуя" if "shuya" in group_url else None
    return [e for e in (event_from_raw(i, label, local=True, hint_city=hint) for i in items if item_is_fresh(i)) if e]

def events_from_chats():
    out = []
    for text, ts, title in get_recent_chat_events(3):
        dt = datetime.fromtimestamp(ts, MSK_TZ)
        if not is_fresh(dt) or not event_date_ok(text): continue
        out.append(build_event(text, dt=dt, source=f"чат «{title}»"))
    return out

_events_cache = {"ts": 0.0, "data": []}

async def collect_all_events():
    """Собирает события из всех источников. Результат кешируется на 5 минут."""
    if time.time() - _events_cache["ts"] < 300:
        return [dict(e) for e in _events_cache["data"]]
    rss, tg, vk, extra_local = get_all_sources()
    sem = asyncio.Semaphore(8)

    async def guard(coro):
        async with sem:
            return await coro

    async with aiohttp.ClientSession() as session:
        tasks = (
            [guard(events_rss(session, u)) for u in rss]
            + [guard(events_tg(session, c, local=(c in LOCAL_TG or c in extra_local))) for c in tg]
            + [guard(events_vk(session, u)) for u in vk]
        )
        results = await asyncio.gather(*tasks, return_exceptions=True)
    events = []
    for res in results:
        if isinstance(res, list): events.extend(res)
    events.extend(events_from_chats())
    _events_cache["ts"] = time.time()
    _events_cache["data"] = events
    return [dict(e) for e in events]

# --- Проверка источников (команда /sources) ---
async def probe_source(kind, value, extra=False):
    label = ("➕ " if extra else "") + source_label(kind, value)
    fn = {"rss": raw_rss, "tg": raw_tg, "vk": raw_vk}[kind]
    try:
        async with aiohttp.ClientSession() as session:
            items = await fn(session, value)
    except Exception as e:
        return f"❌ {label}: {str(e)[:60]}"
    if not items:
        return f"⚠️ {label}: записей нет (источник не найден, закрыт или без новостей)"
    fresh = [i for i in items if item_is_fresh(i)]
    local = kind in ("vk",) or (kind == "tg" and (value in LOCAL_TG or extra))
    relevant = [i for i in fresh if event_from_raw(i, label, local=local)]
    return f"✅ {label}: записей {len(items)}, свежих {len(fresh)}, по теме {len(relevant)}"

# =====================================================================
#                           СВОДКА ПО ДОРОГАМ
# =====================================================================
async def get_traffic_summary(user_id, is_bad_weather, user_cities, mark_seen=True):
    cleanup_old_news()
    all_news = await collect_all_events()

    # Дедупликация похожих новостей из разных источников
    all_news.sort(key=lambda n: n["dt"].timestamp() if n.get("dt") else 0, reverse=True)
    unique_news = []
    for news in all_news:
        words_new = set(w.strip(".,!?\"'()-").lower() for w in news["full_text"].split() if len(w) > 4)
        if not any(len(words_new.intersection(set(w.strip(".,!?\"'()-").lower() for w in ex["full_text"].split() if len(w) > 4))) >= 4 for ex in unique_news):
            unique_news.append(news)

    filtered_news = []
    jam_streets = set()
    traffic_events_count = 0

    for news in unique_news:
        header, impact_text, is_high, ev_type = classify_event_and_impact(
            news["full_text"], city=news["city"], district=news["district"], highway=news["highway"]
        )
        news["header"] = header
        news["impact_text"] = impact_text
        news["ev_type"] = ev_type
        news["is_high"] = is_high

        f_city = news["filter_city"]
        # Важные события (угоны, наезды на пешеходов, перекрытия, серьёзные ДТП) показываем всегда
        is_critical = ev_type in ("theft", "pedestrian", "roadwork") or is_high

        if (
            is_critical
            or f_city == "Ивановская область"
            or f_city in user_cities
            or ("Иваново" in user_cities and f_city in IVANOVO_AGGLOMERATION)
            or ("Иваново" not in user_cities and f_city == "Иваново")
        ):
            news_hash = generate_hash(news["full_text"])
            if not is_news_seen(user_id, news_hash):
                filtered_news.append(news)
                if ev_type != "theft":
                    traffic_events_count += 1
                    if is_high or "затор" in news["full_text"].lower() or "пробка" in news["full_text"].lower():
                        street = extract_street(news["full_text"])
                        if street: jam_streets.add(street)

    # Погода по выбранным городам + выводы по новостям
    risks = await get_cities_weather_risk(user_cities)
    weather_block = build_weather_block(risks, filtered_news)
    weather_bad = is_bad_weather or any(r["bad"] for r in risks.values())

    if not filtered_news and not weather_block:
        return "✅ <b>Новых сообщений об автодвижении и ЧП нет (за последние 2 дня), погода без осложнений.</b>"

    DIV = "━━━━━━━━━━━━━━"
    MAX_EVENTS = 10
    parts = []
    if filtered_news:
        shown_news = filtered_news[:MAX_EVENTS]
        if mark_seen:  # «прочитанными» помечаем только то, что реально показано
            for news in shown_news:
                mark_news_as_seen(user_id, generate_hash(news["full_text"]))
        jam_level = min(10, 2 + traffic_events_count * 2) if traffic_events_count > 0 else 1
        if weather_bad: jam_level = min(10, jam_level + 2)
        bar = "▰" * jam_level + "▱" * (10 - jam_level)
        head = (f"🚨 <b>ДОРОЖНАЯ ОБСТАНОВКА</b> · {datetime.now(MSK_TZ).strftime('%H:%M')}\n"
                f"📊 Пробки: <b>{jam_level}/10</b> {bar}")
        if weather_bad: head += "\n🌧 Погода осложняет движение"
        if jam_streets: head += f"\n📍 Проблемные улицы: {esc(', '.join(list(jam_streets)[:5]))}"
        head += "\n<i>Время затруднений оценено по описанию события, это не данные навигаторов.</i>"
        parts.append(head)
        for news in shown_news:
            parts.append(format_event(news))
        if len(filtered_news) > MAX_EVENTS:
            parts.append(f"…и ещё {len(filtered_news) - MAX_EVENTS} сообщ. Они придут в следующий раз.")
    else:
        parts.append("✅ <b>Новых происшествий нет.</b>")

    if weather_block:
        parts.append(DIV)
        parts.append(weather_block)
    return "\n\n".join(parts)

# =====================================================================
#                              МАРШРУТЫ
# =====================================================================
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0; dlat = math.radians(lat2 - lat1); dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

async def get_coords(session, address):
    query = address if any(city.lower() in address.lower() for city in REGION_CITIES.keys()) else f"Иваново, {address}"
    url = "https://nominatim.openstreetmap.org/search"
    try:
        async with session.get(url, params={"q": query, "format": "json", "limit": 1}, headers={"User-Agent": "MalinkiBot/1.0"}, timeout=tmo(10)) as resp:
            data = await resp.json()
            if data: return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception: pass
    return None

async def get_route_info(session, start_coords, end_coords):
    start_lon, start_lat = start_coords[1], start_coords[0]
    end_lon, end_lat = end_coords[1], end_coords[0]
    url = f"http://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}?overview=full&geometries=geojson"
    try:
        async with session.get(url, timeout=tmo(10)) as resp:
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

    weather_text, is_bad = await get_weather_ivanovo()
    traffic_text = await get_traffic_summary(user_id, is_bad, get_user_cities(user_id), mark_seen=False)
    time_penalty = 10 if "🚨" in traffic_text else 0
    total_min = int(base_min + time_penalty)
    hours, mins = total_min // 60, total_min % 60
    time_str = f"{hours} ч {mins} мин" if hours > 0 else f"{mins} мин"
    weather_short = "✅ Дороги сухие" if not is_bad else "⚠️ Дороги скользкие/мокрые"

    result_text = (
        f"🗺 <b>Маршрут:</b> {esc(start_point.capitalize())} ➡️ {esc(end_point.capitalize())}\n"
        f"📏 <b>Расстояние:</b> {dist_km:.1f} км\n⏱ <b>Время в пути:</b> {time_str}\n\n"
        f"🌤 <b>Условия:</b> {weather_short}\n"
        f"🚦 <b>Трафик:</b> {'Есть ДТП/ограничения, возможны задержки (+10 мин)' if time_penalty else 'Свободно'}\n"
        f"{camera_warning}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💾 Сохранить маршрут", callback_data=f"save_route_{start_point[:10]}_{end_point[:10]}")]])
    await wait_msg.edit_text(result_text, parse_mode="HTML", reply_markup=keyboard)

# =====================================================================
#                      ПОГОДНЫЕ НОВОСТИ И РАССЫЛКА
# =====================================================================
async def get_fuel_situation_news():
    keywords = ["дефицит", "бензин", "топливо", "азс", "заправк", "очеред", "лимит", "ограничен"]
    stop_words = ["украин", "москв", "петербург", "росси", "сша", "европ", "пробаци", "проблем"]
    found_news = []
    async with aiohttp.ClientSession() as session:
        for url in RSS_SOURCES[:4]:
            try:
                for item in await raw_rss(session, url):
                    if not is_fresh(item["dt"]): continue
                    text = item["text"].split(". ")[0]
                    lower_text = text.lower()
                    if any(re.search(rf'\b{kw}\b', lower_text) for kw in keywords) and ("иванов" in lower_text or "област" in lower_text):
                        if not any(re.search(rf'\b{sw}\b', lower_text) for sw in stop_words):
                            if not any(text.lower() in ex.lower() or ex.lower() in text.lower() for ex in found_news):
                                found_news.append(text)
            except Exception: continue
    if not found_news: return None
    return "📰 <b>Топливо (новости):</b>\n" + "".join(f"🔸 {esc(n)}\n\n" for n in found_news[:3])

async def get_weather_news():
    keywords = ["снегопад", "метель", "гололед", "ураган", "мчс", "циклон", "штормовое", "мороз", "занос", "подтоплен", "паводок", "гололедица"]
    found_news = []
    async with aiohttp.ClientSession() as session:
        for url in RSS_SOURCES[:4]:
            try:
                for item in await raw_rss(session, url):
                    if not is_fresh(item["dt"]): continue
                    text = item["text"].split(". ")[0]
                    lower_text = text.lower()
                    if any(re.search(rf'\b{kw}\b', lower_text) for kw in keywords):
                        if not any(text.lower() in ex.lower() or ex.lower() in text.lower() for ex in found_news):
                            found_news.append(text)
            except Exception: continue
    if not found_news: return None
    return "🌪 <b>Погодные предупреждения:</b>\n" + "".join(f"⚠️ {esc(n)}\n\n" for n in found_news[:3])

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
        try:
            for chunk in split_message(full_text):
                await bot.send_message(user_id, chunk, parse_mode="HTML", disable_web_page_preview=True)
        except Exception: pass

# =====================================================================
#                              КЛАВИАТУРЫ
# =====================================================================
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

# =====================================================================
#                              ХЭНДЛЕРЫ
# =====================================================================
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    total, subs = get_bot_stats()
    await message.answer(f"📊 <b>Статистика:</b>\n\n👥 Всего юзеров: <b>{total}</b>\n⏰ Активных подписок: <b>{subs}</b>", parse_mode="HTML")

# --- Админ: управление источниками ---
def normalize_tg(value: str):
    v = value.strip().replace("https://", "").replace("http://", "")
    v = v.replace("t.me/s/", "").replace("t.me/", "").lstrip("@").split("/")[0].split("?")[0]
    return v if re.fullmatch(r"[A-Za-z0-9_]{4,}", v) else None

def normalize_vk(value: str):
    v = value.strip().split("?")[0].rstrip("/")
    name = v.split("/")[-1].lstrip("@")
    return f"https://m.vk.ru/{name}" if re.fullmatch(r"[A-Za-z0-9_.\-]{3,}", name) else None

def normalize_rss(value: str):
    v = value.strip()
    return v if v.startswith("http") else None

async def _add_source(message: types.Message, kind: str, normalizer, example: str):
    if message.from_user.id != ADMIN_ID: return
    parts = (message.text or "").split(maxsplit=1)
    value = normalizer(parts[1]) if len(parts) > 1 else None
    if not value:
        return await message.answer(f"Укажите источник, например: <code>{example}</code>", parse_mode="HTML")
    added = add_extra_source(kind, value)
    _events_cache["ts"] = 0.0  # сбросить кеш новостей
    status = await probe_source(kind, value, extra=True)
    await message.answer(("➕ Добавлено.\n" if added else "Уже был в списке.\n") + status)

@dp.message(Command("addtg"))
async def cmd_addtg(message: types.Message):
    await _add_source(message, "tg", normalize_tg, "/addtg gibdd37")

@dp.message(Command("addvk"))
async def cmd_addvk(message: types.Message):
    await _add_source(message, "vk", normalize_vk, "/addvk https://vk.com/название_паблика")

@dp.message(Command("addrss"))
async def cmd_addrss(message: types.Message):
    await _add_source(message, "rss", normalize_rss, "/addrss https://сайт.ru/rss/")

@dp.message(Command("delsource"))
async def cmd_delsource(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2: return await message.answer("Укажите источник, например: <code>/delsource gibdd37</code>", parse_mode="HTML")
    raw = parts[1].strip()
    candidates = {raw, normalize_tg(raw) or raw, normalize_vk(raw) or raw}
    n = sum(del_extra_source(c) for c in candidates if c)
    _events_cache["ts"] = 0.0
    await message.answer("🗑 Удалено." if n else "Не нашла такой источник среди добавленных (встроенные удаляются только в коде).")

@dp.message(Command("sources"))
async def cmd_sources(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    wait = await message.answer("⏳ Проверяю все источники, это может занять до минуты...")
    extra = get_extra_sources()
    extra_set = {(k, v) for k, v in extra}
    rss, tg, vk, _ = get_all_sources()
    jobs = ([probe_source("rss", u, ("rss", u) in extra_set) for u in rss]
            + [probe_source("tg", c, ("tg", c) in extra_set) for c in tg]
            + [probe_source("vk", u, ("vk", u) in extra_set) for u in vk])
    sem = asyncio.Semaphore(8)

    async def guard(coro):
        async with sem:
            return await coro

    results = await asyncio.gather(*[guard(j) for j in jobs])
    approved = len(get_approved_chats())
    text = "🔎 <b>Проверка источников</b>\n\n" + "\n".join(esc(r) for r in results)
    text += f"\n\n💬 Чатов, подключённых для чтения: {approved}"
    text += "\n\n<i>✅ работает, ⚠️ пусто или не найден, ❌ ошибка. «По теме» - сколько свежих записей прошло фильтры.</i>"
    try: await wait.delete()
    except Exception: pass
    await answer_long(message.answer, text)

# --- Админ: чаты ---
@dp.message(Command("addchat"))
async def cmd_addchat(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    if message.chat.type not in ("group", "supergroup"):
        return await message.answer("Эту команду нужно отправить в самом чате, куда добавлен бот.")
    approve_chat(message.chat.id, message.chat.title or "чат")
    await message.answer("✅ Чат подключён: сообщения о ДТП, пробках и перекрытиях из него попадут в сводку.")

@dp.message(Command("delchat"))
async def cmd_delchat(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    remove_chat(message.chat.id)
    await message.answer("🗑 Чат отключён.")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    save_user_cities(message.from_user.id, get_user_cities(message.from_user.id))
    intro_text = (
        f"👋 <b>Привет, {esc(message.from_user.first_name)}!</b>\n\n"
        "Я — твой персональный помощник по <b>Ивановской области</b>.\n\n"
        "📌 <b>Что я умею:</b>\n"
        "🌤 <b>Погода</b> — прогноз, видимость, длительность осадков и состояние дорог.\n"
        "🌍 <b>Города</b> — погода в выбранных городах.\n"
        "🚦 <b>ДТП и Заторы</b> — короткие карточки: что случилось, где (город, район, трасса), когда возможны затруднения и чем грозит погода в твоих городах.\n"
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
    await answer_long(message.answer, text)

@dp.message(F.text.in_({"🚦 ДТП и Заторы", "🚦 Автодвижение и ЧП"}))
@dp.message(Command("traffic"))
async def cmd_traffic(message: types.Message):
    uid = message.from_user.id
    wait = await message.answer("⏳ Собираю свежие сообщения...")
    try:
        text = await get_traffic_summary(uid, (await get_weather_ivanovo())[1], get_user_cities(uid))
    except Exception as e:
        logging.exception("traffic summary failed")
        text = f"⚠️ Не удалось собрать сводку: {esc(str(e)[:150])}"
    try: await wait.delete()
    except Exception: pass
    await answer_long(message.answer, text)

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
    try:
        text += f"{await get_traffic_summary(user_id, is_bad, get_user_cities(user_id))}"
    except Exception as e:
        logging.exception("traffic summary failed")
        text += f"⚠️ Дорожную сводку собрать не удалось: {esc(str(e)[:150])}"
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
    await message.answer("🏁 <b>Куда едем?</b>\nНапишите конечный адрес или город.", parse_mode="HTML")

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
    await callback.message.edit_text("💾 <b>Как назовем этот маршрут?</b>\nНапример: «Работа», «Домой», «Дача».", parse_mode="HTML")
    await state.set_state(RouteState.waiting_for_route_name)
    await callback.answer()

@dp.message(RouteState.waiting_for_route_name)
async def save_route_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    data = await state.get_data()
    start = data.get("start_point"); end = data.get("end_point")
    if start and end:
        save_new_route(message.from_user.id, name, start, end)
        await message.answer(f"✅ Маршрут <b>«{esc(name)}»</b> сохранен!", parse_mode="HTML")
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

# --- Чтение разрешённых групповых чатов (должен стоять последним) ---
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def collect_chat_message(message: types.Message):
    if message.chat.id not in get_approved_chats(): return
    if message.from_user and message.from_user.is_bot: return
    text = message.text or message.caption
    if not text or len(text) < 25: return
    lower = text.lower()
    if not is_auto_related(lower) or not event_date_ok(text): return
    save_chat_event(text, message.chat.title or "чат")
    _events_cache["ts"] = 0.0

@dp.message(F.chat.type == "private")
async def fallback_private(message: types.Message):
    await message.answer("Выберите действие кнопкой меню внизу 👇", reply_markup=get_main_menu())

async def main():
    logging.basicConfig(level=logging.INFO)
    scheduler = AsyncIOScheduler(timezone=MSK_TZ)
    scheduler.add_job(broadcast_job, trigger='cron', minute='*')
    scheduler.start()
    print("Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

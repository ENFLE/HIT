"""
==============================================================================
 NFT Collection Scanner Bot  |  @MITARSOM | HITL
==============================================================================
یک ربات تلگرام که صفحات t.me/nft/<Collection>-<id> یک کالکشن را به‌صورت
موازی و ناهمگام اسکن می‌کند، ویژگی‌های (Model/Pattern و Backdrop/Color) هر
آیتم را با یک منطق دو لایه استخراج می‌کند و بر اساس انتخاب کاربر فیلتر
متقاطع (AND) اعمال کرده و لینک‌های معتبر را بدون پیش‌نمایش وب برمی‌گرداند.

نصب پیش‌نیازها:
    pip install aiogram aiohttp beautifulsoup4
    # جستجوی وب: بدون API key با DuckDuckGo HTML انجام می‌شود؛ قابل خاموش‌کردن با WEB_SEARCH_ENABLED=0
    (نیازی به lxml نیست؛ از html.parser داخلی پایتون استفاده می‌شود)

اجرا:
    -> ابتدا یک متغیر محیطی برای توکن ست کن (توکن قدیمی را چون در چت
       افشا شده حتماً از BotFather ری‌ووک/رجنریت کن):
         Windows (PowerShell):  $env:BOT_TOKEN="توکن-جدید"
         Windows (cmd):         set BOT_TOKEN=توکن-جدید
    -> python nft_scanner_bot.py

معماری:
    - هر کاربر یک FSMContext مجزا دارد (aiogram Storage) -> سشن قفل‌شده و
      بدون تداخل بین کاربران هم‌زمان.
    - AsyncScanner: تمام درخواست‌های HTTP با aiohttp + asyncio.Semaphore
      (سقف درخواست هم‌زمان) و یک محدودکننده نرخ per-second اجرا می‌شوند.
    - extract_traits(): لایه ۰ (اصلی/جدید) = پارس متن تمیزِ meta og:description
                        / twitter:description («Model: X\nBackdrop: Y...») -
                        این متا-تگ همیشه توسط تلگرام تولید می‌شود و هیچ‌وقت
                        درصد رریتی/تگ تزئینی داخلش نیست، پس نسبت به جدول
                        HTML به‌مراتب پایدارتر و کم‌خطاتره.
                        لایه ۱ = پارس JSON ساخت‌یافته داخل <script> (اگر
                        صفحه چنین ساختاری داشته باشد).
                        لایه ۲ (fallback نهایی) = پارس متنی جدول/کلاس‌های صفحه.
==============================================================================
"""

import asyncio
import colorsys
import hashlib
import html as html_entities
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from logging.handlers import RotatingFileHandler
from typing import Optional
from urllib.parse import quote_plus, urlparse

import aiohttp
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

try:
    from dotenv import load_dotenv
    load_dotenv()  # اگر فایل .env در کنار این اسکریپت باشد، مقادیرش را به os.environ اضافه می‌کند
except ImportError:
    pass  # اگر python-dotenv نصب نباشد، فقط از متغیرهای محیطی سیستم استفاده می‌شود

# ------------------------------------------------------------------------- #
#  تنظیمات کلی
# ------------------------------------------------------------------------- #

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "متغیر محیطی BOT_TOKEN ست نشده. توکن قبلی که قبلاً اینجا هارد-کد شده بود "
        "در چت افشا شده بود و دیگر استفاده نمی‌شود - از BotFather یک توکن جدید "
        "بگیر و آن را به‌صورت متغیر محیطی BOT_TOKEN ست کن."
    )

TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
SIGNATURE = "\n\n— @MITARSOM | HITL"
DAILY_FREE_FILTER_LIMIT = 3             # تعداد دفعات مجاز «فیلتر کردن» رایگان در روز برای هر کاربر عادی (غیر ادمین)
SUPPORT_USERNAME = "hhitl"              # آیدی تلگرام برای خرید سهمیه‌ی بیشتر
GRAM_WALLET = "UQAzV-WjAZNzitVMS_1HCgnKmQGjWohWeceOAO7FMwjWHitl"  # ولت TON برای شارژ
SEARCHES_PER_GRAM = 5                   # هر ۱ TON = ۵ اعتبار استفاده از ربات
TONCENTER_API_URL = os.getenv("TONCENTER_API_URL", "https://toncenter.com/api/v2/getTransactions")
TON_PAYMENT_POLL_SECONDS = int(os.getenv("TON_PAYMENT_POLL_SECONDS", "15"))
TON_PACKAGE_OPTIONS = (1, 2, 5, 10, 20, 50)
TELEGRAM_MSG_LIMIT = 4096
MAX_CONCURRENT_REQUESTS = 20            # مقدار پیش‌فرض اولیه سقف درخواست هم‌زمان (از پنل ادمین قابل تغییره؛ این فقط مقدار شروع/fallback است)
                                        # قبلاً ۱۰۰۰ بود - روی دستگاه‌های محدود (مثل گوشی داخل Termux) این عدد
                                        # کل شبکه/CPU دستگاه رو در طول یک اسکن بزرگ اشغال می‌کرد و باعث می‌شد
                                        # حتی اتصال خودِ ربات به API تلگرام قطع بشه (کانکشن‌ابورت) و هیچ کاربر
                                        # دیگری هم جواب نگیرد. با یک عدد محافظه‌کارانه شروع می‌کنیم و ادمین
                                        # می‌تونه بر اساس قدرت واقعی سرور/شبکه از پنل بالاش ببرد.
MAX_REQUESTS_PER_SECOND = 20            # مقدار پیش‌فرض اولیه سقف نرخ درخواست در ثانیه (از پنل ادمین قابل تغییره؛ این فقط مقدار شروع/fallback است)
                                        # قبلاً ۱۵۰۰ بود - به همان دلیل بالا کاهش یافت.
MIN_ALLOWED_CONCURRENT = 1
MAX_ALLOWED_CONCURRENT = 3000            # کران بالای مقداری که ادمین از پنل وارد می‌کند (به درخواست خودِ
                                        # ادمین از ۴۰ -> ۳۰۰ -> ۳۰۰۰ بالا برده شده). توجه: کامنت‌های قدیمی
                                        # همین فایل هشدار داده بودن که روی گوشی/Termux با دیتای موبایل،
                                        # اعداد در همین بازه (چند صد تا چندهزار) می‌تونن استک شبکه/DNS گوشی
                                        # رو موقتاً قفل کنن تا همه‌ی اپ‌ها بسته بشن. این سقف صرفاً «حداکثر
                                        # مجاز از پنل» است، نه مقدار پیش‌فرض؛ عدد واقعی مصرفی رو خودت از
                                        # پنل تنظیم می‌کنی و پیشنهاد می‌شه پله‌پله (مثلاً ۵۰۰ -> ۱۰۰۰ -> ۲۰۰۰
                                        # -> ۳۰۰۰) بالا ببری، نه یک‌دفعه رفتن روی سقف.
MIN_ALLOWED_PER_SECOND = 1
MAX_ALLOWED_PER_SECOND = 3000            # همان دلیل بالا.
HARD_CONNECTOR_LIMIT = 3000              # سقف سخت و مطلق در سطح خودِ TCPConnector - صرف‌نظر از اینکه ادمین
                                        # از پنل max_concurrent را چه عددی تنظیم کند، این عدد هیچ‌وقت زیر پا
                                        # گذاشته نمی‌شود. با MAX_ALLOWED_CONCURRENT هماهنگ شده (هر دو ۳۰۰۰)
                                        # تا مقداری که از پنل ست می‌شود واقعاً هم به همون سقف TCP برسد، نه
                                        # اینکه پنل عدد بزرگ‌تری نشون بده ولی این‌جا خاموش/نادیده گرفته بشه.
HARD_CONNECTOR_LIMIT_PER_HOST = 3000     # چون تمام درخواست‌ها به یک هاست (t.me) می‌روند، همین سقف کلی هم برای
                                        # هر هاست کافیه؛ سقف جدا و بزرگ‌تر لازم نیست.
# ---- جستجوی موضوع روی وب، نه داخل دیتابیس ---------------------------------- #
# مرورگر (Chrome/Firefox/...) خودش موتور جستجو نیست؛ برای جستجوی خودکار از
# DuckDuckGo HTML استفاده می‌کنیم. اگر بعداً خواستی provider دیگری اضافه کنی،
# فقط تابع web_search را عوض کن. هیچ API key لازم نیست.
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "1") == "1"
WEB_SEARCH_MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "20"))
WEB_SEARCH_TIMEOUT = float(os.getenv("WEB_SEARCH_TIMEOUT", "8"))
WEB_SEARCH_REGION = os.getenv("WEB_SEARCH_REGION", "wt-wt")
WEB_SEARCH_SAFE = os.getenv("WEB_SEARCH_SAFE", "moderate")
WEB_SEARCH_USER_AGENT = os.getenv(
    "WEB_SEARCH_USER_AGENT",
    "Mozilla/5.0 (compatible; NFTScannerBot-WebSearch/1.0)"
)
WEB_SEARCH_MIN_SCORE = float(os.getenv("WEB_SEARCH_MIN_SCORE", "1.0"))
WEB_SEARCH_MAX_MODELS = int(os.getenv("WEB_SEARCH_MAX_MODELS", "5000"))
COLLECTION_WEB_PAGES = int(os.getenv("COLLECTION_WEB_PAGES", "5"))
COLLECTION_MAX_GIFTS = int(os.getenv("COLLECTION_MAX_GIFTS", "8"))
COLLECTION_MAX_MODELS = int(os.getenv("COLLECTION_MAX_MODELS", "24"))
COLLECTION_WEB_MIN_SCORE = float(os.getenv("COLLECTION_WEB_MIN_SCORE", "1.5"))

REQUEST_TIMEOUT = 10                   # ثانیه (روی دیتای موبایل تأخیر لحظه‌ای بیشتر از وای‌فای/سرور طبیعیه؛
                                        # قبلاً ۶ بود و باعث می‌شد تایم‌اوت‌های گذرا زودتر از موعد «خطای شبکه»
                                        # حساب بشن و همون ۳ تلاش هم به سرعت با خطا تموم بشه)
RETRY_ATTEMPTS = 2                     # تلاش مجدد روی خطای شبکه/تایم‌اوت قبل از "نامعتبر" فرض کردن صفحه
RETRY_BACKOFF = 0.25                   # فاصله بین تلاش‌های مجدد (ثانیه)
DISCOVERY_BATCH_SIZE = 200             # اندازه هر دسته در فاز کشف ویژگی‌ها
DISCOVERY_MAX_ITEMS = 3000             # سقف بالای نمونه‌برداری برای کشف کامل مدل/بک‌گراند (فقط برای شناخت مقادیر ممکن، نه اسکن نهایی)
DISCOVERY_STABILITY_WINDOW = 80        # اگر این‌قدر آیتم پشت‌سرهم واقعاً ناموجود بود -> توقف زودهنگام
DEFAULT_MAX_ITEMS_CAP = 1_000_000      # سقف امنیتی مطلق (فقط fallback اگر تشخیص تعداد واقعی کالکشن ممکن نشد)

# ---- تلورانس گپ (اصلاح‌شده) --------------------------------------------- #
# قبلاً این مقادیر ثابت و کوچک بودن (۳۰ و ۶۰). مشکل: در کالکشن‌های خیلی
# بزرگ (چند صدهزارتایی، مثل ۵۰۰-۶۰۰ هزار آیتم) بخش‌های واقعی و پیوسته‌ای
# از آیتم‌های سوخته/استفاده‌نشده می‌تونه به‌مراتب بزرگ‌تر از ۳۰-۶۰ تا باشه؛
# با تلورانس ثابت کوچیک، الگوریتم به یکی از همین گپ‌های واقعی (نه پایان
# واقعی کالکشن) می‌رسید و اشتباهاً همون‌جا (مثلاً حوالی ۴۰-۴۱ هزار از یک
# کالکشن ۵۰۰ هزارتایی) کالکشن رو «تمام‌شده» فرض می‌کرد. راه‌حل: تلورانس
# را متناسب با موقعیت فعلی در کالکشن، پویا (نسبی) حساب می‌کنیم -> هرچی
# جلوتر بریم، گپی که قبول می‌کنیم هم بزرگ‌تر می‌شه.
COUNT_GAP_TOLERANCE = 400              # حداقل تلورانس (برای کالکشن‌های کوچیک هم مطمئن باشیم)
COUNT_GAP_TOLERANCE_RATIO = 0.15       # نسبت تلورانس به موقعیت فعلی (۱۵٪)
COUNT_GAP_TOLERANCE_MAX = 60_000       # سقف بالای تلورانس (برای جلوگیری از کند شدن بیش از حد)
SCAN_ALL_STOP_WINDOW = COUNT_GAP_TOLERANCE  # مقدار پایه؛ در کد از _dynamic_gap_tolerance استفاده می‌شود
MAX_INCONCLUSIVE_STREAK = 20           # قبلاً ۶ بود؛ در کالکشن‌های خیلی بزرگ با ریت‌لیمیت موقت سایت هدف، ۶ بار تلاش ناکافی بود
COUNT_SANITY_CEILING = 3_000_000       # کران بالای مطلق جستجوی نمایی برای پیدا کردن تعداد کل (محافظ در برابر لوپ بی‌نهایت)

# ---- سقف تعداد بررسی واقعی در هر پنجره (اصلاح سرعت) --------------------- #
# مشکل قبلی: COUNT_GAP_TOLERANCE_MAX تا ۶۰٬۰۰۰ می‌رفت و _window_last_valid
# دقیقاً همون تعداد آیتم رو یکی‌یکی (به‌صورت موازی ولی همه‌شون) چک می‌کرد.
# یعنی توی کالکشن‌های بزرگ، هر بار که جستجوی نمایی/دودویی یک «پنجره» رو
# تست می‌کرد، تا ۶۰ هزار درخواست HTTP واقعی می‌رفت - همین باعث می‌شد کل
# پیدا کردن تعداد کالکشن چند دقیقه طول بکشه. راه‌حل: صرف‌نظر از اینکه
# تلورانس محاسبه‌شده (پویا) چقدر بزرگ باشه، هیچ‌وقت بیش از WINDOW_PROBE_CAP
# آیتم واقعاً fetch نمی‌شود؛ برای پنجره‌های بزرگ‌تر از این سقف، به‌جای چک
# کردن تک‌تک آیتم‌ها، به‌صورت یکنواخت (sampled) در طول کل پنجره نمونه‌برداری
# می‌کنیم - چون فقط لازمه بدونیم «آیا جایی جلوتر از این، حداقل یک آیتم
# معتبر هست یا نه» نه اینکه دقیقاً کدوم ایندکس. دقت نهایی با یک پاس ریزتر
# در _window_last_valid (وقتی پنجره کوچیک‌تر از سقفه) دوباره تأمین می‌شود.
WINDOW_PROBE_CAP = 400                 # هماهنگ با HARD_CONNECTOR_LIMIT جدید (۳۰۰۰) بالا برده شد؛ عمداً
                                        # به‌جای رفتن دقیقاً روی ۳۰۰۰، پایین‌تر نگه داشته شده تا فاز کشفِ
                                        # تعداد کالکشن (که فقط برای پیدا کردن انتهای واقعی کالکشنه) به‌تنهایی
                                        # هر «پنجره» رو به کل ظرفیت کانکشن نرسونه؛ اسکن نهایی همچنان از
                                        # get_max_concurrent/get_max_per_second (مقداری که خودت از پنل ست
                                        # می‌کنی) پیروی می‌کنه.


def _dynamic_gap_tolerance(position: int) -> int:
    """
    تلورانس گپ متناسب با موقعیت فعلی (position) را برمی‌گرداند: حداقل
    COUNT_GAP_TOLERANCE، اما هرچی position بزرگ‌تر باشه (یعنی داریم عمیق‌تر
    توی یک کالکشن بزرگ می‌ریم) تلورانس هم به‌صورت نسبی بزرگ‌تر می‌شود، تا
    گپ‌های واقعی و بزرگ (آیتم‌های سوخته/استفاده‌نشده) با «پایان کالکشن»
    اشتباه گرفته نشوند. سقف بالا هم داره که کارایی از دست نره.

    -- تنظیم بهتر نسبت برای کالکشن‌های خیلی بزرگ --
    قبلاً یک نسبت ثابت (COUNT_GAP_TOLERANCE_RATIO=۱۵٪) برای همه‌ی اندازه‌ها
    استفاده می‌شد. مشکل: در کالکشن‌های چند صدهزارتایی این نسبت خیلی زود
    به سقف بالا (COUNT_GAP_TOLERANCE_MAX) می‌رسید و از اونجا به بعد دیگه
    فرقی نمی‌کرد - یعنی داشتیم زودتر از لازم با پنجره‌ی حداکثری کار می‌کردیم.
    حالا نسبت خودش هم پلکانی/adaptive شده: هرچی موقعیت بزرگ‌تر بشه، نسبت
    کمتر می‌شه - نتیجه اینه که پنجره‌های جستجو برای کالکشن‌های خیلی بزرگ
    دیرتر به سقف می‌رسند و در بیشتر بازه‌ها کوچیک‌تر (و سریع‌تر) می‌مونند،
    بدون افت دقت (چون حداقل تضمین‌شده هنوز همون‌جاست).
    """
    if position <= 10_000:
        ratio = COUNT_GAP_TOLERANCE_RATIO                # ۱۵٪ - کالکشن‌های کوچک/متوسط
    elif position <= 100_000:
        ratio = COUNT_GAP_TOLERANCE_RATIO * 0.6           # ~۹٪ - کالکشن‌های بزرگ
    else:
        ratio = COUNT_GAP_TOLERANCE_RATIO * 0.4           # ~۶٪ - کالکشن‌های خیلی بزرگ (۵۰۰-۶۰۰ هزار+)
    return min(
        max(COUNT_GAP_TOLERANCE, int(position * ratio)),
        COUNT_GAP_TOLERANCE_MAX,
    )


def _adaptive_window_probe_cap(position: int) -> int:
    """
    سقف تعداد آیتمی که واقعاً در هر پنجره fetch می‌شود (WINDOW_PROBE_CAP)
    را متناسب با موقعیت فعلی adaptive می‌کند. برای کالکشن‌های کوچک همون
    سقف پایه (سریع‌ترین حالت) کافیه. برای موقعیت‌های خیلی عمیق (کالکشن‌های
    چند صدهزارتایی)، چون تعداد کل پنجره‌هایی که جستجوی نمایی/دودویی باز
    می‌کند در عمل خیلی کم (لگاریتمی) است، افزایش اندک سقف نمونه‌برداری دقت
    مرز تشخیص‌داده‌شده را بالا می‌برد بدون اثر محسوس روی سرعت کلی.
    """
    if position <= 50_000:
        return WINDOW_PROBE_CAP
    elif position <= 200_000:
        return int(WINDOW_PROBE_CAP * 1.5)
    return WINDOW_PROBE_CAP * 2
# مسیر دیتابیس: روی Railway حتماً روی Volume بگذار (مثلاً NFT_CACHE_DB=/data/nft_cache.db)
# تا با هر Redeploy پاک نشود. اگر فایل نباشد، از seed فشردهٔ nft_cache.db.gz کنار اسکریپت ساخته می‌شود.
DB_PATH = os.getenv("NFT_CACHE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "nft_cache.db"))
ADMIN_IDS = {7416102956}               # آیدی عددی تلگرام ادمین(ها) - دسترسی به پنل مدیریت فقط برای این آیدی‌ها

# Seed فشرده (gzip) برای اولین اجرا روی Volume خالی — در ریپو نگه‌داری می‌شود (~۹MB)
SEED_DB_GZ = os.getenv(
    "NFT_CACHE_SEED_GZ",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "nft_cache.db.gz"),
)


def _ensure_db_parent_and_seed() -> None:
    """
    پوشهٔ والد DB را می‌سازد. اگر فایل DB وجود نداشته باشد (یا خالی باشد)
    و seed فشرده موجود باشد، آن را استخراج می‌کند تا کش اولیه از دست نرود.
    بعد از استخراج، WAL/SHM جداگانه ساخته می‌شوند؛ نیازی به کپی آن‌ها نیست.
    """
    import gzip
    import shutil

    parent = os.path.dirname(os.path.abspath(DB_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)

    need_seed = (not os.path.isfile(DB_PATH)) or os.path.getsize(DB_PATH) == 0
    if not need_seed:
        return

    if not os.path.isfile(SEED_DB_GZ):
        # بدون seed هم _init_db جداول خالی می‌سازد؛ فقط کش قبلی را نخواهی داشت
        return

    tmp_path = DB_PATH + ".seed.tmp"
    try:
        with gzip.open(SEED_DB_GZ, "rb") as src, open(tmp_path, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        os.replace(tmp_path, DB_PATH)
        # اگر از seed قدیمی WAL/SHM مانده، پاک کن تا SQLite تمیز شروع کند
        for suffix in ("-wal", "-shm", "-journal"):
            p = DB_PATH + suffix
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
        print(
            f"[seed] دیتابیس اولیه از {SEED_DB_GZ} استخراج شد → {DB_PATH} ({size_mb:.1f} MB)",
            flush=True,
        )
    except Exception as e:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        print(
            f"[seed] استخراج seed ناموفق بود ({e})؛ با دیتابیس خالی ادامه می‌دهیم.",
            flush=True,
        )


# ---- تنظیمات کارت تصویری خروجی (عکس شبیه کارت GRAM/USD اما برای هر NFT) ----
# پوشه‌ای که عکس اصلی هر «مدل» یک‌بار در آن دانلود و برای همیشه نگه‌داری می‌شود
# (دفعه‌های بعد هیچ دانلود جدیدی لازم نیست؛ مستقیم از همین فایل خوانده می‌شود).
def _default_images_dir() -> str:
    # کنار دیتابیس تا اگر Volume روی مسیر DB باشد، عکس‌ها هم ماندگار شوند
    db = os.getenv("NFT_CACHE_DB")
    if db:
        return os.path.join(os.path.dirname(os.path.abspath(db)), "nft_images")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "nft_images")

IMAGES_DIR = os.getenv("NFT_IMAGES_DIR", _default_images_dir())
os.makedirs(IMAGES_DIR, exist_ok=True)

CARD_WIDTH = 1000
CARD_HEIGHT = 640
CARD_MAX_PATTERNS_PER_SCAN = 6    # سقف تعداد عکس‌کارتی که برای یک نتیجه اسکن ساخته/ارسال می‌شود
# قبلاً ۱۲ بود؛ هر کارت یعنی یک دانلود/رندر تصویر (I/O + CPU برای PIL) که
# می‌تونست کارت‌های اول اسکن رو کند کنه، به‌خصوص وقتی کارت‌ها هنوز کش نشده
# بودند و باید یک‌بار از شبکه دانلود می‌شدند. ۵-۶ کارت هم از نظر تجربه
# کاربری (چت پر از عکس نشدن) هم از نظر سرعت پاسخ نهایی بهینه‌تره.
FONT_DIR = "/usr/share/fonts/truetype/dejavu"

# ضریب اندازه‌ی فونت‌های کارت خروجی (عکس نتیجه). هر چند وقت یک‌بار خواسته
# می‌شه که «همه‌ی متن‌ها/عددهای کارت رو بزرگ‌تر کن» - به‌جای اینکه هر بار
# تک‌تک اندازه‌ها (name_font, ratio_font, stat_num_font, ...) توی
# build_card_image دستکاری بشه، فقط همین یک عدد رو تغییر بده: مثلاً ۱.3
# یعنی همه‌ی فونت‌های کارت ۳۰٪ بزرگ‌تر از حالت پایه می‌شوند.
CARD_FONT_SCALE = 1.3

# ---- تنظیمات پیش‌فرض (قابل تغییر از پنل ادمین، ذخیره در دیتابیس) ----
DEFAULT_SETTINGS = {
    "force_sub_enabled": "0",
    "force_sub_channel": "",
    "force_sub_text": "🔒 برای استفاده از ربات، ابتدا باید در کانال زیر عضو شوی:",
    "force_sub_button": "📢 عضویت در کانال",
    "max_concurrent": str(MAX_CONCURRENT_REQUESTS),
    "max_per_second": str(MAX_REQUESTS_PER_SECOND),
    "credit_price_ton": "0.2",
    "charge_auto_button": "💎 پرداخت با Tonkeeper",
    "charge_manual_button": "🧾 پرداخت دستی + ارسال رسید",
    "charge_auto_button_id": "",
    "charge_manual_button_id": "",
    # ایموجی‌های ثابتِ خودِ ربات که روی دکمه‌های شیشه‌ای «انتخاب مدل/بک‌گراند»
    # نشون داده می‌شن (نه ایموجی اختصاصیِ هر مدل، بلکه علامتِ خودِ حالت
    # انتخاب‌شده/انتخاب‌نشده و قفلِ تایید). از پنل ادمین قابل تغییرن؛ چون
    # InlineKeyboardButton فقط متن ساده می‌پذیرد (نه HTML/entity)، حتی اگر
    # ادمین یک ایموجی پرمیوم واقعی بفرسته، فقط فالبکِ ساده‌ی همون ایموجی
    # ذخیره و روی دکمه نشون داده می‌شود - این محدودیتِ خودِ Bot API تلگرامه.
    "mark_selected": "🟩",
    "mark_unselected": "🟥",
    "mark_confirm": "🔒",
    # کدِ ایموجی پرمیومِ اختصاصی (custom_emoji_id) برای همون سه حالتِ بالا -
    # وقتی خالیه یعنی ادمین هنوز ایموجی پرمیومی ثبت نکرده و فقط همون
    # فالبکِ سادهٔ بالا روی دکمه نشون داده می‌شه. وقتی پر باشه، به‌جای فالبکِ
    # متنی، از icon_custom_emoji_id (آیکونِ واقعیِ ایموجی پرمیوم روی خودِ
    # دکمه، طبق Bot API 9.4+) استفاده می‌شه.
    "mark_selected_id": "",
    "mark_unselected_id": "",
    "mark_confirm_id": "",
}

# ------------------------------------------------------------------------- #
#  متن‌های («بنر»های) قابل‌تنظیم از پنل ادمین
# ------------------------------------------------------------------------- #
#  هر پیامی که ربات مستقیم به کاربر عادی نشون می‌ده، اینجا به‌جای اینکه
#  توی کد هارد-کد باشه، یک کلید (msg_...) داره که مقدارش هم مثل بقیه‌ی
#  تنظیمات (force_sub_text و ...) توی همون جدول settings ذخیره می‌شه.
#  یعنی همون مکانیزم قبلی (runtime_settings + db_set_setting) رو برای
#  همه‌ی پیام‌ها هم استفاده می‌کنیم؛ فقط یک لایه‌ی نگاشت روی‌شون گذاشته شده
#  (MSG_TEMPLATES) که هم برچسب فارسی/توضیح هر پیام رو نگه می‌داره، هم
#  متغیرهای قابل‌استفاده (place-holder) داخلش رو.
#
#  نکته‌ی مهم درباره‌ی ایموجی پرمیوم: چون این پیام‌ها با parse_mode=HTML
#  ارسال می‌شن، اگر متنی که ادمین می‌فرسته شامل ایموجی پرمیوم تلگرام باشه،
#  خود aiogram موقع خوندن پیام ادمین (message.html_text) اون ایموجی رو
#  خودکار به تگ <tg-emoji emoji-id="...">📌</tg-emoji> تبدیل می‌کنه؛ یعنی
#  ادمین لازم نیست هیچ کد عددی‌ای رو دستی وارد کنه - کافیه از کیبورد
#  ایموجی پرمیوم تلگرام (در اکانتی که پرمیومه) انتخابش کنه و پیام رو
#  همون‌طوری که هست برای ربات بفرسته.
MSG_TEMPLATES: dict[str, dict] = {
    "msg_start": {
        "label": "🔎 پیام شروع (/start)",
        "vars": [],
        "default": (
            "🔎 <b>NFT Collection Scanner</b>\n\n"
            "لینک پایه کالکشن رو بفرست، مثلاً:\n"
            "<code>https://t.me/nft/CollectionName-</code>"
        ),
    },
    "msg_main_menu": {"label": "🏠 پیام منوی اصلی (/start)", "vars": ["free_remaining", "free_limit", "balance", "credit_price"], "default": ("🎛 <b>منوی اصلی</b>\n\n🔎 سرچ رایگان امروز: <b>{free_remaining}</b> / {free_limit}\n💳 موجودی سرچ: <b>{balance}</b>\n💰 قیمت هر اعتبار: <b>{credit_price} TON</b>\n\nیک گزینه را انتخاب کن:")},
    "msg_charge_menu": {"label": "💳 پیام صفحه شارژ موجودی", "vars": ["credit_price"], "default": ("💳 <b>افزایش موجودی</b>\n\nقیمت هر اعتبار: <b>{credit_price} TON</b>\nمقدار TON و اعتبار از همین قیمت محاسبه می‌شوند.\n\n💎 مقدار موردنظر را انتخاب کن:")},
    "msg_charge_details": {"label": "💎 پیام جزئیات شارژ", "vars": ["ton", "credits"], "default": ("💳 <b>جزئیات شارژ</b>\n\n🔹 <b>{ton} TON</b> پرداخت می‌کنی\n🤖 <b>{credits} اعتبار</b> دریافت می‌کنی\n\nیکی از روش‌های پرداخت را انتخاب کن:")},
    "msg_charge_auto": {"label": "💎 پیام پرداخت خودکار Tonkeeper", "vars": ["ton", "credits", "comment"], "default": ("💎 <b>پرداخت خودکار TON</b>\n\n💰 مبلغ: <b>{ton} TON</b>\n🤖 اعتبار پس از تأیید: <b>{credits}</b>\n🧾 کد پرداخت: <code>{comment}</code>\n\nدر Tonkeeper تراکنش را تأیید کن و برگرد.\nربات تراکنش واقعی را بررسی می‌کند و بعد موجودی را خودکار اضافه می‌کند.")},
    "msg_profile_admin": {
        "label": "👤 پروفایل (ادمین)",
        "vars": [],
        "default": "👤 <b>پروفایل شما</b>\n\n🛡 شما ادمین هستید و محدودیت روزانه ندارید.",
    },
    "msg_profile_user_header": {
        "label": "👤 پروفایل (کاربر عادی - سرتیتر)",
        "vars": ["used", "limit", "remaining"],
        "default": (
            "👤 <b>پروفایل شما</b>\n\n"
            "📊 فیلترهای امروز: {used}/{limit}\n"
            "✅ باقی‌مانده امروز: {remaining}\n\n"
        ),
    },
    "msg_profile_remaining": {
        "label": "👤 پروفایل - وقتی سهمیه باقی مانده",
        "vars": ["support_username"],
        "default": "برای سهمیه‌ی بیشتر می‌تونی به @{support_username} پیام بدی.",
    },
    "msg_profile_exceeded": {
        "label": "👤 پروفایل - وقتی سهمیه تمام شده",
        "vars": ["limit", "support_username"],
        "default": (
            "سهمیه‌ی امروزت تمام شده؛ فردا دوباره {limit}تا صفر می‌شه.\n"
            "برای فیلتر بیشتر همین امروز، پیام بده به:\n👉 @{support_username}"
        ),
    },
    "msg_quota_exceeded": {
        "label": "⛔ پیام اتمام سهمیه‌ی روزانه",
        "vars": ["used", "limit", "support_username"],
        "default": (
            "⛔ سهمیه‌ی رایگان امروزت تمام شده ({used}/{limit} فیلتر).\n\n"
            "سهمیه‌ی هر کاربر روزانه {limit} بار فیلتر کردن است و فردا دوباره صفر می‌شود.\n"
            "اگر همین امروز به فیلتر بیشتری نیاز داری، به آیدی زیر پیام بده:\n"
            "👉 @{support_username}"
        ),
    },
    "msg_invalid_link": {
        "label": "❌ لینک نامعتبر",
        "vars": [],
        "default": "❌ فرمت لینک درست نیست. دوباره امتحان کن (مثال: https://t.me/nft/Name-)",
    },
    "msg_no_items_found": {
        "label": "⚠️ هیچ آیتم معتبری پیدا نشد",
        "vars": [],
        "default": "⚠️ هیچ آیتم معتبری زیر این لینک پیدا نشد. لینک پایه رو چک کن یا دوباره تلاش کن.",
    },
    "msg_no_traits_found": {
        "label": "⚠️ هیچ مدل/بک‌گراندی پیدا نشد",
        "vars": [],
        "default": "⚠️ نتونستم ساختار ویژگی‌ها رو پیدا کنم. لینک پایه رو چک کن یا دوباره تلاش کن.",
    },
    "msg_discovery_complete": {
        "label": "✅ پیام پایان کشف مدل/بک‌گراند",
        "vars": ["patterns_count", "colors_count", "sampled", "total_available"],
        "default": (
            "✅ کشف کامل شد: {patterns_count} مدل و {colors_count} بک‌گراند پیدا شد "
            "(از {sampled} آیتم نمونه‌برداری‌شده).\n"
            "📦 تعداد واقعی کل کالکشن: {total_available} آیتم.\n\n"
            "حالا سقف تعداد آیتم برای اسکن نهایی رو بفرست (عدد، حداکثر {total_available})، "
            "یا از دکمه زیر برای اسکن کامل کالکشن ({total_available} آیتم) استفاده کن:"
        ),
    },
    "msg_pattern_prompt": {
        "label": "🎨 پیام «انتخاب مدل»",
        "vars": ["trait_list"],
        "default": "🎨 مدل‌های (Pattern) موجود رو انتخاب کن (چندتایی مجاز است):{trait_list}",
    },
    "msg_color_prompt": {
        "label": "🌈 پیام «انتخاب بک‌گراند»",
        "vars": ["trait_list"],
        "default": "🎨 رنگ‌های پس‌زمینه (Backdrop) موجود رو انتخاب کن:{trait_list}",
    },
    "msg_result_summary": {
        "label": "📦 خلاصه‌ی نتیجه‌ی اسکن",
        "vars": ["valid_count", "scanned_count"],
        "default": "📦 نتیجه اسکن: {valid_count} آیتم معتبر از {scanned_count} بررسی‌شده",
    },
    "msg_signature": {
        "label": "✍️ امضای انتهای پیام‌ها",
        "vars": [],
        "default": "\n\n— @MITARSOM | HITL",
    },
    "msg_no_valid_after_filter": {
        "label": "⚠️ هیچ نتیجه‌ای با فیلتر انتخابی پیدا نشد",
        "vars": [],
        "default": "هیچ آیتم معتبری با فیلترهای انتخابی پیدا نشد.",
    },
    "msg_discovering_count_start": {
        "label": "⏳ پیام شروع پیدا کردن تعداد کالکشن",
        "vars": [],
        "default": "⏳ در حال پیدا کردن تعداد واقعی آیتم‌های کالکشن...",
    },
    "msg_locked_collection_used": {
        "label": "🔒 پیام استفاده از کالکشن قفل‌شده (کش)",
        "vars": ["cached_total", "patterns_count", "colors_count"],
        "default": (
            "🔒 این کالکشن قفل است - از داده‌های کش‌شده استفاده شد (بعد از چک آیتم‌های جدید).\n"
            "📦 تعداد کالکشن: {cached_total} آیتم.\n"
            "🎨 {patterns_count} مدل و {colors_count} بک‌گراند پیدا شد."
        ),
    },
    "msg_discovering_count_progress": {
        "label": "⏳ پیشرفت پیدا کردن تعداد کالکشن",
        "vars": ["found_so_far"],
        "default": (
            "⏳ در حال پیدا کردن تعداد واقعی کالکشن...\n"
            "تا الان حداقل {found_so_far} آیتم معتبر پیدا شده (در حال جستجوی مرز دقیق)..."
        ),
    },
    "msg_count_found_discovering_traits": {
        "label": "✅ پیام پیدا شدن تعداد کالکشن + شروع کشف ویژگی‌ها",
        "vars": ["total_available"],
        "default": (
            "✅ تعداد واقعی کالکشن پیدا شد: {total_available} آیتم.\n\n"
            "⏳ حالا در حال کشف مدل‌ها و بک‌گراندهای موجود..."
        ),
    },
    "msg_discovering_traits_progress": {
        "label": "⏳ پیشرفت کشف مدل/بک‌گراند",
        "vars": ["scanned_so_far", "patterns_count", "colors_count"],
        "default": (
            "⏳ در حال کشف ویژگی‌ها... {scanned_so_far} آیتم بررسی شد\n"
            "مدل‌های پیدا‌شده: {patterns_count} | بک‌گراندهای پیدا‌شده: {colors_count}"
        ),
    },
    "msg_scan_all_prompt": {
        "label": "👇 پیام کوتاه بالای دکمه «اسکن همه»",
        "vars": [],
        "default": "👇",
    },
    "msg_invalid_max_items_number": {
        "label": "❌ عدد سقف آیتم نامعتبر است",
        "vars": [],
        "default": "❌ لطفاً یک عدد معتبر بفرست یا از دکمه «اسکن همه» استفاده کن.",
    },
    "msg_max_items_out_of_range": {
        "label": "❌ عدد سقف آیتم خارج از بازه است",
        "vars": ["real_cap"],
        "default": "❌ عدد باید بین ۱ و {real_cap} باشه (تعداد واقعی این کالکشن).",
    },
    "msg_scan_all_activated": {
        "label": "✅ فعال شدن حالت «اسکن همه»",
        "vars": ["max_items"],
        "default": "✅ حالت «اسکن همه NFT های موجود» فعال شد ({max_items} آیتم).",
    },
    "msg_pattern_confirm_min_one": {
        "label": "⚠️ هشدار انتخاب حداقل یک مدل",
        "vars": [],
        "default": "حداقل یک مدل انتخاب کن.",
    },
    "msg_pattern_all_disabled_by_color": {
        "label": "⚠️ هشدار تداخل «انتخاب همه مدل» با «انتخاب همه بک‌گراند»",
        "vars": [],
        "default": "چون «انتخاب همه بک‌گراند» فعاله، نمی‌تونی «انتخاب همه مدل» رو هم بزنی.",
    },
    "msg_color_confirm_min_one": {
        "label": "⚠️ هشدار انتخاب حداقل یک بک‌گراند",
        "vars": [],
        "default": "حداقل یک رنگ انتخاب کن.",
    },
    "msg_color_all_disabled_by_pattern": {
        "label": "⚠️ هشدار تداخل «انتخاب همه بک‌گراند» با «انتخاب همه مدل»",
        "vars": [],
        "default": "چون «انتخاب همه مدل» فعاله، نمی‌تونی «انتخاب همه بک‌گراند» رو هم بزنی.",
    },
    "msg_scan_starting": {
        "label": "🚀 پیام شروع اسکن نهایی",
        "vars": [],
        "default": "🚀 شروع اسکن موازی... این ممکنه کمی طول بکشه.",
    },
    "msg_scan_fast_cached": {
        "label": "⚡️ پیام استفاده از کالکشن کامل کش‌شده",
        "vars": [],
        "default": "⚡️ این کالکشن قبلاً کامل کش شده - جست‌وجوی سریع در دیتابیس...",
    },
    "msg_scan_progress": {
        "label": "🚀 پیشرفت اسکن",
        "vars": ["scanned", "total"],
        "default": "🚀 در حال اسکن... {scanned}/{total} بررسی شد.",
    },
    "msg_scan_retry_failed": {
        "label": "🔁 پیام بازبینی آیتم‌های ناموفق",
        "vars": ["failed_count"],
        "default": "🔁 بازبینی {failed_count} آیتمی که با خطای گذرا مواجه شده بودن...",
    },
    "msg_all_patterns_label": {
        "label": "🧩 برچسب «همه مدل‌ها» در خلاصه نتیجه",
        "vars": [],
        "default": "همه مدل‌ها",
    },
    "msg_all_colors_label": {
        "label": "🎨 برچسب «همه بک‌گراندها» در خلاصه نتیجه",
        "vars": [],
        "default": "همه بک‌گراندها",
    },
    "msg_scan_result_header": {
        "label": "📦 سرتیتر پیام نتیجه نهایی اسکن",
        "vars": ["pattern_summary", "color_summary", "result_summary"],
        "default": (
            "🧩 مدل انتخاب‌شده: {pattern_summary}\n"
            "🎨 بک‌گراند انتخاب‌شده: {color_summary}\n\n"
            "{result_summary}\n\n"
        ),
    },
    "msg_force_sub_alert": {
        "label": "⛔ پاپ‌آپ «اول عضو کانال شو»",
        "vars": [],
        "default": "⛔ ابتدا باید در کانال عضو شوی.",
    },
    "msg_checkjoin_confirmed": {
        "label": "✅ پاپ‌آپ تایید عضویت کانال",
        "vars": [],
        "default": "✅ عضویت تایید شد.",
    },
    "msg_checkjoin_not_yet": {
        "label": "⛔ پاپ‌آپ «هنوز عضو نشدی»",
        "vars": [],
        "default": "⛔ هنوز عضو کانال نشدی.",
    },
    "msg_admin_access_denied": {
        "label": "⛔ پیام عدم دسترسی به پنل ادمین",
        "vars": [],
        "default": "⛔ شما به پنل مدیریت دسترسی نداری.",
    },
}


class _SafeFormatDict(dict):
    """اگر یک متغیر داخل قالب ادمین وجود نداشته باشه، به‌جای کرش فقط دست‌نخورده می‌مونه."""

    def __missing__(self, key):
        return "{" + key + "}"


def get_msg(key: str, **kwargs) -> str:
    """
    متن نهاییِ یک «بنر»/پیام قابل‌تنظیم را برمی‌گرداند: اول از تنظیمات
    ذخیره‌شده در دیتابیس (runtime_settings) و اگر ادمین هیچ‌وقت تغییرش
    نداده، از مقدار پیش‌فرض MSG_TEMPLATES. جایگزینی place-holder ها با
    str.format_map انجام می‌شود و اگر ادمین متنی با آکولاد اضافه/اشتباه
    وارد کرده باشد، کرش نمی‌کند (فقط همان‌طور که هست می‌ماند).
    """
    template = runtime_settings.get(key)
    if template is None:
        template = MSG_TEMPLATES.get(key, {}).get("default", "")
    try:
        return template.format_map(_SafeFormatDict(**kwargs))
    except Exception:
        return template


runtime_settings: dict[str, str] = dict(DEFAULT_SETTINGS)
for _k, _v in MSG_TEMPLATES.items():
    DEFAULT_SETTINGS[_k] = _v["default"]
    runtime_settings[_k] = _v["default"]


def get_max_concurrent() -> int:
    """سقف درخواست هم‌زمان فعلی را (از تنظیمات لود شده در دیتابیس یا پیش‌فرض) برمی‌گرداند."""
    try:
        value = int(runtime_settings.get("max_concurrent", str(MAX_CONCURRENT_REQUESTS)))
    except (TypeError, ValueError):
        return MAX_CONCURRENT_REQUESTS
    return min(max(value, MIN_ALLOWED_CONCURRENT), MAX_ALLOWED_CONCURRENT)


def get_max_per_second() -> int:
    """سقف نرخ درخواست در ثانیه فعلی را (از تنظیمات لود شده در دیتابیس یا پیش‌فرض) برمی‌گرداند."""
    try:
        value = int(runtime_settings.get("max_per_second", str(MAX_REQUESTS_PER_SECOND)))
    except (TypeError, ValueError):
        return MAX_REQUESTS_PER_SECOND
    return min(max(value, MIN_ALLOWED_PER_SECOND), MAX_ALLOWED_PER_SECOND)


def get_credit_price_ton() -> float:
    try:
        return max(float(runtime_settings.get("credit_price_ton", "0.2")), 1e-9)
    except (TypeError, ValueError):
        return 0.2

def credits_for_ton(ton: int | float) -> int:
    return max(1, int(float(ton) / get_credit_price_ton()))


# ---- ایموجی‌های دکمه‌های toggle ---- #
# این سه کلید، مقادیرِ آیکونِ دقیقاً دو دکمه‌ی کاربر رو مشخص می‌کنن:
# «انتخاب همه» (که دو حالت فعال/غیرفعال داره -> mark_selected/mark_unselected)
# و «تایید و ادامه» (mark_confirm). قابل‌تغییر از پنل ادمین > 🎛 ایموجی
# دکمه‌های انتخاب.
_TOGGLE_MARK_LABELS = {
    "mark_selected": "حالتِ «فعال» دکمه‌ی «انتخاب همه»",
    "mark_unselected": "حالتِ «غیرفعال» دکمه‌ی «انتخاب همه»",
    "mark_confirm": "دکمه‌ی «تایید و ادامه»",
}


def get_toggle_mark(key: str) -> str:
    return runtime_settings.get(key, DEFAULT_SETTINGS.get(key, "")) or DEFAULT_SETTINGS.get(key, "")


def get_toggle_mark_id(key: str) -> Optional[str]:
    """custom_emoji_id ایموجیِ پرمیومِ اختصاصیِ ثبت‌شده برای این کلید (اگر ادمین ست کرده باشد)، وگرنه None."""
    return runtime_settings.get(f"{key}_id", "") or None


# ------------------------------------------------------------------------- #
#  Regexهای پیش‌کامپایل‌شده (بهینه‌سازی سرعت پارسینگ)
# ------------------------------------------------------------------------- #
#  قبلاً خیلی از این الگوها هر بار که extract_traits/is_page_valid/... روی
#  یک صفحه‌ی جدید صدا زده می‌شدند، از نو (با re.search/re.findall با یک
#  رشته‌ی خام) کامپایل می‌شدند. چون این توابع می‌تونن برای صدها هزار صفحه
#  در یک اسکن بزرگ صدا زده بشن، این کامپایل تکراری هزینه‌ی قابل‌توجهی
#  (هرچند کوچیک در هر بار، ولی ضرب در تعداد آیتم‌ها) داشت. حالا همه یک‌بار
#  در سطح ماژول کامپایل می‌شن و در همه‌جا از همون نمونه‌ی کامپایل‌شده
#  استفاده می‌شه.
RE_SCRIPT_BLOCK = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)
RE_JSON_CANDIDATE = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}")
RE_PERCENT_SUFFIX = re.compile(r"\s*(?:[<>‹›←→«»]+\s*)?\d+(?:\.\d+)?%\s*(?:[<>‹›←→«»]+\s*)*$")
# -- اصلاح باگ تشخیص اشتباه مدل/رنگ (حرف تنها مثل "V"، فضای اضافه) --
# قبلاً این دو regex نه حساسیت به حروف کوچک/بزرگ (re.I) داشتند و نه هیچ
# مرزی برای توقف؛ کلاس کاراکتری [A-Za-z0-9 _\-]+ فضا رو هم مجاز می‌دونه،
# پس اگر بعد از "Model:" بلافاصله برچسب بعدی (مثلاً "Backdrop") هم توی
# همون رشته‌ی متنی می‌اومد (بدون جداکننده‌ی سخت)، این رجکس به اشتباه تا
# وسط اسم بک‌گراند هم جلو می‌رفت و یک مقدار به‌هم‌چسبیده/زباله برمی‌گشت.
# حالا: (۱) با re.I به هر دو حالت حروف حساسیت داره، (۲) با \b کنار برچسب
# از افتادن وسط یک کلمه‌ی دیگه (مثلاً "Modeled") جلوگیری می‌شه، (۳) با
# lookahead غیرحریص، دقیقاً قبل از اولین برچسب شناخته‌شده‌ی بعدی (یا پایان
# متن) متوقف می‌شه؛ یعنی دیگه به مقدار برچسب بعدی نمی‌چسبه.
_KNOWN_LABEL_ALTERNATION = r"Model|Pattern|Backdrop|Background|Color|Quantity|Rarity"
RE_TEXT_PATTERN = re.compile(
    rf"\b(?:Model|Pattern)\b\s*(?::|\|)\s*(.+?)(?=\s+\b(?:{_KNOWN_LABEL_ALTERNATION})\b\s*(?::|\|)|\s*$)",
    re.I,
)
RE_TEXT_COLOR = re.compile(
    rf"\b(?:Backdrop|Background|Color)\b\s*(?::|\|)\s*(.+?)(?=\s+\b(?:{_KNOWN_LABEL_ALTERNATION})\b\s*(?::|\|)|\s*$)",
    re.I,
)

# ------------------------------------------------------------------------- #
#  سیستم لاگ (پیشرفته‌شده)
# ------------------------------------------------------------------------- #
# قبلاً فقط روی کنسول لاگ می‌رفت و اگر پروسه‌ی پایتون بسته/کرش می‌کرد
# (مثلاً روی هاست/سرور بدون ترمینال باز، یا وقتی کنسول اسکرول‌بک محدودی
# داره) همه‌ی اون تاریخچه از دست می‌رفت. حالا:
#   - همه‌چیز هم روی کنسول (stdout) و هم روی یک فایل چرخشی (bot.log) لاگ
#     می‌شود؛ فایل حداکثر ۱۰ مگابایت و تا ۵ نسخه‌ی قدیمی نگه‌داری می‌شود.
#   - هر Exception مدیریت‌نشده (چه در حلقه‌ی اصلی asyncio، چه در یک تسک
#     پس‌زمینه، چه هر خطای دیگری که کل پروسه رو می‌ترکونه) قبل از crash
#     با traceback کامل لاگ می‌شود؛ دیگه هیچ‌وقت ربات «بی‌صدا» نمی‌میره.
LOG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

_log_formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_log_formatter)

_file_handler = RotatingFileHandler(
    LOG_FILE_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
log = logging.getLogger("nft_scanner_bot")


def _log_uncaught_exception(exc_type, exc_value, exc_tb):
    """هر Exception ای که تا بالای پروسه پایتون بره و هیچ‌جا catch نشده باشه
    (یعنی داره کل ربات رو می‌ترکونه) رو قبل از خروج، با traceback کامل لاگ می‌کنه."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    log.critical("خطای مدیریت‌نشده - ربات در حال کرش کردن است:", exc_info=(exc_type, exc_value, exc_tb))


sys.excepthook = _log_uncaught_exception


def _asyncio_exception_handler(loop, context):
    """هر Exception ای که داخل یک تسک/کال‌بک asyncio رخ بده و awaited/handle نشده باشه
    (که به‌طور پیش‌فرض فقط یک warning ریز می‌ده و راحت گم می‌شه) رو کامل با traceback لاگ می‌کند."""
    exc = context.get("exception")
    message = context.get("message", "خطای مدیریت‌نشده در حلقه‌ی asyncio")
    if exc is not None:
        log.error(f"{message}", exc_info=exc)
    else:
        log.error(f"{message} | context: {context}")


class ProgressThrottle:
    """
    قبلاً فرکانس ادیت پیام‌های پیشرفت («در حال اسکن... x/y») بر اساس تعداد
    آیتم (مثلاً هر batch_size*2 آیتم) تنظیم می‌شد؛ وقتی سقف همزمانی/نرخ از
    پنل ادمین پایین بود، batch_size کوچیک می‌شد و همین باعث می‌شد edit_text
    خیلی مکرر (چند بار در ثانیه) صدا زده بشه - که هم روی ریت‌لیمیت خود
    تلگرام فشار می‌آورد (ممکنه با "flood control" مواجه بشیم) و هم هزینه‌ی
    شبکه/CPU غیرضروری داشت، چون از دید کاربر فرقی نمی‌کنه پیام هر ۰.۵ ثانیه
    آپدیت بشه یا هر ۳ ثانیه. حالا صرف‌نظر از batch_size، حداقل یک فاصله‌ی
    زمانی (پیش‌فرض ۳ ثانیه) بین دو ادیت پی‌درپی تضمین می‌شود.
    """

    def __init__(self, min_interval: float = 3.0):
        self.min_interval = min_interval
        self._last = 0.0

    def ready(self) -> bool:
        now = time.monotonic()
        if now - self._last >= self.min_interval:
            self._last = now
            return True
        return False


async def safe_edit(message: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    """
    مثل message.edit_text ولی امن: تلگرام وقتی متن/کیبورد جدید دقیقاً با
    متن/کیبورد فعلی پیام یکسان باشه، خطای "message is not modified" برمی‌گردونه.
    قبلاً خیلی از هندلرهای پنل ادمین (مثلاً «➕ اضافه کردن» -> «🔙 بازگشت»،
    یا رفرش کردن یک کالکشن و بعد برگشتن) این خطا رو مدیریت نمی‌کردن -> با
    یک کلیک عادی، آپدیت با Exception هندل‌نشده شکست می‌خورد و از دید کاربر
    دکمه اصلاً کار نمی‌کرد / ربات آن قسمت رو "خراب" می‌کرد.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        log.exception("خطای غیرمنتظره در ادیت پیام")
    except Exception:
        log.exception("خطای غیرمنتظره در ادیت پیام")


# ---- ارسال/ادیتِ کیبوردهای toggle با fallback خودکار برای آیکونِ پرمیوم ---- #
# icon_custom_emoji_id (آیکون ایموجی پرمیومِ واقعی روی دکمه) فقط وقتی کار
# می‌کنه که اکانتِ صاحبِ ربات پرمیوم باشه (یا ربات یوزرنیم Fragment داشته
# باشه). اگه این شرط برقرار نباشه، تلگرام درخواست رو با خطا رد می‌کنه. برای
# اینکه این حالت باعث خراب‌شدن کل جریان ربات نشه، اولین باری که این خطا رخ
# بده، به‌صورت خودکار (و فقط همون یک‌بار) بدون آیکون دوباره امتحان می‌کنیم و
# از اون به بعد (تا ری‌استارت بعدی ربات) دیگه اصلاً آیکون رو امتحان نمی‌کنیم -
# این‌طوری هم آیکون وقتی واقعاً کار می‌کنه استفاده می‌شه، هم اگه کار نکنه
# ربات هیچ‌وقت برای کاربر خراب/بی‌پاسخ نمی‌مونه.
_premium_icon_buttons_supported: Optional[bool] = None


def _is_icon_related_error(exc: TelegramBadRequest) -> bool:
    msg = str(exc).lower()
    return "icon" in msg or "custom_emoji" in msg or "emoji" in msg or "premium" in msg


async def _send_with_toggle_kb(send_func, text: str, options, selected, prefix: str, **kb_kwargs):
    """ارسال یک پیام *جدید* همراه با toggle keyboard (با fallback خودکار آیکون)."""
    global _premium_icon_buttons_supported
    try_icons = _premium_icon_buttons_supported is not False
    kb = build_toggle_keyboard(options, selected, prefix, use_icons=try_icons, **kb_kwargs)
    try:
        await send_func(text, reply_markup=kb)
        if try_icons and _premium_icon_buttons_supported is None:
            _premium_icon_buttons_supported = True
    except TelegramBadRequest as e:
        if try_icons and _is_icon_related_error(e):
            log.warning("آیکون ایموجی پرمیوم روی دکمه‌ها پشتیبانی نشد (احتمالاً اکانت ربات پرمیوم/فرگمنت نیست)؛ از این به بعد غیرفعال شد.")
            _premium_icon_buttons_supported = False
            kb = build_toggle_keyboard(options, selected, prefix, use_icons=False, **kb_kwargs)
            await send_func(text, reply_markup=kb)
        else:
            raise


async def _edit_text_with_toggle_kb(message: Message, text: str, options, selected, prefix: str, **kb_kwargs):
    """مثل safe_edit ولی مخصوص toggle keyboard: هم «پیام تغییری نداشت» رو مدیریت می‌کنه، هم fallback آیکون رو."""
    global _premium_icon_buttons_supported
    try_icons = _premium_icon_buttons_supported is not False
    kb = build_toggle_keyboard(options, selected, prefix, use_icons=try_icons, **kb_kwargs)
    try:
        await message.edit_text(text, reply_markup=kb)
        if try_icons and _premium_icon_buttons_supported is None:
            _premium_icon_buttons_supported = True
        return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        if try_icons and _is_icon_related_error(e):
            log.warning("آیکون ایموجی پرمیوم روی دکمه‌ها پشتیبانی نشد؛ از این به بعد غیرفعال شد.")
            _premium_icon_buttons_supported = False
        else:
            log.exception("خطای غیرمنتظره در ادیت پیام (toggle keyboard)")
            return
    except Exception:
        log.exception("خطای غیرمنتظره در ادیت پیام (toggle keyboard)")
        return
    kb = build_toggle_keyboard(options, selected, prefix, use_icons=False, **kb_kwargs)
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e2:
        if "message is not modified" not in str(e2).lower():
            log.exception("خطای غیرمنتظره در ادیت پیام (toggle keyboard, بدون آیکون)")
    except Exception:
        log.exception("خطای غیرمنتظره در ادیت پیام (toggle keyboard, بدون آیکون)")


async def _edit_markup_with_toggle_kb(message: Message, options, selected, prefix: str, **kb_kwargs):
    """مثل بالا ولی فقط reply_markup رو ادیت می‌کنه (نه متن پیام)."""
    global _premium_icon_buttons_supported
    try_icons = _premium_icon_buttons_supported is not False
    kb = build_toggle_keyboard(options, selected, prefix, use_icons=try_icons, **kb_kwargs)
    try:
        await message.edit_reply_markup(reply_markup=kb)
        if try_icons and _premium_icon_buttons_supported is None:
            _premium_icon_buttons_supported = True
        return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        if try_icons and _is_icon_related_error(e):
            log.warning("آیکون ایموجی پرمیوم روی دکمه‌ها پشتیبانی نشد؛ از این به بعد غیرفعال شد.")
            _premium_icon_buttons_supported = False
        else:
            log.exception("خطای غیرمنتظره در ادیت کیبورد (toggle keyboard)")
            return
    except Exception:
        log.exception("خطای غیرمنتظره در ادیت کیبورد (toggle keyboard)")
        return
    kb = build_toggle_keyboard(options, selected, prefix, use_icons=False, **kb_kwargs)
    try:
        await message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest as e2:
        if "message is not modified" not in str(e2).lower():
            log.exception("خطای غیرمنتظره در ادیت کیبورد (toggle keyboard, بدون آیکون)")
    except Exception:
        log.exception("خطای غیرمنتظره در ادیت کیبورد (toggle keyboard, بدون آیکون)")


# ------------------------------------------------------------------------- #
#  وضعیت‌های مکالمه (FSM) - سشن جداگانه به‌ازای هر کاربر
# ------------------------------------------------------------------------- #

class ScanFlow(StatesGroup):
    waiting_base_link = State()
    waiting_max_items = State()
    waiting_pattern_choice = State()
    waiting_color_choice = State()
    ready_to_run = State()


class CollectionFlow(StatesGroup):
    waiting_search = State()
    waiting_backdrop = State()
    waiting_keyword = State()


class ChargeFlow(StatesGroup):
    waiting_receipt = State()
    waiting_manual_receipt = State()


class AdminFlow(StatesGroup):
    waiting_add_link = State()             # منتظر لینک پایه برای «اضافه کردن» کالکشن جدید
    waiting_broadcast_text = State()       # منتظر متن پیام همگانی
    waiting_force_sub_channel = State()    # منتظر آیدی/لینک کانال عضویت اجباری
    waiting_force_sub_text = State()       # منتظر متن پیام عضویت اجباری
    waiting_max_concurrent = State()       # منتظر عدد جدید سقف درخواست هم‌زمان
    waiting_max_per_second = State()       # منتظر عدد جدید سقف نرخ درخواست در ثانیه
    waiting_msg_text = State()             # منتظر متن جدید یکی از پیام‌های («بنر»های) قابل‌تنظیم
    waiting_trait_emoji = State()          # منتظر پیام حاوی ایموجی پرمیوم برای یک مدل/بک‌گراند
    waiting_toggle_mark_emoji = State()    # منتظر ایموجی جدید برای علامت انتخاب‌شده/نشده/قفلِ دکمه‌ها
    waiting_credit_user = State()          # منتظر آیدی کاربر برای شارژ موجودی
    waiting_credit_price = State()          # منتظر قیمت اعتبار به TON
    waiting_credit_amount = State()        # منتظر تعداد GRAM برای واریز
    waiting_button_text = State()            # منتظر متن جدید دکمه‌های پرداخت
    waiting_button_emoji = State()           # منتظر ایموجی پرمیوم دکمه‌های پرداخت


# ------------------------------------------------------------------------- #
#  ساختار داده هر آیتم
# ------------------------------------------------------------------------- #

@dataclass
class ItemResult:
    url: str
    valid: bool
    pattern: Optional[str] = None
    color: Optional[str] = None


@dataclass
class SessionData:
    base_link: str = ""
    max_items: int = 0
    scan_all_mode: bool = False
    total_available: int = 0       # تعداد واقعی کالکشن که با جستجوی خودکار پیدا شده (0 یعنی هنوز پیدا نشده)
    available_patterns: set = field(default_factory=set)
    available_colors: set = field(default_factory=set)
    selected_patterns: set = field(default_factory=set)
    selected_colors: set = field(default_factory=set)
    select_all_patterns: bool = False
    select_all_colors: bool = False
    pattern_emoji: dict = field(default_factory=dict)   # نام مدل -> (emoji_id, fallback) پرمیوم اختصاصی
    color_emoji: dict = field(default_factory=dict)     # نام بک‌گراند -> (emoji_id, fallback) پرمیوم اختصاصی


# ------------------------------------------------------------------------- #
#  محدودکننده نرخ درخواست (Token-bucket ساده) + سمافور همزمانی
# ------------------------------------------------------------------------- #

class RateLimiter:
    """
    سقف تعداد درخواست در ثانیه را به‌صورت دقیق کنترل می‌کند.

    قبلاً هر بار acquire() صدا زده می‌شد، کل لیست timestamp ها (که می‌تونست
    تا max_per_second - یعنی تا ۵۰۰۰ - آیتم داشته باشه) از نو با یک list
    comprehension بازسازی می‌شد؛ چون این کار زیر یک قفل مشترک برای *همه‌ی*
    درخواست‌های هم‌زمان اجرا می‌شد، دقیقاً در بالاترین سقف همزمانی (جایی که
    باید سریع‌ترین حالت باشه) خودش تنگنا (bottleneck) می‌شد. حالا با
    deque و پاک کردن O(1) از جلو (چون تایم‌استمپ‌ها صعودی اضافه می‌شن)،
    این عملیات به‌طور محسوسی سریع‌تره.
    """

    def __init__(self, max_per_second: int):
        self.base_max_per_second = max_per_second  # سقف «هدف» که از پنل ادمین تنظیم شده
        self.max_per_second = max_per_second       # سقف مؤثر فعلی (ممکنه موقتاً کمتر از base باشه)
        self._min_per_second = max(1, max_per_second // 8)  # هیچ‌وقت پایین‌تر از این نصف نمی‌شه
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        # ---- adaptive rate limiting (نصف کردن روی 429/5xx) ------------- #
        # وقتی سایت هدف با 429 (Too Many Requests) یا 5xx موقت جواب بده،
        # یعنی داریم بیش از حد فشار می‌آریم؛ قبلاً سقف نرخ ثابت می‌موند و
        # صرفاً fetch() تلاش مجدد می‌کرد - این باعث می‌شد ریت‌لیمیت سایت
        # هدف مدت طولانی‌تری فعال بمونه (چون همچنان با همون نرخ بالا کوبیده
        # می‌شد). حالا با هر برخورد به 429/5xx سقف مؤثر نصف می‌شه (تا کران
        # پایین) و بعد به‌مرور (هر چند ثانیه که برخورد جدیدی نبود) دوباره
        # به سمت سقف اصلی بازیابی می‌شه.
        self._penalty_cooldown = 5.0     # حداقل فاصله بین دو «نصف کردن» پیاپی
        self._recovery_interval = 10.0   # هر چند ثانیه یک قدم ریکاوری تدریجی
        self._last_penalty_time = 0.0
        self._last_recovery_time = time.monotonic()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            # ریکاوری تدریجی: اگر مدتیه تنبیه جدیدی نبوده و هنوز زیر سقف
            # اصلی هستیم، کمی سقف مؤثر رو بالا می‌بریم (نه یک‌باره، تا اگر
            # ریت‌لیمیت سایت هدف هنوز فعاله دوباره فوراً بهش برنخوریم).
            if (
                self.max_per_second < self.base_max_per_second
                and now - self._last_recovery_time >= self._recovery_interval
            ):
                self.max_per_second = min(
                    self.base_max_per_second, int(self.max_per_second * 1.5) + 1
                )
                self._last_recovery_time = now
            while self._timestamps and now - self._timestamps[0] >= 1.0:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_per_second:
                sleep_for = 1.0 - (now - self._timestamps[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
            self._timestamps.append(time.monotonic())

    def penalize(self):
        """با دریافت 429/5xx از سایت هدف صدا زده می‌شود: سقف نرخ مؤثر فعلی
        را نصف می‌کند (تا کران پایین _min_per_second) تا فشار روی سایت هدف
        کم شده و ریت‌لیمیت موقت زودتر برطرف شود. یک cooldown کوتاه هم دارد
        تا با یک دسته درخواست هم‌زمان که همگی 429 بگیرند، سقف چندین بار
        پشت‌سرهم (و بیش از حد) نصف نشود."""
        now = time.monotonic()
        if now - self._last_penalty_time < self._penalty_cooldown:
            return
        self._last_penalty_time = now
        self.max_per_second = max(self._min_per_second, self.max_per_second // 2)


class AsyncScanner:
    """اسکن موازی صفحات t.me/nft/... با کنترل دقیق همزمانی و نرخ درخواست."""

    def __init__(self, max_concurrent: int, max_per_second: int, timeout: int):
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.rate_limiter = RateLimiter(max_per_second)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        """یک سشن HTTP دائمی با connection-pool می‌سازد (keep-alive -> سرعت بالاتر).
        کنترل «معمول» همزمانی بر عهده‌ی self.semaphore است که از پنل ادمین قابل
        تغییر است (بدون نیاز به ری‌استارت سشن)؛ ولی چون آن مقدار می‌تواند از پنل
        تا ۵۰۰۰ تنظیم شود، اینجا یک سقف سخت و مطلق هم در سطح خودِ connector
        می‌گذاریم (HARD_CONNECTOR_LIMIT/HARD_CONNECTOR_LIMIT_PER_HOST) تا حتی یک
        تنظیم اشتباه از پنل ادمین هم نتواند دستگاه/شبکه را با تعداد بی‌رویه
        کانکشن هم‌زمان از پا دربیاورد (قبلاً limit=0 یعنی بدون هیچ سقفی بود)."""
        connector = aiohttp.TCPConnector(
            limit=HARD_CONNECTOR_LIMIT,
            limit_per_host=HARD_CONNECTOR_LIMIT_PER_HOST,
            ttl_dns_cache=300,
        )
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (compatible; NFTScannerBot/1.0)"},
            connector=connector,
        )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def update_limits(self, max_concurrent: int, max_per_second: int):
        """سقف همزمانی/نرخ را در زمان اجرا (بدون ری‌استارت ربات) تغییر می‌دهد.
        سمافور و ریت‌لیمیتر جدید فقط روی fetch()های بعدی اعمال می‌شوند؛
        درخواست‌هایی که همین الان در حال اجرا هستند با نمونه‌ی قبلی تمام می‌شوند."""
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.rate_limiter = RateLimiter(max_per_second)

    async def fetch(self, url: str) -> tuple[Optional[str], bool]:
        """
        یک صفحه را می‌گیرد. روی خطای شبکه/تایم‌اوت یا کدهای موقتی (429/5xx)
        چند بار تلاش مجدد می‌کند تا این خطاهای گذرا با «صفحه واقعاً ناموجود»
        اشتباه گرفته نشوند (که باعث توقف زودهنگام و شمارش غلط کل کالکشن می‌شد).

        خروجی: (html یا None, is_network_error)
        """
        net_err = False
        for attempt in range(RETRY_ATTEMPTS + 1):
            await self.rate_limiter.acquire()
            async with self.semaphore:
                try:
                    async with self._session.get(url, timeout=self.timeout) as resp:
                        if resp.status == 200:
                            return await resp.text(), False
                        if resp.status in (429, 500, 502, 503, 504):
                            net_err = True
                            # سایت هدف داره می‌گه «دارید زیاد فشار می‌آرید» -
                            # سقف نرخ فعلی رو adaptive نصف می‌کنیم (به‌جای
                            # اینکه فقط منتظر بمونیم و دوباره با همون نرخ بالا
                            # بکوبیم) تا خودمون رو زودتر با ظرفیت واقعی سایت
                            # هدف هماهنگ کنیم.
                            self.rate_limiter.penalize()
                            if attempt < RETRY_ATTEMPTS:
                                await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                                continue
                            return None, True
                        # کد دیگر (مثلاً 403/404 واقعی) -> خطای گذرا نیست
                        return None, False
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    net_err = True
                    if attempt < RETRY_ATTEMPTS:
                        await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                        continue
                    return None, True
        return None, net_err

    async def scan_many(self, urls: list[str]) -> list[tuple[str, Optional[str], bool]]:
        tasks = [self.fetch(u) for u in urls]
        fetched = await asyncio.gather(*tasks)
        return [(u, html, net_err) for u, (html, net_err) in zip(urls, fetched)]


# ------------------------------------------------------------------------- #
#  کش دیتابیس (SQLite) - جلوگیری از سرچ دوباره‌ی چیزی که قبلاً چک شده
# ------------------------------------------------------------------------- #
#  هر آیتم (base_link + شماره) که یک بار fetch و بررسی بشه، نتیجه‌اش
#  (معتبر بودن + پترن + رنگ) در یک فایل SQLite کنار اسکریپت ذخیره می‌شود.
#  دفعه بعد -- چه همون کاربر دوباره همین کالکشن رو بزنه، چه کاربر دیگه‌ای --
#  قبل از هر درخواست شبکه، اول از دیتابیس چک می‌شود؛ اگر جواب اونجا بود از
#  همون استفاده می‌شود (بدون درخواست HTTP)، اگر نبود روال معمول (fetch واقعی
#  + ذخیره در دیتابیس برای دفعه بعد) طی می‌شود.

import threading

_db_local = threading.local()  # هر ترد (thread) کانکشن مجزای خودش را دارد


class _NoOpAsyncLock:
    """
    قبلاً یک asyncio.Lock سراسری اینجا بود که عملاً هر عملیات دیتابیس -
    حتی یک SELECT ریز از یک کاربر - را پشت عملیات دیتابیس *همه* کاربران
    دیگر (مثلاً یک INSERT دسته‌ای چند هزار ردیفی از یک اسکن بزرگ) صف
    می‌کرد. این دقیقاً چیزی بود که باعث می‌شد وقتی چند نفر هم‌زمان درخواست
    می‌دادند، یا حتی وقتی یک کالکشن از قبل کامل کش شده بود، خوندن دیتابیس
    کند/معلق به‌نظر برسه.

    حالا هر ترد کانکشن SQLite مخصوص خودش را (در حالت WAL) دارد؛ WAL به‌طور
    ذاتی از چندین خوانندهٔ هم‌زمان + یک نویسنده پشتیبانی می‌کند و با
    busy_timeout هم نویسنده‌های هم‌زمان به‌جای خطا دادن، کوتاه صبر می‌کنند.
    پس این قفل پایتونی سراسری لازم نیست؛ برای اینکه ساختار کد (async with
    _db_lock) دست‌نخورده بماند، یک قفل کاملاً بدون‌عملیات (no-op) گذاشته شده.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


_db_lock = _NoOpAsyncLock()


def _get_conn() -> sqlite3.Connection:
    """کانکشن SQLite مخصوص ترد فعلی را برمی‌گرداند (و اگر لازم بود می‌سازد)."""
    conn = getattr(_db_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        # --------------------------------------------------------------- #
        # تنظیمات کارایی: صرف‌نظر از اینکه دیتابیس چقدر سنگین بشه، خوندن
        # آیتم‌ها باید همیشه فوق‌العاده سریع بمونه.
        #   - WAL: خواندن و نوشتن هم‌زمان بدون قفل شدن رو هم
        #   - synchronous=NORMAL: هماهنگ با WAL، سرعت بالا بدون ریسک واقعی
        #   - cache_size بزرگ + mmap: بخش زیادی از دیتابیس در RAM/آدرس‌دهی مستقیم
        #   - temp_store=MEMORY: عملیات موقت (مرتب‌سازی و...) در RAM
        #   - busy_timeout: اگر دو نویسنده هم‌زمان به دیتابیس برخوردن، به‌جای
        #     خطای فوری «database is locked»، تا ۳۰ ثانیه صبر می‌کنه (این
        #     صبر توی یک ترد جداست، حلقه‌ی اصلی asyncio رو بلاک نمی‌کنه)
        # --------------------------------------------------------------- #
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-131072")   # ~128MB کش صفحات
        conn.execute("PRAGMA mmap_size=268435456")  # 256MB memory-mapped I/O
        conn.execute("PRAGMA busy_timeout=30000")
        _db_local.conn = conn
    return conn




class SQLiteFSMStorage(BaseStorage):
    """
    Persistent FSM storage backed by the bot's existing SQLite database.

    This intentionally uses the same DB that is already placed on Railway's
    persistent Volume, so FSM state/data survive container restarts without
    introducing a second external service such as Redis.
    """

    @staticmethod
    def _key_id(key: StorageKey) -> str:
        fields = getattr(key, "_fields", ())
        if fields:
            values = [getattr(key, name, None) for name in fields]
        else:
            # Compatible fallback for aiogram versions whose StorageKey is not
            # a named tuple with _fields exposed.
            values = [
                getattr(key, "bot_id", None),
                getattr(key, "chat_id", None),
                getattr(key, "user_id", None),
                getattr(key, "destiny", None),
                getattr(key, "business_connection_id", None),
                getattr(key, "thread_id", None),
            ]
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str)

    async def set_state(self, key: StorageKey, state: Optional[str] = None) -> None:
        key_id = self._key_id(key)

        def _run():
            conn = _get_conn()
            conn.execute(
                """
                INSERT INTO fsm_storage (key_id, state, data, updated_at)
                VALUES (?, ?, COALESCE((SELECT data FROM fsm_storage WHERE key_id=?), '{}'), ?)
                ON CONFLICT(key_id) DO UPDATE SET
                    state=excluded.state,
                    updated_at=excluded.updated_at
                """,
                (key_id, state, key_id, int(time.time())),
            )
            conn.commit()

        async with _db_lock:
            await asyncio.to_thread(_run)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        key_id = self._key_id(key)

        def _run():
            row = _get_conn().execute(
                "SELECT state FROM fsm_storage WHERE key_id=?", (key_id,)
            ).fetchone()
            return row[0] if row else None

        async with _db_lock:
            return await asyncio.to_thread(_run)

    async def set_data(self, key: StorageKey, data: dict) -> None:
        key_id = self._key_id(key)
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)

        def _run():
            conn = _get_conn()
            conn.execute(
                """
                INSERT INTO fsm_storage (key_id, state, data, updated_at)
                VALUES (?, COALESCE((SELECT state FROM fsm_storage WHERE key_id=?), NULL), ?, ?)
                ON CONFLICT(key_id) DO UPDATE SET
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (key_id, key_id, encoded, int(time.time())),
            )
            conn.commit()

        async with _db_lock:
            await asyncio.to_thread(_run)

    async def get_data(self, key: StorageKey) -> dict:
        key_id = self._key_id(key)

        def _run():
            row = _get_conn().execute(
                "SELECT data FROM fsm_storage WHERE key_id=?", (key_id,)
            ).fetchone()
            if not row or not row[0]:
                return {}
            try:
                value = json.loads(row[0])
                return value if isinstance(value, dict) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                log.exception("Invalid FSM data in SQLite for key %s", key_id)
                return {}

        async with _db_lock:
            return await asyncio.to_thread(_run)

    async def close(self) -> None:
        # The application's SQLite connection lifecycle is managed by the
        # existing DB layer. There is no separate network resource here.
        return None


def _init_db():
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fsm_storage (
            key_id TEXT PRIMARY KEY,
            state TEXT,
            data TEXT NOT NULL DEFAULT '{}',
            updated_at INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            base_link TEXT NOT NULL,
            idx INTEGER NOT NULL,
            valid INTEGER NOT NULL,
            pattern TEXT,
            color TEXT,
            checked_at INTEGER NOT NULL,
            PRIMARY KEY (base_link, idx)
        ) WITHOUT ROWID
        """
    )
    # WITHOUT ROWID یعنی خود کلید اصلی (base_link, idx) یک ایندکس خوشه‌ای
    # (clustered) هست و جدول مستقیماً بر اساس همون مرتب شده -> برای الگوی
    # اصلی خوندن ما (WHERE base_link=? AND idx IN (...)) سریع‌ترین حالت
    # ممکنه، حتی وقتی دیتابیس به میلیون‌ها ردیف برسه.

    # ایندکس دوم مخصوص فیلتر مدل/بک‌گراند: بدون این، برای پیدا کردن یک
    # مدل خاص باید کل کالکشن (حتی اگر ۵۰۰ هزار تا باشه) خونده و در پایتون
    # چک بشه. با این ایندکس، SQLite مستقیم می‌ره سر ردیف‌های مچ - جواب
    # صرف‌نظر از حجم دیتابیس تقریباً آنی (چند میلی‌ثانیه) میاد.
    _get_conn().execute(
        "CREATE INDEX IF NOT EXISTS idx_items_filter ON items (base_link, valid, pattern, color)"
    )

    # ایندکس دوم مخصوص (base_link, pattern, color) بدون قید valid: برای
    # کوئری‌هایی که فقط بر اساس مدل/بک‌گراند گروه‌بندی/شمارش می‌کنند (مثلاً
    # شمارش سریع تعداد آیتم هر مدل صرف‌نظر از معتبر بودن، یا آمار پنل ادمین
    # در آینده) - بدون این، SQLite برای این‌جور کوئری‌ها به ایندکس ترکیبی
    # idx_items_filter برمی‌گشت که چون ستون valid رو هم اول داره، برای
    # کوئری‌هایی که valid رو فیلتر نمی‌کنند به همون اندازه مؤثر نیست.
    _get_conn().execute(
        "CREATE INDEX IF NOT EXISTS idx_items_pattern_color ON items (base_link, pattern, color)"
    )

    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS collections (
            base_link TEXT PRIMARY KEY,
            total_available INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID
        """
    )
    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS user_balance (
            user_id INTEGER PRIMARY KEY,
            searches INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS ton_payments (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            ton_amount INTEGER NOT NULL,
            credit_amount INTEGER NOT NULL,
            comment TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            tx_id TEXT,
            created_at INTEGER NOT NULL,
            paid_at INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID
        """
    )
    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS admin_added_collections (
            base_link TEXT PRIMARY KEY,
            total_items INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'running',
            added_at INTEGER NOT NULL,
            last_refreshed_at INTEGER NOT NULL,
            search_locked INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID
        """
    )
    # مهاجرت برای دیتابیس‌های قدیمی‌تر که هنوز ستون search_locked رو ندارند
    # (قفل سرچ مخصوص هر کالکشن - قبلاً این قفل یک سوییچ سراسری توی تنظیمات
    # اسکنر بود؛ حالا مخصوص هر لینک/کالکشن، از همون صفحه‌ی جزئیات کالکشن
    # در «افزودن» قابل قفل/باز کردنه).
    try:
        _get_conn().execute(
            "ALTER TABLE admin_added_collections ADD COLUMN search_locked INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass  # ستون از قبل وجود دارد
    # کش «کارت تصویری» هر مدل (pattern) از هر کالکشن: عکس اصلی همان مدل که
    # یک‌بار از صفحه‌ی خودِ آن آیتم دانلود شده + پالت رنگ استخراج‌شده از آن
    # عکس + رنگ کادر که یک‌بار به‌صورت رندوم از همان پالت انتخاب شده. با
    # وجود این کش، دفعه‌ی بعد (برای هر کاربری) هیچ دانلود/پردازش عکسی لازم
    # نیست و ساخت کارت خروجی تقریباً آنی انجام می‌شود.
    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS nft_media (
            base_link TEXT NOT NULL,
            pattern TEXT NOT NULL,
            image_path TEXT NOT NULL,
            colors TEXT NOT NULL,
            border_color TEXT NOT NULL,
            cached_at INTEGER NOT NULL,
            PRIMARY KEY (base_link, pattern)
        ) WITHOUT ROWID
        """
    )
    # کش «کارت تصویری» مخصوص یک ترکیب دقیق مدل+بک‌گراند با هم (وقتی کاربر
    # هم مدل و هم بک‌گراند رو به‌صورت تکی/غیر «همه» انتخاب کرده باشه) - جدا
    # از nft_media که فقط بر اساس مدل (صرف‌نظر از بک‌گراند) کش می‌کنه.
    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS nft_media_combo (
            base_link TEXT NOT NULL,
            pattern TEXT NOT NULL,
            color TEXT NOT NULL,
            image_path TEXT NOT NULL,
            colors TEXT NOT NULL,
            border_color TEXT NOT NULL,
            cached_at INTEGER NOT NULL,
            PRIMARY KEY (base_link, pattern, color)
        ) WITHOUT ROWID
        """
    )
    # سهمیه‌ی روزانه‌ی هر کاربر برای «فیلتر کردن» (تعداد دفعاتی که در همون
    # روز تقویمی یک اسکن/فیلتر جدید شروع کرده). هر روز که عوض بشه، شمارنده
    # به‌صورت خودکار (چون quota_date با امروز فرق می‌کنه) از نو صفر می‌شه.
    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS user_quota (
            user_id INTEGER PRIMARY KEY,
            quota_date TEXT NOT NULL,
            used_count INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID
        """
    )
    # ایموجی پرمیوم اختصاصی هر مدل (pattern) / بک‌گراند (color) - به تفکیک
    # هر کالکشن (base_link). emoji_id همون custom_emoji_id تلگرامه که موقع
    # فوروارد/ارسال پیام حاوی ایموجی پرمیوم توسط ادمین، خودکار از
    # message.entities استخراج می‌شود (ادمین لازم نیست خودش این عدد رو
    # بداند). emoji_fallback همون کاراکتر یونیکد معمولی‌ایه که تلگرام برای
    # سازگاری همراه هر ایموجی پرمیوم می‌فرستد؛ چون دکمه‌های شیشه‌ای
    # (InlineKeyboardButton) هیچ فرمت/entity ای پشتیبانی نمی‌کنند (محدودیت
    # ذاتی Bot API تلگرام) و نمی‌شه ایموجی پرمیوم واقعی/انیمیشنی رو داخل
    # متن یک دکمه نشون داد، همین fallback ساده به‌عنوان پیشوند متن دکمه
    # استفاده می‌شود؛ نسخه‌ی واقعی/پرمیوم (تگ <tg-emoji>) در متن پیامی که
    # بالای همون کیبورد ارسال می‌شود نمایش داده می‌شود.
    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS trait_emoji (
            base_link TEXT NOT NULL,
            trait_type TEXT NOT NULL,
            trait_value TEXT NOT NULL,
            emoji_id TEXT NOT NULL,
            emoji_fallback TEXT NOT NULL,
            PRIMARY KEY (base_link, trait_type, trait_value)
        ) WITHOUT ROWID
        """
    )
    _get_conn().commit()
    log.info(f"دیتابیس کش در مسیر زیر آماده شد: {DB_PATH}")


async def db_load_settings():
    """تنظیمات ذخیره‌شده (مثل عضویت اجباری) را از دیتابیس در حافظه لود می‌کند."""
    def _run():
        cur = _get_conn().execute("SELECT key, value FROM settings")
        return dict(cur.fetchall())

    async with _db_lock:
        stored = await asyncio.to_thread(_run)
    runtime_settings.update({**DEFAULT_SETTINGS, **stored})


async def db_set_setting(key: str, value: str):
    def _run():
        _get_conn().execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)
    runtime_settings[key] = value


# ---- کالکشن‌های اضافه‌شده توسط ادمین (بخش «اضافه کردن») ------------------- #

async def db_upsert_admin_collection(base_link: str, total_items: int, status: str):
    now = int(time.time())

    def _run():
        _get_conn().execute(
            """
            INSERT INTO admin_added_collections (base_link, total_items, status, added_at, last_refreshed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(base_link) DO UPDATE SET
                total_items=excluded.total_items,
                status=excluded.status,
                last_refreshed_at=excluded.last_refreshed_at
            """,
            (base_link, total_items, status, now, now),
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def db_get_admin_collections() -> list[tuple]:
    def _run():
        cur = _get_conn().execute(
            "SELECT base_link, total_items, status, added_at, last_refreshed_at, search_locked "
            "FROM admin_added_collections ORDER BY added_at DESC"
        )
        return cur.fetchall()

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_admin_collection(base_link: str) -> Optional[tuple]:
    def _run():
        cur = _get_conn().execute(
            "SELECT base_link, total_items, status, added_at, last_refreshed_at, search_locked "
            "FROM admin_added_collections WHERE base_link=?",
            (base_link,),
        )
        return cur.fetchone()

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_set_collection_lock(base_link: str, locked: bool):
    """
    قفل/باز کردن سرچ مخصوص یک کالکشن خاص (نه سراسری). فقط برای کالکشن‌هایی
    معنا داره که قبلاً از پنل ادمین > افزودن، اسکن و به دیتابیس اضافه
    شده‌اند (یعنی یک ردیف توی admin_added_collections دارند).
    """
    def _run():
        _get_conn().execute(
            "UPDATE admin_added_collections SET search_locked=? WHERE base_link=?",
            (1 if locked else 0, base_link),
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def db_get_collection_lock(base_link: str) -> bool:
    """آیا سرچ این کالکشن خاص قفل است؟ اگر کالکشن اصلاً توسط ادمین اضافه نشده باشه، False."""
    def _run():
        cur = _get_conn().execute(
            "SELECT search_locked FROM admin_added_collections WHERE base_link=?", (base_link,)
        )
        row = cur.fetchone()
        return bool(row[0]) if row else False

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_delete_admin_collection(base_link: str):
    """
    حذف کامل و همه‌جانبه‌ی یک کالکشن از دیتابیس (نه فقط از لیست «افزودن»):
        - ردیف آن در admin_added_collections (خودِ لیست)
        - تمام آیتم‌های اسکن‌شده‌اش در items
        - رکورد تعداد کل در collections
        - کش کارت‌های تصویری مربوطه در nft_media و nft_media_combo

    چرا حذف *کامل* (نه فقط حذف از لیست)؟ چون اگر فقط ردیف
    admin_added_collections پاک می‌شد ولی آیتم‌های قدیمی توی items
    می‌موندن، دفعه‌ی بعد که همین لینک دوباره از «➕ افزودن لینک جدید»
    اضافه بشه، scan_indices_cached همون دیتای قدیمی (و احتمالاً اشتباه)
    رو از کش برمی‌گردوند و عملاً هیچ اسکن واقعی جدیدی انجام نمی‌شد. با
    این حذف کامل، اضافه‌کردن دوباره‌ی همون کالکشن یعنی یک اسکن واقعاً از صفر.
    """
    def _run():
        conn = _get_conn()
        conn.execute("DELETE FROM admin_added_collections WHERE base_link=?", (base_link,))
        conn.execute("DELETE FROM items WHERE base_link=?", (base_link,))
        conn.execute("DELETE FROM collections WHERE base_link=?", (base_link,))
        conn.execute("DELETE FROM nft_media WHERE base_link=?", (base_link,))
        conn.execute("DELETE FROM nft_media_combo WHERE base_link=?", (base_link,))
        conn.commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def db_get_distinct_patterns_colors(base_link: str) -> tuple[set[str], set[str]]:
    """
    مدل‌ها/بک‌گراندهای یکتای موجود (بین آیتم‌های معتبر) این کالکشن را
    مستقیم با SQL DISTINCT (روی ایندکس idx_items_pattern_color) برمی‌گرداند.
    برای کالکشنی که قفل است (از قبل کاملاً اسکن شده)، این جایگزین حلقه‌ی
    کند «کشف ویژگی‌ها» (خواندن دسته‌به‌دسته‌ی هزاران آیتم) می‌شود: به‌جای
    عبور مرحله‌به‌مرحله از ۱ تا چند هزار آیتم، همون لحظه و با یک کوئری
    ایندکس‌شده تمام مدل‌ها/رنگ‌های موجود برمی‌گردند.
    """
    def _run():
        cur = _get_conn().execute(
            "SELECT DISTINCT pattern FROM items WHERE base_link=? AND valid=1 AND pattern IS NOT NULL",
            (base_link,),
        )
        patterns = {row[0] for row in cur.fetchall()}
        cur = _get_conn().execute(
            "SELECT DISTINCT color FROM items WHERE base_link=? AND valid=1 AND color IS NOT NULL",
            (base_link,),
        )
        colors = {row[0] for row in cur.fetchall()}
        return patterns, colors

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_set_trait_emoji(base_link: str, trait_type: str, trait_value: str, emoji_id: str, emoji_fallback: str):
    """ایموجی پرمیوم اختصاصی یک مدل/بک‌گراند مشخص را ثبت/به‌روزرسانی می‌کند."""
    def _run():
        _get_conn().execute(
            "INSERT INTO trait_emoji (base_link, trait_type, trait_value, emoji_id, emoji_fallback) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(base_link, trait_type, trait_value) DO UPDATE SET "
            "emoji_id=excluded.emoji_id, emoji_fallback=excluded.emoji_fallback",
            (base_link, trait_type, trait_value, emoji_id, emoji_fallback),
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def db_delete_trait_emoji(base_link: str, trait_type: str, trait_value: str):
    def _run():
        _get_conn().execute(
            "DELETE FROM trait_emoji WHERE base_link=? AND trait_type=? AND trait_value=?",
            (base_link, trait_type, trait_value),
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def db_get_trait_emoji_map(base_link: str, trait_type: str) -> dict[str, tuple[str, str]]:
    """{trait_value: (emoji_id, emoji_fallback)} برای همه‌ی مدل‌ها یا بک‌گراندهای این کالکشن که ادمین برایشان ایموجی گذاشته."""
    def _run():
        cur = _get_conn().execute(
            "SELECT trait_value, emoji_id, emoji_fallback FROM trait_emoji WHERE base_link=? AND trait_type=?",
            (base_link, trait_type),
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_cached_items(
    base_link: str, indices: list[int]
) -> dict[int, tuple[bool, Optional[str], Optional[str]]]:
    """
    هرچی از این اندیس‌ها که قبلاً کش شده رو برمی‌گرداند (بدون درخواست شبکه).

    توجه: به‌جای ساختن یک IN (?,?,?,...) با تک‌تک اندیس‌ها (که وقتی
    همزمانی از پنل ادمین روی مقادیر بالا مثل ۵۰۰۰ تنظیم بشه و batch_size
    به ده‌ها هزار برسه، ممکنه از سقف تعداد پارامتر SQLite رد بشه و باعث
    خطا/توقف ناقص اسکن کالکشن‌های بزرگ بشه)، مستقیم بین کمترین و بیشترین
    اندیس این دسته با BETWEEN کوئری می‌زنیم و فیلتر نهایی (محدود کردن به
    همون اندیس‌های دقیق) در پایتون انجام می‌شه. سریع‌تر و بدون هیچ سقفی.
    """
    if not indices:
        return {}
    wanted = set(indices)
    lo, hi = min(indices), max(indices)

    def _run():
        cur = _get_conn().execute(
            "SELECT idx, valid, pattern, color FROM items WHERE base_link=? AND idx BETWEEN ? AND ?",
            (base_link, lo, hi),
        )
        return {
            row[0]: (bool(row[1]), row[2], row[3])
            for row in cur.fetchall()
            if row[0] in wanted
        }

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_store_items(base_link: str, rows: list[tuple[int, bool, Optional[str], Optional[str]]]):
    """ذخیره دسته‌ای نتایج تازه fetch‌شده در دیتابیس."""
    if not rows:
        return
    now = int(time.time())

    def _run():
        # حتی اگر یکی از لایه‌های parser مقدار خام/نیمه‌تمیز برگرداند،
        # مرز نهایی DB همین‌جاست: درصد rarity و markup هرگز نباید بخشی از
        # نام Model/Backdrop ذخیره شود. این باعث می‌شود «آپدیت» و اسکن‌های
        # عادی هر دو دقیقاً یک canonical value تولید کنند.
        clean_rows = [
            (base_link, idx, int(valid),
             _canonicalize_trait_value(pattern),
             _canonicalize_trait_value(color), now)
            for idx, valid, pattern, color in rows
        ]
        _get_conn().executemany(
            "INSERT OR REPLACE INTO items (base_link, idx, valid, pattern, color, checked_at) VALUES (?,?,?,?,?,?)",
            clean_rows,
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def db_get_cached_total(base_link: str) -> Optional[int]:
    def _run():
        cur = _get_conn().execute("SELECT total_available FROM collections WHERE base_link=?", (base_link,))
        row = cur.fetchone()
        return row[0] if row else None

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_store_total(base_link: str, total: int):
    def _run():
        _get_conn().execute(
            "INSERT OR REPLACE INTO collections (base_link, total_available, updated_at) VALUES (?,?,?)",
            (base_link, total, int(time.time())),
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


# ---- جست‌وجوی سریع و ایندکس‌شده برای فیلتر مدل/بک‌گراند ------------------- #
#  به‌جای اینکه برای پیدا کردن یک مدل خاص کل کالکشن (حتی ۵۰۰ هزار آیتم)
#  خونده و در پایتون فیلتر بشه، این توابع مستقیم با SQL و از روی ایندکس
#  idx_items_filter جواب می‌گیرن - صرف‌نظر از حجم دیتابیس تقریباً آنی.

async def db_check_full_coverage(base_link: str, max_items: int) -> bool:
    """آیا بازه‌ی ۱..max_items برای این کالکشن قبلاً کامل در دیتابیس کش شده؟"""
    if max_items <= 0:
        return False

    def _run():
        cur = _get_conn().execute(
            "SELECT COUNT(*), COALESCE(MAX(idx), 0) FROM items WHERE base_link=? AND idx BETWEEN 1 AND ?",
            (base_link, max_items),
        )
        count, max_idx = cur.fetchone()
        # پوشش کامل: همه ایندکس‌ها ثبت شده، یا حداقل max_idx به سقف رسیده
        # و بیش از ۹۵٪ ردیف‌ها موجودند (گپ‌های خیلی کوچک)
        if count >= max_items:
            return True
        if max_idx >= max_items and count >= int(max_items * 0.95):
            return True
        return False

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_query_filtered_items(
    base_link: str,
    patterns: Optional[set[str]],
    colors: Optional[set[str]],
    max_items: Optional[int] = None,
) -> list[int]:
    """
    جست‌وجوی مستقیم و ایندکس‌شده: idx تمام آیتم‌های معتبر (valid=1) که با
    فیلترهای انتخابی مچ دارند را برمی‌گرداند - بدون خوندن یا چک کردن
    آیتم‌های نامرتبط. اگر patterns/colors خالی باشند یعنی «همه» انتخاب شده.
    """
    def _run():
        query = "SELECT idx FROM items WHERE base_link=? AND valid=1"
        params: list = [base_link]
        if max_items is not None:
            query += " AND idx BETWEEN 1 AND ?"
            params.append(max_items)
        if patterns:
            placeholders = ",".join("?" * len(patterns))
            query += f" AND pattern IN ({placeholders})"
            params.extend(patterns)
        if colors:
            placeholders = ",".join("?" * len(colors))
            query += f" AND color IN ({placeholders})"
            params.extend(colors)
        query += " ORDER BY idx"
        cur = _get_conn().execute(query, params)
        return [row[0] for row in cur.fetchall()]

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_count_filtered_items(
    base_link: str,
    patterns: Optional[set[str]],
    colors: Optional[set[str]],
    max_items: Optional[int] = None,
) -> int:
    """
    مثل db_query_filtered_items ولی به‌جای برگرداندن کل لیست idx ها (که
    برای «همه» (select_all) روی یک کالکشن بزرگ می‌تونه صدها هزار عدد باشه
    و ساختنش در پایتون هم حافظه هم زمان مصرف می‌کنه)، فقط COUNT(*) از روی
    همون ایندکس رو برمی‌گردونه - برای هرجایی که فقط «چند تا مچ شد» لازمه
    (مثلاً پیش‌نمایش سریع قبل از ساختن لیست کامل لینک‌ها)، بدون خوندن یا
    انتقال دادن ردیف‌های واقعی.
    """
    def _run():
        query = "SELECT COUNT(*) FROM items WHERE base_link=? AND valid=1"
        params: list = [base_link]
        if max_items is not None:
            query += " AND idx BETWEEN 1 AND ?"
            params.append(max_items)
        if patterns:
            placeholders = ",".join("?" * len(patterns))
            query += f" AND pattern IN ({placeholders})"
            params.extend(patterns)
        if colors:
            placeholders = ",".join("?" * len(colors))
            query += f" AND color IN ({placeholders})"
            params.extend(colors)
        cur = _get_conn().execute(query, params)
        return cur.fetchone()[0]

    async with _db_lock:
        return await asyncio.to_thread(_run)


# ---- کمکی‌های کارت تصویری (شمارش «آپگرید شده»، نمونه‌آیتم هر مدل، کش مدیا) ----

async def db_get_valid_count(base_link: str) -> int:
    """تعداد کل آیتم‌های معتبر (= آپگرید شده به NFT) که تا الان از این کالکشن در کش هستند."""
    def _run():
        cur = _get_conn().execute(
            "SELECT COUNT(*) FROM items WHERE base_link=? AND valid=1", (base_link,)
        )
        return cur.fetchone()[0]

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_invalid_indices(base_link: str, max_idx: int) -> list[int]:
    """
    لیست تمام ایندیس‌هایی که *در محدوده‌ی از قبل اسکن‌شده‌ی* این کالکشن
    (بین ۱ و max_idx) قبلاً نامعتبر (valid=0) کش شده‌اند.

    چرا این تابع لازم بود: scan_indices_cached هر ایندیسی که یک‌بار (چه
    معتبر چه نامعتبر) کش شده باشه رو دیگه هیچ‌وقت از شبکه دوباره نمی‌خونه.
    یعنی اگر یک گیفت که قبلاً «آپگرید نشده» (نامعتبر) چک شده بود، بعداً به
    NFT واقعی آپگرید/ثبت بشه، هیچ رفرشی اون رو نمی‌بینه چون از اول جزو
    «قبلاً چک‌شده» حساب میشه؛ فقط با یک fetch اجباری (scan_indices_force)
    روی همین لیست میشه دوباره بررسیش کرد - این دقیقاً همون چیزیه که قبلاً
    فقط با «حذف کامل و افزودن دوباره» ممکن بود.
    """
    def _run():
        cur = _get_conn().execute(
            "SELECT idx FROM items WHERE base_link=? AND valid=0 AND idx BETWEEN 1 AND ? ORDER BY idx",
            (base_link, max_idx),
        )
        return [r[0] for r in cur.fetchall()]

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_sample_idx_for_pattern(base_link: str, pattern: str) -> Optional[int]:
    """کوچک‌ترین idx معتبری که این pattern (مدل) را دارد - برای گرفتن عکس نمونه همان مدل."""
    def _run():
        cur = _get_conn().execute(
            "SELECT MIN(idx) FROM items WHERE base_link=? AND valid=1 AND pattern=?",
            (base_link, pattern),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_sample_idxs_for_pattern(base_link: str, pattern: str, limit: int = 5) -> list[int]:
    """
    چند idx معتبر (نه فقط کوچک‌ترین) که این pattern را دارند. قبلاً فقط از
    MIN(idx) استفاده می‌شد؛ اگر دقیقاً همون یک صفحه (به‌خاطر عکس og:image
    گم/دانلود ناموفق و ...) عکس نمی‌داد، کل کارت بدون عکس می‌ماند و هیچ
    تلاش دیگری نمی‌شد. با چند نمونه، اگر اولی جواب نداد می‌شه بقیه رو
    امتحان کرد.
    """
    def _run():
        cur = _get_conn().execute(
            "SELECT idx FROM items WHERE base_link=? AND valid=1 AND pattern=? ORDER BY idx LIMIT ?",
            (base_link, pattern, limit),
        )
        return [row[0] for row in cur.fetchall()]

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_media(base_link: str, pattern: str) -> Optional[tuple[str, list[str], str]]:
    """
    اگر کارت این مدل قبلاً ساخته/کش شده، (مسیر عکس، لیست رنگ‌ها، رنگ کادر) را
    برمی‌گرداند - ولی قبلش عکسِ کش‌شده را اعتبارسنجی می‌کند (نگاه کن به
    _validate_cached_media): اگر همین فایل قبلاً برای یک مدلِ *دیگر* از
    همین کالکشن هم استفاده شده (یعنی احتمالاً از باگ قدیمیِ «عکس عمومی/
    مشترک سایت به‌جای عکس اختصاصی مدل» به ارث رسیده)، ردیف کش را پاک
    می‌کند و None برمی‌گرداند تا فراخوان مجبور شود یک عکس واقعی و
    اختصاصی جدید دانلود کند.
    """
    def _run():
        cur = _get_conn().execute(
            "SELECT image_path, colors, border_color FROM nft_media WHERE base_link=? AND pattern=?",
            (base_link, pattern),
        )
        return cur.fetchone()

    async with _db_lock:
        row = await asyncio.to_thread(_run)
    if not row:
        return None
    image_path, colors_json, border_color = row
    try:
        colors = json.loads(colors_json)
    except Exception:
        colors = []

    if not await _validate_cached_media(base_link, pattern, image_path):
        await db_delete_media(base_link, pattern)
        return None

    return image_path, colors, border_color


async def db_delete_media(base_link: str, pattern: str):
    """پاک کردن یک ردیف مشخص از کش nft_media (وقتی تشخیص داده شد عکسش خراب/تکراری بوده)."""
    def _run():
        _get_conn().execute(
            "DELETE FROM nft_media WHERE base_link=? AND pattern=?", (base_link, pattern)
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def db_store_media(base_link: str, pattern: str, image_path: str, colors: list[str], border_color: str):
    def _run():
        _get_conn().execute(
            """
            INSERT OR REPLACE INTO nft_media (base_link, pattern, image_path, colors, border_color, cached_at)
            VALUES (?,?,?,?,?,?)
            """,
            (base_link, pattern, image_path, json.dumps(colors), border_color, int(time.time())),
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def db_get_sample_idx_for_pattern_and_color(
    base_link: str, pattern: str, color: str
) -> Optional[int]:
    """
    کوچک‌ترین idx معتبری که دقیقاً هم این pattern (مدل) و هم این color
    (بک‌گراند) را با هم دارد - برای وقتی کاربر هر دو فیلتر را تکی/غیر «همه»
    انتخاب کرده و می‌خواهیم عکس خروجی دقیقاً همون ترکیب باشه، نه صرفاً
    یک نمونه‌ی رندوم از همون مدل با هر بک‌گراندی.
    """
    def _run():
        cur = _get_conn().execute(
            "SELECT MIN(idx) FROM items WHERE base_link=? AND valid=1 AND pattern=? AND color=?",
            (base_link, pattern, color),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_sample_idxs_for_pattern_and_color(
    base_link: str, pattern: str, color: str, limit: int = 5
) -> list[int]:
    """مثل db_get_sample_idx_for_pattern_and_color ولی چند نمونه برمی‌گرداند (برای تلاش مجدد)."""
    def _run():
        cur = _get_conn().execute(
            "SELECT idx FROM items WHERE base_link=? AND valid=1 AND pattern=? AND color=? ORDER BY idx LIMIT ?",
            (base_link, pattern, color, limit),
        )
        return [row[0] for row in cur.fetchall()]

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_media_combo(
    base_link: str, pattern: str, color: str
) -> Optional[tuple[str, list[str], str]]:
    """
    اگر کارت این ترکیب دقیق مدل+بک‌گراند قبلاً ساخته/کش شده، (مسیر عکس،
    لیست رنگ‌ها، رنگ کادر) را برمی‌گرداند - با همون اعتبارسنجی ضدتکراری
    که db_get_media داره (نگاه کن به _validate_cached_media).
    """
    def _run():
        cur = _get_conn().execute(
            "SELECT image_path, colors, border_color FROM nft_media_combo WHERE base_link=? AND pattern=? AND color=?",
            (base_link, pattern, color),
        )
        return cur.fetchone()

    async with _db_lock:
        row = await asyncio.to_thread(_run)
    if not row:
        return None
    image_path, colors_json, border_color = row
    try:
        colors = json.loads(colors_json)
    except Exception:
        colors = []

    identity = f"{pattern}|{color}"
    if not await _validate_cached_media(base_link, identity, image_path):
        await db_delete_media_combo(base_link, pattern, color)
        return None

    return image_path, colors, border_color


async def db_delete_media_combo(base_link: str, pattern: str, color: str):
    """پاک کردن یک ردیف مشخص از کش nft_media_combo (وقتی تشخیص داده شد عکسش خراب/تکراری بوده)."""
    def _run():
        _get_conn().execute(
            "DELETE FROM nft_media_combo WHERE base_link=? AND pattern=? AND color=?",
            (base_link, pattern, color),
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def db_purge_duplicate_media(base_link: str) -> int:
    """
    پاکسازی یک‌باره‌ی کل کشِ این کالکشن (nft_media + nft_media_combo):
    اگه فایل عکسِ کش‌شده‌ی دو مدل/ترکیبِ *متفاوت* دقیقاً بایت‌به‌بایت
    یکی باشه، یعنی هر دو در واقع یک عکسِ عمومی/مشترکِ اشتباه رو کش کرده
    بودن (نه عکس اختصاصی خودشون - عکس واقعی هر مدل نباید با مدل دیگه
    عیناً یکی باشه)، پس هر دو (همه‌ی اعضای اون گروهِ تکراری) پاک می‌شن.

    برخلاف چکِ لحظه‌ایِ db_get_media/db_get_media_combo (که چون رجیستری
    فقط توی همون درخواست ساخته می‌شه، اولین ردیفِ هر گروهِ تکراری رو
    اشتباهاً معتبر تشخیص می‌ده)، این تابع چون همه‌ی ردیف‌های موجود رو یک‌جا
    با هم مقایسه می‌کنه، اولین عضوِ گروه تکراری رو هم درست پیدا و پاک
    می‌کنه. این تابع سبک/سریعه (فقط خوندن فایل از دیسک، بدون شبکه) و صدا
    زدنش در ابتدای هر build_nft_result_cards امنه.
    """
    def _fetch_rows():
        conn = _get_conn()
        media_rows = conn.execute(
            "SELECT pattern, image_path FROM nft_media WHERE base_link=?", (base_link,)
        ).fetchall()
        combo_rows = conn.execute(
            "SELECT pattern, color, image_path FROM nft_media_combo WHERE base_link=?", (base_link,)
        ).fetchall()
        return media_rows, combo_rows

    async with _db_lock:
        media_rows, combo_rows = await asyncio.to_thread(_fetch_rows)

    if not media_rows and not combo_rows:
        return 0

    def _hash_file(path: str) -> Optional[str]:
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception:
            return None

    # hash -> [(pattern, color_or_None)]
    hash_map: dict[str, list[tuple[str, Optional[str]]]] = {}

    for pattern, image_path in media_rows:
        h = await asyncio.to_thread(_hash_file, image_path)
        if h:
            hash_map.setdefault(h, []).append((pattern, None))
    for pattern, color, image_path in combo_rows:
        h = await asyncio.to_thread(_hash_file, image_path)
        if h:
            hash_map.setdefault(h, []).append((pattern, color))

    to_delete_media: list[str] = []
    to_delete_combo: list[tuple[str, str]] = []
    for entries in hash_map.values():
        distinct_patterns = {p for p, _ in entries}
        if len(distinct_patterns) <= 1:
            continue  # همه‌ی ردیف‌های این هش مال یه مدلن (طبیعیه - همون یه عکس اختصاصی)
        for pattern, color in entries:
            if color is None:
                to_delete_media.append(pattern)
            else:
                to_delete_combo.append((pattern, color))

    if not to_delete_media and not to_delete_combo:
        return 0

    def _delete():
        conn = _get_conn()
        for pattern in to_delete_media:
            conn.execute("DELETE FROM nft_media WHERE base_link=? AND pattern=?", (base_link, pattern))
        for pattern, color in to_delete_combo:
            conn.execute(
                "DELETE FROM nft_media_combo WHERE base_link=? AND pattern=? AND color=?",
                (base_link, pattern, color),
            )
        conn.commit()

    async with _db_lock:
        await asyncio.to_thread(_delete)

    return len(to_delete_media) + len(to_delete_combo)


async def db_store_media_combo(
    base_link: str, pattern: str, color: str, image_path: str, colors: list[str], border_color: str
):
    def _run():
        _get_conn().execute(
            """
            INSERT OR REPLACE INTO nft_media_combo
                (base_link, pattern, color, image_path, colors, border_color, cached_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (base_link, pattern, color, image_path, json.dumps(colors), border_color, int(time.time())),
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


# ---- ردیابی کاربران (برای پنل مدیریت) -------------------------------------

async def db_upsert_user(user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]):
    """هر بار کاربری با ربات تعامل کنه، اطلاعاتش ثبت/به‌روزرسانی می‌شود."""
    now = int(time.time())

    def _run():
        cur = _get_conn().execute("SELECT first_seen FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        first_seen = row[0] if row else now
        _get_conn().execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_seen, message_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                last_seen=excluded.last_seen,
                message_count=message_count + 1
            """,
            (user_id, username, first_name, last_name, first_seen, now),
        )
        _get_conn().commit()

    async with _db_lock:
        await asyncio.to_thread(_run)


async def db_get_user_count() -> int:
    def _run():
        cur = _get_conn().execute("SELECT COUNT(*) FROM users")
        return cur.fetchone()[0]

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_all_users() -> list[tuple]:
    def _run():
        cur = _get_conn().execute(
            "SELECT user_id, username, first_name, last_name, first_seen, last_seen, message_count "
            "FROM users ORDER BY last_seen DESC"
        )
        return cur.fetchall()

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_broadcast_user_ids() -> list[int]:
    """کاربرانی که ربات را حداقل یک‌بار استارت/استفاده کرده‌اند."""
    def _run():
        cur = _get_conn().execute("SELECT user_id FROM users ORDER BY user_id")
        return [int(row[0]) for row in cur.fetchall()]

    async with _db_lock:
        return await asyncio.to_thread(_run)


# ---- پروفایل / سهمیه‌ی روزانه‌ی «فیلتر کردن» هر کاربر --------------------- #
# هر کاربر عادی (غیر ادمین) روزی DAILY_FREE_FILTER_LIMIT بار می‌تواند یک
# فیلتر/اسکن جدید شروع کند. شمارنده بر اساس تاریخ تقویمی (YYYY-MM-DD به وقت
# سرور) است؛ به‌محض عوض شدن روز، quota_date با امروز یکی نیست پس رکورد قدیمی
# نادیده گرفته می‌شود و شمارش از نو از صفر شروع می‌شود - نیازی به کرون‌جاب یا
# پاک‌سازی دستی نیست.

def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


async def db_get_quota_status(user_id: int) -> tuple[int, int]:
    """(used_today, remaining_today) را برمی‌گرداند، بدون مصرف کردن سهمیه."""
    today = _today_str()

    def _run():
        cur = _get_conn().execute(
            "SELECT quota_date, used_count FROM user_quota WHERE user_id=?", (user_id,)
        )
        row = cur.fetchone()
        if not row or row[0] != today:
            return 0
        return row[1]

    async with _db_lock:
        used = await asyncio.to_thread(_run)
    return used, max(0, DAILY_FREE_FILTER_LIMIT - used)


async def db_try_consume_quota(user_id: int) -> tuple[bool, int, int]:
    """
    اگر کاربر هنوز سهمیه‌ی امروزش تمام نشده باشد، یک واحد مصرف می‌کند و
    (True, used_after, remaining_after) برمی‌گرداند. اگر تمام شده باشد،
    چیزی مصرف نمی‌کند و (False, used_before, 0) برمی‌گرداند.
    """
    today = _today_str()

    def _run():
        conn = _get_conn()
        cur = conn.execute(
            "SELECT quota_date, used_count FROM user_quota WHERE user_id=?", (user_id,)
        )
        row = cur.fetchone()
        used = row[1] if (row and row[0] == today) else 0
        if used >= DAILY_FREE_FILTER_LIMIT:
            return False, used
        used += 1
        conn.execute(
            """
            INSERT INTO user_quota (user_id, quota_date, used_count) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET quota_date=excluded.quota_date, used_count=excluded.used_count
            """,
            (user_id, today, used),
        )
        conn.commit()
        return True, used

    async with _db_lock:
        allowed, used = await asyncio.to_thread(_run)
    remaining = max(0, DAILY_FREE_FILTER_LIMIT - used) if allowed else 0
    return allowed, used, remaining


def _quota_exceeded_text(used: int) -> str:
    return get_msg(
        "msg_quota_exceeded",
        used=used,
        limit=DAILY_FREE_FILTER_LIMIT,
        support_username=SUPPORT_USERNAME,
    )


# ---- پرداخت خودکار TON -------------------------------------------------- #

async def db_create_ton_payment(user_id: int, ton_amount: int, credit_amount: int, comment: str) -> str:
    order_id = uuid.uuid4().hex[:16].upper()
    now = int(time.time())
    def _run():
        conn = _get_conn()
        conn.execute(
            "INSERT INTO ton_payments (order_id,user_id,ton_amount,credit_amount,comment,status,created_at) VALUES (?,?,?,?,?,'pending',?)",
            (order_id, user_id, ton_amount, credit_amount, comment, now),
        )
        conn.commit()
        return order_id
    async with _db_lock:
        return await asyncio.to_thread(_run)

async def db_get_pending_ton_payments() -> list[tuple]:
    def _run():
        cur = _get_conn().execute(
            "SELECT order_id,user_id,ton_amount,credit_amount,comment FROM ton_payments WHERE status='pending' ORDER BY created_at ASC LIMIT 100"
        )
        return cur.fetchall()
    async with _db_lock:
        return await asyncio.to_thread(_run)

async def db_mark_ton_payment_paid(order_id: str, tx_id: str) -> Optional[tuple[int, int]]:
    now = int(time.time())
    def _run():
        conn = _get_conn()
        cur = conn.execute("SELECT user_id, credit_amount FROM ton_payments WHERE order_id=? AND status='pending'", (order_id,))
        row = cur.fetchone()
        if not row:
            return None
        conn.execute("UPDATE ton_payments SET status='paid', tx_id=?, paid_at=? WHERE order_id=? AND status='pending'", (tx_id, now, order_id))
        conn.commit()
        return int(row[0]), int(row[1])
    async with _db_lock:
        return await asyncio.to_thread(_run)

async def _toncenter_transactions() -> list[dict]:
    params = {"address": GRAM_WALLET, "limit": 100}
    headers = {}
    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(TONCENTER_API_URL, params=params, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"TON Center HTTP {resp.status}")
            data = await resp.json()
            return data.get("result") or []

async def ton_payment_worker(bot: Bot):
    """پرداخت‌های TON را از روی بلاکچین تأیید و فقط یک‌بار به موجودی اضافه می‌کند."""
    while True:
        try:
            pending = await db_get_pending_ton_payments()
            if pending:
                transactions = await _toncenter_transactions()
                by_comment = {}
                for tx in transactions:
                    in_msg = tx.get("in_msg") or {}
                    comment = (in_msg.get("message") or "").strip()
                    if comment:
                        by_comment.setdefault(comment, []).append(tx)
                for order_id, user_id, ton_amount, credit_amount, comment in pending:
                    for tx in by_comment.get(comment, []):
                        in_msg = tx.get("in_msg") or {}
                        try:
                            value = int(in_msg.get("value") or 0)
                        except (TypeError, ValueError):
                            continue
                        if value != ton_amount * 1_000_000_000:
                            continue
                        tid = tx.get("transaction_id") or {}
                        tx_id = f"{tid.get('lt','')}:{tid.get('hash','')}"
                        if not tx_id.strip(":"):
                            tx_id = hashlib.sha256(json.dumps(tx, sort_keys=True).encode()).hexdigest()
                        paid = await db_mark_ton_payment_paid(order_id, tx_id)
                        if paid:
                            uid, credits = paid
                            new_bal = await db_add_balance(uid, credits)
                            try:
                                await bot.send_message(
                                    uid,
                                    f"✅ <b>پرداخت TON تأیید شد</b>\n\n"
                                    f"💎 پرداخت: <b>{ton_amount} TON</b>\n"
                                    f"🤖 اعتبار اضافه‌شده: <b>+{credits}</b>\n"
                                    f"💳 موجودی جدید: <b>{new_bal}</b>",
                                )
                            except Exception:
                                log.exception("ارسال تأیید پرداخت TON به کاربر ناموفق بود")
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("خطا در بررسی پرداخت‌های TON")
        await asyncio.sleep(max(5, TON_PAYMENT_POLL_SECONDS))

# ---- موجودی سرچ (GRAM) ------------------------------------------------- #

async def db_get_balance(user_id: int) -> int:
    def _run():
        cur = _get_conn().execute(
            "SELECT searches FROM user_balance WHERE user_id=?", (user_id,)
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_add_balance(user_id: int, searches: int) -> int:
    """اضافه کردن سرچ به موجودی کاربر؛ موجودی جدید را برمی‌گرداند."""
    now = int(time.time())

    def _run():
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO user_balance (user_id, searches, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                searches = user_balance.searches + excluded.searches,
                updated_at = excluded.updated_at
            """,
            (user_id, max(0, searches), now),
        )
        conn.commit()
        cur = conn.execute("SELECT searches FROM user_balance WHERE user_id=?", (user_id,))
        return int(cur.fetchone()[0])

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_try_consume_search(user_id: int) -> tuple[bool, str]:
    """
    یک سرچ مصرف می‌کند: اول سهمیه رایگان روزانه، بعد موجودی پولی.
    (True, info) یا (False, پیام خطا)
    """
    if is_admin(user_id):
        return True, "admin"

    allowed, used, remaining = await db_try_consume_quota(user_id)
    if allowed:
        return True, f"free:{remaining}"

    def _run():
        conn = _get_conn()
        cur = conn.execute("SELECT searches FROM user_balance WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        bal = int(row[0]) if row else 0
        if bal <= 0:
            return False, bal
        bal -= 1
        conn.execute(
            """
            INSERT INTO user_balance (user_id, searches, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET searches=?, updated_at=?
            """,
            (user_id, bal, int(time.time()), bal, int(time.time())),
        )
        conn.commit()
        return True, bal

    async with _db_lock:
        ok, val = await asyncio.to_thread(_run)
    if ok:
        return True, f"paid:{val}"
    return False, (
        "⛔ سهمیه رایگان امروز و موجودی سرچت تمام شده.\n\n"
        f"با دکمه «💳 شارژ موجودی» می‌تونی با ارسال GRAM به ولت شارژ کنی.\n"
        f"قیمت هر اعتبار = {get_credit_price_ton():g} TON."
    )


async def db_get_all_patterns(limit: int = 20000) -> list[str]:
    """تمام Model/Patternهای canonical موجود در دیتابیس را یک‌بار می‌گیرد."""
    def _run():
        cur = _get_conn().execute(
            """SELECT DISTINCT pattern FROM items
               WHERE valid=1 AND pattern IS NOT NULL AND TRIM(pattern) != ''
               ORDER BY pattern COLLATE NOCASE LIMIT ?""",
            (limit,),
        )
        return [r[0] for r in cur.fetchall()]
    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_items_for_patterns(
    patterns: list[str], limit_per_pattern: int = 2, total_limit: int = COLLECTION_MAX_GIFTS
) -> list[tuple[str, str, int, str]]:
    """برای چند Model، چند نمونه گیفت از بک‌گراندهای مختلف برمی‌گرداند.
    خروجی: (pattern, base_link, idx, color) و عمداً جزئیات در پیام نهایی تکرار نمی‌شوند.
    """
    if not patterns:
        return []
    patterns = patterns[:COLLECTION_MAX_MODELS]
    def _run():
        conn = _get_conn()
        out = []
        seen = set()
        for pattern in patterns:
            cur = conn.execute(
                """SELECT pattern, base_link, idx, color FROM items
                   WHERE valid=1 AND pattern=?
                   ORDER BY RANDOM() LIMIT ?""",
                (pattern, limit_per_pattern),
            )
            for row in cur.fetchall():
                key=(row[1], row[2])
                if key in seen:
                    continue
                seen.add(key)
                out.append(tuple(row))
                if len(out) >= total_limit:
                    return out
        return out
    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_all_backdrops() -> list[str]:
    def _run():
        cur = _get_conn().execute(
            """
            SELECT DISTINCT color FROM items
            WHERE valid=1 AND color IS NOT NULL AND TRIM(color) != ''
            ORDER BY color COLLATE NOCASE
            """
        )
        return [r[0] for r in cur.fetchall()]

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_patterns_by_backdrop(backdrop: str, limit: int = WEB_SEARCH_MAX_MODELS) -> list[str]:
    """
    فقط فهرست مدل‌های دارای این بک‌گراند را از دیتابیس می‌گیرد.
    نکته: این تابع موضوع/کلمه کاربر را در SQLite جستجو نمی‌کند؛ موضوع فقط روی وب
    جستجو می‌شود و بعد نتیجه وب با این فهرست تطبیق داده می‌شود.
    """
    def _run():
        cur = _get_conn().execute(
            """
            SELECT DISTINCT pattern
            FROM items
            WHERE valid=1
              AND color=?
              AND pattern IS NOT NULL
              AND TRIM(pattern) != ''
            ORDER BY pattern COLLATE NOCASE
            LIMIT ?
            """,
            (backdrop, limit),
        )
        return [r[0] for r in cur.fetchall()]

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_get_items_for_backdrop_pattern(
    backdrop: str, pattern: str, limit: int = 500
) -> list[tuple[str, str, int]]:
    """آیتم‌های دقیقاً همان مدل + همان بک‌گراند؛ بدون LIKE و wildcard."""
    def _run():
        cur = _get_conn().execute(
            """
            SELECT pattern, base_link, idx
            FROM items
            WHERE valid=1
              AND color=?
              AND pattern=?
            ORDER BY base_link, idx
            LIMIT ?
            """,
            (backdrop, pattern, limit),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]

    async with _db_lock:
        return await asyncio.to_thread(_run)


async def db_search_models_by_backdrop_keyword(
    backdrop: str, keyword: str, limit: int = 500
) -> list[tuple[str, str, int]]:
    """
    در کل دیتابیس: مدل‌هایی که نامشان شبیه keyword است و رنگ backdrop دارند.
    خروجی: لیست (pattern, base_link, idx)
    """
    kw = f"%{keyword.strip()}%"

    def _run():
        cur = _get_conn().execute(
            """
            SELECT pattern, base_link, idx FROM items
            WHERE valid=1
              AND color = ?
              AND pattern IS NOT NULL
              AND pattern LIKE ? COLLATE NOCASE
            ORDER BY pattern COLLATE NOCASE, base_link, idx
            LIMIT ?
            """,
            (backdrop, kw, limit),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]

    async with _db_lock:
        return await asyncio.to_thread(_run)


def build_main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 پیدا کردن NFT", callback_data="menu:scan")
    kb.button(text="📦 کالکشن‌ها", callback_data="menu:collections")
    kb.button(text="👤 پروفایل", callback_data="menu:profile")
    kb.button(text="💳 شارژ موجودی", callback_data="menu:charge")
    kb.adjust(1)
    return kb.as_markup()


def build_backdrops_kb(backdrops: list[str], page: int = 0, per_page: int = 12) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(backdrops) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = backdrops[page * per_page : (page + 1) * per_page]
    kb = InlineKeyboardBuilder()
    for i, name in enumerate(chunk):
        idx = page * per_page + i
        kb.button(text=name, callback_data=f"col:bd:{idx}")
    kb.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"col:bdp:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="menu:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"col:bdp:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))
    return kb.as_markup()


def _normalize_search_text(text: str) -> str:
    """نرمال‌سازی ساده برای تطبیق نام مدل با متن نتایج وب."""
    text = html_entities.unescape(text or '').lower()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _model_tokens(model: str) -> list[str]:
    normalized = _normalize_search_text(model)
    return [t for t in normalized.split() if len(t) >= 2]


def _score_model_against_web(model: str, corpus: str, query: str) -> float:
    """
    امتیازدهی محافظه‌کارانه: تطبیق دقیق نام مدل بیشترین امتیاز را دارد؛
    تطبیق توکن‌ها و عبارت‌های چندکلمه‌ای امتیاز کمتری می‌گیرند.
    """
    m = _normalize_search_text(model)
    c = _normalize_search_text(corpus)
    q = _normalize_search_text(query)
    if not m or not c:
        return 0.0
    score = 0.0
    if m in c:
        score += 5.0 if len(m) >= 5 else 3.0
    tokens = _model_tokens(model)
    if tokens:
        hits = sum(1 for token in tokens if token in c)
        score += 1.5 * hits / len(tokens)
    # اگر خود نام مدل با موضوع جستجو هم هم‌پوشانی دارد، کمی تقویت شود.
    if m in q:
        score += 2.0
    return score


async def web_search(query: str, max_results: int = WEB_SEARCH_MAX_RESULTS) -> list[dict[str, str]]:
    """
    جستجوی موضوع روی وب با نسخه HTML داک‌داک‌گو.
    خروجی: title/snippet/url.
    """
    if not WEB_SEARCH_ENABLED or not query.strip():
        return []
    params = {
        "q": query,
        "kl": WEB_SEARCH_REGION,
        "kp": "-1" if WEB_SEARCH_SAFE == "off" else "-2" if WEB_SEARCH_SAFE == "strict" else "",
    }
    url = "https://html.duckduckgo.com/html/?" + "&".join(
        f"{quote_plus(k)}={quote_plus(v)}" for k, v in params.items() if v
    )
    timeout = aiohttp.ClientTimeout(total=WEB_SEARCH_TIMEOUT)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": WEB_SEARCH_USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
        ) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.warning("Web search HTTP %s for query=%r", resp.status, query)
                    return []
                html = await resp.text(errors="ignore")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("Web search failed for query=%r: %s", query, exc)
        return []

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for result in soup.select(".result"):
        a = result.select_one(".result__a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        href = a.get("href", "").strip()
        snippet_el = result.select_one(".result__snippet")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if not href or href in seen:
            continue
        seen.add(href)
        results.append({"title": title, "snippet": snippet, "url": href})
        if len(results) >= max_results:
            break
    return results


async def _fetch_web_page_text(url: str, max_chars: int = 14000) -> str:
    """متن قابل‌جستجوی یک نتیجه وب را می‌گیرد؛ خطاهای یک سایت نباید کل سرچ را خراب کنند."""
    try:
        timeout = aiohttp.ClientTimeout(total=max(WEB_SEARCH_TIMEOUT, 10))
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": WEB_SEARCH_USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return ""
                raw = await resp.text(errors="ignore")
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:max_chars]
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return ""
    except Exception:
        log.exception("خواندن صفحه وب برای collection search ناموفق بود: %s", url)
        return ""


async def _collection_web_corpus(topic: str) -> tuple[str, list[dict[str, str]]]:
    """برای یک موضوع، چند query واقعی وب می‌زند و متن صفحات قوی‌تر را هم می‌خواند."""
    queries = [
        f'"{topic}" Telegram gifts models',
        f'"{topic}" NFT gifts Telegram',
        f'"{topic}" site:telegifter.ru/gifts Telegram gifts',
        f'"{topic}" site:t.me/nft Telegram collectible gift',
    ]
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for q in queries:
        for item in await web_search(q, max_results=WEB_SEARCH_MAX_RESULTS):
            url = item.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(item)
            if len(results) >= WEB_SEARCH_MAX_RESULTS * 2:
                break
        if len(results) >= WEB_SEARCH_MAX_RESULTS * 2:
            break

    pieces = [
        f"{item.get('title', '')} {item.get('snippet', '')}"
        for item in results
    ]
    # چند صفحهٔ اصلی را واقعاً fetch کن؛ این بخش باعث می‌شود اگر snippet موتور جستجو
    # کوتاه بود، نام مدل‌های داخل صفحهٔ thematic/catalog هم وارد corpus شود.
    page_results = results[:COLLECTION_WEB_PAGES]
    page_texts = await asyncio.gather(
        *[_fetch_web_page_text(item["url"]) for item in page_results],
        return_exceptions=True,
    )
    for txt in page_texts:
        if isinstance(txt, str) and txt:
            pieces.append(txt)
    return " ".join(pieces), results


async def find_web_related_models_any(topic: str) -> tuple[list[tuple[str, float]], list[dict[str, str]]]:
    """جستجوی موضوعیِ مستقل از Backdrop.

    فرق مهم با نسخهٔ قبلی: اول کل وب را برای موضوع می‌گردد، بعد Modelهای دیتابیس را
    با متن واقعی صفحات مقایسه می‌کند. بنابراین «Harry Potter» لازم نیست خودش نام
    یک Model یا Backdrop باشد؛ مدل‌هایی مثل Elder Wand / Amortentia / ... می‌توانند
    از منابع وب به‌عنوان نتیجهٔ موضوعی پیدا شوند.
    """
    topic = topic.strip()
    if not topic:
        return [], []
    models = await db_get_all_patterns()
    if not models:
        return [], []
    corpus, web_results = await _collection_web_corpus(topic)
    if not corpus:
        return [], web_results

    scored: list[tuple[str, float]] = []
    for model in models:
        score = _score_model_against_web(model, corpus, topic)
        if score >= COLLECTION_WEB_MIN_SCORE:
            scored.append((model, score))
    scored.sort(key=lambda x: (-x[1], x[0].lower()))
    return scored[:COLLECTION_MAX_MODELS], web_results


# سازگاری با مسیر قدیمی: جاهای دیگری که هنوز Backdrop مشخص دارند از همین تابع استفاده می‌کنند.
async def find_web_related_models(backdrop: str, topic: str) -> tuple[list[tuple[str, float]], list[dict[str, str]]]:
    models = await db_get_patterns_by_backdrop(backdrop)
    if not models:
        return [], []
    corpus, web_results = await _collection_web_corpus(topic)
    if not corpus:
        return [], web_results
    scored = []
    for model in models:
        score = _score_model_against_web(model, corpus, topic)
        if score >= WEB_SEARCH_MIN_SCORE:
            scored.append((model, score))
    scored.sort(key=lambda x: (-x[1], x[0].lower()))
    return scored, web_results


def _collection_image_prompt(topic: str, count: int) -> str:
    """پرامپت اختیاری برای سرویس image-generation؛ خود ربات بدون آن هم collage واقعی می‌سازد."""
    return (
        f"Create a dark premium Telegram collectible-gift presentation background for a search result titled "
        f"{topic} Collection.\n"
        f"Use a subtle magical/fantasy atmosphere inspired by the topic, but do not reproduce copyrighted characters, logos, posters, or exact movie artwork. "
        f"Leave a clean central area for {count} real collectible gift images to be composited on top. "
        f"Use the dominant colors of the supplied gift images when possible, with soft glow, depth, and a clean luxury NFT marketplace aesthetic."
    )


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = (value or "#202020").lstrip("#")
    if len(value) != 6:
        return (32, 32, 32)
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        return (32, 32, 32)


def build_collection_collage(topic: str, image_paths: list[str], colors: list[str]) -> bytes:
    """یک پوستر نتیجه می‌سازد: چند گیفت واقعی + فضای تماتیک بر پایهٔ رنگ خود گیفت‌ها.

    اگر بعداً OPENAI_IMAGE_GENERATION=1 اضافه شود، همین prompt می‌تواند به سرویس
    تصویر وصل شود؛ ولی fallback فعلی کاملاً محلی است و API اضافه نمی‌خواهد.
    """
    W, H = 1200, 820
    base_rgb = _hex_rgb(colors[0] if colors else "#17131d")
    bg = Image.new("RGB", (W, H), base_rgb)
    draw = ImageDraw.Draw(bg, "RGBA")
    # هاله‌های رنگی بزرگ برای حس فضای موضوعی، بدون وابستگی به تصویر کپی‌رایت‌شده.
    palette = [_hex_rgb(c) for c in (colors[:5] or ["#5b3f8c", "#1f5f73", "#3a243f"])]
    for i, (r, g, b) in enumerate(palette):
        x = int((i + 1) * W / (len(palette) + 1))
        y = 180 + (i % 2) * 280
        draw.ellipse((x-280, y-280, x+280, y+280), fill=(r, g, b, 75))
    bg = bg.filter(ImageFilter.GaussianBlur(45))
    draw = ImageDraw.Draw(bg, "RGBA")

    title_font = _font("DejaVuSans-Bold.ttf", 54)
    sub_font = _font("DejaVuSans.ttf", 28)
    draw.rounded_rectangle((40, 35, W-40, 150), radius=28, fill=(0, 0, 0, 120), outline=(255,255,255,45), width=2)
    title = f"{topic.strip()} Collection"
    draw.text((70, 58), title[:36], font=title_font, fill=(255,255,255,245))
    draw.text((72, 115), f"{len(image_paths)} collectible gifts found", font=sub_font, fill=(235,235,245,210))

    # چیدمان 2xN؛ فقط خود عکس‌ها، بدون نوشتن Model/Backdrop/Symbol همهٔ آیتم‌ها.
    usable = image_paths[:6]
    slots = []
    if len(usable) <= 3:
        cols, rows = len(usable), 1
    else:
        cols, rows = 3, 2
    card_w = 330 if cols == 3 else 350
    card_h = 300
    gap_x = 45
    start_x = (W - (cols*card_w + (cols-1)*gap_x)) // 2
    start_y = 190
    for i, path in enumerate(usable):
        try:
            im = Image.open(path).convert("RGB")
            im = ImageOps.fit(im, (card_w, card_h), method=Image.Resampling.LANCZOS)
            x = start_x + (i % cols) * (card_w + gap_x)
            y = start_y + (i // cols) * (card_h + 35)
            shadow = Image.new("RGBA", (card_w+24, card_h+24), (0,0,0,0))
            shd = ImageDraw.Draw(shadow, "RGBA")
            shd.rounded_rectangle((8,8,card_w+16,card_h+16), radius=26, fill=(0,0,0,150))
            shadow = shadow.filter(ImageFilter.GaussianBlur(8))
            bg.paste(shadow, (x-12,y-12), shadow)
            mask = Image.new("L", (card_w, card_h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0,0,card_w,card_h), radius=22, fill=255)
            bg.paste(im, (x,y), mask)
        except Exception:
            continue

    out = BytesIO()
    bg.save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue()

def format_collection_results(rows: list[tuple[str, str, int]]) -> list[str]:
    """گروه‌بندی بر اساس Model و ساخت پیام‌های quote."""
    from collections import OrderedDict
    groups: OrderedDict[str, list[str]] = OrderedDict()
    for pattern, base_link, idx in rows:
        url = f"{base_link}{idx}"
        groups.setdefault(pattern, []).append(url)

    chunks: list[str] = []
    current = ""
    for pattern, urls in groups.items():
        # لینک‌ها داخل blockquote
        body_lines = "\n".join(urls)
        block = f"<b>{html_entities.escape(pattern)}</b>\n<blockquote>{body_lines}</blockquote>\n\n"
        if len(current) + len(block) > TELEGRAM_MSG_LIMIT - 50:
            if current:
                chunks.append(current.strip())
            current = block
        else:
            current += block
    if current.strip():
        chunks.append(current.strip())
    if not chunks:
        chunks = ["⚠️ نتیجه‌ای پیدا نشد."]
    return chunks


def _process_fetched_batch(
    fetched_items: list[tuple[int, str, Optional[str], bool]],
) -> tuple[dict[int, tuple[str, bool, Optional[str], Optional[str], bool]], list[tuple[int, bool, Optional[str], Optional[str]]]]:
    """
    این تابع خالص/سینک است و روی یک ترد جدا (asyncio.to_thread) اجرا می‌شود.

    == این دقیقاً همون چیزیه که باعث «ربات موقع سرچ به هیچ دکمه‌ای جواب
    نمی‌ده / با دو نفر هم‌زمان می‌رینه» می‌شد ==
    قبلاً is_page_valid() و extract_traits() (که BeautifulSoup و چند regex
    سنگین صدا می‌زنند) مستقیم توی حلقه‌ی اصلی asyncio، پشت سر هم برای کل
    یک batch (که می‌تونست هزاران آیتم باشه - batch_size = max_concurrent*4)
    اجرا می‌شدند؛ هیچ await ای هم وسطش نبود که بذاره event loop نفس بکشه.
    یعنی دقیقاً به همون اندازه که این پردازش طول می‌کشید (بر اساس لاگ‌های
    شما: تا ۶۰ ثانیه!) کل ربات - برای *همه‌ی* کاربران، نه فقط همونی که
    داشت سرچ می‌کرد - عملاً فریز می‌شد؛ حتی call.answer() یک کلیک دکمه‌ی
    ساده هم دیر می‌رسید و تلگرام با «query is too old» ردش می‌کرد.
    حالا این پردازش سنگین کامل روی یک ترد جدا اجرا می‌شود؛ حلقه‌ی اصلی
    asyncio در همون لحظه آزاد می‌مونه که هم‌زمان به کلیک دکمه‌ها/پیام‌های
    بقیه کاربرها هم برسه.
    """
    results: dict[int, tuple[str, bool, Optional[str], Optional[str], bool]] = {}
    new_rows: list[tuple[int, bool, Optional[str], Optional[str]]] = []
    for i, url, html, net_err in fetched_items:
        if net_err:
            results[i] = (url, False, None, None, True)
            continue
        valid = is_page_valid(html)
        pattern = color = None
        if valid:
            traits = extract_traits(html)
            pattern = traits.get("pattern")
            color = traits.get("color")
        results[i] = (url, valid, pattern, color, False)
        new_rows.append((i, valid, pattern, color))
    return results, new_rows


async def scan_indices_cached(
    base_link: str, indices: list[int]
) -> list[tuple[str, bool, Optional[str], Optional[str], bool]]:
    """
    نسخه‌ی کش‌دار fetch: اول دیتابیس رو برای این اندیس‌ها چک می‌کند؛ هرچی
    از قبل جستجو شده بود مستقیم (بدون درخواست شبکه) برمی‌گرداند. فقط
    اندیس‌های واقعاً جدید fetch می‌شوند و نتیجه‌شان هم ذخیره می‌شود تا
    دفعه بعد (چه برای همین کاربر، چه کاربر دیگر) از کش استفاده شود.

    خروجی به همون ترتیب indices ورودی: (url, valid, pattern, color, net_err)
    """
    cached = await db_get_cached_items(base_link, indices)
    results: dict[int, tuple[str, bool, Optional[str], Optional[str], bool]] = {}
    to_fetch = [i for i in indices if i not in cached]

    for i, (valid, pattern, color) in cached.items():
        results[i] = (f"{base_link}{i}", valid, pattern, color, False)

    if to_fetch:
        fetch_urls = [f"{base_link}{i}" for i in to_fetch]
        fetched = await scanner.scan_many(fetch_urls)
        fetched_items = [(i, url, html, net_err) for i, (url, html, net_err) in zip(to_fetch, fetched)]
        # پردازش سنگین (parse) روی ترد جدا -> حلقه‌ی اصلی asyncio بلاک نمی‌شود
        batch_results, new_rows = await asyncio.to_thread(_process_fetched_batch, fetched_items)
        results.update(batch_results)
        if new_rows:
            await db_store_items(base_link, new_rows)

    return [results[i] for i in indices]


async def scan_indices_force(
    base_link: str, indices: list[int]
) -> list[tuple[str, bool, Optional[str], Optional[str], bool]]:
    """
    مثل scan_indices_cached ولی هیچ‌وقت از کش نمی‌خواند - همیشه از شبکه
    fetch تازه انجام می‌دهد و نتیجه را دوباره ذخیره (Overwrite) می‌کند.
    توجه: فلوی پیش‌فرض «بروزرسانی» پنل ادمین دیگر این تابع را صدا نمی‌زند
    (آن فلو الان فقط آیتم‌های جدید را با scan_indices_cached اضافه می‌کند)؛
    این تابع همچنان به‌عنوان ابزار کمکی برای «بازبینی کامل و اجباری» (اگر
    در آینده لازم شد) نگه داشته شده است.
    """
    if not indices:
        return []

    fetch_urls = [f"{base_link}{i}" for i in indices]
    fetched = await scanner.scan_many(fetch_urls)
    fetched_items = [(i, url, html, net_err) for i, (url, html, net_err) in zip(indices, fetched)]
    # پردازش سنگین (parse) روی ترد جدا -> حلقه‌ی اصلی asyncio بلاک نمی‌شود
    batch_results, new_rows = await asyncio.to_thread(_process_fetched_batch, fetched_items)
    if new_rows:
        await db_store_items(base_link, new_rows)
    return [batch_results[i] for i in indices]


# ------------------------------------------------------------------------- #
#  استخراج ویژگی‌ها - منطق دو لایه
# ------------------------------------------------------------------------- #

def _layer_zero_meta_description(html: str) -> Optional[dict]:
    """
    لایه صفر (منبع اصلی و جدید - قابل‌اعتمادترین لایه): تلگرام برای هر صفحه
    آیتم NFT، در <head> یک متا-تگ og:description و/یا twitter:description
    قرار می‌دهد که محتوایش دقیقاً به این شکل تمیز است:

        Model: Pumpkin
        Backdrop: Onyx Black
        Symbol: Illuminati

    نکته مهم: این متن هیچ‌وقت شامل درصد رریتی یا تگ HTML اضافه نیست (برخلاف
    سلول جدول که مقدار مدل/بک‌گراند و درصد رریتی‌اش گاهی بدون فاصله به‌هم
    می‌چسبند و باعث می‌شدن رجکس‌های لایه ۲ یک مقدار زباله/نصفه‌نیمه یا
    اشتباه استخراج کنند). چون این متا-تگ ثابت و ساخت‌یافته است، این لایه
    اول اجرا می‌شود و اگر جواب بدهد، دیگر سراغ لایه‌های شکننده‌تر نمی‌رویم.
    """
    if "description" not in html.lower():
        return None

    content: Optional[str] = None
    for meta_tag in RE_META_TAG.findall(html):
        if not RE_META_IS_DESCRIPTION.search(meta_tag):
            continue
        m = RE_META_CONTENT_ATTR.search(meta_tag)
        if m:
            content = m.group(1)
            break  # ترجیح با اولین متا-تگ توضیحات (معمولاً og:description)

    if not content:
        return None

    content = html_entities.unescape(content)
    result: dict = {}
    for line in re.split(r"[\r\n]+", content):
        line = line.strip()
        if not line or ":" not in line:
            continue
        label, _, value = line.partition(":")
        label = label.strip().lower()
        cleaned = _clean_trait_value(value)
        if not cleaned:
            continue
        if "model" in label or "pattern" in label:
            result["pattern"] = cleaned
        elif "backdrop" in label or "background" in label or "color" in label:
            result["color"] = cleaned
        # "Symbol"/"Quantity"/... عمداً نادیده گرفته می‌شوند - فقط مدل و بک‌گراند لازم است

    return result or None


def _layer_one_json(html: str) -> Optional[dict]:
    """
    لایه اول (اصلی): جستجوی هدفمند در <script> های صفحه برای یافتن یک
    آبجکت JSON ساخت‌یافته که آرایه attributes/traits را شامل شود.

    -- بهینه‌سازی سرعت --
    ۱) early-exit: اگر کل صفحه اصلاً کلمات "attributes"/"trait_type" رو
       نداره (یعنی محاله لایه یک چیزی پیدا کنه)، بدون صرف وقت برای پیدا
       کردن/اسکن کردن بلوک‌های <script> بلافاصله None برمی‌گردیم.
    ۲) از regex های از قبل کامپایل‌شده استفاده می‌شود (نه ساخت مجدد هر بار).
    ۳) json.loads از همون import سراسری بالای فایل استفاده می‌کند (قبلاً
       هر صدا زدن این تابع یک import اضافه و بی‌فایده انجام می‌داد).
    """
    if "attributes" not in html and "trait_type" not in html:
        return None
    script_blocks = RE_SCRIPT_BLOCK.findall(html)
    for block in script_blocks:
        if "attributes" not in block and "trait_type" not in block:
            continue
        # دنبال چیزی شبیه {"attributes":[{"trait_type":"Model","value":"..."}, ...]}
        json_candidates = RE_JSON_CANDIDATE.findall(block)
        for candidate in json_candidates:
            if "attributes" not in candidate and "trait_type" not in candidate:
                continue
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            attrs = data.get("attributes") if isinstance(data, dict) else None
            if not attrs:
                continue
            result = {}
            for attr in attrs:
                trait = str(attr.get("trait_type", "")).lower()
                value = attr.get("value")
                if not value:
                    continue
                if "model" in trait or "pattern" in trait:
                    cleaned = _clean_trait_value(str(value))
                    if cleaned:
                        result["pattern"] = cleaned
                elif "backdrop" in trait or "background" in trait or "color" in trait:
                    cleaned = _clean_trait_value(str(value))
                    if cleaned:
                        result["color"] = cleaned
            if result:
                return result
    return None


_LAYER_TWO_HTML_SLICE = 15000  # فقط ابتدای HTML پارس می‌شود؛ جدول ویژگی‌ها همیشه اینجا قرار دارد


def _layer_two_text(html: str) -> Optional[dict]:
    """
    لایه دوم (زاپاس): اگر ساختار JSON تغییر کرده یا پیدا نشد، از روی
    متن جدول/کلاس‌های عمومی توضیحات صفحه پارس متنی انجام می‌شود.

    -- بهینه‌سازی سرعت --
    ۱) فقط ابتدای HTML (اولین _LAYER_TWO_HTML_SLICE کاراکتر) پارس می‌شود؛
       جدول ویژگی‌های Model/Backdrop همیشه در بالای صفحه قرار دارد، پس
       پارس‌کردن کل صفحه (که می‌تونه چند برابر بزرگ‌تر باشه) اتلاف وقته.
    ۲) BeautifulSoup به‌صورت lazy ساخته می‌شود: فقط وقتی صفحه واقعاً یک
       جدول HTML دارد (که با یک چک متنی ارزان قبل از هر پارسی تشخیص داده
       می‌شود) درخت BS4 ساخته می‌شود؛ در غیر این صورت مستقیم با regex روی
       متن خام (بدون هزینه‌ی ساخت کل درخت DOM) نتیجه گرفته می‌شود.
    """
    snippet = html[:_LAYER_TWO_HTML_SLICE]
    lowered = snippet.lower()
    result: dict = {}
    text_for_fallback = snippet

    if "<table" in lowered or "tgme_gift_table" in lowered:
        soup = BeautifulSoup(snippet, "html.parser")
        rows = soup.select("table tr") or soup.select(".tgme_gift_table tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            # -- اصلاح باگ: get_text(strip=True) بدون separator، فاصله‌ی بین
            # متن اصلی مقدار و تگ <mark> درصد رریتی (مثلاً "Pumpkin <mark>3%</mark>")
            # را حذف می‌کرد و "Pumpkin3%" تولید می‌شد؛ اگر اسم مدل/بک‌گراند
            # به رقم ختم می‌شد (مثلاً یک اسم عددی)، رجکس درصد می‌توانست رقم
            # آخر خودِ اسم را هم به اشتباه با رقمِ درصد قاطی کند. با
            # separator=" " فاصله‌ی واقعی حفظ می‌شود.
            label = cells[0].get_text(" ", strip=True).lower()
            value = cells[1].get_text(" ", strip=True)
            value = _clean_trait_value(value)
            if not value:
                continue
            if "model" in label or "pattern" in label:
                result["pattern"] = value
            elif "backdrop" in label or "background" in label or "color" in label:
                result["color"] = value
        if not result:
            text_for_fallback = soup.get_text(" ", strip=True)

    if not result:
        # fallback نهایی: متن ساده‌ی صفحه را می‌خوانیم و هم فرم «Label: Value»
        # و هم فرم جدولیِ «Label | Value» را قبول می‌کنیم. این دقیقاً با
        # ساختاری که صفحات Collectible تلگرام نمایش می‌دهند سازگار است.
        if "<" in text_for_fallback and ">" in text_for_fallback:
            text_for_fallback = BeautifulSoup(text_for_fallback, "html.parser").get_text(" ", strip=True)
        m_pattern = RE_TEXT_PATTERN.search(text_for_fallback)
        m_color = RE_TEXT_COLOR.search(text_for_fallback)
        if m_pattern:
            cleaned = _clean_trait_value(m_pattern.group(1))
            if cleaned:
                result["pattern"] = cleaned
        if m_color:
            cleaned = _clean_trait_value(m_color.group(1))
            if cleaned:
                result["color"] = cleaned

    return result or None


def is_page_valid(html: Optional[str]) -> bool:
    """فیلتر سخت‌گیرانه: صفحات ناموجود/نامعتبر (۴۰۴، خالی، بدون جدول) حذف می‌شوند."""
    if not html:
        return False
    lowered = html.lower()
    if "gift not found" in lowered or "channel not found" in lowered or "404" in lowered[:600]:
        return False
    if "<table" not in lowered and "tgme_gift_table" not in lowered and "attributes" not in lowered:
        return False
    return True


_KNOWN_LABEL_RE = re.compile(rf"\b(?:{_KNOWN_LABEL_ALTERNATION})\b\s*:?", re.I)


def _canonicalize_trait_value(raw: Optional[str]) -> Optional[str]:
    """
    مقدار واقعی Model/Backdrop را از متن صفحه‌ی Telegram تمیز و canonical می‌کند.

    در صفحه‌ی Collectible، درصد rarity بخشی از *نمایش* مقدار trait است، نه
    بخشی از نام trait؛ بعضی نسخه‌های HTML/markup هم کاراکترهای جهت‌نما مثل
    ``<``/``>`` را کنار rarity می‌آورند. بنابراین مقدارهایی مثل
    ``English Violet 1.5% <`` باید همیشه به ``English Violet`` تبدیل شوند.
    """
    if not raw:
        return None
    value = html_entities.unescape(str(raw))
    value = value.replace("\xa0", " ").replace("\u200c", " ")
    value = re.sub(r"\s+", " ", value).strip(" :\u200c-–—")

    # هر چیزی بعد از درصد rarity که صرفاً markup/فلش باشد هم حذف شود.
    value = re.sub(r"\s*(?:[<>‹›←→«»]+\s*)?\d+(?:\.\d+)?%\s*(?:[<>‹›←→«»]+\s*)*$", "", value)
    value = re.sub(r"[<>‹›←→«»]+\s*$", "", value).strip(" :\u200c-–—")

    # اگر parser متن را تا برچسب بعدی کشانده باشد، همان‌جا قطعش کن.
    m = _KNOWN_LABEL_RE.search(value) if "_KNOWN_LABEL_RE" in globals() else None
    if m and m.start() > 0:
        value = value[:m.start()].strip(" :\u200c-–—")

    if len(value) < 2:
        return None
    return value


def _clean_trait_value(raw: Optional[str]) -> Optional[str]:
    """پاکسازی نهایی و یکسان‌سازی نام Model/Backdrop قبل از ذخیره در DB."""
    return _canonicalize_trait_value(raw)


def extract_traits(html: str) -> dict:
    """اجرای لایه صفر (متا-تگ توضیحات - پایدارترین)، سپس در صورت شکست لایه
    اول (JSON داخل script) و در نهایت لایه دوم (پارس متنی جدول/صفحه)."""
    data = _layer_zero_meta_description(html)
    if data:
        return data
    data = _layer_one_json(html)
    if data:
        return data
    data = _layer_two_text(html)
    return data or {}


# ------------------------------------------------------------------------- #
#  استخراج عکس اصلی هر آیتم (برای کارت تصویری) + زیباسازی نام کالکشن
# ------------------------------------------------------------------------- #

def extract_image_candidates(html: str) -> list[str]:
    """
    آدرس‌های *کاندید* عکس اصلی گیفت را به ترتیب اولویت برمی‌گرداند (نه فقط
    اولین موردی که پیدا شد). قبلاً extract_image_url فقط یک URL برمی‌گردوند
    و اولویت با og:image/twitter:image بود - مشکل اینجا بود که این متاتگ‌های
    عمومی گاهی به یک عکس/آیکون *مشترک و پیش‌فرض* کل صفحه/سایت اشاره می‌کنند
    (نه عکس مخصوص همین گیفت خاص)، و چون همیشه اولین کاندید بودن، همیشه همون
    عکس اشتباه/تکراری کش و برای همه‌ی مدل‌ها نشون داده می‌شد. حالا اول
    سراغ کلاس‌های اختصاصی خودِ ویجت گیفت (که به عکس واقعی همون آیتم اشاره
    دارند) می‌رویم و og:image/twitter:image را به‌عنوان آخرین راه‌حل نگه
    می‌داریم؛ فراخوان (fetch_and_cache_*) همه‌ی این کاندیدها را به‌ترتیب
    امتحان می‌کند تا اولین عکسِ واقعاً معتبر و غیرتکراری را پیدا کند.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []

    for sel in (".tgme_gift_photo img", ".tgme_widget_message_photo_wrap img"):
        for img in soup.select(sel):
            src = img.get("src")
            if src:
                candidates.append(src.strip())

    for sel in (".tgme_gift_photo", ".tgme_widget_message_photo_wrap"):
        for tag in soup.select(sel):
            style = tag.get("style")
            if style:
                m = RE_STYLE_URL.search(style)
                if m:
                    candidates.append(m.group(1).strip())

    for prop in ("og:image", "twitter:image"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            candidates.append(tag["content"].strip())

    for m in RE_IMG_URL_FALLBACK.finditer(html):
        candidates.append(m.group(0))

    seen: set[str] = set()
    ordered_unique: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered_unique.append(c)
    return ordered_unique


def extract_image_url(html: str) -> Optional[str]:
    """سازگاری با کد قدیمی: فقط بهترین (اولین) کاندید را برمی‌گرداند."""
    candidates = extract_image_candidates(html)
    return candidates[0] if candidates else None


def _is_valid_image_bytes(data: Optional[bytes], min_side: int = 48) -> bool:
    """
    قبل از قبول یک عکس دانلودشده، مطمئن می‌شویم واقعاً یک عکس سالم و با
    ابعاد معقول است (نه یک صفحه‌ی خطا/HTML که به اشتباه با استاتوس ۲۰۰
    برگشته، و نه یک آیکون/پیکسل ردیابی ۱x۱ که هیچ ارزش نمایشی ندارد).
    """
    if not data or len(data) < 256:
        return False
    try:
        with Image.open(BytesIO(data)) as im:
            im.verify()
        with Image.open(BytesIO(data)) as im2:
            w, h = im2.size
        return w >= min_side and h >= min_side
    except Exception:
        return False


# base_link -> {sha256_hash: identity (pattern یا "pattern|color") که اولین بار
# این عکس برایش قبول شد} - برای تشخیص عکس‌های *مشترک/عمومی* بین چند مدل
# متفاوت از همون کالکشن (که یعنی احتمالاً عکس مخصوص خودِ اون مدل نیست).
_seen_image_hashes: dict[str, dict[str, str]] = {}


def _register_and_check_unique_image(base_link: str, identity: str, img_bytes: bytes) -> bool:
    """
    True یعنی این عکس تا الان برای هیچ مدل/ترکیب *دیگری* از همین کالکشن
    استفاده نشده (پس به احتمال زیاد واقعاً عکس اختصاصی همین مدل/ترکیبه).
    اگر همین عکس دقیقاً قبلاً برای یک identity دیگه ثبت شده بود، False
    برمی‌گردونه تا سراغ کاندید/نمونه‌ی بعدی بریم - نمی‌خوایم یک عکس عمومی/
    مشترک به‌جای عکس واقعی این مدل کش و نمایش داده بشه.
    """
    h = hashlib.sha256(img_bytes).hexdigest()
    bucket = _seen_image_hashes.setdefault(base_link, {})
    owner = bucket.get(h)
    if owner is None:
        bucket[h] = identity
        return True
    return owner == identity


async def _validate_cached_media(base_link: str, identity: str, image_path: str) -> bool:
    """
    اعتبارسنجیِ یک ردیفِ *از قبل کش‌شده* (nft_media / nft_media_combo) قبل
    از استفاده. مشکل قدیمی: اگر یک‌بار (مثلاً به‌خاطر باگ قبلی استخراج
    عکس) یک عکسِ عمومی/مشترک سایت به‌جای عکس اختصاصی مدل کش شده باشه، هر
    بار بعد بدون هیچ چکی همون عکسِ اشتباه دوباره برگردونده می‌شد - چون
    کش هیچ‌وقت با هم مقایسه نمی‌شدن. اینجا همون فایلِ کش‌شده رو هش می‌کنیم
    و با همون رجیستری‌ای که برای دانلودهای تازه استفاده می‌شه چک می‌کنیم:
    اگر این هش قبلاً متعلق به یک identity *دیگه* ثبت شده، یعنی این عکس
    واقعاً مخصوص این مدل/ترکیب نیست (یک عکس مشترک/عمومی به اشتباه کش
    شده) - False برمی‌گردونیم تا فراخوان این ردیف کش رو پاک و دوباره
    (با منطق درست‌تر و ضدتکراریِ فعلی) عکس اختصاصی واقعی رو بگیره.
    """
    if not os.path.isfile(image_path):
        return False

    def _read():
        with open(image_path, "rb") as f:
            return f.read()

    try:
        data = await asyncio.to_thread(_read)
    except Exception:
        return False

    return _register_and_check_unique_image(base_link, identity, data)


def beautify_name(slug: str) -> str:
    """نام خام اسلاگ (مثل PlushPepe یا Plush_Pepe) را برای نمایش تبدیل می‌کند: Plush Pepe."""
    spaced = RE_SLUG_UNDERSCORE.sub(" ", slug)
    spaced = RE_SLUG_CAMELCASE.sub(" ", spaced)
    return " ".join(w.capitalize() for w in spaced.split())


# ------------------------------------------------------------------------- #
#  ابزارهای کمکی UI
# ------------------------------------------------------------------------- #

# تلگرام/aiogram خودش فاصله رو در callback_data قبول می‌کنه، ولی چون مقدار
# مدل/بک‌گراند (opt) می‌تونه شامل چند کلمه با فاصله باشه و بعداً دقیقاً با
# split(":", 1)[1] بدون هیچ نرمال‌سازی خونده می‌شه، هر جای نامتعارف/فاصله
# غیرقابل‌چاپ داخلش باعث می‌شد مقدار برگشتی با opt اصلی داخل
# selected_patterns/selected_colors (که از استخراج متن ساخته شده) یکی
# در نیاد و toggle درست کار نکنه. راه‌حل: قبل از گذاشتن در callback_data
# فاصله‌ها رو با "_" جایگزین می‌کنیم و موقع خوندن دوباره برش می‌گردونیم؛
# مقادیر ویژه "__all__" و "__confirm__" هیچ‌وقت دیکود نمی‌شن چون از قبل
# با _ ساخته شدن و دیکودشون خرابشون می‌کند.
_CB_SPECIAL_VALUES = {"__all__", "__confirm__"}


def _cb_encode(value: str) -> str:
    return value.replace(" ", "_")


def _cb_decode(raw_value: str) -> str:
    if raw_value in _CB_SPECIAL_VALUES:
        return raw_value
    return raw_value.replace("_", " ")


def render_trait_emoji_list(options: set[str], emoji_map: dict[str, tuple[str, str]]) -> str:
    """
    اگر ادمین برای بعضی از این مدل‌ها/بک‌گراندها ایموجی پرمیوم ثبت کرده باشد،
    یک لیست کوچک برای نمایش *بالای* کیبورد شیشه‌ای می‌سازد که در آن ایموجی
    پرمیومِ واقعی (تگ HTML <tg-emoji>) کنار اسم هرکدام دیده می‌شود.
    توجه: این متن جدا از دکمه‌هاست، چون خودِ دکمه‌های شیشه‌ای تلگرام
    (InlineKeyboardButton) هیچ HTML/entity ای پشتیبانی نمی‌کنند و امکان
    نشان‌دادن ایموجی پرمیوم واقعی *داخل متن دکمه* وجود ندارد؛ داخل دکمه
    فقط فالبکِ ساده‌ی همون ایموجی (یک کاراکتر یونیکد معمولی) گذاشته می‌شود.
    """
    relevant = [opt for opt in sorted(options) if opt in emoji_map]
    if not relevant:
        return ""
    lines = []
    for opt in relevant:
        emoji_id, fallback = emoji_map[opt]
        lines.append(f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji> {opt}')
    return "\n\n" + "\n".join(lines)


def build_toggle_keyboard(
    options: set[str],
    selected: set[str],
    prefix: str,
    select_all_active: bool = False,
    select_all_disabled: bool = False,
    emoji_map: Optional[dict[str, tuple[str, str]]] = None,
    use_icons: bool = True,
) -> InlineKeyboardMarkup:
    """
    توجه: از Bot API 9.4 (فوریه ۲۰۲۶) به بعد، تلگرام واقعاً از رنگِ دکمه
    (style: danger/success/primary) و آیکونِ ایموجی پرمیومِ واقعی روی خودِ
    دکمه (icon_custom_emoji_id) پشتیبانی می‌کنه؛ قبلاً این قابلیت‌ها اصلاً
    وجود نداشتن (و اطلاعات قدیمی درباره‌ی همین محدودیت هنوز خیلی جاها
    هست) - الان هم رنگ واقعی دکمه و هم آیکونِ ایموجی پرمیومِ واقعی رو
    تنظیم می‌کنیم.
    ⚠️ icon_custom_emoji_id فقط وقتی کار می‌کنه که ربات پیام رو مستقیم به
    چت خصوصی/گروه/سوپرگروه بفرسته و اکانتِ *صاحبِ ربات* پرمیوم باشه (یا
    ربات یوزرنیم اضافه از Fragment خریده باشه). اگه این شرط برقرار نباشه،
    تلگرام آپدیت رو رد می‌کنه؛ use_icons=False این آیکون رو کلاً حذف
    می‌کنه و به‌جاش فقط فالبکِ متنی ساده‌ی همون ایموجی رو می‌ذاره - این
    برای fallback خودکار وقتی آیکون رد می‌شه استفاده می‌شود (نگاه کن به
    _send_with_toggle_kb / _edit_*_with_toggle_kb).
    """
    emoji_map = emoji_map or {}
    kb = InlineKeyboardBuilder()
    for opt in sorted(options):
        is_selected = opt in selected
        # طبق درخواست: چون الان رنگِ واقعیِ خودِ دکمه (style) وضعیتِ
        # انتخاب‌شده/نشده رو نشون می‌ده، دیگه نیازی به مربعِ سبز/قرمزِ
        # اضافه‌ی کنارِ متن نیست - فقط روی دکمه‌های «انتخاب همه» و «تایید و
        # ادامه» (که رنگ نمی‌گیرن) این علامت نگه داشته می‌شه.
        icon_custom_emoji_id: Optional[str] = None
        emoji_prefix = ""
        if opt in emoji_map:
            emoji_id, fallback = emoji_map[opt]
            if use_icons:
                icon_custom_emoji_id = emoji_id
            else:
                emoji_prefix = f"{fallback} "
        kb.button(
            text=f"{emoji_prefix}{opt}",
            callback_data=f"{prefix}:{_cb_encode(opt)}",
            style="success" if is_selected else "danger",
            icon_custom_emoji_id=icon_custom_emoji_id,
        )

    # دکمه‌ی «انتخاب همه» رنگِ قرمز/سبز نمی‌گیره (style تنظیم نمی‌شه)، ولی
    # علامتِ انتخاب‌شده/نشده‌ی قابل‌تنظیمِ ادمین (🎛 ایموجی دکمه‌های انتخاب)
    # رو نگه می‌داره.
    mark_selected = get_toggle_mark("mark_selected")
    mark_unselected = get_toggle_mark("mark_unselected")
    if select_all_disabled:
        all_icon_id = None
        all_text = "🚫 انتخاب همه (غیرفعال - گزینه مقابل فعاله)"
    elif select_all_active:
        all_icon_id = get_toggle_mark_id("mark_selected") if use_icons else None
        all_text = "انتخاب همه" if all_icon_id else f"{mark_selected} انتخاب همه"
    else:
        all_icon_id = get_toggle_mark_id("mark_unselected") if use_icons else None
        all_text = "انتخاب همه" if all_icon_id else f"{mark_unselected} انتخاب همه"
    kb.button(text=all_text, callback_data=f"{prefix}:__all__", icon_custom_emoji_id=all_icon_id)

    confirm_icon_id = get_toggle_mark_id("mark_confirm") if use_icons else None
    confirm_text = "تایید و ادامه" if confirm_icon_id else f"{get_toggle_mark('mark_confirm')} تایید و ادامه"
    kb.button(text=confirm_text, callback_data=f"{prefix}:__confirm__", icon_custom_emoji_id=confirm_icon_id)
    kb.adjust(2)
    return kb.as_markup()


BLOCKQUOTE_OPEN = "<blockquote expandable>"
BLOCKQUOTE_CLOSE = "</blockquote>"
_BLOCKQUOTE_SAFETY_MARGIN = 16   # حاشیه اطمینان برای گرد کردن اشتباهات محاسبه طول


TELEGRAM_CAPTION_LIMIT = 1024  # سقف کاراکتر کپشن عکس در تلگرام (کمتر از سقف متن پیام معمولی)

# تلگرام/aiogram برای ایموجی پرمیوم در HTML از این تگ استفاده می‌کند.
# در نتیجه‌ی اسکن، وقتی چنین تگی در header باشد، بعضی نسخه‌های کلاینت
# ممکن است <tg-emoji> را کنار <blockquote expandable> بد رندر کنند و
# لینک‌ها را از Quote خارج نشان دهند. برای جلوگیری از این باگ، در حالت
# وجود ایموجی پرمیوم، header را از Quote جداگانه می‌فرستیم.
RE_PREMIUM_EMOJI_TAG = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", re.I | re.S)


def _has_premium_emoji_html(text: str) -> bool:
    return bool(RE_PREMIUM_EMOJI_TAG.search(text or ""))


def chunk_message(lines: list[str], header: str = "", limit: int = TELEGRAM_MSG_LIMIT) -> list[str]:
    """
    بسته‌بندی خروجی با رعایت سقف کاراکتر تلگرام.

    برخلاف قبل که وقتی حجم متن از حد مجاز رد می‌شد مستقیم می‌رفتیم سراغ یک
    پیام کاملاً جدید و باز (که چت رو شلوغ می‌کرد)، حالا لیست لینک‌ها داخل یک
    Quote تاشو (<blockquote expandable>) قرار می‌گیرد که پیش‌فرض minimize/جمع
    شده نمایش داده می‌شود و کاربر با یک تپ می‌تواند بازش کند. اگر حجم واقعاً
    از سقف مطلق تلگرام (که غیرقابل‌عبور است) رد شود، باز هم چند پیام لازم
    است؛ اما در آن حالت هم محتوای هر پیام داخل همین Quote تاشو قرار می‌گیرد
    تا باز هم جمع‌وجور و مرتب بماند.

    پارامتر limit: سقف کاراکتر هر «تکه». پیش‌فرض همون سقف پیام معمولی تلگرام
    (۴۰۹۶) است؛ برای وقتی که این خروجی قراره کپشن یک عکس بشه (سقف ۱۰۲۴)،
    limit=TELEGRAM_CAPTION_LIMIT پاس داده می‌شود.
    """
    signature = get_msg("msg_signature")
    header_len = len(header)
    chunk_groups: list[tuple[bool, list[str]]] = []
    current_lines: list[str] = []
    current_len = 0
    is_first = True

    def budget_for(first: bool) -> int:
        h = header_len if first else 0
        raw = limit - h - len(BLOCKQUOTE_OPEN) - len(BLOCKQUOTE_CLOSE) - len(signature) - _BLOCKQUOTE_SAFETY_MARGIN
        return max(raw, 40)

    budget = budget_for(is_first)
    for line in lines:
        if line.startswith("https://"):
            line = line[len("https://"):]
        elif line.startswith("http://"):
            line = line[len("http://"):]
        add_len = len(line) + 1  # +1 برای \n
        if current_lines and current_len + add_len > budget:
            chunk_groups.append((is_first, current_lines))
            is_first = False
            budget = budget_for(is_first)
            current_lines = []
            current_len = 0
        current_lines.append(line)
        current_len += add_len

    if current_lines:
        chunk_groups.append((is_first, current_lines))

    if not chunk_groups:
        return [f"{header}{get_msg('msg_no_valid_after_filter')}{signature}"]

    messages: list[str] = []
    for i, (first, grp_lines) in enumerate(chunk_groups):
        body = "\n".join(grp_lines)
        # باگ: آیکون جمع/باز کردن (⌃) که تلگرام روی گوشه‌ی پایین همین
        # <blockquote expandable> می‌گذارد، دقیقاً روی خطِ آخر محتوا می‌نشیند.
        # وقتی آن خطِ آخر خودش یک لینک باشد (که همیشه همینطوره - آخرین لینک
        # نتیجه‌ی اسکن)، تپ کاربر روی همون گوشه به‌جای باز کردن لینک، فقط
        # کوت را جمع/باز می‌کند - یعنی لینک آخر عملاً غیرقابل‌کلیک به‌نظر
        # می‌رسد (درحالی‌که خودِ متنش کاملاً درست و سالم است). با افزودن یک
        # خطِ خالیِ نامرئی (zero-width space) بعد از آخرین لینک، آن آیکون
        # یک خط پایین‌تر از لینک آخر قرار می‌گیرد و دیگر تپ روی خودِ لینک را
        # نمی‌بلعد.
        body += "\n\u200b"
        text = (header if first else "") + BLOCKQUOTE_OPEN + body + BLOCKQUOTE_CLOSE
        if i == len(chunk_groups) - 1:
            text += signature
        messages.append(text)
    return messages


def extract_slug(base_link: str) -> str:
    """از روی لینک پایه (https://t.me/nft/Name-) نام کالکشن (Name) را استخراج می‌کند."""
    m = RE_EXTRACT_SLUG.search(base_link)
    return m.group(1) if m else base_link


def normalize_base_link(text: str) -> Optional[str]:
    """
    ورودی مثل:  https://t.me/nft/CollectionName-  یا  t.me/nft/CollectionName-1
    را به پیشوند پایه (بدون شماره انتهایی) تبدیل می‌کند.
    """
    text = text.strip()
    m = RE_NORMALIZE_BASE_LINK.search(text)
    if not m:
        return None
    slug = m.group(1)
    return f"https://t.me/nft/{slug}-"


# ------------------------------------------------------------------------- #
#  تشخیص خودکار تعداد واقعی کالکشن (بدون نیاز به اینکه کاربر عدد بدهد)
# ------------------------------------------------------------------------- #

async def _window_last_valid(base_link: str, start_idx: int, window: int) -> tuple[Optional[int], bool]:
    """
    یک بازه [start_idx, start_idx+window-1] را به‌صورت موازی چک می‌کند (اول از
    کش دیتابیس، بعد شبکه) و بزرگ‌ترین اندیس معتبر پیدا شده در آن بازه را
    برمی‌گرداند. این تلورانس گپ باعث می‌شود چند آیتم پنهان/سوخته وسط کالکشن
    باعث توقف زودهنگام تشخیص تعداد کل نشود.

    خروجی: (last_valid_idx یا None, conclusive)
    conclusive=False یعنی «نتیجه قابل‌اعتماد نیست» - نه اینکه واقعاً هیچ
    آیتم معتبری اونجا نبود، بلکه اکثر درخواست‌های این بازه با خطای شبکه/
    ریت‌لیمیت مواجه شدن (مثلاً چون همزمانی روی سقف بالا تنظیم شده و سایت
    هدف موقتاً محدودمون کرده). قبل از این fix، این حالت با «قطعاً پایان
    کالکشن» اشتباه گرفته می‌شد -> باعث می‌شد تشخیص تعداد کل کالکشن‌های
    بزرگ (مثلاً ۵۰۰ هزارتایی) دقیقاً همون‌جایی که یک دسته درخواست به خطای
    شبکه برخورده بود «برای همیشه» گیر کند (چون تعداد غلط کش هم می‌شد و
    دفعه بعد بازم از همون نقطه‌ی غلط شروع می‌شد).

    این تابع چند بار (با فاصله) تلاش می‌کند تا این حالت‌های گذرا را از
    «واقعاً پایان کالکشن» تشخیص بدهد.
    """
    if start_idx < 1:
        return None, True
    probe_cap = _adaptive_window_probe_cap(start_idx)
    if window <= probe_cap:
        idxs = list(range(start_idx, start_idx + window))
    else:
        # پنجره بزرگ‌تر از سقفه -> به‌جای fetch کردن هر تک آیتم (که می‌تونست
        # تا ۶۰ هزار درخواست بشه)، به‌صورت یکنواخت روی کل پنجره نمونه‌برداری
        # می‌کنیم (حداکثر probe_cap آیتم واقعی - که خودش adaptive است)؛ همیشه
        # هم انتهای دقیق پنجره رو جزو نمونه‌ها نگه می‌داریم که مرز واقعی از
        # قلم نیفتد.
        step = window / probe_cap
        sampled = {start_idx + int(i * step) for i in range(probe_cap)}
        sampled.add(start_idx + window - 1)
        idxs = sorted(sampled)
    max_retries = 4
    last_valid = None
    for attempt in range(max_retries):
        results = await scan_indices_cached(base_link, idxs)
        last_valid = None
        error_count = 0
        for i, (url, valid, pattern, color, net_err) in zip(idxs, results):
            if net_err:
                error_count += 1
                continue
            if valid:
                last_valid = i if last_valid is None else max(last_valid, i)
        # اگر بیش از نیمی از این بازه با خطای گذرا مواجه شد، این یعنی نتیجه
        # قابل‌اعتماد نیست (شاید سایت هدف موقتاً محدودمون کرده) - نه اینکه
        # واقعاً کالکشن اینجا تموم شده. کمی صبر و دوباره تلاش می‌کنیم.
        if error_count <= max(1, len(idxs) // 2):
            return last_valid, True
        await asyncio.sleep(RETRY_BACKOFF * (attempt + 1) * 3)
    # بعد از چند تلاش هم بازم بیشتر خطای شبکه بود؛ صادقانه اعلام می‌کنیم که
    # این بازه قابل‌اعتماد بررسی نشد (به‌جای اینکه به دروغ «پایان کالکشن»
    # فرض کنیم و کالکشن رو کوچک‌تر از واقعیتش نشون بدیم).
    return last_valid, False


async def discover_total_count(base_link: str, progress_cb=None) -> int:
    """
    تعداد واقعی آیتم‌های کالکشن را (بدون وابستگی به عددی که کاربر می‌گوید)
    با ترکیب «جستجوی نمایی» (exponential search) و «جستجوی دودویی»
    (binary search) پیدا می‌کند. این یعنی برای کالکشنی با ۶۰۰ هزار آیتم هم
    با چند صد درخواست (نه ۶۰۰ هزار درخواست) اندازه واقعی پیدا می‌شود.

    اگر این کالکشن قبلاً یک‌بار چک شده باشه، تعداد کل کش‌شده از دیتابیس
    به‌عنوان نقطه شروع استفاده می‌شود (به‌جای شروع از صفر) که یعنی برای
    کالکشن‌های تکراری این مرحله تقریباً آنی انجام می‌شود؛ فقط چک می‌شود که
    از دفعه قبل آیتم جدیدی اضافه نشده باشه.
    """
    cached_total = await db_get_cached_total(base_link)
    lo = 0

    if cached_total and cached_total > 0:
        # چک کن که آخرین آیتم کش‌شده هنوز واقعاً معتبره (یعنی کش هنوز معتبره).
        # اگر جواب inconclusive بود (خطای شبکه/ریت‌لیمیت گذرا، نه یک نتیجه‌ی
        # واقعی)، به دیتابیس کش شک نمی‌کنیم و به‌جای شروع کامل از صفر، همون
        # مقدار قبلی رو نقطه‌ی شروع در نظر می‌گیریم (سریع + امن).
        still_valid, conclusive = await _window_last_valid(base_link, cached_total, 1)
        if still_valid is not None or not conclusive:
            lo = cached_total
            if progress_cb:
                await progress_cb(lo)

    if lo == 0:
        first, conclusive = await _window_last_valid(base_link, 1, 1)
        if first is None and conclusive:
            return 0
        lo = 1 if first is not None else 0
        if lo == 0:
            # حتی آیتم اول هم به‌خاطر خطای شبکه قابل‌تأیید نبود؛ یک تلاش
            # دیگر با تحمل بیشتر می‌کنیم قبل از اینکه کالکشن رو خالی بدونیم
            first, _ = await _window_last_valid(base_link, 1, 5)
            if first is None:
                return 0
            lo = first

    hi = max(lo * 2, 2)   # کاندیدای بعدی برای جستجوی نمایی
    inconclusive_streak = 0

    # مرحله ۱: نمایی جلو می‌رویم تا به کرانی برسیم که حتی با تلورانس گپِ
    # پویا (متناسب با موقعیت فعلی؛ برای کالکشن‌های بزرگ خیلی بزرگ‌تر از
    # قبل) هم چیزی معتبر پیدا نشود. این یعنی گپ‌های واقعی و بزرگ (آیتم‌های
    # سوخته/استفاده‌نشده که در کالکشن‌های ۵۰۰-۶۰۰ هزارتایی رایج هستند)
    # دیگر به‌اشتباه «پایان کالکشن» تلقی نمی‌شوند.
    while hi <= COUNT_SANITY_CEILING:
        tolerance = _dynamic_gap_tolerance(hi)
        found, conclusive = await _window_last_valid(base_link, hi, tolerance)
        if found is not None:
            lo = max(lo, found)
            if progress_cb:
                await progress_cb(lo)
            hi *= 2
            inconclusive_streak = 0
        elif not conclusive:
            # این یعنی نمی‌شه اینجا رو قطعی «پایان کالکشن» دونست (خطای شبکه
            # غالب بود) - نه پیشرفت می‌کنیم نه تسلیم می‌شیم، فقط دوباره
            # (با کمی جلوتر رفتن محتاطانه) امتحان می‌کنیم
            inconclusive_streak += 1
            if inconclusive_streak >= MAX_INCONCLUSIVE_STREAK:
                log.warning(
                    f"discover_total_count: بعد از {MAX_INCONCLUSIVE_STREAK} تلاش هم نشد "
                    f"مطمئن شد کالکشن {base_link} در اطراف {hi} تموم شده یا نه (خطای شبکه پایدار)."
                )
                break
            continue
        else:
            break

    upper_bound = min(hi, COUNT_SANITY_CEILING)

    # مرحله ۲: جستجوی دودویی بین lo (معتبر) و upper_bound (نامعتبر با تلورانس پویا) برای دقیق کردن مرز
    inconclusive_streak = 0
    while lo + _dynamic_gap_tolerance(lo) < upper_bound:
        mid = (lo + upper_bound) // 2
        tolerance = _dynamic_gap_tolerance(mid)
        found, conclusive = await _window_last_valid(base_link, mid, tolerance)
        if found is not None:
            lo = max(lo, found)
            if progress_cb:
                await progress_cb(lo)
            inconclusive_streak = 0
        elif not conclusive:
            inconclusive_streak += 1
            if inconclusive_streak >= MAX_INCONCLUSIVE_STREAK:
                break
            continue
        else:
            upper_bound = mid

    # پاس نهایی «راستی‌آزمایی»: چون در پنجره‌های بزرگ فقط نمونه‌برداری شده
    # بود (نه چک تک‌تک آیتم‌ها)، ممکنه lo دقیقاً روی آخرین آیتم معتبر نباشه.
    # به‌جای فقط یک بار چک کردن، با یک پنجره‌ی کوچیک و *کاملاً چگال* (بدون
    # نمونه‌برداری) درست بعد از lo چک می‌کنیم؛ اگر چیزی جدید پیدا شد، از
    # همون‌جا دوباره ادامه می‌دیم (زنجیره‌ای، حداکثر چند دور محدود) تا حتی
    # چند گپ کوچیک پشت‌سرهم هم درست ردیابی بشن و مطمئن بشیم دقیقاً سر مرز
    # نهایی واقعی ایستادیم.
    for _ in range(5):
        refine_window = min(_dynamic_gap_tolerance(lo), WINDOW_PROBE_CAP)
        found, conclusive = await _window_last_valid(base_link, lo + 1, refine_window)
        if found is None:
            break
        lo = found
        if progress_cb:
            await progress_cb(lo)

    await db_store_total(base_link, lo)
    return lo


# ------------------------------------------------------------------------- #
#  هندلرها
# ------------------------------------------------------------------------- #

router = Router()
# مقدار اولیه از ثابت‌های پیش‌فرض ساخته می‌شود؛ در main() بعد از لود شدن تنظیمات از
# دیتابیس، با scanner.update_limits() مقدار واقعی (احتمالاً تغییریافته از پنل ادمین) اعمال می‌شود.
scanner = AsyncScanner(MAX_CONCURRENT_REQUESTS, MAX_REQUESTS_PER_SECOND, REQUEST_TIMEOUT)
sessions: dict[int, SessionData] = {}  # session state جدا برای هر کاربر (کلید = user_id)


@router.errors()
async def global_error_handler(event, exception: Optional[Exception] = None):
    """
    قبلاً اگر یک هندلر (روی کلیک دکمه، پیام متنی و ...) اکسپشن می‌داد، aiogram
    فقط یک خط warning ریز لاگ می‌کرد و به همون کاربر هیچ جوابی نمی‌رفت -
    یعنی از بیرون دقیقاً همون چیزی به‌نظر می‌رسید که کاربر گزارش داد: «ربات
    یک گهی می‌خوره و هیچ اثری ازش تو لاگ نمی‌مونه که بشه فهمید مشکل کجا بود».
    این‌جا با traceback کامل و آپدیت مربوطه لاگ می‌شود تا علت دقیق همیشه
    قابل ردیابی باشد، و true برمی‌گردانیم تا خطا کل پروسه را پایین نکشد.
    """
    exc = exception or getattr(event, "exception", None)
    update = getattr(event, "update", event)
    log.error(f"خطای مدیریت‌نشده هنگام پردازش یک آپدیت: {update}", exc_info=exc)
    return True


# ---- ردیابی خودکار هر کاربری که با ربات تعامل می‌کند ----------------------

async def _user_tracking_middleware(handler, event, data):
    user = data.get("event_from_user")
    if user is not None:
        try:
            await db_upsert_user(user.id, user.username, user.first_name, user.last_name)
        except Exception:
            log.exception("خطا در ثبت اطلاعات کاربر")
    return await handler(event, data)


router.message.middleware(_user_tracking_middleware)
router.callback_query.middleware(_user_tracking_middleware)


# ------------------------------------------------------------------------- #
#  پنل مدیریت (فقط برای ADMIN_IDS)
# ------------------------------------------------------------------------- #

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ------------------------------------------------------------------------- #
#  عضویت اجباری - فعال/غیرفعال و بررسی عضویت
# ------------------------------------------------------------------------- #

def force_sub_is_enabled() -> bool:
    return runtime_settings.get("force_sub_enabled") == "1"


def force_sub_channel() -> str:
    return runtime_settings.get("force_sub_channel", "")


def _force_sub_channel_link(channel: str) -> str:
    if channel.startswith("http://") or channel.startswith("https://"):
        return channel
    return f"https://t.me/{channel.lstrip('@')}"


async def is_user_member_of_channel(bot: Bot, user_id: int) -> bool:
    """
    بررسی می‌کند که کاربر عضو کانال عضویت اجباری هست یا نه. اگر به هر دلیلی
    (کانال تنظیم نشده، ربات ادمین کانال نیست، خطای شبکه) نتوانیم مطمئن
    بشویم، به‌صورت fail-open عمل می‌کنیم (اجازه عبور) تا یک تنظیم اشتباه
    کل ربات را برای همه قفل نکند.
    """
    channel = force_sub_channel()
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception:
        log.exception("خطا در بررسی عضویت کاربر در کانال اجباری")
        return True


def build_force_sub_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=runtime_settings.get("force_sub_button", DEFAULT_SETTINGS["force_sub_button"]),
        url=_force_sub_channel_link(force_sub_channel()),
    )
    kb.button(text="✅ بررسی مجدد عضویت", callback_data="checkjoin")
    kb.adjust(1)
    return kb.as_markup()


async def _force_sub_middleware(handler, event, data):
    user = data.get("event_from_user")

    # ادمین‌ها همیشه معاف از عضویت اجباری هستند؛ همچنین اگر قابلیت خاموش
    # باشد یا کاربری در ایونت شناسایی نشد، مسیر عادی طی می‌شود.
    if user is None or is_admin(user.id) or not force_sub_is_enabled():
        return await handler(event, data)

    # اجازه بده خودِ دکمه‌ی «بررسی مجدد عضویت» همیشه قابل کلیک باشد.
    if isinstance(event, CallbackQuery) and event.data == "checkjoin":
        return await handler(event, data)

    bot: Bot = data.get("bot")
    if await is_user_member_of_channel(bot, user.id):
        return await handler(event, data)

    text = runtime_settings.get("force_sub_text", DEFAULT_SETTINGS["force_sub_text"])
    kb = build_force_sub_keyboard()
    if isinstance(event, CallbackQuery):
        await event.answer(get_msg("msg_force_sub_alert"), show_alert=True)
        try:
            await event.message.answer(text, reply_markup=kb)
        except Exception:
            pass
    else:
        try:
            await event.answer(text, reply_markup=kb)
        except Exception:
            log.exception("خطا در ارسال پیام عضویت اجباری")
    return  # پردازش هندلر اصلی متوقف می‌شود


router.message.middleware(_force_sub_middleware)
router.callback_query.middleware(_force_sub_middleware)


@router.callback_query(F.data == "checkjoin")
async def on_check_join(call: CallbackQuery, state: FSMContext):
    if await is_user_member_of_channel(call.bot, call.from_user.id):
        await call.answer(get_msg("msg_checkjoin_confirmed"))
        try:
            await call.message.delete()
        except Exception:
            pass
        await send_link_prompt(call.message.answer, call.from_user.id, state)
    else:
        await call.answer(get_msg("msg_checkjoin_not_yet"), show_alert=True)


def build_admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 ارسال پیام همگانی", callback_data="admin:broadcast")
    kb.button(text="📋 لیست کاربران", callback_data="admin:list")
    kb.button(text="🏓 پینگ جز به جز", callback_data="admin:ping")
    kb.button(text="🗄 مدیریت دیتابیس", callback_data="admin:add_menu")
    kb.button(text="📢 عضویت اجباری", callback_data="admin:forcesub")
    kb.button(text="⚙️ تنظیمات اسکنر", callback_data="admin:scanner_settings")
    kb.button(text="⚙️ تنظیمات پیام و ایموجی‌ها", callback_data="admin:msgs")
    kb.button(text="💰 قیمت اعتبار / شارژ", callback_data="admin:pricing")
    kb.button(text="💰 شارژ موجودی کاربر", callback_data="admin:credit")
    kb.adjust(1)
    return kb.as_markup()


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer(get_msg("msg_admin_access_denied"))
        return
    await message.answer("🛠 <b>پنل مدیریت ربات</b>", reply_markup=build_admin_keyboard())


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_broadcast_text)
    await call.answer()
    await call.message.answer(
        "📢 <b>ارسال پیام همگانی</b>\n\n"
        "متنی که می‌خواهی برای همه‌ی کاربرانی که ربات را استارت کرده‌اند ارسال شود بفرست.\n\n"
        "برای لغو: /start"
    )


@router.message(StateFilter(AdminFlow.waiting_broadcast_text))
async def admin_broadcast_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("❌ لطفاً یک پیام متنی بفرست.")
        return

    user_ids = await db_get_broadcast_user_ids()
    if not user_ids:
        await state.clear()
        await message.answer("ℹ️ هنوز کاربری برای ارسال پیام ثبت نشده.", reply_markup=build_admin_keyboard())
        return

    status = await message.answer(
        f"📤 ارسال شروع شد...\n👥 تعداد گیرنده‌ها: <b>{len(user_ids)}</b>"
    )

    success = 0
    failed = 0
    for user_id in user_ids:
        try:
            await message.bot.send_message(user_id, text)
            success += 1
        except Exception as e:
            failed += 1
            log.warning("ارسال پیام همگانی به %s ناموفق بود: %s", user_id, e)
        # فاصله‌ی کوتاه برای جلوگیری از فشار/ریت‌لیمیت تلگرام
        await asyncio.sleep(0.05)

    await state.clear()
    report = (
        "✅ <b>پیام همگانی تمام شد.</b>\n\n"
        f"📨 موفق: <b>{success}</b>\n"
        f"❌ ناموفق: <b>{failed}</b>\n"
        f"👥 مجموع گیرنده‌ها: <b>{len(user_ids)}</b>"
    )
    try:
        await status.edit_text(report)
    except Exception:
        await message.answer(report)


@router.callback_query(F.data == "admin:list")
async def admin_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()

    users = await db_get_all_users()
    if not users:
        await call.message.answer("هنوز هیچ کاربری ثبت نشده.")
        return

    lines = [f"📋 <b>لیست کاربران</b> (مجموع: {len(users)})\n"]
    for user_id, username, first_name, last_name, first_seen, last_seen, msg_count in users:
        uname = f"@{username}" if username else "—"
        full_name = " ".join(filter(None, [first_name, last_name])) or "—"
        last_seen_str = datetime.fromtimestamp(last_seen).strftime("%Y-%m-%d %H:%M")
        first_seen_str = datetime.fromtimestamp(first_seen).strftime("%Y-%m-%d")
        lines.append(
            f"🆔 <code>{user_id}</code>\n"
            f"👤 {full_name} | {uname}\n"
            f"🗓 اولین بازدید: {first_seen_str} | آخرین فعالیت: {last_seen_str}\n"
            f"✉️ تعداد پیام/تعامل: {msg_count}\n"
        )

    text = "\n".join(lines)
    # چون ممکنه از سقف پیام تلگرام رد بشه، به قطعات چندتایی تقسیم می‌شود
    for start in range(0, len(text), TELEGRAM_MSG_LIMIT):
        await call.message.answer(text[start:start + TELEGRAM_MSG_LIMIT])


@router.callback_query(F.data == "admin:ping")
async def admin_ping(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()

    status_msg = await call.message.answer("🏓 در حال اندازه‌گیری پینگ جز به جز...")
    results: dict[str, Optional[float]] = {}

    # ۱) پینگ تلگرام Bot API (زمان پاسخ getMe)
    t0 = time.perf_counter()
    try:
        await call.bot.get_me()
        results["telegram_api"] = (time.perf_counter() - t0) * 1000
    except Exception:
        results["telegram_api"] = None

    # ۲) رفت‌وبرگشت کامل ادیت پیام (یعنی واقعاً چقدر طول می‌کشه یک پیام آپدیت بشه)
    t0 = time.perf_counter()
    try:
        await status_msg.edit_text("🏓 در حال اندازه‌گیری پینگ جز به جز...\n(در حال چک کردن سایت هدف)")
        results["edit_roundtrip"] = (time.perf_counter() - t0) * 1000
    except Exception:
        results["edit_roundtrip"] = None

    # ۳) پینگ سایت هدف (t.me) - همون سایتی که ربات برای اسکن استفاده می‌کند
    t0 = time.perf_counter()
    try:
        async with scanner._session.get(
            "https://t.me", timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            await resp.read()
        results["target_site"] = (time.perf_counter() - t0) * 1000
    except Exception:
        results["target_site"] = None

    # ۴) پینگ دیتابیس محلی (SQLite)
    t0 = time.perf_counter()
    try:
        await db_get_user_count()
        results["database"] = (time.perf_counter() - t0) * 1000
    except Exception:
        results["database"] = None

    # ۵) تأخیر حلقه رویداد asyncio (نشون می‌ده خود پردازش پایتون زیر بار هست یا نه)
    t0 = time.perf_counter()
    await asyncio.sleep(0)
    results["event_loop"] = (time.perf_counter() - t0) * 1000

    def fmt(key: str) -> str:
        v = results.get(key)
        return f"{v:.1f} ms" if v is not None else "❌ خطا / بدون پاسخ"

    report = (
        "🏓 <b>گزارش پینگ جز به جز</b>\n\n"
        f"🤖 تلگرام (Bot API - getMe): {fmt('telegram_api')}\n"
        f"✏️ رفت‌وبرگشت ادیت پیام: {fmt('edit_roundtrip')}\n"
        f"🌐 سایت هدف (t.me): {fmt('target_site')}\n"
        f"🗄 دیتابیس محلی (SQLite): {fmt('database')}\n"
        f"🔁 تأخیر حلقه رویداد (event loop): {fmt('event_loop')}\n\n"
        f"⚙️ تنظیمات فعلی اسکنر: {get_max_concurrent()} درخواست هم‌زمان | "
        f"{get_max_per_second()} درخواست در ثانیه"
    )
    try:
        await status_msg.edit_text(report)
    except Exception:
        await call.message.answer(report)


@router.callback_query(F.data == "admin:back")
async def admin_back(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await safe_edit(call.message, "🛠 <b>پنل مدیریت ربات</b>", reply_markup=build_admin_keyboard())


# ------------------------------------------------------------------------- #
#  پنل مدیریت > عضویت اجباری
# ------------------------------------------------------------------------- #

def build_forcesub_admin_keyboard() -> InlineKeyboardMarkup:
    enabled = force_sub_is_enabled()
    kb = InlineKeyboardBuilder()
    kb.button(
        text=("✅ فعال (بزن برای غیرفعال کردن)" if enabled else "⛔ غیرفعال (بزن برای فعال کردن)"),
        callback_data="fs:toggle",
    )
    kb.button(text="🔗 تنظیم کانال", callback_data="fs:setchannel")
    kb.button(text="📝 تنظیم متن پیام", callback_data="fs:settext")
    kb.button(text="🔙 بازگشت", callback_data="admin:back")
    kb.adjust(1)
    return kb.as_markup()


def _forcesub_menu_text() -> str:
    channel = force_sub_channel() or "— تنظیم نشده —"
    status = "✅ فعال" if force_sub_is_enabled() else "⛔ غیرفعال"
    return (
        "📢 <b>عضویت اجباری</b>\n\n"
        f"وضعیت فعلی: {status}\n"
        f"کانال فعلی: <code>{channel}</code>\n\n"
        "با روشن کردن این قابلیت، هرکسی که عضو کانال زیر نباشد، قبل از "
        "استفاده از ربات یک پیام همراه با دکمه‌ی عضویت می‌بیند."
    )


@router.callback_query(F.data == "admin:forcesub")
async def admin_forcesub_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    await safe_edit(call.message, _forcesub_menu_text(), reply_markup=build_forcesub_admin_keyboard())


@router.callback_query(F.data == "fs:toggle")
async def fs_toggle(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    if not force_sub_is_enabled() and not force_sub_channel():
        await call.answer("⚠️ اول باید یک کانال تنظیم کنی.", show_alert=True)
        return
    new_value = "0" if force_sub_is_enabled() else "1"
    await db_set_setting("force_sub_enabled", new_value)
    await call.answer("انجام شد ✅")
    await safe_edit(call.message, _forcesub_menu_text(), reply_markup=build_forcesub_admin_keyboard())


@router.callback_query(F.data == "fs:setchannel")
async def fs_set_channel(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    await safe_edit(call.message, 
        "🔗 آیدی یا لینک کانال رو بفرست، مثلاً:\n"
        "<code>@channel_username</code> یا <code>https://t.me/channel_username</code>\n\n"
        "⚠️ توجه: ربات باید ادمین همون کانال باشه تا بتونه عضویت رو چک کنه."
    )
    await state.set_state(AdminFlow.waiting_force_sub_channel)


@router.message(StateFilter(AdminFlow.waiting_force_sub_channel))
async def on_force_sub_channel_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    channel = (message.text or "").strip()
    if not channel:
        await message.answer("❌ یک آیدی/لینک معتبر بفرست.")
        return
    m = RE_CHANNEL_T_ME.search(channel)
    if m:
        channel = "@" + m.group(1)
    elif not channel.startswith("@"):
        channel = "@" + channel.lstrip("@")
    await db_set_setting("force_sub_channel", channel)
    await state.clear()
    await message.answer(f"✅ کانال تنظیم شد: <code>{channel}</code>", reply_markup=build_forcesub_admin_keyboard())


@router.callback_query(F.data == "fs:settext")
async def fs_set_text(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    await safe_edit(call.message, "📝 متن جدید پیام عضویت اجباری رو بفرست:")
    await state.set_state(AdminFlow.waiting_force_sub_text)


@router.message(StateFilter(AdminFlow.waiting_force_sub_text))
async def on_force_sub_text_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("❌ متن نمی‌تواند خالی باشد.")
        return
    await db_set_setting("force_sub_text", new_text)
    await state.clear()
    await message.answer("✅ متن جدید ذخیره شد.", reply_markup=build_forcesub_admin_keyboard())


# ------------------------------------------------------------------------- #
#  پنل مدیریت > تنظیمات اسکنر (سقف همزمانی + سقف نرخ درخواست در ثانیه)
# ------------------------------------------------------------------------- #

def build_scanner_settings_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔀 تغییر سقف درخواست هم‌زمان", callback_data="ss:set_concurrent")
    kb.button(text="⏱ تغییر سقف نرخ در ثانیه", callback_data="ss:set_per_second")
    kb.button(text="🔙 بازگشت", callback_data="admin:back")
    kb.adjust(1)
    return kb.as_markup()


def _scanner_settings_menu_text() -> str:
    return (
        "⚙️ <b>تنظیمات اسکنر</b>\n\n"
        f"🔀 سقف درخواست هم‌زمان فعلی: <b>{get_max_concurrent()}</b>\n"
        f"⏱ سقف نرخ درخواست در ثانیه فعلی: <b>{get_max_per_second()}</b>\n\n"
        "تغییرات بلافاصله و بدون نیاز به ری‌استارت ربات اعمال می‌شوند "
        "(روی اسکن‌هایی که همین الان در حال اجرا هستند اثر نمی‌گذارد، فقط "
        "روی درخواست‌های بعدی).\n"
        f"محدوده مجاز: {MIN_ALLOWED_CONCURRENT} تا {MAX_ALLOWED_CONCURRENT} برای همزمانی، "
        f"{MIN_ALLOWED_PER_SECOND} تا {MAX_ALLOWED_PER_SECOND} برای نرخ در ثانیه.\n\n"
        "🔒 قفل سرچ حالا سراسری نیست: از پنل ادمین > ➕ افزودن، روی هر "
        "کالکشن بزن و از همون‌جا مخصوص همون یک لینک قفل/باز کن."
    )


@router.callback_query(F.data == "admin:scanner_settings")
async def admin_scanner_settings_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    await safe_edit(call.message, _scanner_settings_menu_text(), reply_markup=build_scanner_settings_keyboard())


@router.callback_query(F.data == "ss:set_concurrent")
async def ss_set_concurrent(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    await safe_edit(call.message, 
        f"🔀 عدد جدید سقف درخواست هم‌زمان رو بفرست (بین {MIN_ALLOWED_CONCURRENT} تا {MAX_ALLOWED_CONCURRENT}):\n"
        f"مقدار فعلی: <b>{get_max_concurrent()}</b>"
    )
    await state.set_state(AdminFlow.waiting_max_concurrent)


@router.message(StateFilter(AdminFlow.waiting_max_concurrent))
async def on_max_concurrent_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("❌ یک عدد صحیح معتبر بفرست.")
        return
    if not (MIN_ALLOWED_CONCURRENT <= value <= MAX_ALLOWED_CONCURRENT):
        await message.answer(
            f"❌ عدد باید بین {MIN_ALLOWED_CONCURRENT} تا {MAX_ALLOWED_CONCURRENT} باشد."
        )
        return
    await db_set_setting("max_concurrent", str(value))
    scanner.update_limits(get_max_concurrent(), get_max_per_second())
    await state.clear()
    await message.answer(
        f"✅ سقف درخواست هم‌زمان روی {value} تنظیم شد (بلافاصله فعال شد).",
        reply_markup=build_scanner_settings_keyboard(),
    )


@router.callback_query(F.data == "ss:set_per_second")
async def ss_set_per_second(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    await safe_edit(call.message, 
        f"⏱ عدد جدید سقف نرخ درخواست در ثانیه رو بفرست (بین {MIN_ALLOWED_PER_SECOND} تا {MAX_ALLOWED_PER_SECOND}):\n"
        f"مقدار فعلی: <b>{get_max_per_second()}</b>"
    )
    await state.set_state(AdminFlow.waiting_max_per_second)


@router.message(StateFilter(AdminFlow.waiting_max_per_second))
async def on_max_per_second_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("❌ یک عدد صحیح معتبر بفرست.")
        return
    if not (MIN_ALLOWED_PER_SECOND <= value <= MAX_ALLOWED_PER_SECOND):
        await message.answer(
            f"❌ عدد باید بین {MIN_ALLOWED_PER_SECOND} تا {MAX_ALLOWED_PER_SECOND} باشد."
        )
        return
    await db_set_setting("max_per_second", str(value))
    scanner.update_limits(get_max_concurrent(), get_max_per_second())
    await state.clear()
    await message.answer(
        f"✅ سقف نرخ درخواست در ثانیه روی {value} تنظیم شد (بلافاصله فعال شد).",
        reply_markup=build_scanner_settings_keyboard(),
    )


# ------------------------------------------------------------------------- #
#  پنل مدیریت > ایموجی دکمه‌های انتخاب (فقط دو دکمه: «انتخاب همه» و «تایید و ادامه»)
# ------------------------------------------------------------------------- #
#  از Bot API 9.4 (فوریه ۲۰۲۶) به بعد تلگرام واقعاً از icon_custom_emoji_id
#  (آیکونِ ایموجی پرمیومِ واقعی روی خودِ دکمه‌ی شیشه‌ای) پشتیبانی می‌کنه، پس
#  اگه ادمین یک ایموجی پرمیوم بفرسته، همون ایموجی پرمیومِ واقعی به‌عنوان
#  آیکونِ دکمه ذخیره و نشون داده می‌شه (نه فقط یک فالبکِ متنی) - به شرطی که
#  اکانتِ صاحبِ ربات پرمیوم باشه (یا ربات یوزرنیم Fragment داشته باشه)؛
#  در غیر این صورت build_toggle_keyboard/_*_with_toggle_kb خودکار به فالبکِ
#  سادهٔ همون ایموجی (که همیشه هم ذخیره می‌شه) برمی‌گردن. این پنل فقط
#  همین دو دکمه رو مدیریت می‌کنه: «انتخاب همه» (که دو حالت فعال/غیرفعال
#  داره) و «تایید و ادامه».
def _toggle_mark_preview(key: str) -> str:
    emoji_id = get_toggle_mark_id(key)
    fallback = get_toggle_mark(key)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback


def build_toggle_marks_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ ایموجی دکمه «انتخاب همه»", callback_data="tm:group:select_all")
    kb.button(text="✏️ ایموجی دکمه «تایید و ادامه»", callback_data="tm:set:mark_confirm")
    kb.button(text="🔄 بازگردانی هر دو به پیش‌فرض", callback_data="tm:reset")
    kb.button(text="🔙 بازگشت به مدیریت پیام‌ها", callback_data="admin:msgs")
    kb.adjust(1)
    return kb.as_markup()


def build_select_all_marks_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ تغییر حالتِ «فعال»", callback_data="tm:set:mark_selected")
    kb.button(text="✏️ تغییر حالتِ «غیرفعال»", callback_data="tm:set:mark_unselected")
    kb.button(text="🔙 بازگشت", callback_data="admin:toggle_marks")
    kb.adjust(1)
    return kb.as_markup()


def _toggle_marks_menu_text() -> str:
    return (
        "🎛 <b>ایموجی دکمه‌های انتخاب</b>\n\n"
        "این پنل فقط همین دو دکمه رو مدیریت می‌کنه:\n\n"
        f"{_toggle_mark_preview('mark_selected')} / {_toggle_mark_preview('mark_unselected')}"
        " → دکمه‌ی «انتخاب همه» (بسته به فعال/غیرفعال بودنش)\n"
        f"{_toggle_mark_preview('mark_confirm')} → دکمه‌ی «تایید و ادامه»\n\n"
        "✅ اگه یک ایموجی پرمیوم بفرستی، همون ایموجی پرمیومِ واقعی به‌عنوان "
        "آیکونِ خودِ دکمه نشون داده می‌شه (به شرطی که اکانتِ صاحبِ ربات "
        "پرمیوم باشه)؛ در غیر این صورت خودکار به فالبکِ سادهٔ همون ایموجی "
        "برمی‌گرده.\n\n"
        "روی یکی از دو دکمه بزن:"
    )


def _select_all_marks_menu_text() -> str:
    return (
        "✏️ <b>ایموجی دکمه «انتخاب همه»</b>\n\n"
        f"{_toggle_mark_preview('mark_selected')} وقتی «انتخاب همه» فعاله\n"
        f"{_toggle_mark_preview('mark_unselected')} وقتی «انتخاب همه» غیرفعاله\n\n"
        "کدوم حالت رو می‌خوای عوض کنی؟"
    )


@router.callback_query(F.data == "admin:toggle_marks")
async def admin_toggle_marks_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    await safe_edit(call.message, _toggle_marks_menu_text(), reply_markup=build_toggle_marks_keyboard())


@router.callback_query(F.data == "tm:group:select_all")
async def admin_toggle_marks_select_all_group(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    await safe_edit(call.message, _select_all_marks_menu_text(), reply_markup=build_select_all_marks_keyboard())


@router.callback_query(F.data.startswith("tm:set:"))
async def admin_toggle_mark_set(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    key = call.data.split(":", 2)[2]
    if key not in _TOGGLE_MARK_LABELS:
        await call.answer("❌ گزینه نامعتبر.", show_alert=True)
        return
    await call.answer()
    await safe_edit(
        call.message,
        f"✏️ یک پیام حاوی *فقط یک ایموجی* برای «{_TOGGLE_MARK_LABELS[key]}» بفرست.\n"
        f"مقدار فعلی: {_toggle_mark_preview(key)}\n\n"
        "می‌تونی از کیبورد ایموجی پرمیوم هم انتخاب کنی: همون ایموجی پرمیومِ "
        "واقعی به‌عنوان آیکونِ خودِ دکمه ذخیره می‌شه (به شرطی که اکانتِ صاحبِ "
        "ربات پرمیوم باشه)؛ اگه پشتیبانی نشه، ربات خودکار به فالبکِ سادهٔ "
        "همون ایموجی برمی‌گرده.",
    )
    await state.update_data(toggle_mark_key=key)
    await state.set_state(AdminFlow.waiting_toggle_mark_emoji)


@router.message(StateFilter(AdminFlow.waiting_toggle_mark_emoji))
async def on_toggle_mark_emoji_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("toggle_mark_key")
    if key not in _TOGGLE_MARK_LABELS:
        await state.clear()
        await message.answer("❌ خطای داخلی؛ دوباره از پنل مدیریت وارد شو.")
        return

    text = (message.text or "").strip()
    entities = message.entities or []
    custom_emoji_entity = next((e for e in entities if e.type == "custom_emoji"), None)
    if custom_emoji_entity:
        # پیام حاوی ایموجی پرمیومه: هم کدِ عددی‌اش (custom_emoji_id) برای
        # آیکونِ واقعیِ روی دکمه، هم فالبکِ سادهٔ همراهش (برای وقتی که آیکون
        # پشتیبانی نشه) ذخیره می‌شه.
        fallback = text[
            custom_emoji_entity.offset: custom_emoji_entity.offset + custom_emoji_entity.length
        ] or text
        emoji_id = custom_emoji_entity.custom_emoji_id
    else:
        fallback = text
        emoji_id = ""

    if not fallback:
        await message.answer("❌ یک ایموجی بفرست.")
        return
    if len(fallback) > 8:
        await message.answer("❌ متن خیلی طولانیه؛ فقط یک ایموجی بفرست.")
        return

    await db_set_setting(key, fallback)
    await db_set_setting(f"{key}_id", emoji_id)
    await state.clear()

    if emoji_id:
        preview = (
            f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji> '
            "(پرمیوم - روی خودِ دکمه هم به‌عنوان آیکون نشون داده می‌شه، اگه اکانتِ ربات پرمیوم باشه)"
        )
    else:
        preview = fallback
    back_kb = build_toggle_marks_keyboard() if key == "mark_confirm" else build_select_all_marks_keyboard()
    await message.answer(
        f"✅ «{_TOGGLE_MARK_LABELS[key]}» تنظیم شد: {preview}",
        reply_markup=back_kb,
    )


@router.callback_query(F.data == "tm:reset")
async def admin_toggle_marks_reset(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    for key in _TOGGLE_MARK_LABELS:
        await db_set_setting(key, DEFAULT_SETTINGS[key])
        await db_set_setting(f"{key}_id", DEFAULT_SETTINGS.get(f"{key}_id", ""))
    await call.answer("✅ به پیش‌فرض بازگردانده شد.")
    await safe_edit(call.message, _toggle_marks_menu_text(), reply_markup=build_toggle_marks_keyboard())


# ------------------------------------------------------------------------- #
#  پنل مدیریت > مدیریت پیام‌ها/بنرها (شامل ایموجی پرمیوم)
# ------------------------------------------------------------------------- #
#  همه‌ی پیام‌هایی که ربات مستقیم به کاربر عادی نشون می‌ده (MSG_TEMPLATES)
#  از همینجا قابل مشاهده/ویرایش/بازگردانی هستن. برای گذاشتن ایموجی پرمیوم
#  داخل یک پیام، ادمین کافیه توی همون پیامِ جایگزین، از کیبورد ایموجی
#  پرمیوم تلگرام (وقتی اکانتش پرمیومه) انتخاب کنه و بفرسته - چون ربات متن
#  جایگزین رو با message.html_text می‌خونه، خود aiogram/تلگرام کد عددی
#  (custom_emoji_id) رو خودکار به تگ <tg-emoji emoji-id="..."> تبدیل
#  می‌کنه؛ ادمین هیچ‌وقت لازم نیست عدد ایموجی رو دستی وارد کنه.

def build_msgs_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎛 ایموجی و متن دکمه‌ها", callback_data="admin:button_settings")
    kb.button(text="📝 پیام‌ها", callback_data="admin:message_settings")
    kb.button(text="🔙 بازگشت", callback_data="admin:back")
    kb.adjust(1)
    return kb.as_markup()

def _msgs_menu_text() -> str:
    return "⚙️ <b>تنظیمات پیام و ایموجی‌ها</b>\n\nدکمه‌ها و پیام‌های ربات را از دو بخش زیر مدیریت کن."

def build_message_settings_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, meta in MSG_TEMPLATES.items():
        kb.button(text=meta["label"], callback_data=f"msgedit:{key}")
    kb.button(text="🔙 بازگشت", callback_data="admin:msgs")
    kb.adjust(1)
    return kb.as_markup()

def build_button_settings_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🟩🟥 ایموجی دکمه‌های انتخاب", callback_data="admin:toggle_marks")
    kb.button(text="💎 دکمه Tonkeeper", callback_data="bs:edit:charge_auto_button")
    kb.button(text="🧾 دکمه پرداخت دستی", callback_data="bs:edit:charge_manual_button")
    kb.button(text="🔙 بازگشت", callback_data="admin:msgs")
    kb.adjust(1)
    return kb.as_markup()

def _button_settings_text() -> str:
    return ("🎛 <b>ایموجی و متن دکمه‌ها</b>\n\n"
            f"💎 Tonkeeper: <b>{html_entities.escape(runtime_settings.get('charge_auto_button', DEFAULT_SETTINGS['charge_auto_button']))}</b>\n"
            f"🧾 دستی: <b>{html_entities.escape(runtime_settings.get('charge_manual_button', DEFAULT_SETTINGS['charge_manual_button']))}</b>")


@router.callback_query(F.data == "admin:button_settings")
async def admin_button_settings(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return await call.answer("⛔", show_alert=True)
    await state.clear(); await call.answer()
    await safe_edit(call.message, _button_settings_text(), reply_markup=build_button_settings_keyboard())

@router.callback_query(F.data == "admin:message_settings")
async def admin_message_settings(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return await call.answer("⛔", show_alert=True)
    await state.clear(); await call.answer()
    await safe_edit(call.message, "📝 <b>پیام‌ها</b>\n\nپیام موردنظر را انتخاب کن:", reply_markup=build_message_settings_keyboard())

@router.callback_query(F.data.startswith("bs:edit:"))
async def admin_button_edit_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return await call.answer("⛔", show_alert=True)
    key=call.data.split(":",2)[2]
    if key not in ("charge_auto_button","charge_manual_button"): return await call.answer("❌ نامعتبر",show_alert=True)
    await call.answer(); current=runtime_settings.get(key,DEFAULT_SETTINGS[key]); icon=runtime_settings.get(key+"_id","")
    kb=InlineKeyboardBuilder(); kb.button(text="✏️ تغییر متن",callback_data=f"bs:text:{key}"); kb.button(text="✨ تغییر ایموجی پرمیوم",callback_data=f"bs:emoji:{key}"); kb.button(text="🗑 حذف ایموجی",callback_data=f"bs:delicon:{key}"); kb.button(text="🔙 بازگشت",callback_data="admin:button_settings"); kb.adjust(1)
    preview=f'<tg-emoji emoji-id="{icon}">🔹</tg-emoji>' if icon else "—"
    await safe_edit(call.message,f"🎛 <b>تنظیم دکمه</b>\n\nمتن: <b>{html_entities.escape(current)}</b>\nآیکون: {preview}",reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("bs:text:"))
async def admin_button_text_start(call: CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return
    key=call.data.split(":",2)[2]; await call.answer(); await state.update_data(button_key=key); await state.set_state(AdminFlow.waiting_button_text); await safe_edit(call.message,"✏️ متن جدید دکمه را بفرست:")

@router.message(StateFilter(AdminFlow.waiting_button_text))
async def admin_button_text_input(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    key=(await state.get_data()).get("button_key"); text=(message.text or "").strip()
    if key not in ("charge_auto_button","charge_manual_button") or not text: await message.answer("❌ متن نامعتبر است."); return
    await db_set_setting(key,text); await state.clear(); await message.answer("✅ متن دکمه ذخیره شد.",reply_markup=build_button_settings_keyboard())

@router.callback_query(F.data.startswith("bs:emoji:"))
async def admin_button_emoji_start(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return
    key=call.data.split(":",2)[2]; await call.answer(); await state.update_data(button_key=key); await state.set_state(AdminFlow.waiting_button_emoji); await safe_edit(call.message,"✨ فقط یک ایموجی پرمیوم بفرست:")

@router.message(StateFilter(AdminFlow.waiting_button_emoji))
async def admin_button_emoji_input(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    key=(await state.get_data()).get("button_key"); ent=next((e for e in (message.entities or []) if e.type=="custom_emoji"),None)
    if key not in ("charge_auto_button","charge_manual_button") or not ent: await message.answer("❌ یک ایموجی پرمیوم معتبر بفرست."); return
    await db_set_setting(key+"_id",ent.custom_emoji_id); await state.clear(); await message.answer("✅ ایموجی پرمیوم ذخیره شد.",reply_markup=build_button_settings_keyboard())

@router.callback_query(F.data.startswith("bs:delicon:"))
async def admin_button_icon_delete(call:CallbackQuery):
    if not is_admin(call.from_user.id): return
    key=call.data.split(":",2)[2]; await db_set_setting(key+"_id",""); await call.answer("✅ حذف شد"); await safe_edit(call.message,_button_settings_text(),reply_markup=build_button_settings_keyboard())

@router.callback_query(F.data == "admin:msgs")
async def admin_msgs_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await safe_edit(call.message, _msgs_menu_text(), reply_markup=build_message_settings_keyboard())


def _build_msg_edit_view_kb(key: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ تغییر متن این پیام", callback_data=f"msgsettext:{key}")
    kb.button(text="🔄 بازگردانی به پیش‌فرض", callback_data=f"msgreset:{key}")
    kb.button(text="🔙 بازگشت به لیست پیام‌ها", callback_data="admin:msgs")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("msgedit:"))
async def admin_msg_edit_view(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    key = call.data.split(":", 1)[1]
    meta = MSG_TEMPLATES.get(key)
    if not meta:
        await call.answer("❌ این پیام پیدا نشد.", show_alert=True)
        return
    await call.answer()
    current = runtime_settings.get(key, meta["default"])
    is_default = current == meta["default"]
    vars_line = ("، ".join(f"{{{v}}}" for v in meta["vars"])) if meta["vars"] else "— (این پیام متغیر ندارد)"
    info = (
        f"📝 <b>{meta['label']}</b>\n\n"
        f"وضعیت: {'⚪️ پیش‌فرض' if is_default else '✅ سفارشی‌سازی‌شده'}\n"
        f"متغیرهای قابل‌استفاده در این پیام: {vars_line}\n\n"
        f"— متن فعلی (همون‌طور که برای کاربر ارسال می‌شود) —"
    )
    try:
        await call.message.answer(info)
        await call.message.answer(current, reply_markup=_build_msg_edit_view_kb(key))
    except Exception:
        # اگر HTML وارد شده توسط ادمین قبلاً نامعتبر بوده باشه (تگ ناقص و...)
        # حداقل خود پیام مدیریتی از کار نیفته
        await call.message.answer(info + "\n\n⚠️ نمایش پیش‌نمایش با خطا مواجه شد (احتمالاً HTML متن قبلی ناقص است).", reply_markup=_build_msg_edit_view_kb(key))


@router.callback_query(F.data.startswith("msgsettext:"))
async def admin_msg_set_text(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    key = call.data.split(":", 1)[1]
    if key not in MSG_TEMPLATES:
        await call.answer("❌ این پیام پیدا نشد.", show_alert=True)
        return
    await call.answer()
    meta = MSG_TEMPLATES[key]
    vars_line = ("، ".join(f"{{{v}}}" for v in meta["vars"])) if meta["vars"] else None
    hint = f"\n\n⚠️ حتماً این متغیرها رو داخل متن جدید نگه دار: {vars_line}" if vars_line else ""
    await call.message.answer(
        "✍️ متن جدید رو بفرست (فرمت HTML تلگرام مثل <b>پررنگ</b> پشتیبانی می‌شود).\n"
        "اگر می‌خوای ایموجی پرمیوم هم داشته باشه، از کیبورد ایموجی پرمیوم "
        "تلگرام انتخابش کن و همراه متن بفرست - نیازی به وارد کردن کد "
        "عددی‌اش نیست." + hint
    )
    await state.update_data(msg_key=key)
    await state.set_state(AdminFlow.waiting_msg_text)


@router.message(StateFilter(AdminFlow.waiting_msg_text))
async def on_msg_text_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("msg_key")
    if not key or key not in MSG_TEMPLATES:
        await state.clear()
        await message.answer("❌ خطای داخلی؛ دوباره از پنل مدیریت وارد شو.")
        return

    # message.html_text نسخه‌ی HTML پیامیه که ادمین فرستاده - اگر داخلش
    # ایموجی پرمیوم (custom_emoji entity) بوده باشه، aiogram خودش اون رو
    # به تگ <tg-emoji emoji-id="..."> تبدیل کرده؛ یعنی همین یک خط، دقیقاً
    # همون کاری رو می‌کنه که خواسته بودید: «خودش بفهمه کد عددی ایموجی چیه».
    new_text = message.html_text or message.text or message.caption or ""
    new_text = new_text.strip()
    if not new_text:
        await message.answer("❌ متن نمی‌تواند خالی باشد.")
        return

    await db_set_setting(key, new_text)
    await state.clear()
    meta = MSG_TEMPLATES[key]
    await message.answer(f"✅ متن «{meta['label']}» ذخیره شد.")
    try:
        await message.answer(new_text)
    except Exception:
        pass
    await message.answer("بازگشت به لیست پیام‌ها:", reply_markup=build_message_settings_keyboard())


@router.callback_query(F.data.startswith("msgreset:"))
async def admin_msg_reset(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    key = call.data.split(":", 1)[1]
    meta = MSG_TEMPLATES.get(key)
    if not meta:
        await call.answer("❌ این پیام پیدا نشد.", show_alert=True)
        return
    await db_set_setting(key, meta["default"])
    await call.answer("✅ به پیش‌فرض بازگردانده شد.")
    await safe_edit(call.message, _msgs_menu_text(), reply_markup=build_message_settings_keyboard())


# ------------------------------------------------------------------------- #
#  پنل مدیریت > ایموجی پرمیوم اختصاصی هر مدل (Pattern) / بک‌گراند (Color)
# ------------------------------------------------------------------------- #
#  محدودیت مهم Bot API تلگرام: دکمه‌های شیشه‌ای (InlineKeyboardButton) هیچ
#  HTML/entity ای پشتیبانی نمی‌کنند، پس نمی‌شه ایموجی پرمیوم واقعی/انیمیشنی
#  رو *داخل خودِ دکمه* گذاشت - این یک محدودیت پلتفرمه، نه چیزی که با کد
#  این ربات قابل دور زدن باشه. راه‌حلی که اینجا پیاده شده: نسخه‌ی واقعی
#  (پرمیوم) توی متنی که بالای همون کیبورد فرستاده می‌شه نشون داده می‌شه
#  (render_trait_emoji_list در بخش UI بالاتر)، و خودِ دکمه فقط فالبکِ
#  یونیکد ساده‌ی همون ایموجی رو به‌عنوان پیشوند نشون می‌ده.

@router.callback_query(F.data.startswith("traitemoji_menu:"))
async def admin_trait_emoji_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    slug = call.data.split(":", 1)[1]
    kb = InlineKeyboardBuilder()
    kb.button(text="🧩 ایموجی مدل‌ها (Pattern)", callback_data=f"traitemoji_list:pattern:{slug}:0")
    kb.button(text="🌈 ایموجی بک‌گراندها (Backdrop)", callback_data=f"traitemoji_list:color:{slug}:0")
    kb.button(text="🔙 بازگشت", callback_data=f"addview:{slug}")
    kb.adjust(1)
    await safe_edit(call.message, 
        "✨ <b>ایموجی پرمیوم مدل/بک‌گراند</b>\n\n"
        "کدام دسته رو می‌خوای ایموجی پرمیوم براش تنظیم کنی؟\n\n"
        "⚠️ توجه: به‌خاطر محدودیت خودِ تلگرام، ایموجی پرمیوم واقعی/انیمیشنی "
        "نمی‌تونه داخل متن دکمه‌های شیشه‌ای نشون داده بشه (Bot API این رو "
        "پشتیبانی نمی‌کنه) - نسخه‌ی واقعی‌اش بالای همون کیبورد در قالب "
        "پیام نشون داده می‌شه و خودِ دکمه فقط با ایموجی ساده (فالبک) مشخص "
        "می‌شود.",
        reply_markup=kb.as_markup(),
    )


async def _render_trait_emoji_list_view(call: CallbackQuery, trait_type: str, slug: str, page: int):
    base_link = f"https://t.me/nft/{slug}-"
    patterns, colors = await db_get_distinct_patterns_colors(base_link)
    values = sorted(patterns if trait_type == "pattern" else colors)
    emoji_map = await db_get_trait_emoji_map(base_link, trait_type)

    page_size = 10
    total_pages = max(1, (len(values) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    page_values = values[page * page_size: (page + 1) * page_size]

    kb = InlineKeyboardBuilder()
    for v in page_values:
        mark = f"{emoji_map[v][1]} " if v in emoji_map else "▫️ "
        # مقدار trait به‌جای رفتن مستقیم توی callback_data، به شکل ایندکس
        # توی همین لیست مرتب‌شده کدگذاری می‌شه (پایدار چون از همون کوئری
        # مرتب‌شده هر بار دوباره ساخته می‌شه) تا محدودیت طول/فرمت
        # callback_data تلگرام (۶۴ بایت) هیچ‌وقت مشکل‌ساز نشه.
        real_idx = values.index(v)
        kb.button(text=f"{mark}{v}", callback_data=f"traitemoji_pick:{trait_type}:{slug}:{real_idx}")
    if page > 0:
        kb.button(text="◀️ قبلی", callback_data=f"traitemoji_list:{trait_type}:{slug}:{page-1}")
    if page < total_pages - 1:
        kb.button(text="بعدی ▶️", callback_data=f"traitemoji_list:{trait_type}:{slug}:{page+1}")
    kb.button(text="🔙 بازگشت", callback_data=f"traitemoji_menu:{slug}")
    kb.adjust(1)

    label = "مدل‌ (Pattern)" if trait_type == "pattern" else "بک‌گراند (Backdrop)"
    await safe_edit(call.message, 
        f"✨ <b>ایموجی {label} های کالکشن {slug}</b>\n\n"
        f"صفحه {page+1} از {total_pages} | {len(values)} مورد\n"
        "روی هرکدوم بزن تا ایموجی پرمیومش رو تنظیم/تغییر بدی:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("traitemoji_list:"))
async def admin_trait_emoji_list(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    _, trait_type, slug, page = call.data.split(":", 3)
    await _render_trait_emoji_list_view(call, trait_type, slug, int(page))


@router.callback_query(F.data.startswith("traitemoji_pick:"))
async def admin_trait_emoji_pick(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    _, trait_type, slug, idx_str = call.data.split(":", 3)
    base_link = f"https://t.me/nft/{slug}-"
    patterns, colors = await db_get_distinct_patterns_colors(base_link)
    values = sorted(patterns if trait_type == "pattern" else colors)
    try:
        trait_value = values[int(idx_str)]
    except (ValueError, IndexError):
        await call.answer("❌ این مورد دیگر معتبر نیست (لیست تغییر کرده)؛ دوباره وارد لیست شو.", show_alert=True)
        return
    await call.answer()

    emoji_map = await db_get_trait_emoji_map(base_link, trait_type)
    current = emoji_map.get(trait_value)
    current_line = f"\n\nایموجی فعلی: <tg-emoji emoji-id=\"{current[0]}\">{current[1]}</tg-emoji>" if current else "\n\nهنوز ایموجی‌ای برای این مورد تنظیم نشده."

    kb = InlineKeyboardBuilder()
    if current:
        kb.button(text="🗑 حذف ایموجی این مورد", callback_data=f"traitemoji_del:{trait_type}:{slug}:{idx_str}")
    kb.button(text="🔙 بازگشت", callback_data=f"traitemoji_list:{trait_type}:{slug}:0")
    kb.adjust(1)

    await call.message.answer(
        f"✨ <b>{trait_value}</b>{current_line}\n\n"
        "برای تنظیم/تغییر ایموجی پرمیوم این مورد، یک پیام حاوی *فقط همون "
        "ایموجی پرمیوم* (از کیبورد ایموجی پرمیوم تلگرام) بفرست - ربات "
        "کد عددی‌اش رو خودکار تشخیص می‌ده و ذخیره می‌کنه.\n"
        "⚠️ اگر ایموجی فرستاده‌شده پرمیوم نباشد (یک ایموجی معمولی)، به‌جای "
        "کد پرمیوم فقط همون کاراکتر ساده به‌عنوان فالبک ذخیره می‌شود.",
        reply_markup=kb.as_markup(),
    )
    await state.update_data(trait_type=trait_type, trait_value=trait_value, trait_base_link=base_link, trait_slug=slug)
    await state.set_state(AdminFlow.waiting_trait_emoji)


@router.callback_query(F.data.startswith("traitemoji_del:"))
async def admin_trait_emoji_del(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    _, trait_type, slug, idx_str = call.data.split(":", 3)
    base_link = f"https://t.me/nft/{slug}-"
    patterns, colors = await db_get_distinct_patterns_colors(base_link)
    values = sorted(patterns if trait_type == "pattern" else colors)
    try:
        trait_value = values[int(idx_str)]
    except (ValueError, IndexError):
        await call.answer("❌ این مورد دیگر معتبر نیست.", show_alert=True)
        return
    await db_delete_trait_emoji(base_link, trait_type, trait_value)
    await call.answer("✅ ایموجی این مورد حذف شد.")
    await _render_trait_emoji_list_view(call, trait_type, slug, 0)


@router.message(StateFilter(AdminFlow.waiting_trait_emoji))
async def on_trait_emoji_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    trait_type = data.get("trait_type")
    trait_value = data.get("trait_value")
    base_link = data.get("trait_base_link")
    slug = data.get("trait_slug")
    if not (trait_type and trait_value and base_link):
        await state.clear()
        await message.answer("❌ خطای داخلی؛ دوباره از پنل مدیریت وارد شو.")
        return

    entities = message.entities or []
    custom_emoji_entity = next((e for e in entities if e.type == "custom_emoji"), None)
    if not custom_emoji_entity:
        await message.answer(
            "❌ توی پیامت هیچ ایموجی پرمیومی پیدا نکردم. یک ایموجی پرمیوم "
            "(از کیبورد ایموجی پرمیوم تلگرام، مخصوص اکانت‌های پرمیوم) بفرست."
        )
        return

    emoji_id = custom_emoji_entity.custom_emoji_id
    text = message.text or ""
    fallback = text[custom_emoji_entity.offset: custom_emoji_entity.offset + custom_emoji_entity.length] or "🔸"

    await db_set_trait_emoji(base_link, trait_type, trait_value, emoji_id, fallback)
    await state.clear()
    label = "مدل" if trait_type == "pattern" else "بک‌گراند"
    await message.answer(
        f"✅ ایموجی {label} «{trait_value}» ذخیره شد: "
        f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    )
    if slug:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 بازگشت به لیست", callback_data=f"traitemoji_list:{trait_type}:{slug}:0")
        kb.adjust(1)
        await message.answer("می‌تونی مورد بعدی رو هم تنظیم کنی:", reply_markup=kb.as_markup())


# ------------------------------------------------------------------------- #
#  پنل مدیریت > اضافه کردن (اسکن و ذخیره‌ی کامل یک کالکشن در دیتابیس)
# ------------------------------------------------------------------------- #

_admin_scan_running: set[str] = set()   # base_link هایی که الان در حال اسکن/بروزرسانی پس‌زمینه هستند


async def _build_add_menu_text_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    collections = await db_get_admin_collections()
    kb = InlineKeyboardBuilder()
    for base_link, total_items, status, added_at, last_refreshed_at, search_locked in collections:
        slug = extract_slug(base_link)
        dot = "🟡" if base_link in _admin_scan_running else ("🟢" if status == "done" else "⚪️")
        lock_icon = "🔒" if search_locked else ""
        kb.button(text=f"{dot} {lock_icon}{slug} ({total_items})", callback_data=f"addview:{slug}")
    kb.button(text="➕ افزودن لینک جدید", callback_data="admin:add_new")
    kb.button(text="🔄 بروزرسانی همه", callback_data="admin:add_refresh_all")
    kb.button(text="🔙 بازگشت", callback_data="admin:back")
    kb.adjust(1)

    if collections:
        text = f"🗄 <b>مدیریت دیتابیس NFT ها</b>\n\nتعداد کالکشن‌های ثبت‌شده: {len(collections)}"
    else:
        text = "🗄 <b>مدیریت دیتابیس NFT ها</b>\n\nهنوز هیچ کالکشنی اضافه نشده."
    return text, kb.as_markup()


@router.callback_query(F.data == "admin:add_menu")
async def admin_add_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    text, kb = await _build_add_menu_text_and_kb()
    await safe_edit(call.message, text, reply_markup=kb)


@router.callback_query(F.data == "admin:add_new")
async def admin_add_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    await safe_edit(call.message, 
        "🔗 لینک پایه کالکشن مورد نظر رو بفرست، مثلاً:\n"
        "<code>https://t.me/nft/CollectionName-</code>\n\n"
        "با زدن این لینک، تمام NFT های اون کالکشن اسکن و بر اساس مدل/بک‌گراند "
        "توی دیتابیس دسته‌بندی می‌شن."
    )
    await state.set_state(AdminFlow.waiting_add_link)


async def admin_scan_and_add(base_link: str, status_msg: Message):
    """اسکن کامل یک کالکشن در پس‌زمینه و ذخیره‌ی آن در دیتابیس + لیست «اضافه کردن»."""
    if base_link in _admin_scan_running:
        try:
            await status_msg.edit_text("⚠️ این کالکشن همین الان هم در حال اسکن است.")
        except Exception:
            pass
        return

    _admin_scan_running.add(base_link)
    try:
        await db_upsert_admin_collection(base_link, 0, "running")

        count_progress_throttle = ProgressThrottle(3.0)

        async def progress(found_so_far: int):
            if not count_progress_throttle.ready():
                return
            try:
                await status_msg.edit_text(
                    f"⏳ در حال پیدا کردن تعداد کل کالکشن...\n"
                    f"تا الان حداقل {found_so_far} آیتم معتبر پیدا شده."
                )
            except Exception:
                pass

        total = await discover_total_count(base_link, progress_cb=progress)
        if total <= 0:
            await db_upsert_admin_collection(base_link, 0, "empty")
            try:
                await status_msg.edit_text("⚠️ هیچ آیتم معتبری زیر این لینک پیدا نشد.")
            except Exception:
                pass
            return

        scanned = 0
        batch_progress_throttle = ProgressThrottle(3.0)
        batch_size = get_max_concurrent() * 4
        for start in range(1, total + 1, batch_size):
            idxs = list(range(start, min(start + batch_size, total + 1)))
            await scan_indices_cached(base_link, idxs)
            scanned += len(idxs)
            if batch_progress_throttle.ready():
                try:
                    await status_msg.edit_text(f"⏳ در حال اسکن و دسته‌بندی... {scanned}/{total}")
                except Exception:
                    pass

        await db_upsert_admin_collection(base_link, total, "done")
        try:
            await status_msg.edit_text(
                f"✅ کالکشن با موفقیت اضافه شد.\n"
                f"📦 {total} آیتم اسکن و در دیتابیس دسته‌بندی شد.\n"
                f"🔗 <code>{base_link}</code>"
            )
        except Exception:
            pass
    except Exception:
        log.exception("خطا در اسکن/افزودن کالکشن توسط ادمین")
        try:
            await status_msg.edit_text("❌ خطایی در حین اسکن این کالکشن رخ داد. بعداً دوباره امتحان کن.")
        except Exception:
            pass
    finally:
        _admin_scan_running.discard(base_link)


@router.message(StateFilter(AdminFlow.waiting_add_link))
async def admin_on_add_link(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    base = normalize_base_link(message.text or "")
    if not base:
        await message.answer("❌ فرمت لینک درست نیست. دوباره امتحان کن (مثال: https://t.me/nft/Name-)")
        return
    await state.clear()
    status_msg = await message.answer(f"⏳ شروع اسکن و اضافه‌کردن کالکشن...\n<code>{base}</code>")
    asyncio.create_task(admin_scan_and_add(base, status_msg))


async def _render_admin_add_view(call: CallbackQuery, slug: str):
    base_link = f"https://t.me/nft/{slug}-"
    row = await db_get_admin_collection(base_link)
    if not row:
        await safe_edit(call.message, "❌ این کالکشن پیدا نشد.", reply_markup=build_admin_keyboard())
        return

    base_link, total_items, status, added_at, last_refreshed_at, search_locked = row
    added_str = datetime.fromtimestamp(added_at).strftime("%Y-%m-%d %H:%M")
    refreshed_str = datetime.fromtimestamp(last_refreshed_at).strftime("%Y-%m-%d %H:%M")
    running = base_link in _admin_scan_running
    locked = bool(search_locked)
    valid_count = await db_get_valid_count(base_link)

    kb = InlineKeyboardBuilder()
    kb.button(
        text=("⏳ در حال بروزرسانی..." if running else "🔄 بروزرسانی (چک آیتم‌های جدید بعد از مرز فعلی)"),
        callback_data="noop" if running else f"addrefresh:{slug}",
    )
    kb.button(
        text=("⏳ در حال بررسی..." if running else "🆕 بررسی NFT های تازه ثبت‌شده (گپ‌ها)"),
        callback_data="noop" if running else f"addrecheckgaps:{slug}",
    )
    kb.button(
        text=("⏳ در حال اسکن مجدد..." if running else "🔁 اسکن مجدد مدل/بک‌گراند (اصلاح تشخیص اشتباه)"),
        callback_data="noop" if running else f"addrediscover:{slug}",
    )
    kb.button(
        text=("🔒 سرچ این کالکشن قفل است (بزن برای باز کردن)" if locked
              else "🔓 سرچ این کالکشن باز است (بزن برای قفل کردن)"),
        callback_data=f"addlock:{slug}",
    )
    kb.button(text="✨ ایموجی پرمیوم مدل/بک‌گراند", callback_data=f"traitemoji_menu:{slug}")
    kb.button(text="🗑 حذف کامل این کالکشن", callback_data=f"adddelete:{slug}")
    kb.button(text="🔙 بازگشت", callback_data="admin:add_menu")
    kb.adjust(1)

    lock_desc = (
        "🔒 قفل: کاربرها برای این کالکشن مستقیم از تعداد/مدل‌ها/رنگ‌های "
        "همین الان کش‌شده استفاده می‌کنند (بدون هیچ رفت‌وبرگشت شبکه‌ای "
        "برای پیدا کردن دوباره‌شون - آنی)."
        if locked else
        "🔓 باز: هر بار کاربری این لینک رو بفرسته، دوباره تعداد/مدل‌ها/"
        "رنگ‌ها جستجو می‌شن (برای وقتی کالکشن ممکنه رشد کرده باشه)."
    )

    await safe_edit(call.message, 
        f"📦 <b>{slug}</b>\n\n"
        f"وضعیت: {status}\n"
        f"📦 تعداد واقعی NFT ثبت‌شده (معتبر): <b>{valid_count}</b>\n"
        f"📏 بازه‌ی ایندیس بررسی‌شده تا الان: ۱ تا {total_items}\n"
        f"اولین افزودن: {added_str}\n"
        f"آخرین بروزرسانی: {refreshed_str}\n"
        f"🔗 <code>{base_link}</code>\n\n"
        f"{lock_desc}\n\n"
        f"⚠️ اگر یک NFT رو ثبت/آپگرید کردی ولی عدد بالا آپدیت نشد، دلیلش "
        f"اینه که این ایندیس قبلاً داخل بازه‌ی چک‌شده بوده و «نامعتبر» کش "
        f"شده بود - «🔄 بروزرسانی» فقط ایندیس‌های *بعد از* مرز فعلی رو "
        f"چک می‌کنه، نه گپ‌های داخل بازه‌ی قبلی. برای همین از دکمه‌ی "
        f"«🆕 بررسی NFT های تازه ثبت‌شده» استفاده کن - دقیقاً همون آیتم‌های "
        f"نامعتبر قبلی رو دوباره و بدون کش چک می‌کند (بدون نیاز به حذف و "
        f"افزودن دوباره‌ی کل کالکشن).\n\n"
        f"🔁 «اسکن مجدد مدل/بک‌گراند»: تمام آیتم‌های این کالکشن دوباره و "
        f"کاملاً از صفر (بدون استفاده از تشخیص قبلی) بررسی می‌شود؛ استفاده "
        f"کن اگر مدل/بک‌گراند بعضی آیتم‌ها قبلاً اشتباه تشخیص داده شده "
        f"(مثلاً مدل ۵۰ به‌جای ۵۵، یا حروف نامفهوم).\n"
        f"🗑 «حذف کامل»: این کالکشن و تمام آیتم‌های کش‌شده‌اش کاملاً پاک "
        f"می‌شود؛ بعد از حذف می‌تونی همون لینک رو دوباره از «🗄 مدیریت "
        f"دیتابیس» از صفر اضافه کنی.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("addview:"))
async def admin_add_view(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    slug = call.data.split(":", 1)[1]
    await _render_admin_add_view(call, slug)


@router.callback_query(F.data.startswith("addlock:"))
async def admin_add_toggle_lock(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    slug = call.data.split(":", 1)[1]
    base_link = f"https://t.me/nft/{slug}-"
    current = await db_get_collection_lock(base_link)
    await db_set_collection_lock(base_link, not current)
    await call.answer("🔒 قفل شد" if not current else "🔓 باز شد")
    # همون صفحه‌ی جزئیات رو با وضعیت تازه دوباره نشون بده
    await _render_admin_add_view(call, slug)


async def admin_rediscover_traits(base_link: str, status_msg: Message):
    """
    اسکن مجدد و اجباری مدل/بک‌گراند (Pattern/Color) تمام آیتم‌های یک کالکشن
    که قبلاً به لیست «افزودن» اضافه شده - کاملاً بدون اتکا به کش/تشخیص
    قبلی (که ممکنه اشتباه بوده باشه، مثلاً مدل «۵۰» که به اشتباه «۵۵» یا
    با حروف نامفهوم/چرت‌وپرت استخراج شده). هر آیتم دوباره از شبکه گرفته و
    از صفر پارس می‌شود (scan_indices_force) و نتیجه‌ی تازه جایگزین رکورد
    قدیمی همون آیتم در دیتابیس می‌شود. کش کارت‌های تصویری هم پاک می‌شود
    چون احتمالاً بر اساس مدل/رنگِ اشتباه قبلی ساخته شده بودند.
    """
    if base_link in _admin_scan_running:
        try:
            await status_msg.edit_text("⚠️ این کالکشن همین الان هم در حال اسکن/بروزرسانی است.")
        except Exception:
            pass
        return

    row = await db_get_admin_collection(base_link)
    if not row:
        try:
            await status_msg.edit_text("❌ این کالکشن پیدا نشد.")
        except Exception:
            pass
        return
    _, total_items, *_rest = row
    # قبل از «آپدیت مدل/بک‌گراند» تعداد زنده‌ی کالکشن را دوباره می‌گیریم؛
    # این‌طوری اگر از آخرین اسکن NFT جدیدی اضافه شده باشد، آن‌ها هم داخل
    # بازاسکن کامل قرار می‌گیرند و total قدیمی باعث جا افتادن‌شان نمی‌شود.
    try:
        live_total = await discover_total_count(base_link)
        if live_total > 0:
            total_items = live_total
    except Exception:
        log.exception("گرفتن تعداد زنده قبل از rediscover ناموفق بود")

    if total_items <= 0:
        try:
            await status_msg.edit_text("⚠️ این کالکشن آیتمی برای اسکن مجدد ندارد.")
        except Exception:
            pass
        return

    _admin_scan_running.add(base_link)
    try:
        def _clear_media_cache():
            conn = _get_conn()
            conn.execute("DELETE FROM nft_media WHERE base_link=?", (base_link,))
            conn.execute("DELETE FROM nft_media_combo WHERE base_link=?", (base_link,))
            conn.commit()

        async with _db_lock:
            await asyncio.to_thread(_clear_media_cache)
        _seen_image_hashes.pop(base_link, None)

        scanned = 0
        rediscover_progress_throttle = ProgressThrottle(3.0)
        batch_size = get_max_concurrent() * 4
        for start in range(1, total_items + 1, batch_size):
            idxs = list(range(start, min(start + batch_size, total_items + 1)))
            await scan_indices_force(base_link, idxs)
            scanned += len(idxs)
            if rediscover_progress_throttle.ready():
                try:
                    await status_msg.edit_text(
                        f"🔁 در حال اسکن مجدد مدل/بک‌گراند... {scanned}/{total_items}"
                    )
                except Exception:
                    pass

        await db_upsert_admin_collection(base_link, total_items, "done")
        await db_store_total(base_link, total_items)
        try:
            await status_msg.edit_text(
                f"✅ آپدیت مدل/بک‌گراند تمام شد.\n"
                f"📦 {total_items} آیتم از صفر دوباره بررسی و دسته‌بندی شد.\n"
                f"🧹 مقادیر خراب/تکراری قبلی با مقدار canonical جایگزین شدند.\n"
                f"🔗 <code>{base_link}</code>"
            )
        except Exception:
            pass
    except Exception:
        log.exception("خطا در اسکن مجدد مدل/بک‌گراند کالکشن")
        try:
            await status_msg.edit_text("❌ خطایی در حین اسکن مجدد رخ داد. بعداً دوباره امتحان کن.")
        except Exception:
            pass
    finally:
        _admin_scan_running.discard(base_link)


@router.callback_query(F.data.startswith("addrediscover:"))
async def admin_add_rediscover(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    slug = call.data.split(":", 1)[1]
    base_link = f"https://t.me/nft/{slug}-"
    if base_link in _admin_scan_running:
        await call.answer("⚠️ این کالکشن همین الان هم در حال اسکن است.", show_alert=True)
        return
    await call.answer("🔁 اسکن مجدد شروع شد...")
    status_msg = await call.message.answer(
        f"⏳ شروع اسکن مجدد مدل/بک‌گراند...\n<code>{base_link}</code>"
    )
    asyncio.create_task(admin_rediscover_traits(base_link, status_msg))


async def admin_recheck_gaps(base_link: str, status_msg: Message):
    """
    فقط ایندیس‌هایی که قبلاً *نامعتبر* کش شده بودند (گیفت‌های آپگرید‌نشده)
    را با fetch اجباری (بدون کش) دوباره چک می‌کند. اگر یکی از این گیفت‌ها
    از دفعه قبل تا الان به NFT واقعی آپگرید/ثبت شده باشه، این‌جا شناسایی
    و در دیتابیس (valid=1 + مدل/بک‌گراند واقعی) جایگزین می‌شود.

    چرا این جدا از «🔄 بروزرسانی» است: بروزرسانی فقط ایندیس‌های *جدید* بعد
    از آخرین مرز شناخته‌شده رو چک می‌کنه؛ اگر گیفت آپگرید‌شده داخل بازه‌ی
    از قبل اسکن‌شده باشه (نه لزوماً آخرین ایندیس)، بروزرسانی هیچ‌وقت اون
    رو نمی‌بینه. این تابع دقیقاً همون شکافی رو پر می‌کنه که قبلاً فقط با
    «حذف کامل کالکشن و افزودن دوباره‌اش» قابل حل بود.
    """
    if base_link in _admin_scan_running:
        try:
            await status_msg.edit_text("⚠️ این کالکشن همین الان هم در حال اسکن/بروزرسانی است.")
        except Exception:
            pass
        return

    row = await db_get_admin_collection(base_link)
    if not row:
        try:
            await status_msg.edit_text("❌ این کالکشن پیدا نشد.")
        except Exception:
            pass
        return
    _, total_items, *_rest = row
    if total_items <= 0:
        try:
            await status_msg.edit_text("⚠️ این کالکشن آیتمی برای بررسی ندارد.")
        except Exception:
            pass
        return

    _admin_scan_running.add(base_link)
    try:
        before_valid = await db_get_valid_count(base_link)
        gap_idxs = await db_get_invalid_indices(base_link, total_items)

        if not gap_idxs:
            try:
                await status_msg.edit_text(
                    "✅ هیچ آیتم نامعتبر (گپ) کش‌شده‌ای در این کالکشن وجود ندارد؛ "
                    "چیزی برای بررسی مجدد نبود."
                )
            except Exception:
                pass
            return

        try:
            await status_msg.edit_text(
                f"🔎 در حال بررسی مجدد {len(gap_idxs)} آیتم نامعتبر قبلی "
                f"(گیفت‌های احتمالاً تازه آپگرید‌شده)..."
            )
        except Exception:
            pass

        checked = 0
        newly_valid = 0
        batch_size = get_max_concurrent() * 4
        recheck_progress_throttle = ProgressThrottle(3.0)
        for start in range(0, len(gap_idxs), batch_size):
            batch = gap_idxs[start:start + batch_size]
            results = await scan_indices_force(base_link, batch)
            for (url, valid, pattern, color, net_err) in results:
                if valid:
                    newly_valid += 1
            checked += len(batch)
            if recheck_progress_throttle.ready():
                try:
                    await status_msg.edit_text(
                        f"🔎 در حال بررسی مجدد آیتم‌های نامعتبر قبلی... "
                        f"{checked}/{len(gap_idxs)} (تا الان {newly_valid} مورد جدید معتبر پیدا شد)"
                    )
                except Exception:
                    pass

        after_valid = await db_get_valid_count(base_link)
        await db_upsert_admin_collection(base_link, total_items, "done")
        try:
            await status_msg.edit_text(
                f"✅ بررسی مجدد گپ‌ها تمام شد.\n"
                f"🔁 {len(gap_idxs)} آیتم نامعتبر قبلی دوباره چک شد.\n"
                f"✨ {after_valid - before_valid} مورد از آن‌ها الان معتبر (آپگرید‌شده) بود.\n"
                f"📦 تعداد واقعی NFT ثبت‌شده الان: {after_valid}\n"
                f"🔗 <code>{base_link}</code>"
            )
        except Exception:
            pass
    except Exception:
        log.exception("خطا در بررسی مجدد آیتم‌های نامعتبر کالکشن")
        try:
            await status_msg.edit_text("❌ خطایی در حین بررسی مجدد رخ داد. بعداً دوباره امتحان کن.")
        except Exception:
            pass
    finally:
        _admin_scan_running.discard(base_link)


@router.callback_query(F.data.startswith("addrecheckgaps:"))
async def admin_add_recheck_gaps(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    slug = call.data.split(":", 1)[1]
    base_link = f"https://t.me/nft/{slug}-"
    if base_link in _admin_scan_running:
        await call.answer("⚠️ این کالکشن الان در حال اسکن است.", show_alert=True)
        return
    await call.answer("🔎 بررسی مجدد گپ‌ها شروع شد...")
    status_msg = await call.message.answer(
        f"⏳ شروع بررسی مجدد آیتم‌های نامعتبر...\n<code>{base_link}</code>"
    )
    asyncio.create_task(admin_recheck_gaps(base_link, status_msg))


@router.callback_query(F.data.startswith("adddelete:"))
async def admin_add_delete_ask(call: CallbackQuery, state: FSMContext):
    """نمایش صفحه‌ی تایید قبل از حذف کامل (برای جلوگیری از حذف تصادفی با یک تپ)."""
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    slug = call.data.split(":", 1)[1]
    base_link = f"https://t.me/nft/{slug}-"
    if base_link in _admin_scan_running:
        await call.answer("⚠️ این کالکشن الان در حال اسکن است؛ صبر کن تمام بشه.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 بله، کامل حذف کن", callback_data=f"adddeleteok:{slug}")
    kb.button(text="🔙 انصراف", callback_data=f"addview:{slug}")
    kb.adjust(1)
    await safe_edit(
        call.message,
        f"⚠️ <b>حذف کامل کالکشن</b>\n\n"
        f"📦 <code>{slug}</code>\n\n"
        f"با تایید، این کالکشن و تمام آیتم‌های کش‌شده‌اش (مدل/بک‌گراند هر "
        f"آیتم) کاملاً از دیتابیس پاک می‌شوند. این کار قابل بازگشت نیست، "
        f"ولی هر وقت خواستی می‌تونی همین لینک رو دوباره از «➕ افزودن لینک "
        f"جدید» از صفر اضافه کنی.\n\n"
        f"مطمئنی؟",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("adddeleteok:"))
async def admin_add_delete_confirm(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    slug = call.data.split(":", 1)[1]
    base_link = f"https://t.me/nft/{slug}-"
    if base_link in _admin_scan_running:
        await call.answer("⚠️ این کالکشن الان در حال اسکن است؛ صبر کن تمام بشه.", show_alert=True)
        return
    await db_delete_admin_collection(base_link)
    _seen_image_hashes.pop(base_link, None)
    await call.answer("🗑 کامل حذف شد.")
    text, kb = await _build_add_menu_text_and_kb()
    await safe_edit(call.message, text, reply_markup=kb)


@router.callback_query(F.data == "noop")
async def noop_callback(call: CallbackQuery):
    await call.answer()


async def admin_refresh_collection(base_link: str, status_msg: Message, total_override: Optional[int] = None):
    """
    بروزرسانی هوشمند یک کالکشن:
      ۱) اول تعداد واقعی/زنده‌ی الان کالکشن با discover_total_count() پیدا می‌شود
         (همون تابعی که در «اضافه کردن» استفاده می‌شود؛ چون از تعداد کش‌شده به‌عنوان
         نقطه شروع استفاده می‌کند، این مرحله برای کالکشن‌های تکراری تقریباً آنی است).
      ۲) با تعداد آیتم‌هایی که همین الان در دیتابیس ثبت شده مقایسه می‌شود.
      ۳) فقط آیتم‌های جدید (از اون به بعد) اسکن و در دیتابیس اضافه می‌شوند -
         نه اینکه همه‌ی آیتم‌های قبلی از صفر دوباره fetch شوند.
    """
    if base_link in _admin_scan_running:
        try:
            await status_msg.edit_text("⚠️ این کالکشن همین الان هم در حال بروزرسانی است.")
        except Exception:
            pass
        return

    _admin_scan_running.add(base_link)
    try:
        existing_total = total_override if total_override else (await db_get_cached_total(base_link) or 0)

        try:
            await status_msg.edit_text(
                f"🔎 در حال بررسی تعداد واقعی/زنده‌ی کالکشن...\n"
                f"تعداد فعلی در دیتابیس: {existing_total}"
            )
        except Exception:
            pass

        count_progress_throttle = ProgressThrottle(3.0)

        async def progress(found_so_far: int):
            if not count_progress_throttle.ready():
                return
            try:
                await status_msg.edit_text(
                    f"🔎 در حال بررسی تعداد واقعی/زنده‌ی کالکشن...\n"
                    f"تعداد فعلی در دیتابیس: {existing_total} | تا الان حداقل {found_so_far} پیدا شده."
                )
            except Exception:
                pass

        new_total = await discover_total_count(base_link, progress_cb=progress)

        if new_total <= 0:
            await db_upsert_admin_collection(base_link, existing_total, "done")
            try:
                await status_msg.edit_text("⚠️ هیچ آیتم معتبری برای این کالکشن پیدا نشد.")
            except Exception:
                pass
            return

        if new_total <= existing_total:
            await db_upsert_admin_collection(base_link, existing_total, "done")
            try:
                await status_msg.edit_text(
                    f"✅ آیتم جدیدی پیدا نشد - دیتابیس از قبل به‌روز است.\n"
                    f"📦 مجموع: {existing_total} آیتم."
                )
            except Exception:
                pass
            return

        new_count = new_total - existing_total
        batch_size = get_max_concurrent() * 4
        scanned_new = 0
        new_items_progress_throttle = ProgressThrottle(3.0)
        for start in range(existing_total + 1, new_total + 1, batch_size):
            idxs = list(range(start, min(start + batch_size, new_total + 1)))
            await scan_indices_cached(base_link, idxs)
            scanned_new += len(idxs)
            if new_items_progress_throttle.ready():
                try:
                    await status_msg.edit_text(
                        f"➕ در حال افزودن آیتم‌های جدید <code>{extract_slug(base_link)}</code>...\n"
                        f"{scanned_new}/{new_count} آیتم جدید بررسی شد (مجموع: {existing_total + scanned_new}/{new_total})"
                    )
                except Exception:
                    pass

        await db_upsert_admin_collection(base_link, new_total, "done")
        try:
            await status_msg.edit_text(
                f"✅ بروزرسانی کامل شد.\n"
                f"➕ {new_count} آیتم جدید اضافه شد.\n"
                f"📦 مجموع فعلی: {new_total} آیتم.\n"
                f"🔗 <code>{base_link}</code>"
            )
        except Exception:
            pass
    except Exception:
        log.exception("خطا در بروزرسانی کالکشن توسط ادمین")
        try:
            await status_msg.edit_text("❌ خطایی در حین بروزرسانی این کالکشن رخ داد.")
        except Exception:
            pass
    finally:
        _admin_scan_running.discard(base_link)


@router.callback_query(F.data.startswith("addrefresh:"))
async def admin_add_refresh_one(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    slug = call.data.split(":", 1)[1]
    base_link = f"https://t.me/nft/{slug}-"
    status_msg = await call.message.answer(f"🔄 شروع بروزرسانی <code>{slug}</code>...")
    asyncio.create_task(admin_refresh_collection(base_link, status_msg))


_refresh_all_running = False


async def admin_refresh_all_collections(collections: list[tuple], status_msg: Message):
    global _refresh_all_running
    if _refresh_all_running:
        try:
            await status_msg.edit_text("⚠️ بروزرسانی همه همین الان هم در حال اجراست.")
        except Exception:
            pass
        return

    _refresh_all_running = True
    try:
        total_collections = len(collections)
        for i, (base_link, total_items, status, added_at, last_refreshed_at, search_locked) in enumerate(collections, start=1):
            try:
                await status_msg.edit_text(
                    f"🔄 در حال بروزرسانی کالکشن {i}/{total_collections}: "
                    f"<code>{extract_slug(base_link)}</code>..."
                )
            except Exception:
                pass
            await admin_refresh_collection(base_link, status_msg, total_override=total_items or None)
        try:
            await status_msg.edit_text(f"✅ بروزرسانی همه‌ی {total_collections} کالکشن به پایان رسید.")
        except Exception:
            pass
    finally:
        _refresh_all_running = False


@router.callback_query(F.data == "admin:add_refresh_all")
async def admin_add_refresh_all(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی نداری.", show_alert=True)
        return
    await call.answer()
    collections = await db_get_admin_collections()
    if not collections:
        await call.message.answer("هیچ کالکشنی برای بروزرسانی ثبت نشده.")
        return
    status_msg = await call.message.answer(f"🔄 شروع بروزرسانی {len(collections)} کالکشن...")
    asyncio.create_task(admin_refresh_all_collections(collections, status_msg))


async def send_link_prompt(send_func, user_id: int, state: FSMContext):
    """سشن رو ریست می‌کند و دوباره منتظر لینک پایه کالکشن می‌ماند (شروع یک اسکن تازه)."""
    await state.clear()
    sessions[user_id] = SessionData()
    await send_func(get_msg("msg_start"))
    await state.set_state(ScanFlow.waiting_base_link)


async def send_main_menu(send_func, user_id: int, state: FSMContext):
    await state.clear()
    sessions[user_id] = SessionData()
    bal = await db_get_balance(user_id)
    used, free_rem = await db_get_quota_status(user_id)
    text = get_msg("msg_main_menu", free_remaining=free_rem, free_limit=DAILY_FREE_FILTER_LIMIT, balance=bal, credit_price=f"{get_credit_price_ton():g}")
    await send_func(text, reply_markup=build_main_menu_kb())


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await send_main_menu(message.answer, message.from_user.id, state)


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    bal = await db_get_balance(user_id)
    if is_admin(user_id):
        await message.answer(
            get_msg("msg_profile_admin") + f"\n\n💳 موجودی سرچ: <b>{bal}</b>",
            reply_markup=build_main_menu_kb(),
        )
        return

    used, remaining = await db_get_quota_status(user_id)
    text = get_msg(
        "msg_profile_user_header",
        used=used, limit=DAILY_FREE_FILTER_LIMIT, remaining=remaining,
    )
    text += f"💳 موجودی سرچ پولی: <b>{bal}</b>\n"
    text += f"(قیمت هر اعتبار = {get_credit_price_ton():g} TON)\n\n"
    if remaining <= 0 and bal <= 0:
        text += get_msg("msg_profile_exceeded", limit=DAILY_FREE_FILTER_LIMIT, support_username=SUPPORT_USERNAME)
    else:
        text += get_msg("msg_profile_remaining", support_username=SUPPORT_USERNAME)
    await message.answer(text, reply_markup=build_main_menu_kb())


@router.message(StateFilter(ScanFlow.waiting_base_link))
async def on_base_link(message: Message, state: FSMContext):
    base = normalize_base_link(message.text or "")
    if not base:
        await message.answer(get_msg("msg_invalid_link"))
        return

    user_id = message.from_user.id
    # سهمیه‌ی روزانه‌ی فیلتر کردن: فقط برای کاربرهای عادی اعمال می‌شود، ادمین محدودیت ندارد.
    # اینجا (نه انتهای اسکن) مصرف می‌شود چون همین لحظه است که منابع واقعی
    # (اسکن شبکه‌ای کالکشن) شروع به مصرف شدن می‌کند.
    if not is_admin(user_id):
        ok, info = await db_try_consume_search(user_id)
        if not ok:
            await message.answer(info, reply_markup=build_main_menu_kb())
            return

    sess = sessions.setdefault(user_id, SessionData())
    sess.base_link = base

    status_msg = await message.answer(get_msg("msg_discovering_count_start"))

    cached_total = await db_get_cached_total(base)
    collection_locked = await db_get_collection_lock(base)
    # اگر قفل نبود ولی قبلاً اسکن کامل شده، مثل قفل رفتار کن
    if not collection_locked and cached_total and cached_total > 0:
        if await db_check_full_coverage(base, cached_total):
            collection_locked = True
            try:
                await db_set_collection_lock(base, True)
            except Exception:
                pass

    if collection_locked and cached_total and cached_total > 0:
        # ------------------------------------------------------------- #
        # این کالکشن مشخص از پنل ادمین > افزودن قفل شده: قصد اصلی قفل این
        # بود که برای کالکشن‌های پرتکرار، تعداد/مدل‌ها/رنگ‌ها بدون هیچ
        # رفت‌وبرگشت شبکه‌ای از دیتابیس خونده بشه. مشکل نسخه‌ی قبلی این
        # بود که این حالت کورکورانه به cached_total اکتفا می‌کرد و *هیچ‌وقت*
        # چک نمی‌کرد که از آخرین بار، NFT جدیدی به کالکشن اضافه شده یا نه؛
        # تنها راه دیدن آیتم‌های جدید، حذف کامل و اضافه‌کردن دوباره‌ی
        # کالکشن از پنل ادمین بود.
        # راه‌حل: یک چک بسیار سبک و سریع (discover_total_count با شروع از
        # همون cached_total، یعنی فقط یکی-دو درخواست شبکه، نه اسکن کامل)
        # انجام می‌شه تا مطمئن بشیم چیزی جدید اضافه نشده. اگر واقعاً چیزی
        # اضافه نشده بود، این چک تقریباً آنیه و تجربه‌ی سریع قفل حفظ می‌شه؛
        # اگر آیتم جدیدی پیدا شد، همون‌جا مدل/رنگ‌های جدیدش هم کشف و در
        # دیتابیس ثبت می‌شه تا هم این کاربر هم کاربرهای بعدی از کش تازه
        # استفاده کنن.
        # ------------------------------------------------------------- #
        live_total = await discover_total_count(base)
        if live_total > cached_total:
            new_patterns: set[str] = set()
            new_colors: set[str] = set()
            for start in range(cached_total + 1, live_total + 1, DISCOVERY_BATCH_SIZE):
                batch_idxs = list(range(start, min(start + DISCOVERY_BATCH_SIZE - 1, live_total) + 1))
                results = await scan_indices_cached(base, batch_idxs)
                for url, valid, pattern, color, net_err in results:
                    if net_err or not valid:
                        continue
                    if pattern:
                        new_patterns.add(pattern)
                    if color:
                        new_colors.add(color)
            await db_upsert_admin_collection(base, live_total, "done")
            cached_total = live_total

        sess.total_available = cached_total
        patterns, colors = await db_get_distinct_patterns_colors(base)
        sess.available_patterns = patterns
        sess.available_colors = colors
        idx = cached_total + 1  # کوئری DISTINCT کل دیتابیس کش‌شده رو پوشش داده -> «نمونه‌برداری‌شده» = کل کالکشن
        try:
            await status_msg.edit_text(
                get_msg(
                    "msg_locked_collection_used",
                    cached_total=cached_total,
                    patterns_count=len(patterns),
                    colors_count=len(colors),
                )
            )
        except Exception:
            pass
    else:
        count_progress_throttle = ProgressThrottle(3.0)

        async def _count_progress(found_so_far: int):
            if not count_progress_throttle.ready():
                return
            try:
                await status_msg.edit_text(
                    get_msg("msg_discovering_count_progress", found_so_far=found_so_far)
                )
            except Exception:
                pass

        sess.total_available = await discover_total_count(base, progress_cb=_count_progress)

        if sess.total_available <= 0:
            await status_msg.edit_text(get_msg("msg_no_items_found"))
            return

        try:
            await status_msg.edit_text(
                get_msg("msg_count_found_discovering_traits", total_available=sess.total_available)
            )
        except Exception:
            pass

        discovery_cap = min(DISCOVERY_MAX_ITEMS, sess.total_available)
        idx = 1
        stable_streak = 0  # چند آیتم پشت‌سرهم بدون کشف ویژگی جدید
        discovery_progress_throttle = ProgressThrottle(3.0)

        while idx <= discovery_cap and stable_streak < DISCOVERY_STABILITY_WINDOW:
            batch_end = min(idx + DISCOVERY_BATCH_SIZE - 1, discovery_cap)
            batch_idxs = list(range(idx, batch_end + 1))
            results = await scan_indices_cached(base, batch_idxs)

            found_new_in_batch = False
            for url, valid, pattern, color, net_err in results:
                if net_err:
                    continue  # خطای گذرا (تایم‌اوت/۵xx) -> نه معتبر نه نامعتبر، نادیده گرفته می‌شود
                if not valid:
                    stable_streak += 1
                    continue
                is_new = False
                if pattern and pattern not in sess.available_patterns:
                    sess.available_patterns.add(pattern)
                    is_new = True
                if color and color not in sess.available_colors:
                    sess.available_colors.add(color)
                    is_new = True
                if is_new:
                    found_new_in_batch = True
                    stable_streak = 0
                else:
                    stable_streak += 1

            idx = batch_end + 1

            if discovery_progress_throttle.ready():
                try:
                    await status_msg.edit_text(
                        get_msg(
                            "msg_discovering_traits_progress",
                            scanned_so_far=idx - 1,
                            patterns_count=len(sess.available_patterns),
                            colors_count=len(sess.available_colors),
                        )
                    )
                except Exception:
                    pass

    if not sess.available_patterns and not sess.available_colors:
        await status_msg.edit_text(get_msg("msg_no_traits_found"))
        return

    await status_msg.edit_text(
        get_msg(
            "msg_discovery_complete",
            patterns_count=len(sess.available_patterns),
            colors_count=len(sess.available_colors),
            sampled=idx - 1,
            total_available=sess.total_available,
        )
    )
    scan_all_kb = InlineKeyboardBuilder()
    scan_all_kb.button(text="🔍 اسکن همه NFT های موجود در کالکشن", callback_data="scanall")
    scan_all_kb.adjust(1)
    await message.answer(get_msg("msg_scan_all_prompt"), reply_markup=scan_all_kb.as_markup())
    await state.set_state(ScanFlow.waiting_max_items)


async def _go_to_menus(user_id: int, send_func, state: FSMContext):
    """بعد از مشخص شدن سقف اسکن (عددی یا حالت همه)، به منوی مدل یا رنگ می‌رود."""
    sess = sessions[user_id]
    # ایموجی‌های پرمیوم اختصاصی این کالکشن (اگر ادمین برایشان تنظیم کرده
    # باشد) یک‌بار اینجا لود و در سشن کش می‌شوند تا توی toggle های بعدی
    # (که فقط reply_markup رو ادیت می‌کنند) دوباره از دیتابیس خونده نشوند.
    sess.pattern_emoji = await db_get_trait_emoji_map(sess.base_link, "pattern")
    sess.color_emoji = await db_get_trait_emoji_map(sess.base_link, "color")
    if sess.available_patterns:
        await _send_with_toggle_kb(
            send_func,
            get_msg("msg_pattern_prompt", trait_list=""),
            sess.available_patterns, sess.selected_patterns, "pat",
            select_all_active=sess.select_all_patterns,
            emoji_map=sess.pattern_emoji,
        )
        await state.set_state(ScanFlow.waiting_pattern_choice)
    else:
        await _send_with_toggle_kb(
            send_func,
            get_msg("msg_color_prompt", trait_list=""),
            sess.available_colors, sess.selected_colors, "col",
            select_all_active=sess.select_all_colors,
            emoji_map=sess.color_emoji,
        )
        await state.set_state(ScanFlow.waiting_color_choice)


@router.message(StateFilter(ScanFlow.waiting_max_items))
async def on_max_items(message: Message, state: FSMContext):
    if not (message.text or "").strip().isdigit():
        await message.answer(get_msg("msg_invalid_max_items_number"))
        return

    sess = sessions[message.from_user.id]
    # سقف واقعی = تعداد واقعی کالکشن اگر پیدا شده باشه، وگرنه fallback امنیتی مطلق
    real_cap = sess.total_available if sess.total_available > 0 else DEFAULT_MAX_ITEMS_CAP

    max_items = int(message.text.strip())
    if max_items <= 0 or max_items > real_cap:
        await message.answer(get_msg("msg_max_items_out_of_range", real_cap=real_cap))
        return

    sess.max_items = max_items
    sess.scan_all_mode = False

    await _go_to_menus(message.from_user.id, message.answer, state)


@router.callback_query(F.data == "scanall", StateFilter(ScanFlow.waiting_max_items))
async def on_scan_all(call: CallbackQuery, state: FSMContext):
    sess = sessions[call.from_user.id]
    # استفاده از تعداد واقعی کشف‌شده‌ی کالکشن (نه یک سقف ثابت دلخواه)
    sess.max_items = sess.total_available if sess.total_available > 0 else DEFAULT_MAX_ITEMS_CAP
    sess.scan_all_mode = True
    await safe_edit(call.message, 
        get_msg("msg_scan_all_activated", max_items=sess.max_items)
    )
    await _go_to_menus(call.from_user.id, call.message.answer, state)
    await call.answer()


@router.callback_query(F.data.startswith("pat:"), StateFilter(ScanFlow.waiting_pattern_choice))
async def on_pattern_toggle(call: CallbackQuery, state: FSMContext):
    sess = sessions[call.from_user.id]
    value = _cb_decode(call.data.split(":", 1)[1])

    if value == "__confirm__":
        if not sess.selected_patterns:
            await call.answer(get_msg("msg_pattern_confirm_min_one"), show_alert=True)
            return
        await _edit_text_with_toggle_kb(
            call.message,
            get_msg("msg_color_prompt", trait_list=""),
            sess.available_colors, sess.selected_colors, "col",
            select_all_active=sess.select_all_colors,
            select_all_disabled=sess.select_all_patterns,
            emoji_map=sess.color_emoji,
        )
        await state.set_state(ScanFlow.waiting_color_choice)
        await call.answer()
        return

    if value == "__all__":
        if sess.select_all_colors:
            await call.answer(
                get_msg("msg_pattern_all_disabled_by_color"),
                show_alert=True,
            )
            return
        if sess.select_all_patterns:
            sess.select_all_patterns = False
            sess.selected_patterns.clear()
        else:
            sess.select_all_patterns = True
            sess.selected_patterns = set(sess.available_patterns)
        await _edit_markup_with_toggle_kb(
            call.message,
            sess.available_patterns, sess.selected_patterns, "pat",
            select_all_active=sess.select_all_patterns,
            emoji_map=sess.pattern_emoji,
        )
        await call.answer()
        return

    if value in sess.selected_patterns:
        sess.selected_patterns.discard(value)
    else:
        sess.selected_patterns.add(value)
    sess.select_all_patterns = False  # انتخاب دستی -> حالت "همه" دیگر فعال نیست

    await _edit_markup_with_toggle_kb(
        call.message,
        sess.available_patterns, sess.selected_patterns, "pat",
        select_all_active=sess.select_all_patterns,
        emoji_map=sess.pattern_emoji,
    )
    await call.answer()


@router.callback_query(F.data.startswith("col:"), StateFilter(ScanFlow.waiting_color_choice))
async def on_color_toggle(call: CallbackQuery, state: FSMContext):
    sess = sessions[call.from_user.id]
    value = _cb_decode(call.data.split(":", 1)[1])

    if value == "__confirm__":
        if not sess.selected_colors:
            await call.answer(get_msg("msg_color_confirm_min_one"), show_alert=True)
            return
        await call.answer()
        await safe_edit(call.message, get_msg("msg_scan_starting"))
        await run_scan(call.message, call.from_user.id, state)
        return

    if value == "__all__":
        if sess.select_all_patterns:
            await call.answer(
                get_msg("msg_color_all_disabled_by_pattern"),
                show_alert=True,
            )
            return
        if sess.select_all_colors:
            sess.select_all_colors = False
            sess.selected_colors.clear()
        else:
            sess.select_all_colors = True
            sess.selected_colors = set(sess.available_colors)
        await _edit_markup_with_toggle_kb(
            call.message,
            sess.available_colors, sess.selected_colors, "col",
            select_all_active=sess.select_all_colors,
            emoji_map=sess.color_emoji,
        )
        await call.answer()
        return

    if value in sess.selected_colors:
        sess.selected_colors.discard(value)
    else:
        sess.selected_colors.add(value)
    sess.select_all_colors = False  # انتخاب دستی -> حالت "همه" دیگر فعال نیست

    await _edit_markup_with_toggle_kb(
        call.message,
        sess.available_colors, sess.selected_colors, "col",
        emoji_map=sess.color_emoji,
    )
    await call.answer()


# ------------------------------------------------------------------------- #
#  کارت تصویری خروجی (شبیه کارت GRAM/USD اما مخصوص هر NFT/مدل)
# ------------------------------------------------------------------------- #
#  ایده: قالب کارت (کادر سفید، لوگوی گرد، چیدمان، واترمارک) همیشه ثابت
#  می‌ماند - فقط محتوایی که به خودِ آن NFT مربوط است روی آن جایگزین می‌شود:
#    - متن "GRAM"                -> اسم کالکشن NFT
#    - "GRAM / USD"              -> تعداد آپگرید شده از تعداد کل کالکشن
#    - کندل + قیمت/درصد          -> عکس واقعی همون مدل (کمیاب/گیفت)
#    - پایین‌ترین/بالاترین قیمت   -> تعداد کل مدل‌ها و تعداد کل رنگ بک‌گراند
#    - رنگ آبی دور کادر سفید     -> رندوم از بین رنگ‌های خودِ عکس آن مدل
#  عکس هر مدل فقط یک‌بار دانلود می‌شود (در پوشه IMAGES_DIR) و پالت رنگ +
#  رنگ کادر انتخابی در دیتابیس (جدول nft_media) کش می‌شود؛ بار بعد ساخت
#  کارت هیچ درخواست شبکه‌ای لازم ندارد و تقریباً آنی انجام می‌شود.

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_FONT_MISSING_WARNED: set[str] = set()
# مسیرهایی که DejaVu معمولاً روی سیستم‌عامل‌های مختلف نصب می‌شود - اگر
# FONT_DIR درست نباشه یا فونت روی سرور نصب نشده باشه، اینا هم چک می‌شن.
_FONT_SEARCH_DIRS = [
    FONT_DIR,
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/dejavu-sans-fonts",
    "/usr/local/share/fonts",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts"),
]


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """
    == این دقیقاً همون دلیلیه که اندازه‌ی فونت کارت خروجی هرچی تغییر داده
    می‌شد، توی عکس واقعی هیچ فرقی نمی‌کرد ==
    قبلاً اگر فایل فونت (DejaVuSans*.ttf) توی FONT_DIR پیدا نمی‌شد (مثلاً
    روی سرور پکیج فونت نصب نبود)، این تابع بی‌سروصدا (بدون هیچ لاگی)
    ImageFont.load_default() رو برمی‌گردوند - و اون فونت پیش‌فرض PIL یک
    بیت‌مپ خیلی کوچیک با اندازه‌ی ثابته که پارامتر size رو کلاً نادیده
    می‌گیره. یعنی هرچقدر عدد size رو بزرگ‌تر می‌کردی، چون اصلاً از اون
    فونت جایگزین استفاده می‌شد، هیچ تغییری توی خروجی دیده نمی‌شد.
    حالا: (۱) چند مسیر رایج دیگه هم برای پیدا کردن فونت چک می‌شه، (۲) اگر
    واقعاً هیچ‌کدوم پیدا نشد، یک بار توی لاگ هشدار روشن داده می‌شه (که
    بگه دقیقاً همین باعث نادیده گرفته شدن اندازه‌ها میشه و باید پکیج فونت
    مثل fonts-dejavu-core روی سرور نصب بشه)، (۳) نتیجه هم کش می‌شه که هر
    بار ساخت کارت، فایل فونت از دیسک دوباره خونده نشه (سریع‌تر).
    """
    cache_key = (name, size)
    cached = _FONT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    for d in _FONT_SEARCH_DIRS:
        path = os.path.join(d, name)
        if os.path.isfile(path):
            try:
                font = ImageFont.truetype(path, size)
                _FONT_CACHE[cache_key] = font
                return font
            except Exception:
                # نه فقط فایل گم‌شده - ممکنه Pillow اصلاً بدون FreeType
                # کامپایل شده باشه (ImportError روی _imagingft) که در اون
                # حالت هیچ فایل .ttf ای، حتی معتبر، لود نمی‌شه. در هر دو
                # حالت باید رد بشیم سراغ فال‌بک، نه اینکه کرش کنیم.
                pass

    if name not in _FONT_MISSING_WARNED:
        _FONT_MISSING_WARNED.add(name)
        log.error(
            f"⚠️ فونت «{name}» توی هیچ‌کدوم از این مسیرها پیدا/لود نشد: "
            f"{_FONT_SEARCH_DIRS} - در نتیجه فونت جایگزین پیش‌فرض PIL "
            f"استفاده می‌شه که ممکنه اندازه‌ی سفارشی رو رعایت نکنه. اگه علتش "
            f"نبودن فایل فونته: پکیج فونت DejaVu (مثلاً fonts-dejavu-core) "
            f"رو نصب کن یا .ttf رو کنار اسکریپت توی پوشه‌ی fonts/ بذار. اگه "
            f"علتش ImportError روی _imagingft هست، یعنی خودِ Pillow بدون "
            f"FreeType نصب شده: روی Termux با «pip uninstall pillow -y && "
            f"pkg install python-pillow -y» درستش کن."
        )

    # فال‌بک نهایی: هر خطایی که باشه (TypeError روی نسخه‌های قدیمی Pillow،
    # یا ImportError/OSError وقتی Pillow اصلاً FreeType نداره) نباید کل
    # ساخت کارت رو بترکونه. ImageFont.load_default() بدون آرگومان size
    # فونت بیت‌مپ کلاسیک و همیشگی PIL رو برمی‌گردونه که به FreeType هیچ
    # نیازی نداره و تضمینی کار می‌کنه - نتیجه فقط کوچیک‌تر/کم‌کیفیت‌تره،
    # ولی عکس بالاخره ساخته و فرستاده می‌شه.
    try:
        fallback = ImageFont.load_default(size=size)  # Pillow >= 10.1 + FreeType سالم
    except Exception:
        fallback = ImageFont.load_default()
    _FONT_CACHE[cache_key] = fallback
    return fallback


def extract_palette(image_path: str, n: int = 6) -> list[str]:
    """پالت رنگی غالب یک عکس را به‌صورت لیست کدهای HEX برمی‌گرداند (پرتکرارترین اول)."""
    try:
        im = Image.open(image_path).convert("RGB")
    except Exception:
        return []
    im.thumbnail((120, 120))
    paletted = im.quantize(colors=max(n, 2), method=Image.MEDIANCUT)
    palette = paletted.getpalette() or []
    counts = sorted(paletted.getcolors() or [], reverse=True)
    hex_colors = []
    for count, idx in counts:
        r, g, b = palette[idx * 3: idx * 3 + 3]
        hex_colors.append("#{:02x}{:02x}{:02x}".format(r, g, b))
    return hex_colors


def _vivid_colors(colors: list[str]) -> list[str]:
    """از پالت، فقط رنگ‌های «زنده» (نه تقریباً سفید و نه تقریباً سیاه) را برای کادر انتخاب می‌کند."""
    vivid = []
    for c in colors:
        r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        if max(r, g, b) - min(r, g, b) < 12 and (max(r, g, b) > 235 or max(r, g, b) < 25):
            continue  # تقریباً خاکستری خالص/سفید/سیاه -> برای کادر جذاب نیست
        vivid.append(c)
    return vivid or colors


async def _download_bytes(url: str) -> Optional[bytes]:
    try:
        async with scanner._session.get(url, timeout=scanner.timeout) as resp:
            if resp.status == 200:
                return await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    return None


async def fetch_and_cache_model_image(
    base_link: str, pattern: str, sample_idx: int
) -> Optional[tuple[str, list[str], str]]:
    """
    برای یک مدل (pattern) که هنوز کش نشده: صفحه‌ی یک نمونه آیتم آن مدل را
    می‌گیرد، عکس اصلی گیفت را از آن استخراج و دانلود می‌کند، پالت رنگ آن
    استخراج می‌شود، یک رنگ کادر رندوم از همان پالت انتخاب و همه در
    دیتابیس (nft_media) برای استفاده‌های بعدی ذخیره می‌شود.
    """
    url = f"{base_link}{sample_idx}"
    html, net_err = await scanner.fetch(url)
    if not html:
        return None

    img_bytes = None
    fallback_bytes = None
    for img_url in extract_image_candidates(html):
        data = await _download_bytes(img_url)
        if not _is_valid_image_bytes(data):
            continue
        if fallback_bytes is None:
            fallback_bytes = data
        if not _register_and_check_unique_image(base_link, pattern, data):
            # عکس تکراری بین مدل‌ها - ترجیحاً رد، ولی اگر هیچ چیز دیگری نبود می‌پذیریم
            continue
        img_bytes = data
        break
    if img_bytes is None:
        img_bytes = fallback_bytes
    if img_bytes is None:
        return None

    slug = extract_slug(base_link)
    safe_pattern = RE_SAFE_FILENAME.sub("_", pattern).strip("_") or "model"
    filename = f"{slug}__{safe_pattern}.jpg"
    path = os.path.join(IMAGES_DIR, filename)

    def _save():
        im = Image.open(BytesIO(img_bytes)).convert("RGB")
        im.thumbnail((640, 640))
        im.save(path, "JPEG", quality=90)

    try:
        await asyncio.to_thread(_save)
    except Exception:
        log.exception(f"ذخیره عکس مدل {pattern} از کالکشن {base_link} ناموفق بود")
        return None

    colors = await asyncio.to_thread(extract_palette, path, 6)
    border_color = random.choice(_vivid_colors(colors)) if colors else "#2AA7F5"

    await db_store_media(base_link, pattern, path, colors, border_color)
    return path, colors, border_color


async def fetch_and_cache_combo_image(
    base_link: str, pattern: str, color: str, sample_idx: int
) -> Optional[tuple[str, list[str], str]]:
    """
    مثل fetch_and_cache_model_image ولی مخصوص یک ترکیب دقیق مدل+بک‌گراند:
    وقتی کاربر هم مدل و هم بک‌گراند رو تکی/غیر «همه» انتخاب کرده، عکس
    خروجی باید دقیقاً همون ترکیب باشه (نه صرفاً یک نمونه‌ی رندوم از همون
    مدل با هر بک‌گراند دیگری). نتیجه در nft_media_combo کش می‌شود.
    """
    url = f"{base_link}{sample_idx}"
    html, net_err = await scanner.fetch(url)
    if not html:
        return None

    identity = f"{pattern}|{color}"
    img_bytes = None
    fallback_bytes = None
    for img_url in extract_image_candidates(html):
        data = await _download_bytes(img_url)
        if not _is_valid_image_bytes(data):
            continue
        if fallback_bytes is None:
            fallback_bytes = data
        if not _register_and_check_unique_image(base_link, identity, data):
            continue
        img_bytes = data
        break
    if img_bytes is None:
        img_bytes = fallback_bytes
    if img_bytes is None:
        return None

    slug = extract_slug(base_link)
    safe_pattern = RE_SAFE_FILENAME.sub("_", pattern).strip("_") or "model"
    safe_color = RE_SAFE_FILENAME.sub("_", color).strip("_") or "color"
    filename = f"{slug}__{safe_pattern}__{safe_color}.jpg"
    path = os.path.join(IMAGES_DIR, filename)

    def _save():
        im = Image.open(BytesIO(img_bytes)).convert("RGB")
        im.thumbnail((640, 640))
        im.save(path, "JPEG", quality=90)

    try:
        await asyncio.to_thread(_save)
    except Exception:
        log.exception(f"ذخیره عکس ترکیب {pattern}+{color} از کالکشن {base_link} ناموفق بود")
        return None

    colors = await asyncio.to_thread(extract_palette, path, 6)
    border_color = random.choice(_vivid_colors(colors)) if colors else "#2AA7F5"

    await db_store_media_combo(base_link, pattern, color, path, colors, border_color)
    return path, colors, border_color


async def _fetch_model_image_with_retry(
    base_link: str, pattern: str, sample_idxs: list[int]
) -> Optional[tuple[str, list[str], str]]:
    """
    قبلاً فقط یک idx (کوچک‌ترین) امتحان می‌شد و اگر همون یک صفحه عکس
    نمی‌داد (og:image نداشت، یا دانلود عکسش خطا می‌خورد)، کل کارت بدون
    عکس می‌ماند و کاربر می‌گفت «بیشتر وقتا عکس نمیاد». حالا چند نمونه از
    همون مدل به‌ترتیب امتحان می‌شوند تا یکی جواب بدهد.
    """
    last_error: Optional[Exception] = None
    for idx in sample_idxs:
        try:
            media = await fetch_and_cache_model_image(base_link, pattern, idx)
        except Exception as exc:
            last_error = exc
            log.exception(f"تلاش برای گرفتن عکس نمونه idx={idx} مدل {pattern} با خطا مواجه شد")
            continue
        if media:
            return media
    if last_error is not None:
        log.warning(
            f"هیچ‌کدام از {len(sample_idxs)} نمونه‌ی امتحان‌شده برای مدل {pattern} از "
            f"کالکشن {base_link} عکس معتبر ندادند (آخرین خطا: {last_error})"
        )
    return None


async def _fetch_combo_image_with_retry(
    base_link: str, pattern: str, color: str, sample_idxs: list[int]
) -> Optional[tuple[str, list[str], str]]:
    """مثل _fetch_model_image_with_retry ولی برای ترکیب دقیق مدل+بک‌گراند."""
    last_error: Optional[Exception] = None
    for idx in sample_idxs:
        try:
            media = await fetch_and_cache_combo_image(base_link, pattern, color, idx)
        except Exception as exc:
            last_error = exc
            log.exception(f"تلاش برای گرفتن عکس نمونه idx={idx} ترکیب {pattern}+{color} با خطا مواجه شد")
            continue
        if media:
            return media
    if last_error is not None:
        log.warning(
            f"هیچ‌کدام از {len(sample_idxs)} نمونه‌ی امتحان‌شده برای ترکیب {pattern}+{color} از "
            f"کالکشن {base_link} عکس معتبر ندادند (آخرین خطا: {last_error})"
        )
    return None


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0], size[1]], radius=radius, fill=255)
    return mask


def _dominant_color(img: Image.Image) -> tuple[int, int, int]:
    """
    رنگ واقعیِ پس‌زمینه/بک‌گراند خودِ عکس نمونه‌ی NFT را برمی‌گرداند (نه
    میانگین کل عکس که با رنگ خودِ آیتم/مدل وسط عکس قاطی و اشتباه می‌شد).

    عکس‌های گیفت تلگرام همیشه یک بک‌گراند تخت (Backdrop) دارند و خودِ
    آیتم (مدل) وسط عکس قرار دارد؛ پس چهار گوشه‌ی عکس تقریباً همیشه فقط
    بک‌گراند خالص است و خودِ آیتم بهشون نمی‌رسه. با نمونه‌گیری و میانگین
    گرفتن از چهار گوشه (نه کل عکس)، رنگ برگشتی همیشه دقیقاً هم‌رنگِ
    بک‌گراند واقعی همون آیتم می‌شود - نه یک رنگ ترکیبی/مه‌آلود.
    """
    rgb_img = img.convert("RGB")
    w, h = rgb_img.size
    # اندازه‌ی هر پچ گوشه: نسبتی کوچک از عکس (فقط ناحیه‌ی خالص بک‌گراند،
    # نه اینکه به مرکز/خودِ آیتم برسد)
    patch_w = max(1, w // 8)
    patch_h = max(1, h // 8)
    corners = [
        (0, 0, patch_w, patch_h),
        (w - patch_w, 0, w, patch_h),
        (0, h - patch_h, patch_w, h),
        (w - patch_w, h - patch_h, w, h),
    ]
    r_sum = g_sum = b_sum = 0
    count = 0
    for box in corners:
        patch = rgb_img.crop(box).resize((1, 1), Image.LANCZOS)
        r, g, b = patch.getpixel((0, 0))
        r_sum += r
        g_sum += g
        b_sum += b
        count += 1
    return (r_sum // count, g_sum // count, b_sum // count)


def _shade(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """رنگ را روشن‌تر (factor > 1) یا تیره‌تر (factor < 1) می‌کند، با کمی افزایش اشباع رنگ."""
    r, g, b = rgb
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    l = max(0.0, min(1.0, l * factor))
    s = max(0.0, min(1.0, s * 1.15))
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return (int(r2 * 255), int(g2 * 255), int(b2 * 255))


def _accent_text_color(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """
    به‌جای خاکستری ثابت (که قبلاً برای همه‌ی کارت‌ها یکسان و بی‌روح بود)،
    یک رنگ متن آکسنت تیره و پراشباع، هم‌خانواده با رنگ خود کالکشن/مدل
    می‌سازد - در نتیجه هر کارت، رنگ‌بندی متن مخصوص به خودش را دارد ولی
    همیشه به‌اندازه‌ی کافی روی زمینه‌ی سفید کادر خوانا می‌ماند.
    """
    r, g, b = rgb
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    l = min(l, 0.38)
    s = max(s, 0.55)
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return (int(r2 * 255), int(g2 * 255), int(b2 * 255))


def _draw_gem(draw: ImageDraw.ImageDraw, canvas: Image.Image, cx: float, cy: float, size: int,
              color: tuple[int, int, int], alpha: int = 60, rotation: int = 0) -> None:
    """
    یک نماد ساده‌ی «جم/الماس» (شبیه لوگوی خود ربات، نه خودِ عکس NFT) برای
    تزئین فضای بیرون قاب سفید رسم می‌کند. این نمادها کاملاً جدا از عکس
    اصلی NFT هستند و فقط جنبه‌ی دکوری/برندینگ دارند.
    """
    layer = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    pts = [(size, 2), (size * 2 - 2, size * 0.7), (size, size * 2 - 2), (2, size * 0.7)]
    ld.polygon(pts, fill=(*color, alpha), outline=(*color, min(alpha + 60, 255)))
    ld.line([(2, size * 0.7), (size * 2 - 2, size * 0.7)], fill=(*color, min(alpha + 40, 255)), width=2)
    ld.line([(size, 2), (size, size * 2 - 2)], fill=(*color, min(alpha + 20, 255)), width=1)
    if rotation:
        layer = layer.rotate(rotation, expand=True, resample=Image.BICUBIC)
    lw, lh = layer.size
    canvas.paste(layer, (int(cx - lw / 2), int(cy - lh / 2)), layer)


def build_card_image(
    nft_name: str,
    upgraded: int,
    total: int,
    image_path: Optional[str],
    border_color: str,
    total_patterns: int,
    total_colors: int,
    model_name: str,
) -> bytes:
    """
    کارت خروجی نهایی (PNG) را می‌سازد.

    نسخه‌ی بازطراحی‌شده (v3) نسبت به قبل:
      - داخل کادر اصلی دیگر سفیدِ خالی نیست: یک نسخه‌ی بزرگ‌نمایی‌شده و
        بلورشده از همان عکس نمونه‌ی NFT، کل مستطیل را به‌عنوان پس‌زمینه
        پر می‌کند (فضای راستِ کادر دیگر خالی نمی‌ماند) و یک لایه‌ی نیمه‌
        شفافِ تیره روی آن برای خوانایی متن‌ها زده می‌شود.
      - عکس اصلیِ NFT (تیز، با قاب سفید نازک و سایه) دقیقاً وسطِ همان
        مستطیل قرار می‌گیرد؛ نه وسطِ کل کارت.
      - اسم مدل هم درست زیرِ عکس ولی وسطِ همان مستطیل نوشته می‌شود.
      - عدد Upgraded/Total Supply دیگر به برچسبش نچسبیده - فاصله‌ی
        عمودی مشخص بین عدد و برچسب رعایت شده.
      - آمار «تعداد مدل‌ها»/«تعداد رنگ‌ها» به‌صورت یک ردیف در پایین
        مستطیل، کنار هم و با جداکننده قرار گرفته‌اند.
      - اگر عکس نمونه در دسترس نباشد (image_path نامعتبر/None)، کارت باز
        هم با یک جای‌گزین (placeholder) ساخته و ارسال می‌شود - یعنی دیگر
        هیچ نتیجه‌ی اسکنی بدون هیچ عکسی برای کاربر نمی‌رود.
    """
    W, H = CARD_WIDTH, CARD_HEIGHT

    model_img: Optional[Image.Image] = None
    if image_path:
        try:
            model_img = Image.open(image_path).convert("RGB")
        except Exception:
            model_img = None

    if model_img is not None:
        bg_seed = _dominant_color(model_img)
    else:
        bc = (border_color or "#2E86F5").lstrip("#")
        bg_seed = tuple(int(bc[i:i + 2], 16) for i in (0, 2, 4)) if len(bc) == 6 else (46, 134, 245)

    top_color = _shade(bg_seed, 1.25)
    bottom_color = _shade(bg_seed, 0.55)

    # پس‌زمینه‌ی بیرون کادر (گرادیانت هم‌رنگ با خودِ عکس NFT)
    grad = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / H
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        grad.putpixel((0, y), (r, g, b))
    canvas = grad.resize((W, H)).convert("RGB")
    draw = ImageDraw.Draw(canvas, "RGBA")

    # نمادهای تزئینی جم در فضای بیرون کادر (نه خودِ عکس NFT)
    _draw_gem(draw, canvas, 90, 60, 26, (255, 255, 255), alpha=55, rotation=8)
    _draw_gem(draw, canvas, W - 70, H - 40, 20, (255, 255, 255), alpha=45, rotation=-12)
    _draw_gem(draw, canvas, 60, H - 90, 16, (255, 255, 255), alpha=40, rotation=20)
    _draw_gem(draw, canvas, W - 110, 50, 14, (255, 255, 255), alpha=35, rotation=-6)

    # ---- کادر اصلی: به‌جای پر شدن با سفیدِ خالی، با پس‌زمینه‌ی خودِ عکس ---- #
    # ---- NFT (بزرگ‌نمایی‌شده + بلورشده) پر می‌شود تا فضای راست خالی نماند ---- #
    margin = 34
    box_x0, box_y0, box_x1, box_y1 = margin, margin, W - margin, H - 90
    box_w, box_h = box_x1 - box_x0, box_y1 - box_y0
    border_w = 8
    radius = 28

    box_mask = _rounded_mask((box_w, box_h), radius)
    box_layer = Image.new("RGB", (box_w, box_h), "white")

    if model_img is not None:
        backdrop = ImageOps.fit(model_img, (box_w, box_h), method=Image.LANCZOS, centering=(0.5, 0.42))
        backdrop = backdrop.filter(ImageFilter.GaussianBlur(18))
        dark = Image.new("RGB", (box_w, box_h), (0, 0, 0))
        backdrop = Image.blend(backdrop, dark, 0.35)  # تیره‌کردن ملایم برای خوانایی متن روی آن
        box_layer.paste(backdrop, (0, 0))
    else:
        box_layer.paste(Image.new("RGB", (box_w, box_h), _shade(bg_seed, 0.9)), (0, 0))

    canvas.paste(box_layer, (box_x0, box_y0), box_mask)
    draw.rounded_rectangle([box_x0, box_y0, box_x1, box_y1], radius=radius, outline=border_color, width=border_w)

    # ---- هدر داخل کادر: لوگو (تزئینی/برند ربات، نه آیکون واقعی کالکشن) + نام کالکشن ---- #
    logo_r = 30
    logo_cx, logo_cy = box_x0 + 46, box_y0 + 44
    draw.ellipse(
        [logo_cx - logo_r, logo_cy - logo_r, logo_cx + logo_r, logo_cy + logo_r],
        fill=border_color,
    )
    draw.regular_polygon((logo_cx, logo_cy, logo_r * 0.55), n_sides=4, rotation=45, fill="white")

    name_font = _font("DejaVuSans-Bold.ttf", int(46 * CARD_FONT_SCALE))
    name = nft_name if len(nft_name) <= 16 else nft_name[:15] + "…"
    draw.text((logo_cx + logo_r + 20, logo_cy - 30), name, font=name_font, fill="white")

    # ---- بالا راست: تعداد آپگرید/کل، با فاصله‌ی عمودی کافی بین عدد و برچسب (دیگر چسبیده نیستند) ---- #
    ratio_font = _font("DejaVuSans-Bold.ttf", int(30 * CARD_FONT_SCALE))
    label_font = _font("DejaVuSans-Bold.ttf", int(15 * CARD_FONT_SCALE))
    ratio_text = f"{upgraded:,} / {total:,}"
    label_text = "UPGRADED / TOTAL SUPPLY"

    top_right_x = box_x1 - 26
    ratio_y = box_y0 + 24
    label_y = ratio_y + 46  # فاصله‌ی ثابت و کافی بین عدد و برچسب

    rw = draw.textlength(ratio_text, font=ratio_font)
    draw.text((top_right_x - rw, ratio_y), ratio_text, font=ratio_font, fill="white")
    lw = draw.textlength(label_text, font=label_font)
    draw.text((top_right_x - lw, label_y), label_text, font=label_font, fill=(230, 230, 235))

    # ---- عکس واقعی NFT: دقیقاً وسطِ مستطیل (نه وسطِ کل کارت) ---- #
    img_box_size = 236
    img_box_x = box_x0 + (box_w - img_box_size) // 2
    img_box_y = box_y0 + 108

    if model_img is not None:
        try:
            model_img_fit = ImageOps.fit(
                model_img, (img_box_size, img_box_size), method=Image.LANCZOS, centering=(0.5, 0.5)
            )
            mask = _rounded_mask((img_box_size, img_box_size), 20)
            shadow = Image.new("RGBA", (img_box_size + 30, img_box_size + 30), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle(
                [15, 20, img_box_size + 15, img_box_size + 20], radius=24, fill=(0, 0, 0, 110)
            )
            shadow = shadow.filter(ImageFilter.GaussianBlur(12))
            canvas.paste(shadow, (img_box_x - 15, img_box_y - 15), shadow)
            canvas.paste(model_img_fit, (img_box_x, img_box_y), mask)
            # قاب سفید نازک دور عکس اصلی تا از بک‌گراند بلورشده جدا دیده شود
            draw.rounded_rectangle(
                [img_box_x, img_box_y, img_box_x + img_box_size, img_box_y + img_box_size],
                radius=20, outline="white", width=4,
            )
        except Exception:
            model_img = None

    if model_img is None:
        # placeholder: حتی وقتی هیچ عکس نمونه‌ای در دسترس نیست کارت ساخته می‌شود
        draw.rounded_rectangle(
            [img_box_x, img_box_y, img_box_x + img_box_size, img_box_y + img_box_size],
            radius=20, fill=_shade(bg_seed, 1.6), outline="white", width=4,
        )
        ph_font = _font("DejaVuSans-Bold.ttf", int(18 * CARD_FONT_SCALE))
        ph_text = "بدون پیش‌نمایش"
        ptw = draw.textlength(ph_text, font=ph_font)
        draw.text(
            (img_box_x + (img_box_size - ptw) / 2, img_box_y + img_box_size / 2 - 12),
            ph_text, font=ph_font, fill=_shade(bg_seed, 0.5),
        )

    # ---- اسم مدل: زیرِ عکس، وسطِ مستطیل (نه وسطِ کل کارت) ---- #
    model_font = _font("DejaVuSans-Bold.ttf", int(24 * CARD_FONT_SCALE))
    mtxt = model_name if len(model_name) <= 30 else model_name[:29] + "…"
    mtw = draw.textlength(mtxt, font=model_font)
    subtitle_y = img_box_y + img_box_size + 20
    draw.text((box_x0 + (box_w - mtw) / 2, subtitle_y), mtxt, font=model_font, fill="white")

    # ---- آمار MODELS/COLORS: یک ردیف پایینِ مستطیل، کنار هم با جداکننده ---- #
    stat_num_font = _font("DejaVuSans-Bold.ttf", int(30 * CARD_FONT_SCALE))
    stat_lbl_font = _font("DejaVuSans-Bold.ttf", int(13 * CARD_FONT_SCALE))
    stats_y = subtitle_y + 46

    def _stat(cx: float, number: int, label: str) -> None:
        num_text = str(number)
        nw = draw.textlength(num_text, font=stat_num_font)
        draw.text((cx - nw / 2, stats_y), num_text, font=stat_num_font, fill="white")
        lwid = draw.textlength(label, font=stat_lbl_font)
        draw.text((cx - lwid / 2, stats_y + 38), label, font=stat_lbl_font, fill=(220, 220, 225))

    _stat(box_x0 + box_w * 0.38, total_patterns, "MODELS")
    _stat(box_x0 + box_w * 0.62, total_colors, "COLORS")
    sep_x = box_x0 + box_w * 0.5
    draw.line([(sep_x, stats_y + 4), (sep_x, stats_y + 40)], fill=(255, 255, 255, 90), width=2)

    # واترمارک (برند ثابت ربات)
    # نکته: msg_signature از پنل ادمین قابل‌تغییره و ممکنه ادمین داخلش یک
    # خطِ جدید (\n) وسط متن گذاشته باشه (نه فقط ابتدا/انتهای پیش‌فرض که با
    # strip حذف می‌شه). PIL's draw.textlength() برای متن چندخطی ValueError
    # می‌ده (نمی‌تونه طول یک متن چندخطی رو اندازه بگیره) - همون خطایی که
    # باعث شکست کامل ساخت کارت می‌شد. چون این واترمارک همیشه باید فقط یک
    # خط باشه (جای ثابت نزدیک پایین کارت)، هر \n باقی‌مانده‌ی وسط متن رو
    # به فاصله تبدیل می‌کنیم تا همیشه یک‌خطی و امن برای textlength بماند.
    brand_text = re.sub(r"<[^>]+>", "", get_msg("msg_signature")).strip(" \n—")
    brand_text = re.sub(r"\s*\n\s*", " ", brand_text).strip()
    # هر نماد/ایموجی تزئینی قبل از اولین @ (مثلاً 🔹 یا هر بولت دیگه‌ای که
    # ادمین از پنل ممکنه ابتدای امضا گذاشته باشه) حذف می‌شود - واترمارک
    # روی خودِ عکس کارت باید همیشه دقیقاً از @یوزرنیم شروع بشه، نه از یک
    # نماد قبل از آن.
    at_idx = brand_text.find("@")
    if at_idx > 0:
        brand_text = brand_text[at_idx:]
    brand_font = _font("DejaVuSans-Bold.ttf", int(16 * CARD_FONT_SCALE))
    bw = draw.textlength(brand_text, font=brand_font)
    draw.text(((W - bw) / 2, H - 62), brand_text, font=brand_font, fill="white")

    buf = BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()


async def build_nft_result_cards(
    base_link: str,
    nft_name: str,
    patterns: list[str],
    total_patterns: int,
    total_colors: int,
    exact_color: Optional[str] = None,
) -> list[tuple[str, bytes]]:
    """
    برای هر مدل (pattern) داده‌شده یک کارت تصویری می‌سازد (از کش دیتابیس/عکس
    اگر قبلاً ساخته شده، وگرنه یک‌بار دانلود+کش می‌کند) و لیست
    (اسم مدل، بایت‌های PNG کارت) را برمی‌گرداند.

    اگر exact_color داده شده باشه (یعنی کاربر هم مدل و هم بک‌گراند را
    تکی/غیر «همه» انتخاب کرده)، عکس نمونه دقیقاً از یک آیتم با همون
    ترکیب مدل+بک‌گراند گرفته می‌شود (نه هر آیتمی از همون مدل با هر
    بک‌گراند دیگری) و در کش جداگانه‌ی ترکیبی (nft_media_combo) نگه‌داری
    می‌شود.
    """
    if not patterns:
        return []

    # پاکسازی یک‌باره‌ی کشِ این کالکشن از عکس‌های تکراری/مشترکِ اشتباه
    # (باگ قدیمی: عکس عمومی سایت به‌جای عکس اختصاصی مدل کش شده بود) - قبل
    # از اینکه از کش استفاده کنیم. سبک و بدون شبکه است (فقط هش فایل‌های
    # روی دیسک)، برای همین صدا زدنش هر بار مشکلی ایجاد نمی‌کند.
    try:
        purged = await db_purge_duplicate_media(base_link)
        if purged:
            log.warning(
                f"{purged} ردیف کش تصویری تکراری/مشترک (احتمالاً عکس اشتباه) "
                f"برای کالکشن {base_link} پاک شد و دوباره ساخته می‌شود."
            )
    except Exception:
        log.exception(f"پاکسازی کش تصویری تکراری برای کالکشن {base_link} با خطا مواجه شد")

    upgraded = await db_get_valid_count(base_link)
    total = await db_get_cached_total(base_link) or upgraded

    # پالت ثابتِ رنگ کادر برای وقتی که هیچ عکس نمونه‌ای پیدا نشد (تا کارت
    # placeholder هم همیشه یک رنگ کادر مشخص و ثابت برای همون مدل داشته باشد)
    _fallback_palette = ["#3AA6FF", "#E63C96", "#22C1A0", "#F5A623", "#8E5CE6", "#EF5350"]

    def _fallback_border_color(pattern_name: str) -> str:
        return _fallback_palette[hash(pattern_name) % len(_fallback_palette)]

    cards: list[tuple[str, bytes]] = []
    for pattern in patterns[:CARD_MAX_PATTERNS_PER_SCAN]:
        media = None
        if exact_color:
            media = await db_get_media_combo(base_link, pattern, exact_color)
            if media and not os.path.exists(media[0]):
                media = None  # فایل روی دیسک پاک شده -> باید دوباره ساخته شود

            if not media:
                # قبلاً فقط یک idx (کوچک‌ترین) امتحان می‌شد؛ حالا چند نمونه
                # (تا ۵ تا) به‌ترتیب امتحان می‌شوند تا یکی عکس بدهد.
                sample_idxs = await db_get_sample_idxs_for_pattern_and_color(base_link, pattern, exact_color, limit=5)
                if sample_idxs:
                    media = await _fetch_combo_image_with_retry(base_link, pattern, exact_color, sample_idxs)

            if not media and not exact_color:
                # فقط وقتی بک‌گراند خاصی درخواست نشده، عکس عمومی مدل مجاز است.
                media = await db_get_media(base_link, pattern)
                if media and not os.path.exists(media[0]):
                    media = None
                if not media:
                    fallback_idxs = await db_get_sample_idxs_for_pattern(base_link, pattern, limit=5)
                    if fallback_idxs:
                        media = await _fetch_model_image_with_retry(base_link, pattern, fallback_idxs)
        else:
            media = await db_get_media(base_link, pattern)
            if media and not os.path.exists(media[0]):
                media = None  # فایل روی دیسک پاک شده -> باید دوباره ساخته شود

            if not media:
                # قبلاً فقط یک idx (کوچک‌ترین) امتحان می‌شد و اگر همون یک
                # صفحه عکس نمی‌داد، کل کارت بدون عکس می‌ماند. حالا چند
                # نمونه (تا ۵ تا) امتحان می‌شود.
                sample_idxs = await db_get_sample_idxs_for_pattern(base_link, pattern, limit=5)
                if sample_idxs:
                    media = await _fetch_model_image_with_retry(base_link, pattern, sample_idxs)

        # نکته‌ی مهم: قبلاً اگر هیچ عکس نمونه‌ای برای یک مدل گیر نمی‌آمد،
        # اصلاً کارتی برای آن ساخته نمی‌شد (continue) - یعنی گاهی کل نتیجه‌ی
        # اسکن بدون هیچ عکسی برای کاربر می‌رفت. حالا در این حالت هم build_card_image
        # با image_path=None صدا زده می‌شود که یک کارت placeholder (بدون عکس واقعی
        # ولی با همون آمار/برندینگ) می‌سازد - یعنی همیشه حداقل یک کارت ارسال می‌شود.
        if media:
            image_path, _colors, border_color = media
        else:
            image_path, border_color = None, _fallback_border_color(pattern)

        try:
            card_bytes = await asyncio.to_thread(
                build_card_image,
                nft_name, upgraded, total, image_path, border_color,
                total_patterns, total_colors, pattern,
            )
        except Exception:
            log.exception(f"ساخت کارت تصویری برای مدل {pattern} ناموفق بود")
            continue
        cards.append((pattern, card_bytes))

    return cards


# ------------------------------------------------------------------------- #
#  اجرای اسکن نهایی + فیلتر متقاطع + ارسال بسته‌بندی‌شده
# ------------------------------------------------------------------------- #

async def run_scan(message: Message, user_id: int, state: FSMContext):
    sess = sessions[user_id]
    all_idxs = list(range(1, sess.max_items + 1))

    valid_links: list[str] = []
    failed_idxs: list[int] = []  # آیتم‌هایی که به‌خاطر خطای گذرا (نه واقعاً ناموجود) رد شدند
    scanned = 0
    batch_size = get_max_concurrent() * 4  # پردازش دسته‌ای برای گزارش پیشرفت (متناسب با تنظیم فعلی همزمانی)
    stop_now = False
    # -- اصلاح مهم (باگ اسکن ناقص کالکشن‌های بزرگ ۱۰۰-۲۰۰ هزارتایی به بالا) --
    # قبلاً اینجا هم یک «توقف زودهنگام روی گپ طولانی» مثل discover_total_count
    # اعمال می‌شد. اما sess.max_items در این تابع از قبل دقیقاً همان تعداد
    # واقعی و تأییدشده‌ی کالکشن است (خروجی discover_total_count که خودش با
    # الگوریتم تلورانس‌دار/دو-مرحله‌ای این را پیدا کرده). یعنی اینجا دیگر
    # لازم نیست حدس بزنیم «به انتهای کالکشن رسیدیم یا نه» - از قبل می‌دانیم.
    # نتیجه‌ی وجود این چک زائد این بود که یک بازه‌ی طولانیِ واقعی از آیتم‌های
    # سوخته/استفاده‌نشده (که در کالکشن‌های بزرگ کاملاً طبیعی است) به‌اشتباه
    # «پایان کالکشن» تلقی می‌شد و کل اسکن همان‌جا (مثلاً حوالی ۲۰-۴۰ هزار از
    # یک کالکشن ۲۰۰ هزارتایی) متوقف می‌شد - یعنی نه‌فقط نتیجه‌ی اسکن ناقص
    # می‌ماند، بلکه آیتم‌های بعد از آن نقطه هرگز در دیتابیس هم ثبت نمی‌شدند.
    # حالا اسکن همیشه تا انتهای بازه‌ی واقعی (sess.max_items) کامل ادامه پیدا
    # می‌کند و چک زودهنگام حذف شده است.

    # --------------------------------------------------------------- #
    # مسیر سریع: اگر کل این کالکشن قبلاً یک‌بار (چه توسط همین کاربر، چه
    # توسط ادمین از بخش «اضافه کردن») به‌طور کامل اسکن و کش شده باشه،
    # هیچ نیازی به لوپ زدن روی تک‌تک آیتم‌ها نیست - مستقیم با یک کوئری
    # ایندکس‌شده جواب فیلتر مدل/بک‌گراند گرفته می‌شه (صرف‌نظر از اینکه
    # کالکشن ۱۰۰۰ تا باشه یا ۵۰۰,۰۰۰ تا، تقریباً آنی).
    # --------------------------------------------------------------- #
    if await db_check_full_coverage(sess.base_link, sess.max_items):
        try:
            await message.edit_text(get_msg("msg_scan_fast_cached"))
        except Exception:
            pass

        patterns = None if sess.select_all_patterns else (sess.selected_patterns or None)
        colors = None if sess.select_all_colors else (sess.selected_colors or None)
        matched_idxs = await db_query_filtered_items(sess.base_link, patterns, colors, sess.max_items)
        valid_links = [f"{sess.base_link}{i}" for i in matched_idxs]
        scanned = sess.max_items

    else:
        progress_throttle = ProgressThrottle(3.0)

        def process_result(url: str, valid: bool, pattern: Optional[str], color: Optional[str], net_err: bool):
            nonlocal scanned
            if net_err:
                return
            scanned += 1
            if not valid:
                # دیگر هیچ حدس زودهنگامی درباره‌ی «پایان کالکشن» زده نمی‌شود؛
                # sess.max_items از قبل تعداد واقعی و تأییدشده است، پس یک گپ
                # طولانی از آیتم‌های سوخته اینجا کاملاً طبیعی است و نباید کل
                # اسکن را متوقف کند.
                return

            # فیلتر متقاطع دقیق: فقط پترن و رنگ چک می‌شوند (نه همه ویژگی‌ها هم‌زمان)
            pattern_ok = (not sess.selected_patterns) or (pattern in sess.selected_patterns)
            color_ok = (not sess.selected_colors) or (color in sess.selected_colors)
            if pattern_ok and color_ok:
                valid_links.append(url)

        for start in range(0, len(all_idxs), batch_size):
            batch = all_idxs[start:start + batch_size]
            results = await scan_indices_cached(sess.base_link, batch)

            for i, (url, valid, pattern, color, net_err) in zip(batch, results):
                if net_err:
                    failed_idxs.append(i)
                    continue
                process_result(url, valid, pattern, color, net_err)
                if stop_now:
                    break

            if progress_throttle.ready():
                try:
                    await message.edit_text(get_msg("msg_scan_progress", scanned=scanned, total=len(all_idxs)))
                except Exception:
                    pass

            if stop_now:
                break

        # پاس نهایی: آیتم‌هایی که فقط به‌خاطر خطای گذرا (تایم‌اوت/۵xx) رد شده بودند، یک بار دیگر چک می‌شوند
        if failed_idxs and not stop_now:
            try:
                await message.edit_text(
                    get_msg("msg_scan_retry_failed", failed_count=len(failed_idxs))
                )
            except Exception:
                pass
            retry_results = await scan_indices_cached(sess.base_link, failed_idxs)
            for url, valid, pattern, color, net_err in retry_results:
                if net_err:
                    continue  # نهایتاً هم جواب نداد -> نادیده گرفته می‌شود
                scanned += 1
                if not valid:
                    continue
                pattern_ok = (not sess.selected_patterns) or (pattern in sess.selected_patterns)
                color_ok = (not sess.selected_colors) or (color in sess.selected_colors)
                if pattern_ok and color_ok:
                    valid_links.append(url)

    # خلاصه انتخاب‌های کاربر در ابتدای پیام نهایی
    if sess.select_all_patterns:
        pattern_summary = get_msg("msg_all_patterns_label")
    elif sess.selected_patterns:
        pattern_summary = "، ".join(sorted(sess.selected_patterns))
    else:
        pattern_summary = "—"

    if sess.select_all_colors:
        color_summary = get_msg("msg_all_colors_label")
    elif sess.selected_colors:
        color_summary = "، ".join(sorted(sess.selected_colors))
    else:
        color_summary = "—"

    header = get_msg(
        "msg_scan_result_header",
        pattern_summary=pattern_summary,
        color_summary=color_summary,
        result_summary=get_msg("msg_result_summary", valid_count=len(valid_links), scanned_count=scanned),
    )

    try:
        # ------------------------------------------------------------ #
        # یک عکس کارتی (نمونه‌ی اولین مدل مچ‌شده) + متن نتیجه، همه با هم
        # در *یک* پیام (کپشن روی همون عکس) ارسال می‌شود - نه یک پیام متنی
        # جدا و بعد یک عکس جدا. اگر لینک‌ها آن‌قدر زیاد باشند که در سقف
        # کپشن (۱۰۲۴ کاراکتر) جا نشوند، فقط قسمت اضافه به‌صورت پیام(های)
        # متنی *بعد* از همین پیام اصلی ارسال می‌شود.
        # ------------------------------------------------------------ #
        card_bytes = None
        card_model_name = None
        if valid_links:
            if sess.select_all_patterns or not sess.selected_patterns:
                patterns_for_cards = sorted(sess.available_patterns)
            else:
                patterns_for_cards = sorted(sess.selected_patterns)

            if patterns_for_cards:
                nft_display_name = beautify_name(extract_slug(sess.base_link))

                # اگر کاربر هم دقیقاً یک مدل و هم دقیقاً یک بک‌گراند را
                # (نه حالت «همه») انتخاب کرده باشه، عکس خروجی باید دقیقاً
                # همون ترکیب مدل+بک‌گراند باشه، نه هر نمونه‌ای از همون مدل.
                exact_color = None
                if (
                    not sess.select_all_colors
                    and len(sess.selected_colors) == 1
                    and not sess.select_all_patterns
                    and len(sess.selected_patterns) == 1
                ):
                    exact_color = next(iter(sess.selected_colors))

                # اگر ترکیبِ «دقیقاً یک مدل + دقیقاً یک بک‌گراند» صدق نکرد،
                # بازم بک‌گراند عکسِ کارت نباید کاملاً بی‌ربط به انتخاب
                # کاربر باشه: اگه کاربر چند رنگ خاص انتخاب کرده، یکی از
                # همونا رندوم انتخاب می‌شه؛ اگه دکمه‌ی «همه‌ی رنگ‌ها» رو زده،
                # یکی از کل رنگ‌های موجود کالکشن رندوم انتخاب می‌شه. این
                # رنگِ انتخابی به‌عنوان بک‌گراندِ عکس کارت استفاده می‌شود (نه
                # یک رنگ رندوم/میانگین بی‌ربط از خودِ عکس).
                if not exact_color:
                    if sess.select_all_colors and sess.available_colors:
                        exact_color = random.choice(sorted(sess.available_colors))
                    elif sess.selected_colors:
                        exact_color = random.choice(sorted(sess.selected_colors))

                # فقط یک کارت در نهایت به کاربر فرستاده می‌شه (cards[0])، ولی
                # قبلاً فقط دقیقاً یک مدل (patterns_for_cards[:1]) امتحان
                # می‌شد - اگر عکس نمونه‌ی همون یک مدل با هر ۵ نمونه‌ش هم گیر
                # نمی‌اومد، کل نتیجه اسکن بدون هیچ عکسی می‌رفت. حالا تا ۵ مدل
                # کاندید امتحان می‌شوند (وقتی exact_color نباشه، معمولاً چندتا
                # مدل کاندید داریم) تا احتمال بی‌عکس ماندن نتیجه به حداقل برسه.
                cards = await build_nft_result_cards(
                    sess.base_link,
                    nft_display_name,
                    patterns_for_cards[:5],
                    len(sess.available_patterns),
                    len(sess.available_colors),
                    exact_color=exact_color,
                )
                if cards:
                    card_model_name, card_bytes = cards[0]

        # ---------------------------------------------------------------- #
        # FIX: ایموجی پرمیوم در header نباید با Quote لینک‌ها در یک HTML tree
        # قرار بگیرد. بعضی نسخه‌های تلگرام در این حالت expandable blockquote
        # را ناقص رندر می‌کنند و ظاهر لینک‌ها از Quote بیرون می‌زند.
        # در حالت عادی (بدون <tg-emoji>) دقیقاً رفتار قبلی حفظ می‌شود.
        # در حالت پرمیوم، header جدا ارسال می‌شود و لینک‌ها همیشه در Quote
        # مستقل خودشان می‌مانند.
        # ---------------------------------------------------------------- #
        premium_header = _has_premium_emoji_html(header)
        premium_signature = _has_premium_emoji_html(get_msg("msg_signature"))
        premium_result_layout = premium_header or premium_signature

        if premium_result_layout:
            if card_bytes is not None:
                # عکس فقط header را به‌عنوان caption می‌گیرد؛ Quote لینک‌ها
                # در یک پیام متنی مستقل ارسال می‌شود تا تگ <tg-emoji> و
                # <blockquote expandable> هیچ‌وقت با هم تداخل نداشته باشند.
                await message.answer_photo(
                    BufferedInputFile(card_bytes, filename=f"{card_model_name}.png"),
                    caption=header,
                )
            else:
                # header مستقل است تا ایموجی پرمیوم روی Quote اثر نگذارد.
                await message.answer(header, disable_web_page_preview=True)

            # لینک‌ها بدون header ساخته می‌شوند، بنابراین کل Quote از نظر
            # ساختار HTML مستقل و پایدار است. امضای انتهایی همچنان در آخرین
            # chunk قرار می‌گیرد.
            link_chunks = chunk_message(valid_links, "", limit=TELEGRAM_MSG_LIMIT)
            for chunk in link_chunks:
                await message.answer(chunk, disable_web_page_preview=True)
        elif card_bytes is not None:
            caption_chunks = chunk_message(valid_links, header, limit=TELEGRAM_CAPTION_LIMIT)
            await message.answer_photo(
                BufferedInputFile(card_bytes, filename=f"{card_model_name}.png"),
                caption=caption_chunks[0],
            )
            # اگر متن نتیجه از سقف کپشن رد شده باشه، بقیه‌اش به‌صورت پیام
            # متنی *بعد* از همین عکس ارسال می‌شود (نه قبلش)
            for chunk in caption_chunks[1:]:
                await message.answer(chunk, disable_web_page_preview=True)
        else:
            # هیچ کارتی ساخته نشد (مثلاً هیچ آیتم معتبری پیدا نشد) -> فقط متن
            chunks = chunk_message(valid_links, header)
            for chunk in chunks:
                await message.answer(chunk, disable_web_page_preview=True)
    except Exception:
        log.exception("خطا در ارسال نتیجه نهایی اسکن یا کارت تصویری")
    finally:
        # بعد از اسکن کامل: کالکشن را در لیست ادمین ثبت و قفل کن تا
        # دفعه‌های بعد از کش استفاده شود (بدون rediscovery از صفر)
        try:
            if sess.max_items > 0 and await db_check_full_coverage(sess.base_link, sess.max_items):
                await db_store_total(sess.base_link, max(sess.max_items, sess.total_available or 0))
                await db_upsert_admin_collection(sess.base_link, sess.max_items, "done")
                await db_set_collection_lock(sess.base_link, True)
                log.info(f"auto-lock cache: {sess.base_link} max_items={sess.max_items}")
        except Exception:
            log.exception("auto-lock بعد از اسکن ناموفق بود")
        # بعد از پایان سرچ دیگر منوی /start را نفرست؛ نتیجه باید آخرین پیام
        # جریان جستجو باشد تا چت شلوغ نشود. کاربر هر وقت خواست از دکمه‌ی
        # «منوی اصلی» همان نتیجه/پنل قبلی برمی‌گردد.
        try:
            await state.clear()
        except Exception:
            pass


# ------------------------------------------------------------------------- #
#  منوی اصلی / کالکشن‌ها / شارژ
# ------------------------------------------------------------------------- #

@router.callback_query(F.data == "menu:noop")
async def menu_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data == "menu:home")
async def menu_home(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await send_main_menu(call.message.answer, call.from_user.id, state)


@router.callback_query(F.data == "menu:scan")
async def menu_scan(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await send_link_prompt(call.message.answer, call.from_user.id, state)


@router.callback_query(F.data == "menu:profile")
async def menu_profile(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    # reuse profile logic
    user_id = call.from_user.id
    bal = await db_get_balance(user_id)
    if is_admin(user_id):
        await call.message.answer(
            get_msg("msg_profile_admin") + f"\n\n💳 موجودی سرچ: <b>{bal}</b>",
            reply_markup=build_main_menu_kb(),
        )
        return
    used, remaining = await db_get_quota_status(user_id)
    text = get_msg(
        "msg_profile_user_header",
        used=used, limit=DAILY_FREE_FILTER_LIMIT, remaining=remaining,
    )
    text += f"💳 موجودی سرچ پولی: <b>{bal}</b>\n(هر ۱ GRAM = {get_credit_price_ton():g} TON)\n\n"
    if remaining <= 0 and bal <= 0:
        text += get_msg("msg_profile_exceeded", limit=DAILY_FREE_FILTER_LIMIT, support_username=SUPPORT_USERNAME)
    else:
        text += get_msg("msg_profile_remaining", support_username=SUPPORT_USERNAME)
    await call.message.answer(text, reply_markup=build_main_menu_kb())


@router.callback_query(F.data == "menu:collections")
async def menu_collections(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await state.set_state(CollectionFlow.waiting_search)
    await safe_edit(
        call.message,
        "📦 <b>جستجوی موضوعی کالکشن</b>\n\n"
        "اسم چیزی که دنبالش هستی را بفرست؛ مثلاً <code>Harry Potter</code>.\n"
        "ربات اول روی وب سرچ می‌کند، بعد نتیجه را با Modelهای واقعی دیتابیس تطبیق می‌دهد و چند گیفت واقعی نشان می‌دهد."
    )


def build_collection_gift_kb(rows: list[tuple[str, str, int, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, (_pattern, base_link, idx, _color) in enumerate(rows[:COLLECTION_MAX_GIFTS], 1):
        kb.button(text=f"🎁 گیفت {i}", url=f"{base_link}{idx}")
    kb.adjust(2)
    return kb.as_markup()


@router.message(StateFilter(CollectionFlow.waiting_search))
async def col_search(message: Message, state: FSMContext):
    """جستجوی موضوعی کالکشن با یک تصویر مشترک برای کل نتیجه و بخش متنی برای هر Model.

    خروجی فقط یک تصویر در ابتدای نتیجه دارد؛ بعد از آن برای هر Model اطلاعات و
    لینک‌های واقعی همان Model داخل blockquote نمایش داده می‌شوند. هیچ منوی Start
    بعد از پایان سرچ ارسال نمی‌شود.
    """
    q = (message.text or "").strip()
    if len(q) < 2:
        await message.answer("❌ حداقل ۲ حرف وارد کن.")
        return

    user_id = message.from_user.id
    ok, info = await db_try_consume_search(user_id)
    if not ok:
        await message.answer(info)
        await state.clear()
        return

    status = await message.answer(
        f"🌐 دارم <b>{html_entities.escape(q)}</b> را روی وب جستجو می‌کنم...\n"
        "🔎 بعد Modelهای واقعی دیتابیس را با نتایج وب تطبیق می‌دهم."
    )
    temp_paths: list[str] = []
    try:
        matches, web_results = await find_web_related_models_any(q)

        if not matches:
            normalized = _normalize_search_text(q)
            local = [
                (p, 2.0 if normalized and normalized in _normalize_search_text(p) else 1.5)
                for p in await db_get_all_patterns()
                if normalized and normalized in _normalize_search_text(p)
            ]
            matches = sorted(local, key=lambda x: (-x[1], x[0].lower()))[:COLLECTION_MAX_MODELS]

        if not matches:
            await status.edit_text(
                f"⚠️ برای <b>{html_entities.escape(q)}</b> نتیجهٔ قابل‌اعتمادی پیدا نشد.\n\n"
                "جستجوی وب انجام شد ولی Model ذخیره‌شده‌ای که با این موضوع تطبیق کافی داشته باشد پیدا نشد."
            )
            await state.clear()
            return

        patterns = [p for p, _score in matches]
        rows = await db_get_items_for_patterns(
            patterns,
            limit_per_pattern=max(2, COLLECTION_MAX_GIFTS),
            total_limit=max(COLLECTION_MAX_GIFTS * 4, 32),
        )
        if not rows:
            await status.edit_text(
                f"⚠️ Model مرتبط با <b>{html_entities.escape(q)}</b> پیدا شد، اما نمونهٔ ذخیره‌شده‌ای از آن در دیتابیس نداریم.\n"
                "ابتدا کالکشن‌های مربوطه را از پنل ادمین/اسکن به‌روز کن."
            )
            await state.clear()
            return

        groups: dict[str, list[tuple[str, str, int, str]]] = {}
        for row in rows:
            groups.setdefault(row[0], []).append(row)
        ordered_models = [p for p, _score in matches if p in groups]
        ordered_models += [p for p in groups if p not in ordered_models]

        await status.delete()

        collection_title = f"{q} Collection"
        total_links = sum(len(groups[p]) for p in ordered_models)
        await message.answer(
            "\n".join([
                f"📦 <b>{html_entities.escape(collection_title)}</b>",
                f"🔎 جستجو: <b>{html_entities.escape(q)}</b>",
                f"🧩 {len(ordered_models)} مدل مرتبط پیدا شد",
                f"🎁 {total_links} لینک گیفت ذخیره‌شده",
            ])
        )

        # فقط یک تصویر برای کل Collection می‌سازیم. از چند Model نماینده انتخاب
        # می‌شود، اما همه در یک collage واحد قرار می‌گیرند.
        collage_paths: list[str] = []
        collage_colors: list[str] = []
        for pattern in ordered_models:
            representative = groups[pattern][0]
            _pattern, base_link, idx, color = representative
            try:
                cards = await build_nft_result_cards(
                    base_link,
                    beautify_name(extract_slug(base_link)),
                    [pattern],
                    1,
                    1,
                    exact_color=color or None,
                )
                if cards and cards[0][1]:
                    import tempfile
                    fd, path = tempfile.mkstemp(prefix="collection_", suffix=".jpg")
                    os.close(fd)
                    with open(path, "wb") as f:
                        f.write(cards[0][1])
                    temp_paths.append(path)
                    collage_paths.append(path)
                    if color:
                        collage_colors.append(color)
            except Exception:
                log.exception("ساخت تصویر نماینده collection برای model=%s ناموفق بود", pattern)
            if len(collage_paths) >= 6:
                break

        if collage_paths:
            try:
                collage_bytes = build_collection_collage(q, collage_paths, collage_colors)
                await message.answer_photo(
                    BufferedInputFile(
                        collage_bytes,
                        filename=f"{RE_SAFE_FILENAME.sub('_', q)[:45]}_collection.jpg",
                    ),
                    caption=(
                        f"📦 <b>{html_entities.escape(collection_title)}</b>\n"
                        f"🧩 {len(ordered_models)} مدل مرتبط\n"
                        f"🎁 {total_links} لینک گیفت\n"
                        "🖼️ تصویر بالا یک نمای کلی از کل نتایج این کالکشن است."
                    ),
                )
            except Exception:
                log.exception("ساخت collage collection برای %r ناموفق بود", q)

        # بعد از همان یک تصویر، هر Model فقط متن + لینک‌های واقعی خودش را دارد.
        for model_index, pattern in enumerate(ordered_models, 1):
            model_rows = groups[pattern]
            representative = model_rows[0]
            _pattern, _base_link, _idx, color = representative
            backdrop_name = color or "—"

            await message.answer(
                "\n".join([
                    f"🧩 <b>نتیجه {model_index}</b>",
                    f"🎭 مدل: <b>{html_entities.escape(pattern)}</b>",
                    f"🎨 بک‌گراند: <b>{html_entities.escape(backdrop_name)}</b>",
                    f"🎁 تعداد گیفت‌های این مدل: <b>{len(model_rows)}</b>",
                ])
            )

            link_lines = []
            seen_urls = set()
            for _p, link_base, item_idx, _color in model_rows:
                url = f"{link_base}{item_idx}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                link_lines.append(url)

            if link_lines:
                quote_body = "\n".join(link_lines)
                for chunk in chunk_message(quote_body, "", limit=TELEGRAM_MSG_LIMIT):
                    await message.answer(
                        f"<blockquote>{chunk}</blockquote>",
                        disable_web_page_preview=True,
                    )

        log.info(
            "collection search finished: query=%r models=%d web_results=%d links=%d",
            q, len(ordered_models), len(web_results), total_links,
        )
    except Exception:
        log.exception("collection web search failed for %r", q)
        try:
            await status.edit_text(
                "❌ جستجوی وب موقتاً با خطا مواجه شد.\n"
                "اتصال اینترنت سرور و WEB_SEARCH_ENABLED را بررسی کن."
            )
        except Exception:
            await message.answer(
                "❌ جستجوی وب موقتاً با خطا مواجه شد.\n"
                "اتصال اینترنت سرور و WEB_SEARCH_ENABLED را بررسی کن."
            )
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        await state.clear()


def build_charge_packages_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ton in TON_PACKAGE_OPTIONS:
        kb.button(text=f"💎 {ton} TON  →  🤖 {credits_for_ton(ton)} اعتبار", callback_data=f"charge:pkg:{ton}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))
    return kb.as_markup()


def build_charge_selected_kb(ton: int) -> InlineKeyboardMarkup:
    credits = credits_for_ton(ton)
    kb = InlineKeyboardBuilder()
    # دو ردیف نمایشیِ متصل: عدد TON و اعتبار دقیقاً از یک مقدار محاسبه می‌شوند.
    kb.row(InlineKeyboardButton(text=f"🔹 تعداد TON: {ton}", callback_data="menu:noop"))
    kb.row(InlineKeyboardButton(text=f"🤖 اعتبار استفاده از ربات: {credits}", callback_data="menu:noop"))
    kb.row(InlineKeyboardButton(text=runtime_settings.get("charge_auto_button", DEFAULT_SETTINGS["charge_auto_button"]), callback_data=f"charge:auto:{ton}", icon_custom_emoji_id=runtime_settings.get("charge_auto_button_id") or None))
    kb.row(InlineKeyboardButton(text=runtime_settings.get("charge_manual_button", DEFAULT_SETTINGS["charge_manual_button"]), callback_data=f"charge:manual:{ton}", icon_custom_emoji_id=runtime_settings.get("charge_manual_button_id") or None))
    kb.row(InlineKeyboardButton(text="🔄 تغییر مقدار", callback_data="menu:charge"))
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))
    return kb.as_markup()


@router.callback_query(F.data == "menu:charge")
async def menu_charge(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await call.message.answer(get_msg("msg_charge_menu", credit_price=f"{get_credit_price_ton():g}"), reply_markup=build_charge_packages_kb())


@router.callback_query(F.data.startswith("charge:pkg:"))
async def charge_pick_package(call: CallbackQuery, state: FSMContext):
    try:
        ton = int(call.data.split(":")[-1])
    except ValueError:
        await call.answer("مقدار نامعتبر است.", show_alert=True)
        return
    if ton not in TON_PACKAGE_OPTIONS:
        await call.answer("مقدار نامعتبر است.", show_alert=True)
        return
    await state.update_data(charge_ton=ton)
    await call.answer()
    await call.message.edit_text(get_msg("msg_charge_details", ton=ton, credits=credits_for_ton(ton)), reply_markup=build_charge_selected_kb(ton))


@router.callback_query(F.data.startswith("charge:auto:"))
async def charge_auto_start(call: CallbackQuery, state: FSMContext):
    try:
        ton = int(call.data.split(":")[-1])
    except ValueError:
        await call.answer("مقدار نامعتبر است.", show_alert=True)
        return
    if ton not in TON_PACKAGE_OPTIONS:
        await call.answer("مقدار نامعتبر است.", show_alert=True)
        return
    credits = credits_for_ton(ton)
    order_id = uuid.uuid4().hex[:16].upper()
    comment = f"NFTBOT-{order_id}"
    # ساخت سفارش با ID یکتا؛ تأیید نهایی فقط با تراکنش واقعی روی شبکه انجام می‌شود.
    await db_create_ton_payment(call.from_user.id, ton, credits, comment)
    amount_nano = ton * 1_000_000_000
    tonkeeper_url = f"ton://transfer/{GRAM_WALLET}?amount={amount_nano}&text={quote_plus(comment)}"
    kb = InlineKeyboardBuilder()
    kb.button(text=runtime_settings.get("charge_auto_button", DEFAULT_SETTINGS["charge_auto_button"]), url=tonkeeper_url, icon_custom_emoji_id=runtime_settings.get("charge_auto_button_id") or None)
    kb.button(text="🔄 بررسی وضعیت پرداخت", callback_data="charge:status")
    kb.button(text="🔙 منوی اصلی", callback_data="menu:home")
    kb.adjust(1)
    await state.clear()
    await call.answer()
    await call.message.answer(get_msg("msg_charge_auto", ton=ton, credits=credits, comment=comment), reply_markup=kb.as_markup())


@router.callback_query(F.data == "charge:status")
async def charge_status(call: CallbackQuery):
    await call.answer("پرداخت‌ها به‌صورت خودکار بررسی می‌شوند. اگر پرداخت تأیید شده باشد، موجودی اضافه می‌شود.", show_alert=True)


@router.callback_query(F.data.startswith("charge:manual:"))
async def charge_manual_start(call: CallbackQuery, state: FSMContext):
    try:
        ton = int(call.data.split(":")[-1])
    except ValueError:
        await call.answer("مقدار نامعتبر است.", show_alert=True)
        return
    if ton not in TON_PACKAGE_OPTIONS:
        await call.answer("مقدار نامعتبر است.", show_alert=True)
        return
    credits = credits_for_ton(ton)
    await state.set_state(ChargeFlow.waiting_manual_receipt)
    await state.update_data(charge_ton=ton, charge_credits=credits)
    await call.answer()
    await call.message.answer(
        f"🧾 <b>پرداخت دستی</b>\n\n"
        f"💎 مبلغ: <b>{ton} TON</b>\n"
        f"🤖 اعتبار موردنظر: <b>{credits}</b>\n\n"
        f"به ولت زیر <b>دقیقاً {ton} TON</b> بفرست و سپس عکس/فایل رسید را همین‌جا ارسال کن:\n\n"
        f"<code>{GRAM_WALLET}</code>",
    )


@router.message(StateFilter(ChargeFlow.waiting_manual_receipt), F.photo | F.document)
async def charge_manual_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    ton = int(data.get("charge_ton") or 0)
    credits = int(data.get("charge_credits") or 0)
    user = message.from_user
    caption = (
        "🧾 <b>رسید شارژ دستی جدید</b>\n\n"
        f"👤 {html_entities.escape(user.full_name or '')}\n"
        f"🆔 <code>{user.id}</code>\n"
        f"username: @{user.username or '—'}\n"
        f"💎 مبلغ اعلام‌شده: <b>{ton} TON</b>\n"
        f"🤖 اعتبار موردنظر: <b>{credits}</b>\n\n"
        f"برای شارژ از پنل /admin → «💰 شارژ موجودی کاربر»\n"
        f"آیدی: <code>{user.id}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            if message.photo:
                await message.bot.send_photo(admin_id, message.photo[-1].file_id, caption=caption)
            elif message.document:
                await message.bot.send_document(admin_id, message.document.file_id, caption=caption)
        except Exception:
            log.exception(f"ارسال رسید دستی به ادمین {admin_id} ناموفق")
    await state.clear()
    await message.answer("✅ رسید برای ادمین ارسال شد. بعد از تأیید، موجودی‌ات شارژ می‌شود.", reply_markup=build_main_menu_kb())


@router.message(StateFilter(ChargeFlow.waiting_manual_receipt))
async def charge_manual_receipt_other(message: Message, state: FSMContext):
    await message.answer("لطفاً فقط عکس یا فایل رسید را بفرست، یا /start برای انصراف.")


def build_pricing_keyboard() -> InlineKeyboardMarkup:
    kb=InlineKeyboardBuilder(); kb.button(text="✏️ تغییر قیمت هر اعتبار",callback_data="pricing:set"); kb.button(text="🔙 بازگشت",callback_data="admin:back"); kb.adjust(1); return kb.as_markup()

@router.callback_query(F.data == "admin:pricing")
async def admin_pricing(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return await call.answer("⛔",show_alert=True)
    await state.clear(); await call.answer(); p=get_credit_price_ton(); await safe_edit(call.message,f"💰 <b>قیمت اعتبار</b>\n\nهر ۱ اعتبار = <b>{p:g} TON</b>\n۱ TON ≈ <b>{credits_for_ton(1)}</b> اعتبار\n\nاین قیمت برای اعتبار دریافتی تراکنش Tonkeeper استفاده می‌شود.",reply_markup=build_pricing_keyboard())

@router.callback_query(F.data == "pricing:set")
async def admin_pricing_set(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return
    await call.answer(); await state.set_state(AdminFlow.waiting_credit_price); await safe_edit(call.message,"✏️ قیمت هر اعتبار را به TON بفرست. مثال: <code>0.2</code>")

@router.message(StateFilter(AdminFlow.waiting_credit_price))
async def admin_pricing_input(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    try: value=float((message.text or "").strip().replace(",","."))
    except ValueError: await message.answer("❌ عدد معتبر بفرست."); return
    if value<=0 or value>100: await message.answer("❌ قیمت باید بین 0 و 100 TON باشد."); return
    await db_set_setting("credit_price_ton",str(value)); await state.clear(); await message.answer(f"✅ قیمت هر اعتبار: <b>{value:g} TON</b>",reply_markup=build_pricing_keyboard())

# ---- ادمین: شارژ موجودی ----

@router.callback_query(F.data == "admin:credit")
async def admin_credit_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_credit_user)
    await call.answer()
    await call.message.answer(
        "💰 آیدی عددی کاربر را بفرست (مثلاً <code>7416102956</code>):"
    )


@router.message(StateFilter(AdminFlow.waiting_credit_user))
async def admin_credit_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ فقط عدد آیدی.")
        return
    await state.update_data(credit_uid=uid)
    await state.set_state(AdminFlow.waiting_credit_amount)
    await message.answer(
        f"کاربر <code>{uid}</code>\n"
        f"چند <b>GRAM</b> واریز شود؟ (هر GRAM = {get_credit_price_ton():g} TON)\n"
        "فقط عدد بفرست، مثلاً <code>2</code>"
    )


@router.message(StateFilter(AdminFlow.waiting_credit_amount))
async def admin_credit_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        grams = int((message.text or "").strip())
        if grams <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ عدد معتبر بفرست.")
        return
    data = await state.get_data()
    uid = data.get("credit_uid")
    await state.clear()
    if not uid:
        await message.answer("لغو شد.")
        return
    searches = credits_for_ton(grams)
    new_bal = await db_add_balance(uid, searches)
    await message.answer(
        f"✅ به کاربر <code>{uid}</code> مقدار <b>{grams}</b> GRAM "
        f"(= {searches} سرچ) اضافه شد.\\n"
        f"موجودی فعلی او: <b>{new_bal}</b> سرچ",
        reply_markup=build_admin_keyboard(),
    )
    try:
        await message.bot.send_message(
            uid,
            f"✅ حسابت شارژ شد: <b>+{grams}</b> GRAM "
            f"(= {searches} سرچ)\\nموجودی: <b>{new_bal}</b>",
        )
    except Exception:
        pass



# -------------------------------------------------------------------------
#  اجرای ربات
# ------------------------------------------------------------------------- #

async def main():
    _ensure_db_parent_and_seed()
    _init_db()
    await db_load_settings()
    scanner.update_limits(get_max_concurrent(), get_max_per_second())
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=SQLiteFSMStorage())
    dp.include_router(router)
    ton_worker_task = asyncio.create_task(ton_payment_worker(bot))

    # هر Exception ای که داخل یک تسک پس‌زمینه‌ی asyncio بترکه (نه مستقیم در
    # مسیر یک هندلر) رو هم کامل با traceback لاگ می‌کنه، نه فقط یک warning ریز.
    asyncio.get_running_loop().set_exception_handler(_asyncio_exception_handler)

    await scanner.start()
    log.info("Bot starting...")

    # قبلاً یک کرش تو start_polling کل پروسه رو با یک traceback (که اگر
    # کنسول بسته بود اصلاً دیده نمی‌شد) می‌کشت پایین. حالا: هر کرش با
    # traceback کامل هم روی کنسول هم روی فایل bot.log لاگ می‌شود، و به‌جای
    # مردن کامل ربات، بعد از یک مکث کوتاه (با backoff افزایشی تا سقف ۶۰
    # ثانیه، برای جلوگیری از لوپ دیوانه‌وار روی یک خطای پایدار) دوباره
    # polling شروع می‌شود.
    restart_delay = 3
    try:
        while True:
            try:
                await dp.start_polling(bot)
                break  # توقف عادی/graceful (نه کرش) -> از حلقه خارج شو
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception:
                log.exception(
                    f"ربات با یک خطای مدیریت‌نشده متوقف شد؛ {restart_delay} ثانیه دیگر ری‌استارت می‌شود..."
                )
                await asyncio.sleep(restart_delay)
                restart_delay = min(restart_delay * 2, 60)
    finally:
        ton_worker_task.cancel()
        try:
            await ton_worker_task
        except asyncio.CancelledError:
            pass
        await scanner.close()


if __name__ == "__main__":
    asyncio.run(main())

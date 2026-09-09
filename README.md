# NFT Collection Scanner Bot

ربات تلگرام اسکن کالکشن‌های NFT (t.me/nft/...)

## فایل‌های مهم

| فایل | توضیح |
|------|--------|
| `nft_scanner_bot.py` | کد اصلی ربات |
| `nft_cache.db.gz` | seed فشردهٔ کش (~۹MB) — در اولین اجرا استخراج می‌شود |
| `Dockerfile` | ساخت ایمیج برای Railway |
| `requirements.txt` | وابستگی‌های پایتون |
| `.gitignore` | دیتابیس runtime و عکس‌ها در گیت نیستند |

**دیتابیس کامل (~۷۳MB) در گیت نیست.** فقط seed gzip شده است تا از حد GitHub رد نشود.

---

## استقرار روی Railway (پایدار ماندن کش)

بدون Volume، هر Redeploy فایل‌های داخل کانتینر را پاک می‌کند. حتماً Volume بساز:

### ۱) ساخت پروژه
1. ریپو را به GitHub بفرست (همین پوشه).
2. در [Railway](https://railway.app) → New Project → Deploy from GitHub.
3. Variables را ست کن:
   - `BOT_TOKEN` = توکن BotFather
   - (اختیاری) `TONCENTER_API_KEY`
   - اگر Volume را جای دیگری mount کردی:
     - `NFT_CACHE_DB=/data/nft_cache.db`
     - `NFT_IMAGES_DIR=/data/nft_images`  
     (این دو در Dockerfile هم به‌صورت پیش‌فرض هستند)

### ۲) Volume (حیاتی)
1. در سرویس Railway → **Volumes** → Add Volume
2. Mount path: **`/data`**
3. Redeploy کن

با این کار:
- اولین بوت: اگر `/data/nft_cache.db` نباشد، از `nft_cache.db.gz` ساخته می‌شود.
- بعد از آن اسکن‌ها و موجودی کاربران روی Volume می‌مانند و با آپدیت کد پاک نمی‌شوند.

### ۳) تست لوکال
```bash
export BOT_TOKEN="توکن"
# اختیاری: مسیر دیتابیس موقت
# export NFT_CACHE_DB=./data/nft_cache.db
python nft_scanner_bot.py
```

---

## سینک seed با دیتابیس واقعی

اگر بعداً کش قوی‌تری ساختی و خواستی seed را عوض کنی:

```bash
# روی سروری که DB خوب دارد:
sqlite3 /path/to/nft_cache.db "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;"
gzip -9 -c /path/to/nft_cache.db > nft_cache.db.gz
# فایل gz را در ریپو commit کن (جایگزین قبلی)
```

فقط وقتی Volume خالی باشد seed دوباره استخراج می‌شود؛ Volume پر را دست نمی‌زند.

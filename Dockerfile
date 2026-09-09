FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    libjpeg62-turbo \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کد + seed فشردهٔ دیتابیس (~۹MB). دیتابیس کامل روی Volume ساخته می‌شود.
COPY nft_scanner_bot.py .
COPY nft_cache.db.gz .

ENV PYTHONUNBUFFERED=1
# پیش‌فرض: دیتابیس روی Volume در /data (در Railway Volume را به /data mount کن)
ENV NFT_CACHE_DB=/data/nft_cache.db
ENV NFT_IMAGES_DIR=/data/nft_images

CMD ["python", "nft_scanner_bot.py"]

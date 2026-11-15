FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    CLIPFORGE_WORKDIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git curl ca-certificates nodejs npm \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libx11-xcb1 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libasound2 libxshmfence1 libgtk-3-0 \
    libdrm2 libxfixes3 libxkbcommon0 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN cd /app/remotion && npm install --no-audit --no-fund \
    && npx remotion browser ensure
RUN mkdir -p /data/jobs

EXPOSE 7860
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT}"]

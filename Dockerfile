FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    CLIPFORGE_WORKDIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN sed -i 's/^ scenedetect/scenedetect/' requirements.txt \
    && pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data/jobs

EXPOSE 7860
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT}"]

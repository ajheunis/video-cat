FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "duckdb>=2.0"

WORKDIR /app
COPY video_browser.py .

ENV VB_HOST=0.0.0.0 \
    VB_PORT=8000 \
    VB_DB=/data/videos_catalog.duckdb \
    VB_CACHE=/cache \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["python", "video_browser.py"]

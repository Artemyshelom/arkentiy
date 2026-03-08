FROM python:3.11-slim

WORKDIR /app

# Системные зависимости (curl + sqlite3 + Playwright/Chromium deps)
RUN apt-get update && apt-get install -y \
    curl \
    sqlite3 \
    # Playwright Chromium системные библиотеки
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libx11-xcb1 \
    libxcb1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Python зависимости (слой кэшируется отдельно)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright: скачиваем Chromium (~130MB, отдельный слой)
RUN playwright install chromium

# Код приложения
COPY app/ ./app/
COPY assets/ ./assets/

# Папки для данных и секретов
RUN mkdir -p /app/data /app/secrets /app/logs

EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]

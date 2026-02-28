# Проект «Аркентий» (Интеграции Ёбидоёби)

Кодовое имя: **Аркентий**.
Сервер автоматизаций: iiko → Telegram, Google Sheets → Telegram, пре-встречные саммари, мониторинг стоп-листа.

---

## Быстрый старт

```bash
# 1. Скопировать шаблон секретов
cp .env.example .env

# 2. Заполнить .env (токены, ключи)
nano .env

# 3. Положить Google Service Account JSON
mkdir -p secrets
cp /путь/к/service-account.json secrets/google-service-account.json

# 4. Запустить
docker compose up -d --build

# 5. Проверить
curl http://localhost:8000/health
```

---

## Что делает сервис

| Задача | Расписание | Откуда | Куда |
|--------|-----------|--------|------|
| Мониторинг стоп-листа | Каждые 5 мин | iiko | Telegram #алерты |
| Выгрузка заказов/выручки | 23:00 ежедневно | iiko | Google Sheets |
| Ежедневный отчёт | 23:30 ежедневно | Google Sheets | Telegram #отчёты |
| Пре-встречные саммари | Каждые 15 мин | Google Calendar | Telegram #встречи |
| Просроченные задачи | 09:00 ежедневно | Битрикс24 | Telegram #отчёты |
| Heartbeat | Каждые 30 мин | — | Telegram #мониторинг |
| Бэкап базы данных | 02:00 ежедневно | SQLite | Google Drive |
| Webhook MyMeet | По событию | MyMeet.ai | Файл + Telegram #встречи |
| Webhook Битрикс24 | По событию | Битрикс24 | Telegram #отчёты |

---

## Структура

```
app/
├── main.py              # FastAPI + APScheduler (точка входа)
├── config.py            # Настройки из .env
├── database.py          # SQLite: кэш токенов, логи, очереди
├── clients/             # Обёртки над API
│   ├── iiko.py          # iiko Cloud API
│   ├── telegram.py      # Telegram Bot API
│   ├── google_sheets.py # Google Sheets API
│   ├── google_calendar.py
│   ├── bitrix24.py      # Битрикс24 REST API
│   └── mymeet.py        # MyMeet.ai API
├── jobs/                # Задачи по расписанию
│   ├── iiko_stoplist.py
│   ├── iiko_to_sheets.py
│   ├── sheets_to_tg.py
│   ├── pre_meeting.py
│   └── task_tracker.py
├── webhooks/            # Входящие события
│   ├── mymeet.py
│   └── bitrix.py
└── monitoring/
    └── healthcheck.py   # /health + heartbeat + бэкап
```

---

## API эндпоинты

| Метод | URL | Описание |
|-------|-----|---------|
| GET | `/health` | Статус сервиса (для UptimeRobot) |
| GET | `/jobs` | Список задач и время следующего запуска |
| POST | `/run/{job_id}` | Запустить задачу вручную |
| POST | `/webhook/mymeet` | Webhook от MyMeet.ai |
| POST | `/webhook/bitrix` | Webhook от Битрикс24 |
| GET | `/docs` | Swagger UI (автодокументация) |

### Ручной запуск задач

```bash
# Запустить проверку стоп-листа прямо сейчас
curl -X POST https://api.твой-домен.ru/run/iiko_stoplist

# Запустить выгрузку iiko → Sheets
curl -X POST https://api.твой-домен.ru/run/iiko_to_sheets

# Посмотреть все задачи
curl https://api.твой-домен.ru/jobs
```

---

## Переменные окружения

Все переменные в `.env`. Шаблон: `.env.example`.

Ключевые:
- `IIKO_API_KEY` — ключ iiko API
- `IIKO_ORG_IDS` — JSON `{"Абакан": "uuid", ...}`
- `TELEGRAM_BOT_TOKEN` — токен бота
- `GOOGLE_SERVICE_ACCOUNT_FILE` — путь к JSON-ключу

---

## Настройка VPS

Подробная пошаговая инструкция: [SETUP.md](SETUP.md)

Кратко: Ubuntu 24.04 + Docker + Caddy (автоSSL).

---

## Мониторинг

- **UptimeRobot** пингует `/health` каждые 5 мин → алерт в Telegram при падении
- **Heartbeat** в Telegram #мониторинг каждые 30 минут
- **Логи задач** в SQLite (таблица `job_logs`)

Просмотр логов:
```bash
docker compose logs -f app
docker compose exec app sqlite3 /app/data/app.db "SELECT * FROM job_logs ORDER BY started_at DESC LIMIT 20;"
```

---

## Города

Абакан, Барнаул, Томск, Черногорск.
Все данные разбиты по городам через `IIKO_ORG_IDS`.

---

## Связи с агентами

- **@интегратор** — отвечает за этот код
- **@координатор** — читает транскрибации из `/app/data/конспекты/`
- **@аудитор** — использует выгрузки из Google Sheets для аудита

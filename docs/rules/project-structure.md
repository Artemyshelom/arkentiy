# Структура проекта Аркентия

```
app/
├── main.py              — FastAPI + scheduler registry (только импорты + add_job)
├── config.py            — Settings
├── database.py          — ORM, таблицы, UPSERT
├── database_pg.py       — PostgreSQL backend
├── access.py            — RBAC
├── clients/             — API-клиенты (iiko, telegram, sheets, bitrix)
├── jobs/                — Scheduled tasks (отдельный .py на каждую задачу)
├── routers/             — FastAPI routes
├── webhooks/            — Incoming webhooks
├── monitoring/          — Health checks
└── migrations/          — DB migrations

web/                     — Frontend (cabinet, landing, login)
docs/                    — Документация
├── specs/tg/            — TG UX specs
├── specs/web/           — Web UX specs
├── rules/               — Справочники интегратора
├── CHANGELOG.md
└── README.md

tests/                   — Тесты
dev/                     — Debug-скрипты, эксперименты
```

## Правила размещения

- Новый job → `app/jobs/имя.py`
- Новый API-клиент → `app/clients/имя.py`
- Новый роут → `app/routers/имя.py`
- Debug-скрипт → `dev/` или `tests/`, **никогда** в корне
- Не захламлять корень проекта

## .gitignore

В git: `app/`, `web/`, `docs/`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`
НЕ в git: `.env`, `secrets/`, `*.bak.*`, `__pycache__/`, `backups/`

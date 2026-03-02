# Инфраструктура Аркентия

## VPS

| Параметр | Значение |
|----------|---------|
| IP | `5.42.98.2` (Timeweb Cloud, Ubuntu 24.04, 1 vCPU 2GB) |
| SSH | `ssh -i ~/.ssh/artemii_vps root@5.42.98.2` |
| Проект | `/opt/ebidoebi/` |
| Стек | Python 3.11, FastAPI, APScheduler, PostgreSQL, Docker |

## Google Service Account

| Параметр | Значение |
|----------|---------|
| Email | `cursoraccountgooglesheets@cursor-487608.iam.gserviceaccount.com` |
| JSON (VPS) | `/opt/ebidoebi/secrets/google-service-account.json` |

## Telegram

| Чат | ID | Назначение |
|-----|-----|-----------|
| Группа Ёбидоёби | `-5160506328` | alerts, reports, meetings |
| Артемий (лично) | `255968113` | monitoring, admin |
| Доступ к боту в личке | только admin `255968113` | |
| Доступ в группе | `TELEGRAM_ALLOWED_IDS` в `.env` | |

## Ключевые конфиги

- **Точки:** `/opt/ebidoebi/secrets/branches.json` — 9 точек, поля: `name`, `dept_id`, `city`, `utc_offset`, `bo_url`
- **Новая точка:** добавь строку в `branches.json` — код менять не нужно
- **Новый клиент** (не точка): уточни у Артемия изоляцию данных

## PostgreSQL

- **Строка подключения:** `DATABASE_URL` в `.env` (формат: `postgresql://user:pass@host:5432/db`)
- **Бэкап БД:** `/opt/ebidoebi/backups/` — ежедневно в 02:00 МСК автоматически

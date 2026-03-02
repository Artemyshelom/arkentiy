# Аркентий — SaaS-бот автоматизации доставки (iiko)

> Production-система. Живые заказы, живые деньги.

## Быстрая навигация

| Что нужно | Где искать |
|-----------|-----------|
| Спецификация команды бота | `docs/specs/tg/` |
| Спецификация веб-интерфейса | `docs/specs/web/` |
| API интеграций (iiko, ТБанк) | `docs/rules/API_iiko.md`, `docs/rules/API_*.md` |
| Архитектура, модули, jobs | `docs/rules/project-structure.md` |
| Подключение нового клиента | `app/onboarding/README.md` |
| История изменений (для пользователей) | `docs/CHANGELOG.md` |
| Техническая история работ | `docs/Журнал.md` |
| Стратегия и приоритеты | `docs/Дорожная карта.md` |
| Очередь задач | `docs/BACKLOG.md` |
| Протокол деплоя | `docs/rules/deploy-protocol.md` |
| Правила архитектуры | `docs/rules/project-structure.md` |

## Архитектура проекта

```
app/
├── main.py                  # Точка входа, реестр jobs и routers
├── config.py                # Конфигурация из .env
├── ctx.py                   # ContextVar для мультитенанта
│
├── database.py              # SQLite (legacy)
├── database_pg.py           # PostgreSQL (production)
│
├── clients/                 # API интеграции (читают внешние сервисы)
│   ├── iiko.py              # iiko BO API
│   ├── telegram.py          # Telegram Bot API
│   ├── competitor_scraper.py
│   ├── tbank_reconciliation.py
│   └── __init__.py
│
├── services/                # Бизнес-логика (обработка данных, хранение)
│   ├── access.py            # Управление доступом и правами
│   ├── access_manager.py    # UI для управления доступом
│   └── __init__.py
│
├── jobs/                    # Scheduled tasks (APScheduler)
│   ├── arkentiy.py          # Telegram polling loop (основной бот)
│   ├── daily_report.py      # Утренний отчёт
│   ├── late_alerts.py       # Алерты опоздания
│   ├── iiko_status_report.py
│   ├── iiko_to_sheets.py
│   ├── billing.py
│   ├── subscription_lifecycle.py
│   ├── competitor_monitor.py
│   ├── audit.py
│   ├── marketing_export.py
│   ├── __init__.py
│   └── ...
│
├── routers/                 # FastAPI endpoints
│   ├── cabinet.py           # Веб-кабинет клиента
│   ├── onboarding.py        # Подключение клиента (веб)
│   ├── payments.py          # ЮKassa интеграция
│   └── __init__.py
│
├── webhooks/                # Incoming webhooks
│   ├── bitrix.py
│   └── __init__.py
│
├── monitoring/              # Health check, метрики
│   ├── healthcheck.py
│   └── __init__.py
│
├── onboarding/              # Скрипты для подключения новых клиентов
│   ├── README.md
│   ├── backfill_shaburov.py (пример)
│   ├── backfill_daily.py    (шаблон)
│   └── __init__.py
│
├── migrations/              # SQL миграции (PostgreSQL)
│   ├── 001_initial.sql
│   ├── 002_payment_changed.sql
│   ├── 003_web_platform.sql
│   ├── 004_shaburov_onboarding.sql
│   └── ...
│
└── utils/                   # Shared utilities (если будут)
    └── __init__.py
```

## Принципы архитектуры

### 1. Модульная изоляция
- Каждый job/service = **отдельный файл** + **отдельная логика**
- `main.py` = только реестр (импорты + `scheduler.add_job()`)
- Связь между модулями = через `database_pg.py` или API

### 2. Слои ответственности
- **clients/** = читают данные из внешних API (iiko, Telegram, банков)
- **services/** = обрабатывают данные, хранят в БД
- **jobs/** = scheduled tasks (APScheduler), бесконечные loop (бот)
- **routers/** = FastAPI endpoints для веб

### 3. Multi-tenant
- Каждый клиент = отдельная запись в таблице `tenants`
- `ContextVar` в `ctx.py` для динамического routing по tenant_id
- Jobs работают на **всех** активных тенантах

## Как добавить новый job

1. Создай файл в `jobs/<job_name>.py`
2. Напиши функцию `async def main()` или `async def job_handler()`
3. Импортируй в `main.py`: `from app.jobs import <job_name>`
4. Зарегистрируй: `scheduler.add_job(<job_name>.main, trigger=IntervalTrigger(...))`
5. Логирование: `logger.info(...)` во все критические точки

## Как добавить новый клиент

1. **Миграция БД**: создай `app/migrations/00X_<client>.sql`
   - INSERT в tenants, iiko_credentials, tenant_chats
   - Используй шаблон в `docs/rules/migration-template.sql`

2. **Бэкфилл данных** (опционально):
   - Скопируй `app/onboarding/backfill_shaburov.py` → `backfill_<client>.py`
   - Обнови `tenant_id`, `branch_names`, `iiko_url`
   - Запусти локально: `python -m app.onboarding.backfill_<client>`

3. **Деплой**:
   - Следуй `docs/rules/deploy-protocol.md`
   - Запусти миграцию на VPS
   - Перезапусти контейнер: `docker compose build --no-cache && up -d`

## Критические правила iiko API

> Нарушение = повреждение данных. Заучи наизусть.

1. **Events API — сортировка по дате обязательна**
   - iiko отдаёт события не хронологически
   - Без `sorted(events, key=lambda e: e.findtext("date"))` — 30-40% заказов с неверным статусом

2. **deliveryOrderEdited — merge, не overwrite**
   - Содержит только изменённые поля
   - Правильно: `existing.update(changed_fields)`

3. **OLAP — через /service/, не /api/**
   - `/api/reports/olap` — broken (500)
   - Правильно: `GET /service/reports/report.jspx?presetId=UUID` + JSESSIONID

4. **OLAP — клиентская фильтрация**
   - `departmentIds` не работает
   - Получай все → фильтруй по `<Department>` == `branch["name"]`

5. **XML теги с точками — iterate, не findtext**
   - `findtext("Tag.With.Dots")` — broken
   - Правильно: `for child in elem: if child.tag == "Tag.With.Dots"`

## Чеклист деплоя

- [ ] `ls` + `cat` на VPS, сравни с локальными
- [ ] Бэкап: `cp файл.py файл.py.bak.YYYYMMDD_HHMMSS`
- [ ] SCP: только дельту/новые файлы
- [ ] Build: `docker compose build --no-cache && up -d`
- [ ] Проверка: `docker compose ps` (healthy?) + `logs --tail=20`
- [ ] Git push: только после healthy + чистые логи

## Команда

| Роль | Вызов | Что делает |
|------|-------|-----------|
| ПМ | `@пм` | Декомпозирует задачи, координирует agentов |
| UX-бот | `@ux-бот` | Проектирует команды Telegram, интерфейс |
| Веб | `@веб` | Проектирует веб-страницы, дашборды |
| Интегратор | `@интегратор` | Реализует, деплоит, отлаживает |

## Полезные ссылки

- **GitHub**: https://github.com/Artemyshelom/arkentiy
- **VPS**: `ssh -i ~/.ssh/cursor_arkentiy_vps root@5.42.98.2`
- **Telegram для уведомлений**: группа `-5160506328`

---

*Последнее обновление: 2 марта 2026*

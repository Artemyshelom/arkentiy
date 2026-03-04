# Аркентий — статус обновлений

> Последнее обновление: 2026-03-03

---

## Сессия 2026-03-03: Мультитенантность + чистка кода

### Проблема

Мультитенантность добавлена, но работает только для первого тенанта (tenant_id=1).
Новые тенанты: заказы не ищутся, отчёты не приходят, алерты не срабатывают.
Причина — `settings.branches` всегда возвращает точки tenant_id=1 (хардкод в `config.py:138`).

---

## Что сделано

### Этап A — Мультитенантные фиксы

**Ключевое решение:** jobs получают список точек из БД через `get_branches(tenant_id)`, а не из `settings.branches`.

| Файл | Что изменено |
|------|-------------|
| `app/utils/tenant.py` | **СОЗДАН.** Универсальный декоратор `run_for_all_tenants(job_fn)` — получает активных тенантов из БД, устанавливает `ctx_tenant_id`, вызывает job для каждого |
| `app/utils/__init__.py` | **СОЗДАН.** Пустой init для пакета utils |
| `app/main.py` | Jobs обёрнуты в `run_for_all_tenants`: `job_olap_enrichment`, `job_export_iiko_to_sheets`, `job_cancel_sync`. Удалена старая inline-функция `_run_export_for_all_tenants` |
| `app/jobs/cancel_sync.py` | `settings.branches` → `get_branches(tenant_id)`, добавлен параметр `tenant_id` |
| `app/jobs/olap_enrichment.py` | `settings.branches` → `get_branches(tenant_id)`, добавлен параметр `tenant_id` |
| `app/jobs/daily_report.py` | Передаёт `branches=branches` в `get_all_branches_stats()` |
| `app/jobs/audit.py` | `settings.branches` → `get_all_branches()` из БД (5 мест) |
| `app/jobs/iiko_to_sheets.py` | `settings.branches` → `get_branches(tenant_id)`, branches пробрасывается через параметры |
| `app/jobs/marketing_export.py` | Использует `ctx_tenant_id` + `get_branches(tenant_id)` |
| `app/jobs/iiko_status_report.py` | Упрощена fallback-логика в `get_available_branches` |
| `app/clients/iiko_bo_olap_v2.py` | Добавлен параметр `branches` в `get_all_branches_stats`, `get_payment_breakdown`, `get_online_orders` (если None → fallback на settings.branches) |

### Этап B — Чистка мёртвого кода

**Удалённые файлы** (бэкапы в `backups/cleanup_20260303_225426/`):

| Файл | Причина удаления |
|------|-----------------|
| `/main.py` (корень) | Дубликат `app/main.py` со старыми импортами |
| `app/database.py` | Старый SQLite backend (~2000 строк), заменён `database_pg.py` |
| `app/onboarding/backfill_daily.py` | Зависел от SQLite |
| `app/jobs/iiko_stoplist.py` | Отключён, помечен "коряво работает" |
| `app/jobs/task_tracker.py` | Отключён, Битрикс24 |

**Другие изменения:**

| Файл | Что изменено |
|------|-------------|
| `app/db.py` | Убран SQLite fallback (`from app.database import *`). Теперь только PostgreSQL. Если `DATABASE_URL` не задан — `RuntimeError` при импорте |
| `app/jobs/olap_enrichment.py` | Убран `import aiosqlite`. Реализована PG-версия `_update_orders_raw` (раньше была no-op для PG). Динамические параметры `$1..$N` для asyncpg |
| `app/jobs/arkentiy.py` | Убран `import aiosqlite`, убран `DB_PATH` из импорта. Удалены 2 мёртвые SQLite-ветки (~90 строк) в функциях поиска заказов и отчёта опозданий |
| `app/monitoring/healthcheck.py` | Убран `aiosqlite`, удалён `job_backup_sqlite` целиком. Health check — только PG |
| `app/main.py` | Удалён `job_backup_sqlite` из scheduler. Удалены закомментированные блоки (Арсений, stoplist, pre-meeting, task_tracker, heartbeat) |
| `app/config.py` | Default `database_url` изменён с `sqlite+aiosqlite://...` на пустую строку |

### Этап C — Структурные улучшения

**Новые модули:**

| Файл | Содержимое |
|------|-----------|
| `app/utils/formatting.py` | `fmt_money(v)`, `fmt_num(v)`, `fmt_pct(v)` — были продублированы в `daily_report.py` и `tbank_reconciliation.py` |
| `app/utils/timezone.py` | `branch_tz(branch)`, `tz_from_offset(utc_offset)`, `now_local(tz)` — были продублированы в `iiko_status_report.py`, `daily_report.py`, `iiko_to_sheets.py` |
| `app/services/auth.py` | `hash_password()`, `verify_password()`, `_jwt_secret()`, `JWT_ALGO` — были в `routers/cabinet.py`, импортировались через кросс-модульный импорт из `routers/onboarding.py` |

**Обновлённые импорты:**

| Файл | Было | Стало |
|------|------|-------|
| `app/jobs/iiko_status_report.py` | Локальные `branch_tz`, `now_local` | `from app.utils.timezone import branch_tz, now_local` |
| `app/jobs/daily_report.py` | Локальные `_branch_tz`, `_fmt_money`, `_fmt_num`, `_fmt_pct` | Импорт из `app.utils.formatting` и `app.utils.timezone` (алиасы сохранены: `fmt_money as _fmt_money`) |
| `app/jobs/iiko_to_sheets.py` | `from app.jobs.iiko_status_report import branch_tz` | `from app.utils.timezone import branch_tz` |
| `app/main.py` | `from app.jobs.iiko_status_report import branch_tz` | `from app.utils.timezone import branch_tz` |
| `app/jobs/arkentiy.py` | `from app.jobs.daily_report import _fmt_money` | `from app.utils.formatting import fmt_money as _fmt_money` |
| `app/routers/cabinet.py` | Локальные `hash_password`, `verify_password`, `_jwt_secret`, `JWT_ALGO` | `from app.services.auth import ...` |
| `app/routers/onboarding.py` | `from app.routers.cabinet import hash_password, JWT_ALGO` | `from app.services.auth import hash_password, JWT_ALGO` |

### Расписание отчётов — таймзоны

**Архитектурное решение:** scheduler запускает jobs каждый час и определяет, какой `utc_offset` нужен сейчас.

Формула (scheduler в `Europe/Moscow`, UTC+3):
```
target_offset = 12 - текущий_час_МСК
```

Примеры (отчёт в 09:25 местного):
- UTC+7 (Красноярск/Канск): 05:25 МСК → `12 - 5 = 7` ✅
- UTC+3 (Москва): 09:25 МСК → `12 - 9 = 3` ✅
- UTC+10 (Владивосток): 02:25 МСК → `12 - 2 = 10` ✅

| Job | Trigger | Логика |
|-----|---------|--------|
| OLAP enrichment | каждый час в :00 | Обогащает только тенантов с matching offset |
| Утренний отчёт | каждый час в :25 | Шлёт отчёт для matching offset |
| Аудит | каждый час в :27 | Шлёт аудит для matching offset |

`utc_offset` хранится в таблице `iiko_credentials` для каждой точки.

---

## Потенциальные баги — куда смотреть

### 1. `app/db.py` — RuntimeError при запуске
**Симптом:** приложение не запускается, `RuntimeError: DATABASE_URL не задан`
**Причина:** убран SQLite fallback. Теперь `DATABASE_URL` обязателен.
**Решение:** убедиться что в `.env` / docker-compose задан `DATABASE_URL=postgresql://...`

### 2. Импорты — `ModuleNotFoundError` или `ImportError`
**Симптом:** `cannot import 'branch_tz' from 'app.jobs.iiko_status_report'` или аналогичное
**Причина:** функции перенесены в `app/utils/` и `app/services/auth.py`, но в каком-то месте остался старый импорт
**Куда смотреть:** `app/utils/timezone.py`, `app/utils/formatting.py`, `app/services/auth.py`
**Проверка:**
```bash
grep -r "from app.jobs.iiko_status_report import branch_tz" app/
grep -r "from app.jobs.daily_report import.*_fmt_money" app/
grep -r "from app.routers.cabinet import.*hash_password" app/
grep -r "from app.database import" app/
grep -r "import aiosqlite" app/
```

### 3. `_update_orders_raw` — новая PG-реализация
**Симптом:** OLAP enrichment не обновляет orders_raw или ошибки SQL
**Файл:** `app/jobs/olap_enrichment.py:155-209`
**Причина:** переписано с aiosqlite на asyncpg, динамические `$N` параметры
**Что проверить:** логи `olap_enrichment`, столбец `updated` в `log_job_finish`

### 4. Отчёты не приходят для нового тенанта
**Симптом:** тенант добавлен, но отчёт не приходит
**Куда смотреть:**
1. `iiko_credentials.utc_offset` — задан ли для точек нового тенанта?
2. `tenants.status` — должен быть `'active'`
3. `tenant_chats` — привязан ли Telegram-чат?
4. Логи: `morning_report UTC+X` — есть ли запись для нужного offset?

### 5. `settings.branches` — всё ещё используется
**Симптом:** какой-то job работает только для tenant_id=1
**Причина:** мог остаться незамеченный вызов `settings.branches`
**Проверка:**
```bash
grep -rn "settings\.branches" app/ --include="*.py" | grep -v "\.pyc" | grep -v "__pycache__"
```
**Исключения (допустимые):** `config.py` (определение property), `iiko_bo_olap_v2.py` (fallback если branches=None)

### 6. `job_backup_sqlite` — удалён
**Симптом:** в логах `No job by the id of backup_sqlite`
**Причина:** удалён из scheduler и healthcheck.py
**Решение:** не баг, ожидаемое поведение. PG имеет свою стратегию бэкапа.

### 7. `BACKEND` проверки в arkentiy.py
**Симптом:** код в `if BACKEND == "postgresql":` не выполняется
**Причина:** маловероятно, т.к. BACKEND всегда "postgresql", но SQLite-ветки удалены
**Файл:** `app/jobs/arkentiy.py` строки ~907, ~1500 — осталась `if` обёртка без `else`

---

## Не трогали (осознанно)

- Хардкод admin/chat ID — по указанию Артемия
- `app/jobs/bank_statement.py` — активно используется через `arkentiy.py` для сверки выписок 1С
- `tbank_reconciliation.py` — свой `_fmt_money` без "₽" (другая семантика, вызывающий код добавляет "р")
- `ctx_tenant_id` default=1 в `app/ctx.py` — оставлено для обратной совместимости
- `database_pg.py` — все функции с `tenant_id: int = 1` по умолчанию (не ломаем сигнатуры)
- `DB_PATH = None` в `database_pg.py:1536` — sentinel для совместимости

---

## Бэкапы

```
backups/cleanup_20260303_225426/
├── main.py              # дубликат из корня
├── database.py          # старый SQLite backend
├── backfill_daily.py    # SQLite-зависимый onboarding
├── iiko_stoplist.py     # отключённый job
└── task_tracker.py      # отключённый Битрикс24 job
```

---

## Чеклист перед деплоем

- [ ] `docker compose build --no-cache && docker compose up -d`
- [ ] Проверить `/health` — должен вернуть `{"status": "ok", "database": "ok"}`
- [ ] `/статус` для каждого тенанта — видны только его города
- [ ] Дождаться утреннего отчёта — пришёл ли для всех тенантов
- [ ] Проверить логи: `docker compose logs -f --tail=100 | grep -E "ERROR|WARNING"`
- [ ] Проверить что `job_late_alerts` работает (опоздания трекаются)
- [ ] Проверить `job_cancel_sync` (отмены синхронизируются)
- [ ] Проверить OLAP enrichment (логи `olap_enrichment`, `updated > 0`)

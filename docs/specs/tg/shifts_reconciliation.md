# ТЗ: Пересверка смен — ежедневная и еженедельная

**Кому:** @интегратор  
**Приоритет:** высокий  
**Статус:** готово к реализации

---

## Проблема

`shifts_raw` заполняется из Events API в реальном времени. Если управляющий вручную правит смены в iiko после факта (например, после аварии с интернетом), Events API эти правки не отдаёт. Результат — `fot_daily` считается по некорректным данным, ФОТ в отчётах завышен или занижен.

**Пример:** 11 марта 2026, Томск_1 Яко — авария с интернетом в 22:30, кассовая смена не закрылась. Управляющий исправил смены вручную утром следующего дня. В БД осталось 80.8ч на 4 поваров вместо реальных ~35ч. ФОТ завышен вдвое.

---

## Решение

Два регулярных джоба которые перезаписывают `shifts_raw` из `schedule API` (`/api/v2/employees/schedule`) и пересчитывают `fot_daily`.

`schedule API` всегда отдаёт актуальные (уже исправленные) данные. `ShiftsBackfiller` (`app/onboarding/backfill_shifts_generic.py`) уже умеет это делать — нужно только завернуть в регулярный джоб.

---

## Джоб 1 — Ежедневная пересверка за вчера

**Расписание:** каждый день в **04:00 МСК** (01:00 UTC)  
**Запускается строго до fot_pipeline (04:30 МСК)**

### Логика

```python
from app.utils.timezone import tz_from_offset

tz = tz_from_offset(7)  # все тенанты UTC+7
yesterday = (datetime.now(tz) - timedelta(days=1)).date()

for tenant in await get_active_tenants_with_tokens():
    await ShiftsBackfiller(tenant_id=tenant["id"], date_from=yesterday, date_to=yesterday).run()
    await run_fot_pipeline(target_date=yesterday, tenant_id=tenant["id"], notify=False)
```

### Детали
- Период: только вчера (1 день)
- Перезаписывает `shifts_raw` через `upsert_shifts_batch` (ON CONFLICT UPDATE) — безопасно, не удаляет
- После перезаписи вызывает `run_fot_pipeline` за тот же день, чтобы пересчитать `fot_daily`
- Все тенанты последовательно

---

## Джоб 2 — Еженедельная пересверка за прошлую неделю

**Расписание:** каждый **понедельник в 08:00 МСК** (05:00 UTC)  
**Запускается строго до weekly_report (08:30 МСК)**

### Логика

```python
from app.utils.timezone import tz_from_offset

tz = tz_from_offset(7)
today = datetime.now(tz).date()
week_start = today - timedelta(days=7)
week_end   = today - timedelta(days=1)

for tenant in await get_active_tenants_with_tokens():
    await ShiftsBackfiller(tenant_id=tenant["id"], date_from=week_start, date_to=week_end).run()
    for i in range(7):
        day = week_start + timedelta(days=i)
        await run_fot_pipeline(target_date=day, tenant_id=tenant["id"], notify=False)
```

### Детали
- Период: 7 дней (Пн–Вс прошлой недели)
- Тот же механизм upsert — перезаписывает только изменившиеся данные
- Пересчитывает fot_daily за каждый день недели (7 вызовов run_fot_pipeline)

---

## Изменение расписания fot_pipeline

**Текущее расписание:** 04:00 МСК  
**Новое расписание:** 04:30 МСК

Причина: джоб 1 (пересверка за вчера) запускается в 04:00 МСК и должен завершиться до старта fot_pipeline. Пересверка занимает ~1–3 мин на тенант.

---

## Изменение расписания weekly_report

**Текущее расписание:** понедельник 06:00 МСК  
**Новое расписание:** понедельник 08:30 МСК

Причина: джоб 2 (пересверка за неделю) запускается в 08:00 МСК и должен завершиться до weekly_report.

---

## Итоговое расписание (все джобы утреннего блока)

| Время МСК | Джоб |
|-----------|------|
| 03:35 | Пересчёт hourly_stats за вчера |
| 04:00 | **[новый]** shifts_reconciliation_daily — пересверка за вчера |
| 04:30 | fot_pipeline (сдвинуть с 04:00) |
| 05:25 | Утренний отчёт |
| Пн 08:00 | **[новый]** shifts_reconciliation_weekly — пересверка за неделю |
| Пн 08:30 | Еженедельный отчёт (сдвинуть с 06:00) |

---

## Реализация

### Новый файл: `app/jobs/shifts_reconciliation.py`

```python
"""
shifts_reconciliation.py — пересверка смен из schedule API.

Два джоба:
  job_shifts_reconciliation_daily()   — каждый день в 04:00 МСК
  job_shifts_reconciliation_weekly()  — каждый понедельник в 08:00 МСК

Источник: GET /api/v2/employees/schedule (ShiftsBackfiller)
После перезаписи shifts_raw → пересчитывает fot_daily через run_fot_pipeline.
"""
from datetime import datetime, timedelta
from app.onboarding.backfill_shifts_generic import ShiftsBackfiller
from app.jobs.fot_pipeline import run_fot_pipeline
from app.database_pg import get_active_tenants_with_tokens
from app.utils.timezone import tz_from_offset
from app.utils.job_tracker import track_job

@track_job("shifts_reconciliation_daily")
async def job_shifts_reconciliation_daily():
    """Пересверка за вчера для всех тенантов. Запуск: 04:00 МСК."""
    tz = tz_from_offset(7)
    yesterday = (datetime.now(tz) - timedelta(days=1)).date()
    for tenant in await get_active_tenants_with_tokens():
        await ShiftsBackfiller(tenant_id=tenant["id"], date_from=yesterday, date_to=yesterday).run()
        await run_fot_pipeline(target_date=yesterday, tenant_id=tenant["id"], notify=False)

@track_job("shifts_reconciliation_weekly")
async def job_shifts_reconciliation_weekly():
    """Пересверка за прошлую неделю. Запуск: Пн 08:00 МСК."""
    tz = tz_from_offset(7)
    today = datetime.now(tz).date()
    week_start = today - timedelta(days=7)
    week_end   = today - timedelta(days=1)
    for tenant in await get_active_tenants_with_tokens():
        await ShiftsBackfiller(tenant_id=tenant["id"], date_from=week_start, date_to=week_end).run()
        for i in range(7):
            day = week_start + timedelta(days=i)
            await run_fot_pipeline(target_date=day, tenant_id=tenant["id"], notify=False)
```

### Изменения в `app/main.py`

Scheduler настроен на `AsyncIOScheduler(timezone="Europe/Moscow")` — писать МСК напрямую, без пересчёта в UTC.

1. Импортировать `job_shifts_reconciliation_daily`, `job_shifts_reconciliation_weekly`
2. Добавить крон `shifts_reconciliation_daily` — `CronTrigger(hour=4, minute=0)`
3. Добавить крон `shifts_reconciliation_weekly` — `CronTrigger(day_of_week='mon', hour=8, minute=0)`
4. Сдвинуть `fot_pipeline` — `CronTrigger(hour=4, minute=30)` (было `hour=4, minute=0`)
5. Сдвинуть `weekly_report` — `CronTrigger(day_of_week='mon', hour=8, minute=30)` (было `hour=6, minute=0`)

---

## Проверка (как тестировать)

1. Вручную запустить `job_shifts_reconciliation_daily()` через `/api/run-job` или docker exec
2. Проверить что `shifts_raw` обновился для нужных точек (смотреть `updated_at`)
3. Проверить что `fot_daily` пересчитался (суммы изменились)
4. Убедиться что fot_pipeline в 04:30 не конфликтует (дублированный запуск не ломает данные — upsert идемпотентен)

---

## Зависимости

- `app/onboarding/backfill_shifts_generic.py` — уже существует, использовать без изменений
- `app/jobs/fot_pipeline.py` → функция `run_fot_pipeline(target_date, tenant_id)` — уже существует
- `app/database_pg.py` → `upsert_shifts_batch` — уже существует

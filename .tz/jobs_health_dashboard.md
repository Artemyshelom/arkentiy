# ТЗ: Jobs Health Dashboard

> Мониторинг scheduled jobs: автоалерты при падении + команда `/jobs` для ручной проверки.

---

## Проблема

~10 scheduled jobs (авторепорты, OLAP → Sheets, мониторинг конкурентов, биллинг и т.д.). Когда job падает — узнаём случайно или из логов. Нужен единый dashboard.

---

## Решение

### 1. Автоалерты при падении (главное)

**Триггер:** любой job завершился с ошибкой (exception)

**Формат алерта:**
```
❌ Job упал: [название]
⏰ 12:34 MSK
💥 [краткая причина / первая строка traceback]
```

**Получатель:** Артемий (ADMIN_CHAT_ID)

**Правила:**
- Алерт отправляется сразу при падении
- Не спамить: один алерт на одно падение
- Если job падает несколько раз подряд — алерт каждый раз (важно видеть паттерн)

---

### 2. Команда `/jobs`

**Доступ:** только admin

**Формат ответа:**
```
📊 Scheduled Jobs

✅ Утренний отчёт
   09:25 · 2.3 сек · → завтра 09:25

✅ OLAP → Sheets  
   09:26 · 45 сек · → завтра 09:26

❌ Мониторинг конкурентов
   вчера 12:00 · ОШИБКА
   → Timeout при парсинге
   → след. запуск: пн 12:00

⏳ Алерты опозданий
   сейчас · работает 2 мин
   → каждые 2 мин

🔘 Биллинг (отключён)
   последний: никогда
```

**Поля:**
- Статус: ✅ ок / ❌ ошибка / ⏳ выполняется / 🔘 отключён
- Последний запуск (когда, сколько длился)
- Если ошибка — краткое описание
- Следующий запуск

---

## Реализация (@интегратор)

### Таблица `job_runs`

```sql
CREATE TABLE job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,           -- уникальный id job'а (например 'daily_report')
    job_name TEXT NOT NULL,         -- человеческое название ('Утренний отчёт')
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,          -- NULL если ещё выполняется
    status TEXT NOT NULL,           -- 'running' / 'success' / 'error'
    duration_sec REAL,              -- время выполнения в секундах
    error_message TEXT,             -- если status='error'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_job_runs_job_id ON job_runs(job_id);
CREATE INDEX idx_job_runs_started_at ON job_runs(started_at DESC);
```

### Декоратор `@track_job`

```python
# app/core/job_tracker.py

from functools import wraps
from datetime import datetime
from app.database import save_job_run, update_job_run
from app.clients.telegram import error_alert

def track_job(job_id: str, job_name: str):
    """
    Декоратор для отслеживания выполнения job'ов.
    Логирует запуск, завершение, ошибки.
    При ошибке — отправляет алерт в Telegram.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            run_id = await save_job_run(
                job_id=job_id,
                job_name=job_name,
                started_at=datetime.now(),
                status='running'
            )
            try:
                result = await func(*args, **kwargs)
                await update_job_run(
                    run_id=run_id,
                    status='success',
                    finished_at=datetime.now()
                )
                return result
            except Exception as e:
                await update_job_run(
                    run_id=run_id,
                    status='error',
                    finished_at=datetime.now(),
                    error_message=str(e)[:500]
                )
                # Отправляем алерт
                await error_alert(
                    f"❌ Job упал: {job_name}\n"
                    f"⏰ {datetime.now().strftime('%H:%M')} MSK\n"
                    f"💥 {str(e)[:200]}"
                )
                raise
        return wrapper
    return decorator
```

### Применение к существующим jobs

```python
# app/jobs/daily_report.py

from app.core.job_tracker import track_job

@track_job('daily_report', 'Утренний отчёт')
async def send_daily_report():
    # существующий код
    ...
```

**Список jobs для трекинга:**

| job_id | job_name | Файл |
|--------|----------|------|
| `daily_report` | Утренний отчёт | daily_report.py |
| `olap_to_sheets` | OLAP → Sheets | iiko_to_sheets.py |
| `audit` | Аудит операций | audit.py |
| `competitor_monitor` | Мониторинг конкурентов | competitor_monitor.py |
| `late_alerts` | Алерты опозданий | late_alerts.py |
| `cancel_sync` | Синхронизация отмен | cancel_sync.py |
| `tbank_reconciliation` | Сверка ТБанк | tbank_reconciliation.py |
| `billing` | Биллинг SaaS | billing.py |
| `olap_enrichment` | Обогащение OLAP | olap_enrichment.py |

### Реестр jobs для `/jobs`

```python
# app/core/job_registry.py

JOB_REGISTRY = {
    'daily_report': {
        'name': 'Утренний отчёт',
        'schedule': '09:25 MSK ежедневно',
        'next_run': lambda: get_next_run('daily_report'),  # из APScheduler
    },
    'olap_to_sheets': {
        'name': 'OLAP → Sheets',
        'schedule': '09:26 MSK ежедневно',
        'next_run': lambda: get_next_run('olap_to_sheets'),
    },
    # ... остальные
}
```

---

## Реализация (@ux-бот)

### Handler `/jobs`

```python
# В arkentiy.py

@dp.message(Command("jobs"))
@admin_only
async def cmd_jobs(message: Message):
    """Показать статус всех scheduled jobs."""
    jobs_status = await get_all_jobs_status()
    
    lines = ["📊 Scheduled Jobs\n"]
    
    for job in jobs_status:
        if job['status'] == 'running':
            emoji = "⏳"
            status_line = f"сейчас · работает {job['duration']}"
        elif job['status'] == 'success':
            emoji = "✅"
            status_line = f"{job['last_run']} · {job['duration']} сек"
        elif job['status'] == 'error':
            emoji = "❌"
            status_line = f"{job['last_run']} · ОШИБКА\n   → {job['error'][:50]}"
        elif job['status'] == 'disabled':
            emoji = "🔘"
            status_line = "отключён"
        else:
            emoji = "❓"
            status_line = "нет данных"
        
        lines.append(f"{emoji} {job['name']}")
        lines.append(f"   {status_line}")
        if job.get('next_run'):
            lines.append(f"   → {job['next_run']}")
        lines.append("")
    
    await message.answer("\n".join(lines))
```

---

## Миграция

1. Создать таблицу `job_runs`
2. Создать `app/core/job_tracker.py` с декоратором
3. Создать `app/core/job_registry.py` с реестром
4. Обернуть все jobs в `@track_job`
5. Добавить handler `/jobs` в arkentiy.py
6. Добавить `/jobs` в модуль `admin` в access_architecture

---

## Тесты

- [ ] Job успешно завершился → запись в `job_runs` со status='success'
- [ ] Job упал → запись со status='error' + алерт в Telegram
- [ ] `/jobs` показывает все jobs с актуальными статусами
- [ ] Долгий job показывает статус 'running' пока выполняется
- [ ] Отключённый job показывается как 🔘

---

*Создано: 2026-03-06*

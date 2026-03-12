# ТЗ: Real-time ФОТ поваров в /статус

**Кому:** @интегратор  
**Приоритет:** средний  
**Статус:** готово к реализации

---

## Задача

При запросе `/статус` показывать накопленный ФОТ поваров прямо сейчас: открытые смены × ставка × часы с начала смены до момента запроса.

---

## Как выглядит результат

```
📍 Томск_1 Яко — 19:45
💰 Выручка: 87 340 ₽
🧾 Чеков: 52 | Средний чек: 1 680 ₽

💸 Скидки: 3 200 ₽ | Оплата бонусами: 1 100 ₽
📦 Себестоимость: 34.2%

✅ Кассовая смена открыта

✅ Опозданий: 0 из 34 доставок
🚚 Заказы: 3 активных | доставлено: 34
   Новые:      0  (—)
   Готовятся:  1  (среднее: 18 мин)
   Готовы:     1  (ждут: 4 мин)
   В пути:     1  (среднее: 22 мин)
👥 На смене: поваров: 3, курьеров: 2
💼 ФОТ поваров: ~8 400 ₽ (3 чел · 4.2ч)
```

Строка добавляется сразу после `👥 На смене`. Тильда `~` показывает что это расчётное значение, не закрытое.

Если поваров на смене нет — строку не показывать.

---

## Архитектура

### Шаг 1 — Новая таблица `employee_rates_cache`

```sql
CREATE TABLE IF NOT EXISTS employee_rates_cache (
    tenant_id     INTEGER NOT NULL DEFAULT 1,
    branch_name   TEXT NOT NULL,
    employee_id   TEXT NOT NULL,
    employee_name TEXT NOT NULL,
    rate_per_hour NUMERIC(10,2) NOT NULL DEFAULT 0,
    cached_at     TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (tenant_id, branch_name, employee_id)
);

CREATE INDEX IF NOT EXISTS idx_rates_cache_branch
    ON employee_rates_cache (tenant_id, branch_name);
```

### Шаг 2 — Джоб обновления кеша ставок

**Расписание:** каждый день в **03:30 МСК** (до fot_pipeline в 04:30)

**Логика:**
```python
for tenant in await get_active_tenants_with_tokens():
    for branch in branches_of_tenant:
        token = await get_bo_token(branch.bo_url, ...)
        salary_map = await fetch_salary_map(branch.bo_url, client, token, date.today())
        # salary_map: {employee_id: rate_per_hour}
        await upsert_rates_cache(tenant_id, branch.name, salary_map)
```

Файл: `app/jobs/rates_cache_updater.py`  
Функция в `database_pg.py`: `upsert_rates_cache(tenant_id, branch_name, rates: dict[str, Decimal])`

### Шаг 3 — Функция расчёта real-time ФОТ

Новая функция в `database_pg.py`:

```python
async def get_realtime_fot(branch_name: str, tenant_id: int = 1) -> dict | None:
    """
    Считает накопленный ФОТ поваров по открытым сменам прямо сейчас.
    Возвращает {'fot': float, 'hours': float, 'cooks': int} или None.
    
    Открытая смена = clock_out IS NULL в shifts_raw.
    Часы = (NOW() AT TIME ZONE 'Asia/Krasnoyarsk') - clock_in.
    Ставка берётся из employee_rates_cache, fallback — 0 (не считается).
    """
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT
            s.employee_id,
            EXTRACT(EPOCH FROM (
                NOW() AT TIME ZONE 'Asia/Krasnoyarsk' - 
                s.clock_in::timestamptz AT TIME ZONE 'Asia/Krasnoyarsk'
            )) / 3600.0 AS hours_now,
            COALESCE(r.rate_per_hour, 0) AS rate
        FROM shifts_raw s
        LEFT JOIN employee_rates_cache r
            ON r.tenant_id = s.tenant_id
           AND r.branch_name = s.branch_name
           AND r.employee_id = s.employee_id
        WHERE s.tenant_id = $1
          AND s.branch_name = $2
          AND s.role_class = 'cook'
          AND s.clock_out IS NULL
          AND s.clock_in IS NOT NULL
    """, tenant_id, branch_name)
    
    if not rows:
        return None
    
    total_fot = sum(float(r['hours_now']) * float(r['rate']) for r in rows if r['rate'] > 0)
    total_hours = sum(float(r['hours_now']) for r in rows)
    cooks = len(rows)
    avg_hours = round(total_hours / cooks, 1) if cooks else 0
    
    return {
        'fot': round(total_fot),
        'hours': avg_hours,
        'cooks': cooks,
    }
```

### Шаг 4 — Встройка в `/статус`

Файл: `app/jobs/iiko_status_report.py`, функция `format_branch_status` (блок после `👥 На смене`).

```python
# После блока 👥 На смене (строки ~281-285):
if not db_fallback and (cooks is not None and cooks > 0):
    try:
        rt_fot = await get_realtime_fot(name, tenant_id)
        if rt_fot and rt_fot['fot'] > 0:
            fot_str = f"{rt_fot['fot']:,} ₽".replace(",", " ")
            lines.append(
                f"💼 ФОТ поваров: ~{fot_str} "
                f"({rt_fot['cooks']} чел · {rt_fot['hours']}ч)"
            )
    except Exception as _e:
        logger.debug(f"realtime_fot [{name}]: {_e}")
```

Всё в try/except — не ломает /статус при любой ошибке.

---

## Изменения в `app/main.py`

1. Импортировать `job_rates_cache_updater`
2. Добавить крон: `CronTrigger(hour=3, minute=30)` — ежедневно

---

## Миграция

Новый файл: `app/migrations/014_employee_rates_cache.sql`

---

## Зависимости

- `app/clients/iiko_schedule.py` → `fetch_salary_map` — **уже существует**
- `app/clients/iiko_auth.py` → `get_bo_token` — **уже существует**
- `app/database_pg.py` → добавить `upsert_rates_cache`, `get_realtime_fot`
- `app/jobs/iiko_status_report.py` → `format_branch_status` — добавить вызов
- Новый джоб: `app/jobs/rates_cache_updater.py`
- Новая миграция: `app/migrations/014_employee_rates_cache.sql`

---

## Итоговое расписание (дополнение к shifts_reconciliation)

| Время МСК | Джоб |
|-----------|------|
| 03:30 | **[новый]** rates_cache_updater — обновление ставок |
| 04:00 | shifts_reconciliation_daily |
| 04:30 | fot_pipeline |
| 05:25 | Утренний отчёт |

---

## Проверка

1. Запустить `job_rates_cache_updater()` вручную → проверить `SELECT * FROM employee_rates_cache LIMIT 20`
2. Вызвать `/статус Томск` → убедиться что строка `💼 ФОТ поваров` появилась
3. Если смен нет (ночь) — строка не должна отображаться
4. Проверить что `/статус` не стал медленнее (запрос к `employee_rates_cache` должен работать <10мс по индексу)

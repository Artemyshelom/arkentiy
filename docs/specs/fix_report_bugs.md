# fix/report-bugs — трекинг 09.03.2026

Все 6 багов подтверждены. Нули = баги везде.  
Статусы: ⬜ не начато · 🔄 в работе · ✅ готово

---

## ТЛ;ДР
Сессия 74 ввела единый OLAP-пайплайн: отчёты читают из `daily_stats` (05:00),
не живой OLAP. После этого 6 вещей сломалось.

---

## Фаза 1 — Код (подтверждённые баги, без диагностики)

### Баг 3 — Скидки: суммы заказов вместо сумм скидок ✅

`SUM(sum)` → `SUM(COALESCE(discount_sum, 0))` в трёх местах `database_pg.py`:

| # | Функция | Строка | Статус |
|---|---------|--------|--------|
| 3а | `get_live_today_stats()` | ~1066 | ✅ |
| 3б | `aggregate_orders_for_daily_stats()` | ~1186 | ✅ |
| 3в | `get_period_stats()` | ~1342 | ✅ |

---

### Баг 4 — Нет времени приготовления ✅

`get_period_stats()`, вес для `avg_cooking_min` изменён с `orders_count` → `total_delivered`.

Файл: `app/database_pg.py:~1306–1307` — Статус: ✅

---

### Баг 5 — Нет новых/повторных клиентов ✅

`_format_branch_report` читает клиентов из `agg`, а данные лежат в `stats`/`ds`.

| # | Файл | Место | Статус |
|---|------|-------|--------|
| 5а | `arkentiy.py` | `_build_branch_report` блок period (~1338) | ✅ |
| 5б | `arkentiy.py` | `_build_city_aggregate` (~1430) | ✅ |
| 5в | `weekly_report.py` | agg dict — поля уже были | ✅ уже было |

Поля: `new_customers`, `new_customers_revenue`, `repeat_customers`, `repeat_customers_revenue`

---

## Фаза 2 — Баги 1 и 2 ✅

### Баг 1 — Чеков: 0 / средний чек пустой ✅

**Root cause:** `row.get("UniqOrderId.OrdersCount", 0)` мог возвращать None или 0.
Документация подтверждает правильный ключ — добавлен явный `int()` для надёжности.

**Основной фикс (orders_raw fallback):**
В `aggregate_orders_for_daily_stats()` добавлены `COUNT(*) AS raw_orders_count` и `raw_sailplay`.
В `_upsert_daily_stats_from_aggregate()` — fallback: `chk = int(stats.get("check_count") or agg.get("raw_orders_count") or 0)`.
Если pipeline чеки не получит от OLAP — возьмёт из `orders_raw` напрямую.

**Дополнительный фикс (defensive):**
`int(row.get("UniqOrderId.OrdersCount") or 0)` в `olap_queries.py` и `iiko_bo_olap_v2.py`.

Файлы: `app/database_pg.py`, `app/jobs/olap_pipeline.py`, `app/clients/olap_queries.py`, `app/clients/iiko_bo_olap_v2.py`
Статус: ✅

---

### Баг 2 — Оплата бонусами: — ✅

**Root cause:** OLAP Q2 может не вернуть SailPlay (timeout/ошибка), записывая 0.

**Фикс (orders_raw fallback):**
В `aggregate_orders_for_daily_stats()` добавлено:
```sql
COALESCE(SUM(CASE
    WHEN pay_breakdown LIKE '%SailPlay%'
    THEN (pay_breakdown::jsonb->>'SailPlay Бонус')::numeric
END), 0) AS raw_sailplay
```
В pipeline Step C: `sailplay_val = float(stats.get("sailplay") or agg.get("raw_sailplay") or 0.0)`.

Файл: `app/database_pg.py`, `app/jobs/olap_pipeline.py`
Статус: ✅

---

## Фаза 3 — Расписание ✅

### Баг 6 — Еженедельный отчёт не пришёл ✅

> **Уточнение:** Реальной race condition нет (pipeline 01:00 МСК, weekly 05:30 МСК — 4.5 ч разница).
> Причина: отчёт пришёл с нулевыми данными из-за багов 1–5. Но schedule улучшен.

Изменено: `CronTrigger(day_of_week="mon", hour=6, minute=0)` = 06:00 МСК = 10:00 UTC+7  
(строго после утреннего в 09:25, удобный зазор).  
Формула: `target_offset = 13 - msk_now.hour` (13 - 6 = 7 ✓).

Файл: `app/main.py` — Статус: ✅

---

## Фаза 4 — Backfill данных ⬜

### Перезаполнить daily_stats за 02.03–08.03

После деплоя — ручной прогон (VPS, в screen):
```bash
docker exec -it arkentiy_app python3 -c "
import asyncio
from app.database_pg import init_db, close_db
from app.jobs.olap_pipeline import job_olap_pipeline
async def run():
    await init_db()
    await job_olap_pipeline(tenant_id=1)
    await job_olap_pipeline(tenant_id=3)
    await close_db()
asyncio.run(run())
"
```

Статус: ⬜ (ждёт деплоя)

---

## /статус — оценка влияния

| Данные | Затронут? | Путь |
|--------|-----------|------|
| Выручка, COGS | ✅ OK | live OLAP iiko_bo_olap_v2 |
| Чеки | ✅ Фикс applied | `iiko_bo_olap_v2.py` defensive int() |
| Скидки (суммы) | ✅ OK | OLAP Q3 DELIVERIES напрямую |
| SailPlay | ✅ Фикс applied | `iiko_bo_olap_v2.py` defensive |
| Тайминги | ✅ OK | Events API realtime |
| Клиенты | N/A | не отображаются в /статус |

---

## Файлы с изменениями

| Файл | Баги |
|------|------|
| `app/database_pg.py` | 3а/б/в + 4 + 1/2 (raw fallback в aggregate_orders_for_daily_stats) |
| `app/jobs/olap_pipeline.py` | 1/2 (chk+sailplay fallback в Step C) |
| `app/jobs/arkentiy.py` | 5а, 5б |
| `app/clients/olap_queries.py` | 1 (defensive) |
| `app/clients/iiko_bo_olap_v2.py` | 1 (defensive, для /статус) |
| `app/main.py` | 6 (scheduler) |

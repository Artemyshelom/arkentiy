# OLAP-пайплайн — Архитектура

> Читать перед работой с OLAP-запросами или daily_stats.

---

## Принцип

Один ночной пайплайн (`job_olap_pipeline`) делает **все** OLAP-запросы за предыдущий день.
Остальные джобы читают только из БД — никаких прямых вызовов iiko в `daily_report`, `iiko_to_sheets`.

**Запуск:** 05:00 по местному времени каждой точки (APScheduler, CronTrigger, каждый час вычисляет offset).

**В понедельник** диапазон расширяется до 7 дней — перезаписывает воскресные корректировки iiko.

---

## 4 канонических запроса (`app/clients/olap_queries.py`)

| # | Функция | reportType | groupByRowFields | Назначение |
|---|---------|-----------|-----------------|-----------|
| **A** | `fetch_order_detail` | DELIVERIES | 16 полей (Delivery.*) | Метаданные заказа: тайминги, оплата, скидка, статус |
| **B** | `fetch_dish_detail` | SALES | Delivery.Number, Department, DishName, WaiterName | Состав заказа + курьер |
| **C** | `fetch_branch_aggregate` | SALES | Department (+ PayTypes + ServiceType в sub-запросах) | Агрегат по точке: выручка, COGS, чеки, нал/безнал, самовывоз |
| **D** | `fetch_storno_audit` | SALES | OrderNum, Storned, CashierName | Только для audit.py, on-demand |

### Правила использования

- **Query A** возвращает одну строку на (order × pay_type × discount_type) → агрегировать у потребителя.
- **Query B** возвращает одну строку на (order × dish). `WaiterName` = курьер (только для бэкфила, текущие данные — Events API).
- **Query C** делает 3 параллельных sub-запроса на сервер. Возвращает уже структурированный `dict[dept, dict]` (в отличие от A/B/D которые возвращают сырые строки).
- **Query D** — единственный запрос где нужен `Storned`/`CashierName`. Не консолидировать с A/B.

### ⚠️ Задокументированный баг iiko

**НИКОГДА не включай `OpenDate.Typed` в `groupByRowFields`** — это обнуляет поле `Delivery.Number` в DELIVERIES-запросах. Фильтрация по дате только через `filters`.

---

## Шаги пайплайна (`app/jobs/olap_pipeline.py`)

```
Step A: fetch_order_detail → _aggregate_order_rows() → _upsert_order_data()
         DELIVERIES за день → группировка по (branch, delivery_num)
         → orders_raw (тайминги force-overwrite, остальное COALESCE)

Step B: fetch_dish_detail → _aggregate_dish_rows() → _upsert_dish_data()
         SALES dishes за день → items JSON + courier
         → orders_raw (COALESCE — не перезаписываем если уже есть)

Step C: fetch_branch_aggregate → aggregate_orders_for_daily_stats() → upsert_daily_stats_batch()
         Query C за день + расчёт timing-статистики из orders_raw
         → daily_stats (все 30 полей включая cash/noncash/exact_time_count)
```

### Тайминги в Step A — force-overwrite

Поля `opened_at`, `cooked_time`, `send_time`, `service_print_time` ВСЕГДА перезаписываются из OLAP — OLAP точнее Events API для исторических данных.

---

## daily_stats — кто что пишет

| Поле | Пайплайн Step C | late_alerts | Другие |
|------|----------------|-------------|--------|
| revenue, orders_count, avg_check | ✅ | — | — |
| cogs_pct, discount_sum, pickup_count | ✅ | — | — |
| cash, noncash | ✅ | — | — |
| avg_cooking_min, avg_wait_min, avg_delivery_min | ✅ | — | — |
| late_delivery_count, late_pickup_count | ✅ | — | — |
| exact_time_count | ✅ | — | — |

Если пайплайн ещё не запустился (утро, до 05:00) — `get_daily_stats()` вернёт данные предыдущего дня или `None`.

---

## Как добавить новое поле в daily_stats

1. Добавь колонку в миграцию (`app/migrations/0XX_*.sql`)
2. Добавь параметр в `upsert_daily_stats_batch` (`app/database_pg.py`)
3. Вычисли и передай в `job_olap_pipeline` Step C (`app/jobs/olap_pipeline.py`)
4. Обнови `backfill_daily_stats_generic.py` если нужна историческая заливка

---

## On-demand запросы (не пайплайн)

| Команда/момент | Источник данных |
|---------------|----------------|
| `/статус` | `iiko_status_report.py` → `fetch_branch_aggregate` (Query C, today) |
| `/аудит` | `audit.py` → `fetch_storno_audit` (Query D, on-demand) |
| Сверка эквайринга | `tbank_reconciliation.py` → собственные запросы (не через olap_queries.py) |

---

## Устаревшие файлы (не удалять — обратная совместимость)

| Файл | Статус | Кто ещё использует |
|------|--------|-------------------|
| `app/jobs/cancel_sync.py` | DEPRECATED, scheduler закомментирован | — |
| `app/jobs/olap_enrichment.py` | DEPRECATED, не регистрируется | — |
| `app/clients/iiko_bo_olap_v2.py` | Активный, но legacy | `iiko_status_report.py`, `tbank_reconciliation.py` |

Когда `iiko_status_report.py` будет переведён на `fetch_branch_aggregate` — `iiko_bo_olap_v2.py` можно депрекировать.

---

## Бэкфил

| Скрипт | Что делает | Фазы |
|--------|-----------|------|
| `backfill_orders_generic.py` | Заполняет orders_raw | Phase 1: DELIVERIES (нед. чанки) → Phase 2: SALES dishes (нед. чанки) |
| `backfill_daily_stats_generic.py` | Заполняет daily_stats | Per-day, через `fetch_branch_aggregate` |

Прогресс бэкфила сохраняется в файл (`/tmp/<hash>.json`) — можно возобновить после падения.

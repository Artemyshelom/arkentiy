# PROJECT UPDATE STATUS

> Снапшот состояния проекта для быстрого входа в контекст.  
> Обновляется в конце каждой сессии работы.

---

## 📍 Где остановились — Сессия 76 (9 марта 2026)

**Последнее действие:** Бэкфилл T1 (100% с 01.12.2025) — все скрипты запущены/завершены. `bf_hourly` (step 5) ещё выполняется (~99 дней × 9 точек).

**Рефактор orchestrator:** `backfill_new_client.py` теперь 5 шагов (добавлен shifts как step 4, hourly → step 5).

**Все 5 багов в бэкфилл-скриптах устранены**, архитектура зафиксирована.

---

## ✅ Что сделано в сессии 76

| # | Что | Статус |
|---|-----|--------|
| 1 | fix: SyntaxError в backfill_orders_generic (481 мёртвых строк) | ✅ коммит `7f85114` |
| 2 | fix: auth через get_bo_token с env-fallback в backfill_orders_generic | ✅ коммит `07ce3b7` |
| 3 | fix: date::text вместо date=$3::date в backfill_new_client step 3 | ✅ коммит `07ce3b7` |
| 4 | fix: init_db/close_db для HourlyStatsBackfiller в step 4 (hourly) | ✅ коммит `80f3559` |
| 5 | feat: backfill_shifts_generic.py — исторические смены из /api/v2/employees/schedule | ✅ коммит `f61357f` |
| 6 | fix: datetime.date вместо str для shift_date в backfill_shifts_generic | ✅ коммит `8ef6d3c` |
| 7 | refactor: shifts как step 4 в orchestrator, hourly → step 5 | ✅ коммит `(текущий)` |
| 8 | bf_daily: 277 строк cash/noncash заполнено в daily_stats | ✅ завершён |
| 9 | bf_orders: 14 531 заказ phase1 обработан | ✅ завершён |
| 10 | bf_step3: 879 записей new_customers обновлено | ✅ завершён |
| 11 | bf_shifts: 4 024 смены Dec 2025 – Feb 21 2026 загружены | ✅ завершён |
| 12 | bf_hourly (step 5): почасовая аналитика пересчитывается | ⏳ запущен |

---

## 🔴 Активные проблемы / риски

- **`bf_hourly` (step 5) ещё выполняется** на сервере в screen-сессии `bf_hourly` (~1 час). Можно проверить: `ssh arkentiy 'tail -5 /tmp/bf_hourly.log'`
- **`boris_api_regression_audit`** — не проверена изоляция `_states` по tenant_id в `/api/stats?metric=realtime`. Данные Шабурова потенциально видны в ответах Бориса.

---

## 📋 Приоритеты на следующую сессию

1. Проверить завершение `bf_hourly` + финальная валидация данных T1
2. `boris_api_regression_audit` — проверить изоляцию tenant_id в `/api/stats`, `_states`
3. `orders_status_optimization` — анализ OLAP vs Events для активных заказов

---

## 🏗 Текущее состояние системы

| Компонент | Статус |
|-----------|--------|
| Docker контейнер | ✅ Running, healthy |
| iiko Events polling | ✅ каждые 30с, 9 точек (Артемий + Шабуров) |
| Telegram бот | ✅ работает |
| APScheduler | ✅ 14 jobs зарегистрировано |
| Бэкфилл T1 (shifts) | ✅ 4 024 смены Dec 2025 – Feb 21 |
| Бэкфилл T1 (hourly) | ⏳ выполняется в screen bf_hourly |

---

## 📌 Ключевые файлы сессии

- [app/onboarding/backfill_new_client.py](app/onboarding/backfill_new_client.py) — 5 шагов, shifts=4, hourly=5
- [app/onboarding/backfill_shifts_generic.py](app/onboarding/backfill_shifts_generic.py) — новый скрипт
- [app/onboarding/backfill_orders_generic.py](app/onboarding/backfill_orders_generic.py) — bugfix auth + SyntaxError
- [app/onboarding/README.md](app/onboarding/README.md) — обновлена таблица (5 скриптов + 5 шагов)
- [docs/journal.md](docs/journal.md) — сессия 76

---

## 🗺 Архитектура backfill-скриптов (актуально)

```
backfill_new_client.py (мастер-оркестратор, 5 шагов)
├── step 1 → backfill_orders_generic.OrdersBackfiller      (iiko OLAP → orders_raw)
├── step 2 → backfill_daily_stats_generic.DailyStatsBackfiller (iiko OLAP → daily_stats)
├── step 3 → inline в new_client                            (orders_raw → daily_stats timing)
├── step 4 → backfill_shifts_generic.ShiftsBackfiller       (iiko schedule API → shifts_raw)
└── step 5 → backfill_hourly_stats.HourlyStatsBackfiller    (orders_raw + shifts_raw → hourly_stats)

Все 4 компонента можно запускать и отдельно (standalone).
```

---

*Обновлено: 9 марта 2026, сессия 76*

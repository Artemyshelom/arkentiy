# PROJECT UPDATE STATUS

> Снапшот состояния проекта для быстрого входа в контекст.  
> Обновляется в конце каждой сессии работы.

---

## 📍 Где остановились — Сессия 78 (10 марта 2026)

**Последнее действие:** Добавлены выписки Томска (ИП Сергеев). Рефактор `bank_statement.py` — мультитенант, поддержка Точка банка. Сверка эквайринга для Томска работает.

**bank_accounts.json** переведён на per-tenant структуру: новый тенант = новый ключ в JSON, код менять не нужно.

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

- **`boris_api_regression_audit`** — не проверена изоляция `_states` по tenant_id в `/api/stats?metric=realtime`. Данные Шабурова потенциально видны в ответах Бориса.

---

## 📋 Приоритеты на следующую сессию

1. `boris_api_regression_audit` — проверить изоляцию tenant_id в `/api/stats`, `_states`
2. `orders_status_optimization` — анализ OLAP vs Events для активных заказов
3. `email_statement_auto` — автозабор выписок с почты (актуально для Томска)

---

## 🏗 Текущее состояние системы

| Компонент | Статус |
|-----------|--------|
| Docker контейнер | ✅ Running, healthy |
| iiko Events polling | ✅ каждые 30с, 9 точек (Артемий + Шабуров) |
| Telegram бот | ✅ работает |
| APScheduler | ✅ 14 jobs зарегистрировано |
| Бэкфилл T1 (shifts) | ✅ 4 024 смены Dec 2025 – Feb 21 |
| Бэкфилл T1 (hourly) | ✅ завершён (сессия 76) |
| bank_statement Томск | ✅ Томск-1, Томск-2 + Точка банк формат |

---

## 📌 Ключевые файлы сессии 78

- [app/jobs/bank_statement.py](app/jobs/bank_statement.py) — per-tenant конфиг, _ACQ_RE_TOCHKA, auto CITY_ORDER
- [app/jobs/arkentiy.py](app/jobs/arkentiy.py) — убрана summary, accounts_map из result
- `secrets/bank_accounts.json` (сервер) — новая структура по тенантам
- [docs/journal.md](docs/journal.md) — сессия 78

---

**Архитектура:** см. [rules/integrator/architecture.md](rules/integrator/architecture.md)

---

*Обновлено: 10 марта 2026, сессия 78*

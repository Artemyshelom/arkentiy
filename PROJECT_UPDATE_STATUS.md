# PROJECT UPDATE STATUS

> Снапшот состояния проекта для быстрого входа в контекст.  
> Обновляется в конце каждой сессии работы.

---

## 📍 Где остановились — Сессия 79 (10 марта 2026)

**Последнее действие:** Завершена разработка и деплой ФОТ-фичи (фонд оплаты труда). Добавлены расчёты по категориям персонала (повара/курьеры/администратор), интегрированы в утренние и еженедельные отчёты. Все 7 коммитов задеплоены на VPS. Бэкфилы завершены. Тестовые отчёты отправлены в личку.

**Остановились на:** Завтра (11 марта) проверяем достоверность ФОТ-данных. Известные вопросы:
- Томск_1 Яко: 0.2% ФОТ (9 марта были 8 нулевых смен в iiko — bug в данных, не коде)
- Курьеры: payment=0 в iiko salary (не почасовые, мотивационная программа)
- Небольшие расхождения в счётчиках поваров (исключены нулевые смены)

---

## ✅ Что сделано в сессии 79

| # | Что | Статус |
|---|-----|--------|
| 1 | Добавлена классификация admin в `_classify_role()` | ✅ коммит `(feat)` |
| 2 | Создан `app/clients/iiko_schedule.py` — fetch salary rates с iiko BO | ✅ коммит `(feat)` |
| 3 | Создана миграция `011_fot_tables.sql` — таблица `fot_daily` | ✅ коммит `(feat)` |
| 4 | Добавлены 4 функции в `database_pg.py` для ФОТ | ✅ коммит `(feat)` |
| 5 | Создан `app/jobs/fot_pipeline.py` — job для расчёта ФОТ (04:00 МСК) | ✅ коммит `(feat)` |
| 6 | Интеграция ФОТ блока в `daily_report.py` (только повара) | ✅ коммит `(fix)` |
| 7 | Интеграция ФОТ блока в `weekly_report.py` (только повара) | ✅ коммит `(fix)` |
| 8 | Регистрация job'a в `app/main.py` | ✅ коммит `(feat)` |
| 9 | Создан `app/onboarding/backfill_fot.py` с resumable progress | ✅ коммит `(feat)` |
| 10 | Обновлена `docs/onboarding/protocol.md` — добавлен Шаг 6 | ✅ коммит `(feat)` |
| 11 | Бэкфил для tenant_id=1 (17 дней, 1099 no_rate workers) | ✅ завершён |
| 12 | Бэкфил для tenant_id=3 (37 дней, 132 no_rate workers) | ✅ завершён |
| 13 | Тестовые отчёты в личку Артемия (за 9 марта, прошлую неделю) | ✅ отправлены |
| 14 | fix: timedelta импорт в `get_repeat_conversion` | ✅ коммит `(fix)` |
| 15 | fix: date объект вместо строки в `get_repeat_conversion` | ✅ коммит `(fix)` |
| 16 | fix: исключены нулевые смены из счётчика поваров на день | ✅ коммит `(fix)` |
| 17 | fix: параметр `notify=False` при бэкфиле, не спамим чаты | ✅ коммит `(fix)` |

**Коммиты:**
1. `feat: ФОТ по категориям персонала — pipeline, отчёты, бэкфил`
2. `fix: ФОТ — только повара (курьеры на мотивационной программе)`
3. `fix: ФОТ-алерты только при ежедневном прогоне, не при бэкфиле`
4. `fix: timedelta не импортирован в get_repeat_conversion`
5. `fix: явный ::date каст в get_repeat_conversion`
6. `fix: передавать date объект в get_repeat_conversion, не строку`
7. `fix: исключить нулевые смены из счётчика поваров/курьеров на день`
8. `docs: сессия 79 — ФОТ по категориям персонала (pipeline, отчёты, бэкфил)`

---

## 🔴 Активные проблемы / риски

- **`fot_data_validation`** ⏳ завтра — проверка ФОТ-процентов Артемием, сверка с его записанными значениями за прошлые недели. Томск_1 аномалия (0.2%) требует расследования.
- **`boris_api_regression_audit`** — не проверена изоляция `_states` по tenant_id в `/api/stats?metric=realtime`. Данные Шабурова потенциально видны в ответах Бориса.

---

## 📋 Приоритеты на следующую сессию

1. ✅ **Завтра (11.03):** Артемий проверяет ФОТ-данные против записанных значений → подтверждение или правки
2. `boris_api_regression_audit` — проверить изоляцию tenant_id в `/api/stats`, `_states`
3. `orders_status_optimization` — анализ OLAP vs Events для активных заказов
4. `email_statement_auto` — автозабор выписок с почты (актуально для Томска)

---

## 🏗 Текущее состояние системы

| Компонент | Статус |
|-----------|--------|
| Docker контейнер | ✅ Running, healthy (commit f1c4870) |
| iiko Events polling | ✅ каждые 30с, 9 точек (Артемий + Шабуров) |
| Telegram бот | ✅ работает, алерты отправляются |
| APScheduler | ✅ 15 jobs зарегистрировано (добавлен `job_fot_pipeline`) |
| Бэкфил T1 (shifts) | ✅ 4 024 смены Dec 2025 – Feb 21 |
| Бэкфил T1 (ФОТ) | ✅ 17 дней (01.02 – 09.03), 1099 workers no_rate |
| Бэкфил T3 (ФОТ) | ✅ 37 дней (01.02 – 09.03), 132 workers no_rate |
| ФОТ-пайплайн | ✅ job_fot_pipeline 04:00 МСК, работает с уведомлениями |
| bank_statement Томск | ✅ Томск-1, Томск-2 + Точка банк формат |
| Daily/Weekly отчёты | ✅ интегрирован ФОТ блок (повара только) |

---

## 📌 Ключевые файлы сессии 79

**Новые:**
- [app/clients/iiko_schedule.py](app/clients/iiko_schedule.py) — fetch salary rates XML parsing
- [app/jobs/fot_pipeline.py](app/jobs/fot_pipeline.py) — job расчёта ФОТ (04:00 МСК)
- [app/onboarding/backfill_fot.py](app/onboarding/backfill_fot.py) — резюмируемый бэкфил
- [app/migrations/011_fot_tables.sql](app/migrations/011_fot_tables.sql) — `fot_daily` таблица

**Изменённые:**
- [app/clients/iiko_bo_events.py](app/clients/iiko_bo_events.py) — добавлена admin категория
- [app/database_pg.py](app/database_pg.py) — 4 новые ФОТ-функции + 2 фикса
- [app/jobs/daily_report.py](app/jobs/daily_report.py) — ФОТ блок (повара)
- [app/jobs/weekly_report.py](app/jobs/weekly_report.py) — ФОТ блок (повара)
- [app/main.py](app/main.py) — регистрация job_fot_pipeline
- [docs/onboarding/protocol.md](docs/onboarding/protocol.md) — Шаг 6 бэкфил ФОТ
- [docs/journal.md](docs/journal.md) — сессия 79 (154 строки)

---

**Архитектура:** см. [rules/integrator/architecture.md](rules/integrator/architecture.md)

---

*Обновлено: 10 марта 2026, сессия 79 — ФОТ завершена и задеплоена*

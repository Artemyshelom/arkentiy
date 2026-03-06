# Журнал изменений — Интегратор

> Основной журнал по проекту Аркентий. Вести новые записи здесь.
>
> **Роль файла:** техническая история выполненных работ (что сделали, где, зачем, с каким результатом).
> **Не хранит:** стратегию и идеи (они в `roadmap.md` и `BACKLOG.md`).
> **Когда обновлять:** после каждой завершённой технической задачи или деплоя.
> **Архив старых сессий:** `docs/archive/journal_2025.md`

---

## Сессия 57 — 6 марта 2026 (feat: station_breakdown + jobs_health_dashboard) ✅

**Фокус:** Детализация заказов по этапам в `/статус` с реальными временами + dashboard статуса всех jobs.

### 1. fix: shift_status_check — неверный enum `/статус` (коммит `4fcb700`)

**Проблема:** `/статус` не показывал статус кассовой смены, в логах `409 Conflict` от iiko cashshifts API.

**Причина:** передавался `status="OPENED"` — несуществующий enum. Правильный: `status="OPEN"`.

**Фикс:** `app/jobs/iiko_status_report.py` — одна строка `"OPENED"` → `"OPEN"`.

---

### 2. feat: daily_revenue_by_branch — итоговая сводка по выручке (коммит `d036291`)

**Что:** после утренних отчётов по каждой точке бот теперь отправляет одно итоговое сообщение с таблицей выручки по всем точкам (сортировка по убыванию + строка итого).

**Где:** `app/jobs/daily_report.py` — функция `_format_daily_summary()` + сбор `branch_summaries` в основном цикле.

---

### 3. feat: station_breakdown_detail — разбивка заказов по этапам (коммиты `2da5f11`, `f452797`, `0bc8104`, `3a4dcb4`)

**Что:** в `/статус` добавлена таблица активных заказов по стадиям с реальными временами ожидания.

**Итоговый формат:**
```
🚚 Заказы: 41 активных | доставлено: 185
   Новые:      18  (—)
   Готовятся:  12  (среднее: 18 мин)
   Готовы:      5  (ждут: 8 мин)
   В пути:      6  (среднее: 22 мин)
```

**Критический баг по пути:** счётчики «Готовятся» и «Готовы» всегда были 0.

**Причина бага:** в `iiko_bo_events.py` iiko передаёт `orderNum` как `"81317.000000000"` (float-string). `int("81317.000000000")` → `ValueError` → cooking statuses никогда не сохранялись.

**Фикс:** `int(float(order_num_str))` — `app/clients/iiko_bo_events.py`.

**Времена (3a4dcb4):** `orders_agg` из БД для сегодняшних заказов всегда пустой (`send_time`, `opened_at` не пишутся в течение дня — только при ночном OLAP-обогащении за вчера). Переключились на **честные RT-времена из Events API**:
- `sent_at` — фиксируется при переходе статуса → "В пути к клиенту"
- `cooked_time` / `ready_time_actual` — уже пишутся при `cookingStatusChangedToNext`
- 3 новых property на `BranchState`: `avg_cooking_current_min`, `avg_wait_current_min`, `avg_delivery_current_min` — считают сколько **сейчас** висят заказы на каждом этапе
- Прокинуты через `get_branch_rt()`

**Файлы:** `app/clients/iiko_bo_events.py`, `app/jobs/iiko_status_report.py`.

---

### 4. feat: jobs_health_dashboard — /jobs + алерты при падении (коммит `358759a`)

**Что:** мониторинг scheduled jobs — автоалерт при любом исключении + команда `/jobs` для ручной проверки.

**Реализация:**

`app/utils/job_tracker.py` (новый файл):
- `@track_job("job_id")` — декоратор: при старте пишет `running` в `job_logs`, при успехе — `ok`, при исключении — `error` + алерт в monitoring chat через `telegram.error_alert()`, затем пробрасывает исключение
- `get_jobs_status()` — читает последний запуск каждого job через `LIKE ANY(patterns)` (покрывает старые имена: `morning_report_utc7` → canonical `daily_report`)
- `JOB_REGISTRY` — реестр 8 jobs с человеческими названиями

**Декораторы добавлены к 8 jobs:**
| Job | Файл |
|-----|------|
| `daily_report` | `jobs/daily_report.py` |
| `iiko_to_sheets` | `jobs/iiko_to_sheets.py` |
| `audit_report` | `jobs/audit.py` |
| `olap_enrichment` | `jobs/olap_enrichment.py` |
| `competitor_monitor` | `jobs/competitor_monitor.py` |
| `late_alerts` | `jobs/late_alerts.py` |
| `cancel_sync` | `jobs/cancel_sync.py` |
| `recurring_billing` | `jobs/billing.py` |

**Команда `/jobs`** (только admin, `app/jobs/arkentiy.py`):
- Показывает статус каждого job: ✅/❌/⏳/🔘
- Время последнего запуска (МСК) и длительность
- Краткий текст ошибки при `status=error`

**Примечание:** таблица `job_logs` уже существовала в схеме — никакой миграции не потребовалось.

---

**Коммиты сессии:** `4fcb700` → `d036291` → `2da5f11` → `f452797` → `0bc8104` → `3a4dcb4` → `358759a`

**BACKLOG:** закрыты `shift_status_check`, `daily_revenue_by_branch`, `station_breakdown_detail`, `jobs_health_dashboard`.

---

## Сессия 58 — 6 марта 2026 (fix: payment_changed признак 2 + report_consistency) ✅

**Фокус:** Завершение `payment_changes` (признак 2) + выравнивание метрик /отчёт.

### 1. fix: импорт track_job в audit.py (коммит `c2c072b`)

**Проблема:** после деплоя сессии 57 сервис падал с `NameError: name 'track_job' is not defined` на строке `@track_job("audit_report")` в `audit.py`.

**Причина:** декоратор был добавлен в прошлой сессии, но строку импорта забыли.

**Фикс:** `app/jobs/audit.py` — добавлен `from app.utils.job_tracker import track_job`.

---

### 2. feat: payment_changed признак 2 (коммит `0534eec`)

**Что:** реализован второй критерий детектирования смены оплаты в `_delivery_to_row()`.

**Логика признака 2:**
- Курьер ждал заказ ≥ 120 мин (`sent_at - ready_time_actual ≥ 120 мин`)
- При этом время доставки < 5 мин (`actual_time - sent_at < 5 мин`)
- Значит заказ был искусственно закрыт без реальной доставки (классическая схема при смене формы оплаты)

**Где:** `app/clients/iiko_bo_events.py`, функция `_delivery_to_row()`. Использует `BranchState._parse_ts()` для парсинга timestamp-строк из iiko.

**Данные доступны** потому что `ready_time_actual` (статус "Собран") и `sent_at` (статус "В пути") начали фиксироваться ещё в сессии 57.

**Итоговая логика детектирования:**
```python
# Признак 1: "смен" в комментарии
# Признак 2: idle >= 120 мин И delivery < 5 мин
```

---

### 3. fix: report_consistency — выравнивание метрик /отчёт (коммит `c137478`)

**Что:** устранены 4 расхождения между режимами команды `/отчёт` (день / период / город).

**Фикс ①: `payment_changed_count` в городском агрегате** (`_build_city_aggregate`)
- **Было:** `payment_changed_count` отсутствовал в `sum_keys` → терялся при суммировании по точкам → строка `⚠️ Исключено из расчёта: N` никогда не появлялась для `/отчёт Иркутск`
- **Стало:** добавлен в `sum_keys` и в `agg_out`

**Фикс ②: штат (повара/курьеры) в городском однодневном отчёте**
- **Было:** `cooks_today` и `couriers_today` принудительно = 0 даже при однодневном запросе
- **Стало:** добавлены в `sum_keys`, суммируются по точкам через `ds.get(k) or agg.get(k)`

**Фикс ③ и ④: живой баннер (одна точка и городской агрегат)**
- **Было:** `"COGS и скидки появятся после закрытия"` — пользователь не понимал, почему нет строки `🕐`
- **Стало:** `"COGS, скидки и времена этапов появятся после закрытия смены"` — теперь очевидно

**Файл:** `app/jobs/arkentiy.py`.

---

**Коммиты сессии:** `c2c072b` → `0534eec` → `c137478`

**BACKLOG:** закрыты `payment_changes`, `report_consistency`.

---

## Сессия 53 — 5 марта 2026 (fix: изоляция тенантов — полный аудит и фикс) ✅

**Фокус:** Комплексный аудит и устранение всех мест с хардкодом `tenant_id=1`, утечка данных Шабурова в тенант 1, фикс `_refresh_cache`.

**Причины (выявленные проблемы):**

1. **`_refresh_cache` в `access_manager.py`** — при вызове `/доступ` (toggle модулей) обновлял кэш `_db_cfg` только для `tenant_id==1`. Изменения для Шабурова (tenant_id=3) в кэш не попадали до следующего рестарта.

2. **Неправильный модульный конфиг** — `late_queries` был добавлен в 5 чатов (включая Поиск заказов). Нужен только в Опоздания (3 чата) + Отчёты. Поиск заказов должен иметь только `["search"]`.

3. **Данные Шабурова в tenant_id=1** — `shifts_raw`, `daily_stats`, `audit_events` писались с дефолтным `tenant_id=1` вместо `tenant_id=3`.

4. **6 критических мест хардкода** — функции без явной передачи `tenant_id`, использовали дефолт `=1`.

**Что сделано:**

1. **Фикс `_refresh_cache`** (`app/services/access_manager.py`):
   - Теперь перезагружает мержед-кэш для **всех** активных тенантов (запрос всех tenant configs → мерж → `access.update_db_cache(merged)`)
   - Ранее: `if tenant_id == 1: access.update_db_cache(cfg)` → Шабуров никогда не обновлялся

2. **SQL-фикс модулей на VPS** — Поиск заказов возвращён в `["search"]`; Опоздания x3 = `["late_alerts","late_queries"]`; Отчёты = `["reports","late_queries"]`

3. **Аудит чатов тенанта 1** — чат `-5243657179` переименован из "Алерты" в "Опасные операции" (аудит-модуль, корректно); тестовый чат `-1001448061976` с `["late_alerts"]` деактивирован

4. **6 критических фиксов в коде:**
   - `iiko_bo_events.py`: `upsert_shifts_batch(..., tenant_id=state.tenant_id)`
   - `jobs/daily_report.py`: `upsert_daily_stats_batch(..., tenant_id=branch.get("tenant_id", 1))`
   - `jobs/arkentiy.py`: `log_silence(..., tenant_id=_ctx_tid.get())`
   - `jobs/audit.py`: scheduled job группирует findings по tenant_id; `handle_audit_command` берёт `ctx_tenant_id`
   - `jobs/iiko_to_sheets.py`: `record_data_update(..., tenant_id=_tid)`
   - `routers/cabinet.py`: JWT без `tenant_id` → 401 (было: молча падало на тенант 1)

5. **Фикс данных в БД** — Шабуровские записи `shifts_raw`, `daily_stats`, `audit_events` с `tenant_id=1` пересохранены с `tenant_id=3`

6. **Обновлена миграция** `004_shaburov_onboarding.sql` — корректные `modules_json` для всех чатов

7. **Git коммиты:** `f365130` (6 критических фиксов) + `94e9821` (iiko_to_sheets + cabinet JWT)

8. **BACKLOG.md** — добавлена задача `tenant_id_default_hardening` (P2): убрать `tenant_id: int = 1` как дефолт из 50+ функций `database_pg.py`

**Итог:** Полная изоляция данных между тенантами. Данные Шабурова больше не смешиваются с tenant_id=1. Кэш корректно обновляется при любом переключении доступов. App healthy.

---

## Сессия 52 — 5 марта 2026 (fix: /опоздания у Шабурова) ✅

**Фокус:** Диагностика и фикс — команда `/опоздания` не работала ни у одного из тенантов Шабурова.

**Причина:**
Чаты Шабурова (`tenant_id=3`) не имели модуля `late_queries` в `modules_json`. Бот вызывает `get_permissions(chat_id)` → проверяет `_db_cfg["chats"][chat_id]` → модуль отсутствовал → `silent continue` (строка 2394 `arkentiy.py`), команда игнорировалась без ответа пользователю.

Дополнительно: чат **Отчёты** имел несуществующий модуль `"alerts"`.

**Что сделано:**

1. **Диагностика на VPS** — проверены логи (нет ошибок сохранения), схема БД (все 5 колонок есть), `is_late` записи (tenant_1=754, tenant_3=286), модули чатов (подтверждено отсутствие `late_queries`).

2. **SQL-фикс** — применён напрямую на VPS:
   ```sql
   -- 5 чатов: добавить late_queries
   UPDATE tenant_chats SET modules_json = modules_json || '["late_queries"]'
   WHERE slug = 'shaburov' AND chat_id IN (...)
   -- Отчёты: убрать несуществующий alerts
   UPDATE tenant_chats SET modules_json = убрать 'alerts' WHERE chat_id = -5128713915
   ```

3. **`docker compose restart app`** — кэш `_db_cfg` перезагружен (15 чатов, 3 пользователя).

4. **Обновлена миграция** `app/migrations/004_shaburov_onboarding.sql` — все 5 чатов теперь содержат `late_queries`, убран `"alerts"` из subscription.

5. **Git commit** `d26ce6c` — `fix: добавить late_queries к чатам Шабурова, убрать несуществующий модуль alerts`

**Итог:** `/опоздания` работает у всех тенантов Шабурова. Команда маршрутизируется через ebidoebi-polling-loop (Шабуров без bot_token), tenant резолвится по `get_tenant_id_for_chat(chat_id)`.

---

## Сессия 51 — 4 марта 2026 (Phase 7: производные временные метрики) ✅

**Фокус:** Реализация Phase 7 — расчёт производных метрик длительности для аналитики процессов.

**Реализовано:**

1. **Phase 7 (app/onboarding/phase7_calculate_durations.py):**
   - Автоматическое создание 4 новых колонок типа INTERVAL:
     - `cooking_duration` = время готовки (cooked_time - opened_at)
     - `idle_time` = время ожидания отправки (ready_time - cooked_time)
     - `delivery_duration` = время доставки (actual_time - send_time) ← **Критичное!**
     - `total_duration` = сквозное время (actual_time - opened_at)
   - Конвертирует TEXT временные поля в TIMESTAMP для расчётов
   - Работает для обоих тенантов

2. **Результаты Tenant 1 (Мой):**
   ```
   cooking_duration: 376,216 (98.2%) ✅
   idle_time: 370,200 (96.6%) ✅
   delivery_duration: 376,199 (98.2%) ✅
   total_duration: 383,065 (100.0%) ⭐ ИДЕАЛЬНО
   ```

3. **Результаты Tenant 3 (Шабуров):**
   ```
   cooking_duration: 326 (4.1%) ⚠️ нет opened_at в истории
   idle_time: 7,359 (91.9%) ✅
   delivery_duration: 7,356 (91.8%) ✅
   total_duration: 709 (8.9%) ⚠️ нет opened_at в истории
   ```

**SQL примеры использования:**
```sql
-- Аналитика готовки по станциям
SELECT 
  department,
  EXTRACT(EPOCH FROM cooking_duration) / 60 as cook_minutes,
  COUNT(*) as orders
FROM orders_raw
WHERE tenant_id = 1 AND cooking_duration IS NOT NULL
GROUP BY department
ORDER BY cook_minutes DESC;

-- Время доставки по курьерам
SELECT 
  EXTRACT(EPOCH FROM delivery_duration) / 60 as delivery_minutes,
  COUNT(*) as orders,
  ROUND(EXTRACT(EPOCH FROM AVG(delivery_duration)) / 60, 1) as avg_minutes
FROM orders_raw
WHERE tenant_id = 1 AND delivery_duration IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

**Коммиты:**
- 2517962: Phase 6: правильные OLAP field names (BillTime=ready_time, PrintTime=service_print_time)
- ac0a023: Phase 7: расчёт производных временных метрик

---

## Сессия 50 — 4 марта 2026 (Phase 6: обогащение временных полей) 🚀

**Фокус:** Реализация фазы обогащения временных полей (`cooked_time`, `ready_time`, `send_time`, `service_print_time`) из OLAP v2 для полноты аналитики.

**Реализовано:**

1. **Phase 6 (app/onboarding/phase6_enrich_times.py):**
   - Добавлен `send_time` в набор обогащаемых полей (для расчёта `delivery_duration = actual_time - send_time`)
   - Скрипт пробует оба варианта naming convention OLAP полей (простые и Delivery.*)
   - DATE_FROM=2026-02-01, обработка вчерашних дней с 2-часовыми интервалами

2. **Migration 005 (безопасная идемпотентность):**
   - Переименование `cancel_comment` → `cancellation_details` 
   - Удаление неиспользуемой колонки `problem_comment`
   - Использованы PostgreSQL DO блоки для проверки existence перед ALTER

3. **Деплой и тестирование:**
   - Контейнер успешно стартует, миграция применена
   - Статус tenant_id=1 (мой): временные поля заполнены 96-98%
     - cooked_time: 98.2% (Events API)
     - ready_time: 96.6% ← почти идеально!
     - send_time: 98.2% ✓ (НОВОЕ)
     - service_print_time: 98.2% (Events API)

4. **Available for Phase 7:**
   ```python
   cooking_duration = cooked_time - opened_at
   idle_time = ready_time - cooked_time
   delivery_duration = actual_time - send_time  # ← Использует send_time
   total_duration = actual_time - opened_at
   ```

**Статус Шабурова (tenant_id=3):**
- ⚠️  Все временные поля = 0% (исторические заказы, Event API только недавно заработала с фиксом tenant_id)
- 📝 Phase 6 не заполнила: OLAP v2 возвращает 400 (неправильные field names)
- 💡 План: дождаться пока Events API заполнит текущие заказы; для истории — запросить выгруз у Никиты

**Коммиты:**
- e27b3a5: Phase 6: добавляем send_time в backfill временных полей
- c55012d: Phase 6: исправляем имена полей OLAP для better SALES report compat
- 7edee68: Migration 005: safe ALTER TABLE using DO blocks для idempotency

---

## Сессия 49 — 4 марта 2026 (Multi-tenant: стабилизация данных Шабурова) ✅ ЗАВЕРШЕНО

**Фокус:** Исправление критических ошибок при работе со вторым клиентом (Шабуров, tenant_id=3).

**Проблемы найдены:**

1. **Events API писал заказы Шабурова в tenant_id=1** — 473 заказа за 3+ дня некорректно записаны
   - Причина: `BranchState` не содержал `tenant_id`, функция `upsert_orders_batch()` использовала дефолт `tenant_id=1`
   - Масштаб: дубли возникли 176 (были в t1 и t3), остаток 297 только в t1

2. **Зависшие заказы за вчера (03.03)** — 46 заказов в статусе "Новая", "В пути", "Доставлена"
   - Причина: Events API был недоступен/перезагружался во время подписки Шабурова → события закрытия пропущены
   - cancel_sync не помог: закрывает только отменённые (с CancelCause), не доставленные
   - Почему не закрылись сами: cancel_sync закрывает зависшие только если они старше 1 дня (Фаза 2) → вчерашние закроются завтра

3. **Данные неполные за первый день (03.03)**
   - Шабуров: 46 активных на момент проверки, у нас t1 0 зависших → очередная авторизационная проблема в первые часы
   - Ижевск: OLAP v2 таймаутит (сервер медленный)

**Исправлено:**

1. **Fix Events API tenant_id** (app/clients/iiko_bo_events.py):
   - Добавлено `tenant_id: int = 1` в `BranchState` dataclass
   - При инициализации: `tenant_id=branch.get("tenant_id", 1)`
   - При сохранении: `upsert_orders_batch(order_rows, tenant_id=state.tenant_id)`
   - Деплой: `docker compose build --no-cache && up -d`

2. **Дедубликация в orders_raw:**
   ```
   Дублей (есть в t1 И t3): 176 → удалены
   Только в t1 (перебито t1→t3): 297 → обновлены
   Итого после: Канск 5506, Зеленогорск 2279, Ижевск 221 — все в tenant_id=3
   ```

3. **Закрытие зависших за вчера:**
   - Запущен cancel_sync для tenant_id=3 — помог только для Зеленогорска (Канск, Ижевск OK)
   - Дополнительный скрипт: для каждого зависшего проверили в OLAP v2
   - Результат: 24 закрыты (есть в OLAP без CancelCause), 22 закрыты (не в OLAP, день прошёл)

**Документировано:**
- `docs/Уроки_и_баги.md` — 4 новых урока про Events API, cancel_sync, фильтрацию по времени, миграцию данных
- `app/onboarding/README.md` — полный 9-шаговый чеклист онбординга для новых клиентов

**Проверка:**
- ✅ `/поиск 9233716606` в Шабурове → находит заказ 142460 (Канск)
- ✅ Все заказы Канска/Зеленогорска/Ижевска теперь в tenant_id=3
- ✅ Контейнер healthy, новые заказы пишутся правильно

**Урок:** Multi-tenant architecture требует прокидывания tenant_id на ВСЕ уровни: от polling loop через in-memory state в БД. In-memory кэш — источник истины в real-time системе, если он хранит неверный tenant_id, вся цепочка ломается.

---

## Сессия 48 — 3 марта 2026 (Multi-tenant hotfixes) ✅ ЗАВЕРШЕНО

**Фокус:** Исправление критических bugs в multi-tenant логике.

**Проблемы найдены:**
1. `/поиск` показывала заказы ИЗ ЛЮБОГО ТЕНАНТА (7 SQL запросов без `tenant_id` фильтра)
2. `/точные` выгружала заказы всех тенантов
3. `/статус` (aggregate_orders_today) — 2 SQL запроса без `tenant_id`
4. `/выгрузка` — ONE-SHOT job, не создавалась для других тенантов
5. Дефолтный параметр `tenant_id=1` в helper-функциях скрывал забывчивость

**Исправлено:**
- `/поиск`: добавлены `tenant_id` фильтры во все 7 SQL запросов (arkentiy.py L915-955)
- `/точные`: добавлены параметр и фильтр в `get_exact_time_orders` (database_pg.py L1257)
- `/статус`: добавлены параметр и 2 фильтра в `aggregate_orders_today` (database_pg.py L998)
- `/выгрузка`: переделана на multi-tenant (job_export_iiko_to_sheets + wrapper в main.py)
- Helper: `get_module_chats_for_city` — убран дефолт, явная ошибка если забыли передать tenant_id
- Документация: добавлен подробный урок в `docs/Уроки_и_баги.md`

**Результат:** 
- Все команды теперь корректно фильтруют по текущему тенанту
- Каждый tenant видит только свои данные
- Google Sheets выгрузка работает для каждого tenant'а отдельно

---

## Сессия 47 — 3 марта 2026 (Phase 4+5: planned_time и client_name) ✅ ЗАВЕРШЕНО

### Что сделано

**Добавлены Phase 4 и Phase 5 в бэкфилл:**

**Phase 4 — enrichment `planned_time` через `Delivery.ExpectedTime`:**
- Поле: `Delivery.ExpectedTime` (только dimension, в `groupByRowFields`, `reportType: DELIVERIES`)
- Обновлено: **7301 заказов (100%)**
- `Delivery.ExpectedDeliveryTime` НЕ существует (confusion point, но `Delivery.ExpectedTime` есть)

**Phase 5 — enrichment `client_name` через `Delivery.CustomerName`:**
- Поле: `Delivery.CustomerName` (только dimension, в `groupByRowFields`, `reportType: DELIVERIES`)
- Обновлено: **7047 заказов (~96%)**
- 254 пропущены: анонимные заказы (GUEST12345) — не нужны в картах

**Результат итого:**
| Ветка | Всего | Состав | Курьер | Сумма | Плановое | Имя |
|-------|-------|--------|--------|-------|----------|-----|
| Канск | 5392 | 5392 | 5352 | 5124 | 5392 | 5169 |
| Зеленогорск | 2209 | 2209 | 2181 | 2096 | 2209 | 2178 |
| Ижевск | 108 | 108 | 68 | 106 | 108 | 108 |

### Открытия по OLAP v2

- `Delivery.ExpectedTime` — work-around: есть в DELIVERIES, возвращает ISO datetime (2026-02-10T11:55:00)
- `Delivery.CustomerName` — dimension field, возвращает либо имя (Павличенко Евгения), либо GUEST ID (GUEST649036)
- Оба поля работают ТОЛЬКО как `groupByRowFields`, не как `aggregateFields`

---

## Сессия 46 — 3 марта 2026 (Бэкфилл orders_raw для Шабурова + баги опоздания) ✅ ЗАВЕРШЕНО

### Что сделано

**1. Исправлен `tenant_id` в `orders_raw` для Шабурова**

Все 408 заказов, записанных реалтаймом Events API с момента подключения (Март 2026), хранились с `tenant_id=1` (дефолт из схемы БД) вместо `tenant_id=3`. Исправлено напрямую в БД:
```sql
UPDATE orders_raw SET tenant_id=3
WHERE branch_name IN ('Канск_1 Сов','Зеленогорск_1 Изы','Ижевск_1 Авт') AND tenant_id=1;
-- Обновлено 408 строк
```

**2. Написан и запущен бэкфилл `orders_raw` через OLAP v2**

Файл: `app/onboarding/backfill_orders_shaburov.py`

Метод: OLAP v2 (`/api/v2/reports/olap`) с group fields включая `Delivery.Number` и `Delivery.CustomerPhone`.

Ключевые открытия (см. `Уроки_и_баги.md`):
- `Delivery.CustomerPhone` — рабочее поле в OLAP v2 (телефон клиента) ✅
- `OpenDate` в `groupByRowFields` → `Delivery.Number` становится null на некоторых серверах iiko → **не добавлять `OpenDate` в group fields**
- Ижевск (`yobidoyobi-izhevsk.iiko.it`) — OLAP v2 таймаутит на исторических запросах → пропускаем

Результат бэкфилла (Feb 1 — Mar 2, 2026):
| Ветка | Заказов | Период |
|-------|---------|--------|
| Канск_1 Сов | 5 392 | 01.02 → 08.03 |
| Зеленогорск_1 Изы | 2 209 | 01.02 → 08.03 |
| Ижевск_1 Авт | 108 | только реалтайм (03.03+) |

**3. Фикс `/опоздания` — устаревшие/отменённые заказы**

`_handle_late()` (команда `/опоздания`) не имел фильтра `overdue_min > LATE_MAX_MIN (60 мин)`, в отличие от `job_late_alerts`. Заказы с пропущенным событием отмены зависали в `_states` со статусом "Новая" и показывались как активные опоздания часами.

Фикс: `app/jobs/arkentiy.py` → добавлено `if overdue_min <= 0 or overdue_min > LATE_MAX_MIN: continue`

### Архитектурное открытие: как устроен бэкфилл `orders_raw`

**Старая схема (для основного тенанта, Артемий):**
- OLAP v1 через кастомный пресет (`presetId=ca008919-...`) с cookie-auth на `tomat-i-chedder-ebidoebi-co.iiko.it`
- Скрипт запускался one-off внутри контейнера, не в git

**Новая схема (для новых клиентов):**
- OLAP v2 (`/api/v2/reports/olap`) с token-auth
- Скрипт в `app/onboarding/backfill_orders_shaburov.py` — шаблон для новых клиентов
- Команда запуска: `docker compose exec app python -m app.onboarding.backfill_orders_shaburov`
- Возобновляемый: прогресс в `/app/data/backfill_orders_shaburov_progress.json`

### Файлы изменены
- `app/jobs/arkentiy.py` — фикс `overdue_min > LATE_MAX_MIN` в `_handle_late`
- `app/clients/iiko_bo_events.py` — удалена инструментация
- `app/onboarding/backfill_orders_shaburov.py` — новый скрипт бэкфилла orders_raw

### Коммиты
- `fdd7c68` — fix: `/опоздания` + удаление инструментации
- `2d9ac2d` — feat: backfill orders_raw для Шабурова через OLAP v2

---

## Сессия 45 — 2 марта 2026 (Фикс `/отчет` для мультитенанта) ✅ ЗАВЕРШЕНО

### Что сделано

**Критический баг мультитенанта:** `/отчет` не работает для клиентов с `tenant_id > 1` (например, Шабуров).

**Симптом:** команда `/отчет 01.03` показывает "нет данных" хотя данные есть в БД для корректного тенанта.

**Причины (3 слоя):**

1. **Основная**: функции `get_daily_stats(name, date_from)` и `get_period_stats(name, date_from, date_to)` вызывались БЕЗ аргумента `tenant_id` → всегда использовалось значение по умолчанию `tenant_id=1`, игнорируя текущий контекст. Для клиента Шабурова (tenant_id=3) запрос попадал в пустую таблицу.

2. **Вторичная**: функция `get_available_branches(query)` иногда возвращала пусто если кэш точек не был заполнен для этого тенанта при запуске polling loop.

3. **Третичная**: контекст `ctx_tenant_id` не загружался на старте `run_polling_loop` для заполнения кэша.

### Фикс

**Файл `app/jobs/arkentiy.py`:**
- `_build_branch_report()` — теперь передаёт `tenant_id=_tid` в `get_daily_stats()` и `get_period_stats()`
- `_build_city_aggregate()` — то же самое для агрегации по городам
- `run_polling_loop()` — добавлено `await load_branches_cache(tenant_id)` при старте loop для заполнения кэша точек

**Файл `app/database_pg.py`:**
- `get_period_stats()` — добавлен параметр `tenant_id: int = 1` в сигнатуру функции, добавлен в WHERE-клауз

**Файл `app/jobs/iiko_status_report.py`:**
- `get_available_branches()` — добавлен fallback `if not branches and tenant_id != 1: branches = settings.branches` на случай если кэш не заполнен

### Файлы изменены
- `app/jobs/arkentiy.py` — строки 1232-1238 (tenant_id в _build_branch_report), 1290-1291 (в _build_city_aggregate), 2552-2558 (загрузка кэша в run_polling_loop)
- `app/database_pg.py` — строка 1173 (добавлен tenant_id в get_period_stats)
- `app/jobs/iiko_status_report.py` — строки 219-221 (fallback для пустого кэша)

### Деплой
- Разведка, бэкап, SCP 3 файлов, build --no-cache, up -d, проверка здоровья, логи чистые ✅
- VPS: `5.42.98.2`, контейнер healthy, нет ошибок в логах
- Дата: 02.03.2026 22:12 MSK

### Результат
- ✅ `/отчет 01.03` и `/отчет 28.02` работают для Шабурова (tenant_id=3)
- ✅ Данные отображаются корректно
- ✅ Изолированы по тенантам — нет утечек

### Уроки
- Добавлено в `Уроки_и_баги.md`: новый раздел "Мультитенант: get_daily_stats без tenant_id"
- Обновлены справочники в `интегратор.mdc` с ссылкой на этот урок

---

## Сессия 44 — 2 марта 2026 (Tenant isolation в данных) ✅ ЗАВЕРШЕНО

### Что сделано

**Два критических бага мультитенанта** — утечка данных между клиентами в `/поиск` и `/выгрузка`.

1. **`/поиск` возвращал заказы других тенантов** → при запросе без явного города (`city_filter=None`) переменная `city_branch_names` оставалась пустой, SQL-запрос выполнялся без фильтра `branch_name`, возвращая заказы всех тенантов. **Фикс:** добавлен fallback `if not city_branch_names: city_branch_names = [b["name"] for b in get_available_branches()]` (используется `ctx_tenant_id`).

2. **`/выгрузка` (CSV экспорт) утекала данные всех тенантов** → `run_export()` не получала `tenant_id`, `build_sql()` не добавлял фильтр по веткам. **Фикс:** получение текущего тенанта из `ctx_tenant_id`, добавление `_tenant_branch_names` в params, фильтрация по веткам во всех CTE (customer_first, customer_total, period_orders, excluded_phones) и основном WHERE.

3. **Проверка других команд** — `/статус`, `/отчёт`, `/опоздания` и т.д. используют `get_available_branches()` безопасно. Утечек нет.

### Файлы изменены
- `app/jobs/arkentiy.py` — строка 885: fallback в `_handle_search` для текущего тенанта
- `app/jobs/marketing_export.py` — строки 776-791 (получение tenant_id и ветвей), 313-330 (добавление в CTE), 356-379 (period_orders), 390-414 (excluded_phones)

### Коммиты
- `3eff381` — `fix: tenant isolation in /поиск`
- `c9cb249` — `fix: tenant isolation in /выгрузка`

---

## Сессия 43 — 2 марта 2026 (Баги мультитенанта + access_manager) ✅ ЗАВЕРШЕНО

### Что сделано

**Три критических бага обнаружены и исправлены после онбординга Шабурова.**

1. **Бот молчал на все сообщения** → `settings.openclaw_enabled` отсутствовал в VPS `config.py` → `run_polling_loop` падал с `AttributeError` молча (asyncio Task глотает unhandled exceptions). **Фикс:** добавлен `openclaw_enabled: bool = False` в класс `Settings`.

2. **`/доступ` показывал чаты Ёбидоёби** → `_resolve_tenant_id()` в `access_manager.py` возвращал `1` для любого global admin, не проверяя `tenant_users`. **Фикс:** проверка `tenant_users` ПЕРВАЯ, потом fallback на 1.

3. **Города в настройках чата — чужие (Барнаул/Томск вместо Ижевск/Канск)** → `CITIES` захардкожен глобально в `access.py`. **Фикс:** `get_tenant_cities()` из `iiko_credentials` → `cfg["tenant_cities"]`, все экраны используют tenant-specific список.

4. **Недоступные модули** → Добавлен `get_tenant_available_modules()` из `subscriptions.modules_json`. Недоступные модули показываются как 🔒, при нажатии — "Недоступно в вашем тарифе".

5. **Бэкфилл Зеленогорск+Канск** — перезапущен без Ижевска (Ижевск — неверный сервер, нужно уточнить у Никиты).

6. **Тест-аккаунт Артемия (8140013653)** — добавлен как admin Шабурова (`tenant_users` + `access_config.json`).

### Файлы изменены
- `app/config.py` — `openclaw_enabled: bool = False`
- `app/database_pg.py` — `get_tenant_cities()`, `get_tenant_available_modules()`, `get_access_config_from_db()` возвращает `tenant_cities` и `available_modules`
- `app/jobs/access_manager.py` — `_resolve_tenant_id()` исправлен, экраны tenant-aware (города + модули)

---

## Сессия 42 — 2 марта 2026 (Онбординг Шабурова + мультитенант) ✅ ЗАВЕРШЕНО

### Что сделано

**Первый внешний клиент: Никита Шабуров (tenant_id=3), города: Канск, Зеленогорск, Ижевск.**

1. **Миграция БД** (`004_shaburov_onboarding.sql`):
   - tenant, subscription, 3 iiko_credentials, 8 tenant_chats, 1 tenant_user
   - Исправлены ошибки: `ON CONFLICT (slug)` вместо `(email)`, убраны несуществующие колонны `period`, `connection_fee_paid`, `tenant_events`

2. **Мультитенантный бот на общем токене** (`app/ctx.py`):
   - Новый `app/ctx.py` — ContextVar вынесен в отдельный модуль
   - `database_pg.py` — кэш `chat_id → tenant_id` + `load_chat_tenant_map()`
   - `iiko_status_report.py` — `get_available_branches()` читает `ctx_tenant_id`
   - `arkentiy.py` — resolve tenant per message/callback по chat_id
   - `main.py` — загрузка конфигов ВСЕХ тенантов при старте

3. **Бэкфилл** (`app/backfill_shaburov.py`):
   - OLAP v2 данные с 01.02.2026 по вчера
   - Зеленогорск и Канск — успешно, Ижевск — timeout на OLAP v2 (сервер медленный)
   - Запущен в фоне через nohup

4. **Протокол онбординга** (`docs/Протокол_онбординга.md`):
   - Полный шаблон SQL, чеклист данных, описание граблей, тест-чеклист

### Изменённые файлы
**Новые:** `app/ctx.py`, `app/migrations/004_shaburov_onboarding.sql`, `app/backfill_shaburov.py`, `docs/Протокол_онбординга.md`
**Изменённые:** `app/database_pg.py`, `app/jobs/iiko_status_report.py`, `app/jobs/arkentiy.py`, `app/main.py`, `app/jobs/late_alerts.py`, `app/jobs/daily_report.py`, `app/clients/iiko_bo_events.py`

### Ключевые решения
- Общий бот (без отдельного токена для Шабурова) — изоляция через `chat_id → tenant_id`
- `global _openclaw_enabled` перенесён в начало `poll_analytics_bot` (Python 3.12 требует объявления до использования)
- OLAP v2 поля: `DishDiscountSumInt.withoutVAT`, `UniqOrderId.OrdersCount`, `ProductCostBase.Percent`

### Статус после сессии
- ✅ Шабуров активен в системе
- ✅ Контейнер healthy
- ✅ Мультитенантность работает (bot resolves tenant per chat_id)
- ⏳ Бэкфилл выполняется (~45 мин, Ижевск timeout)
- ⏳ Тест изоляции: `/статус` из группы Шабурова должен видеть только его 3 точки

---

## Сессия 41 — 1 марта 2026 (OpenClaw AI @ mention)

### Что сделано

**Интеграция OpenClaw AI — ответы на @ mention бота в групповых чатах.**

**Новые файлы:**
- `app/clients/openclaw.py` — async HTTP-клиент OpenClaw API (OpenAI-compatible)
  - Кастомные исключения по статусу: Auth, RateLimit, Timeout, Server
  - Structured logging: время ответа, размер запроса/ответа
- `app/jobs/openclaw_mention.py` — модуль обработки @ mention
  - Rate limiter: 3 запроса / 60 сек на user_id (in-memory)
  - Очистка @username из текста через regex
  - System prompt с контекстом пользователя (роль, город)
  - Все ошибки → понятные сообщения пользователю

**Изменения существующих файлов:**
- `app/config.py` — добавлены поля `OPENCLAW_*` + `TELEGRAM_BOT_USERNAME`
- `app/access.py` — добавлен модуль `"ai"` (метка: "🤖 AI (@ mention)") — выдаётся через `/доступ`
- `app/jobs/arkentiy.py`:
  - Хелперы `_react()` (setMessageReaction Bot API 7.0+) и `_reply()` (reply с разбивкой 4096)
  - Функция `_is_bot_mentioned()` по Telegram entities (надёжнее regex)
  - In-memory флаг `_openclaw_enabled` — инициализируется из `OPENCLAW_ENABLED` при старте
  - Команда `/ai on|off` для admin — горячее включение/выключение без редеплоя
  - Polling loop: mention detection → `_react(🤔)` → `handle_mention()` → `_reply()` → `_react(✅/❌)`
**Конфиг в `.env` (дописать на VPS):**
```
OPENCLAW_ENABLED=true
OPENCLAW_API_URL=http://72.56.107.85:18789/v1/chat/completions
OPENCLAW_API_TOKEN=275b5496a5cf9203575dabda8ed81ebabec438476062000b
OPENCLAW_MODEL=openclaw:arkentiy-brain
TELEGRAM_BOT_USERNAME=arkentiy_bot
```

**Примечание:** OpenClaw на отдельном сервере (72.56.107.85), не на том же хосте что Аркентий. `extra_hosts` в docker-compose не нужен — обычный HTTP по внешнему IP.

**Статус:** код готов локально, деплой по подтверждению Артемия.

---

## Сессия 40 — 2 марта 2026 (Деплой веб-платформы на VPS) ✅ ЗАВЕРШЕНО

### Что сделано

**Полный деплой веб-платформы на VPS 5.42.98.2:**

1. **Разведка + Бэкап:** 
   - Проверены VPS-версии `main.py`, `config.py`, `.env`
   - Созданы бэкапы с timestamps (20260302_HHMMSS) — откат возможен
   - SSH-ключ добавлен (`cursor_arkentiy_vps`)

2. **SCP новых модулей** (завершено ✅):
   - `app/clients/yukassa.py` — ЮKassa API-клиент
   - `app/routers/onboarding.py`, `payments.py`, `cabinet.py` (обновлён с `hash_password`)
   - `app/jobs/billing.py`, `subscription_lifecycle.py`
   - `app/migrations/003_web_platform.sql`
   - Папка `web/` целиком (все HTML, JS, CSS)

3. **Обновление конфигов** (завершено ✅):
   - `app/main.py` — импорты + регистрация `job_recurring_billing`, `job_trial_expiry`, `job_payment_grace`. **Важно:** mount("/") перемещён в конец файла (после всех routes)
   - `app/config.py` — поля ЮKassa + JWT + DEBUG
   - `.env` — переменные для ЮKassa, JWT, DEBUG
   - `requirements.txt` — зависимости
   - `docker-compose.yml` — добавлен volume `./web:/app/web` (для статических файлов)

4. **Миграция БД** (завершено ✅):
   - Применена `003_web_platform.sql` → 5 новых таблиц

5. **Docker Build & Run** (завершено ✅):
   - Docker Hub Rate Limit 429: решено через Docker Hub auth с email/пароль
   - Build успешен, все зависимости установлены
   - Контейнер healthy, база healthy
   - **Все 12 jobs видны**, включая 3 новых для веб-платформы

### Текущий статус — PRODUCTION ✅

- ✅ Контейнер **healthy**
- ✅ PostgreSQL **healthy**
- ✅ Health endpoint **OK**
- ✅ `/jobs` API работает, **12 jobs**:
  - ✅ `recurring_billing` — 03:00 МСК (завтра)
  - ✅ `trial_expiry` — 04:00 МСК (завтра)
  - ✅ `payment_grace` — 04:10 МСК (завтра)
  - ✅ Все старые jobs на месте
- ✅ Static files (`web/`) смонтированы и доступны

### Следующие шаги (TODO)

1. ⏳ Заполнить `YUKASSA_SHOP_ID`/`YUKASSA_SECRET_KEY` в `.env` на VPS
2. ⏳ Зарегистрировать webhook URL в ЮKassa
3. ⏳ Установить production `JWT_SECRET` в `.env` на VPS
4. ⏳ Выключить `DEBUG=false` в `.env` на VPS
5. ⏳ Тестирование: онбординг, оплата, webhook, lifecycle
6. ⏳ Git push коммит с Сессией 40

---


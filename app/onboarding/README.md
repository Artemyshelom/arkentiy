# Onboarding — Подключение новых клиентов

Скрипты для онбординга новых SaaS-клиентов (бэкфилл исторических данных).

## Актуальные скрипты

| Скрипт | Что делает | Когда запускать |
|--------|-----------|----------------|
| `backfill_new_client.py` | **Мастер-скрипт**: 4 шага последовательно (orders_raw + daily_stats + timing + hourly_stats) | Основной инструмент при онбординге |
| `backfill_orders_generic.py` | Заполняет `orders_raw` — 2 фазы (DELIVERIES + SALES dishes) | Если нужно только orders_raw |
| `backfill_daily_stats_generic.py` | Заполняет `daily_stats` — daily через Query C (только OLAP-поля) | Если нужно только пересчитать revenue/cash/noncash |
| `backfill_hourly_stats.py` | Заполняет `hourly_stats` — почасовая аналитика | Если нужно только hourly_stats |
| `set_chat_avatars.py` | Устанавливает аватарки чатам из iiko | Разово при подключении |

## Как запускать (новый клиент — быстрый старт)

```bash
# Полный бэкфилл нового клиента (все 4 шага)
python -m app.onboarding.backfill_new_client \
    --tenant-id 5 \
    --date-from 2026-01-01 \
    --date-to 2026-03-10

# Если iiko-сервер одного из городов недоступен — исключить его
python -m app.onboarding.backfill_new_client \
    --tenant-id 5 \
    --date-from 2026-01-01 \
    --date-to 2026-03-10 \
    --skip-cities "Город1,Город2"

# Запустить только отдельные шаги
python -m app.onboarding.backfill_new_client \
    --tenant-id 1 \
    --date-from 2026-01-01 \
    --date-to 2026-03-10 \
    --steps 3,4
```

### Шаги backfill_new_client

| Шаг | Источник | Таблица | Поля |
|-----|---------|---------|------|
| 1 | iiko OLAP | `orders_raw` | все поля заказов + блюда |
| 2 | iiko OLAP | `daily_stats` | revenue, cogs_pct, cash, noncash, pickup_count, discount_sum |
| 3 | orders_raw (БД) | `daily_stats` | avg_cooking_min, avg_delivery_min, late_*, exact_time_count, new/repeat customers |
| 4 | orders_raw + shifts_raw (БД) | `hourly_stats` | почасовая аналитика |

Прогресс шагов 1 и 4 сохраняется в `/app/data/` — скрипты возобновляемы после падения.

## Индивидуальный запуск скриптов

```bash
# Только orders_raw (Фаза 1: DELIVERIES + Фаза 2: SALES dishes)
python -m app.onboarding.backfill_orders_generic \
    --tenant-id 3 --date-from 2025-12-01 --date-to 2026-03-09

# Только daily_stats (выручка, COGS, cash/noncash, самовывоз)
python -m app.onboarding.backfill_daily_stats_generic \
    --tenant-id 3 --date-from 2025-12-01 --date-to 2026-03-09

# Только hourly_stats
python -m app.onboarding.backfill_hourly_stats \
    --tenant-id 3 --date-from 2025-12-01 --date-to 2026-03-09
```

## Архив

`archive/` — устаревшие скрипты (не удалять, нужны как справочник):
- `backfill_shaburov.py`, `backfill_orders_shaburov.py` — заменены generic-версиями
- `phase6_enrich_times.py`, `phase7_calculate_durations.py` — тайминги теперь заполняет пайплайн, duration-колонки дропнуты (migration 009)
- `backfill_olap_enrichment.py` — `olap_enrichment` deprecated (сессия 74)

## Миграции

Используй шаблон из `docs/rules/migration-template.sql` для создания SQL миграции нового клиента.

## Пример подключения клиента

```python
# 1. Миграция
app/migrations/00X_<client>.sql

# 2. Бэкфилл (если нужны исторические данные)
python -m app.onboarding.backfill_<client>

# 3. Проверка в боте
/статус — должны видеться только его города
/доступ — только его чаты
```

---

## 🔴 ЧЕКЛИСТ ОНБОРДИНГА НОВОГО КЛИЕНТА (Март 2026)

> Проверено на примере Шабурова (tenant_id=3). Без выполнения этих шагов — data leaks и функциональные баги.

### ШАГ 1: Миграция БД + авторизация

- [ ] Создан файл `app/migrations/00X_<client>.sql` с INSERT в `tenants`, `iiko_credentials`
- [ ] `iiko_credentials` содержит **правильные** значения:
  - [ ] `bo_url` — проверен через `curl https://bo_url/api/auth?login=...&pass=...` (должен вернуть UUID)
  - [ ] `bo_login` и `bo_password` — если есть свой логин для клиента (не глобальный)
  - [ ] `branch_name` — совпадает с названиями в Events API (иначе polling не найдёт ветку)
  - [ ] `tenant_id` — правильный ID (не 1!)
  - [ ] `utc_offset` — соответствует часовому поясу ветки
- [ ] Миграция применена: `docker compose exec app python -m alembic upgrade head` (или вручную через `psql`)

### ШАГ 2: Проверка Events API и BranchState

- [ ] В коде `app/clients/iiko_bo_events.py` проверить что `BranchState` инициализируется с `tenant_id`:
  ```python
  _states[name] = BranchState(
      bo_url=bo_url,
      branch_name=name,
      bo_login=branch.get("bo_login", ""),
      bo_password=branch.get("bo_password", ""),
      tenant_id=branch.get("tenant_id", 1),  # ← ИЗ ВЕТКИ
  )
  ```
- [ ] При сохранении вызывается `upsert_orders_batch(order_rows, tenant_id=state.tenant_id)` (не дефолтный tenant_id=1)
- [ ] После деплоя запустить 30 сек polling — проверить что новые заказы пишутся с правильным `tenant_id`:
  ```sql
  SELECT tenant_id, branch_name, COUNT(*) FROM orders_raw
  WHERE branch_name IN (<ветки_клиента>)
  GROUP BY tenant_id, branch_name;
  -- Результат: все в tenant_id = <нужный тенант>, не 1
  ```

### ШАГ 3: Бэкфилл исторических данных (5 фаз + обогащение)

**Фазы 1-5** (основные):
- [ ] Создан скрипт `app/onboarding/backfill_<client>.py` на основе `backfill_shaburov.py`
- [ ] В скрипте:
  - [ ] `TENANT_ID = <нужный ID>`
  - [ ] `SKIP_CITIES = {"Ижевск"}` если сервер не отвечает (проверить через curl OLAP v2)
  - [ ] Дата начала `DATE_FROM = date(YYYY, M, D)` — с даты активации подписки
- [ ] Запущен скрипт: `docker compose exec app python -m app.onboarding.backfill_<client> 2>&1 | tee logs.txt`
  - [ ] Проверены логи — нет ошибок авторизации, 404, таймаутов
  - [ ] Фаза 1 завершилась (покрыта дата начало → сегодня)
  - [ ] Фазы 2–5 завершились (items, courier, planned_time, client_name заполнены)

**Phase 6** (обогащение временных полей):
- [ ] Запущен `python -m app.onboarding.phase6_enrich_times` для заполнения:
  - [ ] `cooked_time` (время готовки) — было ~0%, должно быть 100%
  - [ ] `ready_time` (время готовности) — было ~50%, должно быть 100%
  - [ ] `service_print_time` (время печати) — было ~60%, должно быть 100%
- [ ] Финальная проверка:
  ```sql
  SELECT COUNT(*) FILTER (WHERE cooked_time IS NOT NULL) as cooked_pct,
         COUNT(*) FILTER (WHERE ready_time IS NOT NULL) as ready_pct,
         COUNT(*) FILTER (WHERE service_print_time IS NOT NULL) as print_pct
  FROM orders_raw WHERE tenant_id = <ID> AND date >= '2026-02-01';
  -- Ожидаемо: все близки к 100%
  ```

- [ ] Проверка качества данных (базовая):
  ```sql
  SELECT branch_name, 
         COUNT(*) as total,
         COUNT(*) FILTER (WHERE items != '') as has_items,
         COUNT(*) FILTER (WHERE courier != '') as has_courier,
         COUNT(*) FILTER (WHERE planned_time IS NOT NULL) as has_planned,
         COUNT(*) FILTER (WHERE client_name != '') as has_client_name,
         COUNT(*) FILTER (WHERE cooked_time IS NOT NULL) as has_cooked,
         COUNT(*) FILTER (WHERE ready_time IS NOT NULL) as has_ready
  FROM orders_raw WHERE tenant_id = <ID> GROUP BY branch_name;
  
  -- Ожидаемые метрики:
  -- - Все заказы с tenant_id = <ID> (не 1!)
  -- - items: 100% (обязательно)
  -- - courier: 95%+ (пропускаются редкие)
  -- - planned_time: 100% (обязательно)
  -- - client_name: 95%+ (пропускаются GUEST*)
  -- - cooked_time: 100% (ново!)
  -- - ready_time: 100% (ново!)
  ```

### ШАГ 4: Настройка доступа (модули)

- [ ] В таблице `subscriptions` создана запись для клиента с активными модулями:
  ```sql
  INSERT INTO subscriptions (tenant_id, plan_name, modules_json, ...)
  VALUES (<ID>, '<plan>', '{"reports": true, "late_queries": true, "marketing": true, ...}');
  ```
- [ ] Проверить в боте `/доступ` — модули НЕ показываются как 🔒 (locked)

### ШАГ 5: Проверка изоляции данных (КРИТИЧНО!)

- [ ] `/поиск <номер_заказа>` — находит только заказы клиента, не других
- [ ] `/выгрузка [параметры]` — экспортирует только его филиалы
- [ ] Команда `/статус` показывает только его города
- [ ] Команда `/отчет` показывает только его филиалы (если есть)

**Как проверить утечку:** зайти в чат ДРУГОГО клиента и выполнить `/поиск <номер_заказа_первого_клиента>` — не должен найти!

### ШАГ 6: Обновление main.py и lifespan

- [ ] При старте приложения в `lifespan` загружаются конфиги ВСЕх активных тенантов:
  ```python
  _tid_rows = await _pg_pool.fetch("SELECT id FROM tenants WHERE status = 'active'")
  for _row in _tid_rows:
      _cfg = await get_access_config_from_db(_row["id"])
      _merged_access["chats"].update(_cfg.get("chats", {}))
      # ...
  ```
- [ ] После деплоя перезапустить контейнер: `docker compose up -d`

### ШАГ 7: Дедубликация данных (если Events API писал раньше)

- [ ] Если заказы клиента уже были записаны до миграции (с tenant_id=1):
  ```sql
  -- Посчитать дубли
  SELECT COUNT(*) FROM orders_raw o1
  WHERE o1.branch_name IN (<ветки_клиента>)
    AND o1.tenant_id = 1
    AND EXISTS (
      SELECT 1 FROM orders_raw o2
      WHERE o2.branch_name = o1.branch_name
        AND o2.delivery_num = o1.delivery_num
        AND o2.tenant_id = <ID>
    );
  
  -- Удалить старые дубли (в t1) — в t<ID> они актуальнее
  DELETE FROM orders_raw o1
  WHERE o1.branch_name IN (<ветки_клиента>)
    AND o1.tenant_id = 1
    AND EXISTS (...);
  
  -- Перебить оставшиеся
  UPDATE orders_raw SET tenant_id = <ID>
  WHERE branch_name IN (<ветки_клиента>) AND tenant_id = 1;
  
  -- Проверка
  SELECT tenant_id, branch_name, COUNT(*) FROM orders_raw
  WHERE branch_name IN (<ветки_клиента>)
  GROUP BY tenant_id, branch_name;
  ```

### ШАГ 8: Проверка зависших заказов за первые дни

- [ ] После дня работы запустить `cancel_sync` вручную для клиента:
  ```bash
  docker compose exec app python -c "
  import asyncio
  async def run():
      from app.jobs.cancel_sync import job_cancel_sync
      await job_cancel_sync(tenant_id=<ID>)
  asyncio.run(run())
  "
  ```
- [ ] Проверить что зависшие заказы (старше 1 дня) закрылись
- [ ] Если есть зависшие за вчера — это норма, закроются завтра через Фазу 2

### ШАГ 9: Финальная проверка через 24h

- [ ] После первого дня работы:
  - [ ] `/поиск` работает быстро (данные в одном tenant_id)
  - [ ] Events API пишет новые заказы с правильным tenant_id
  - [ ] `/статус` показывает реальные цифры (не 0)
  - [ ] Нет дублей в `orders_raw` по разным tenant_id
  - [ ] Нет логов про ошибки авторизации

---

## Антипаттерны, которых нужно избежать

❌ **Не делай так:**
1. Запустить клиента БЕЗ проверки что Events API пишет правильный tenant_id
2. Пропустить дедубликацию данных если клиент пересекается с существующей веткой
3. Забыть загрузить конфиг клиента в lifespan
4. Считать что `/поиск` работает если заказ есть в БД (может быть утечка данных другого клиента!)
5. Деплоить БЕЗ перезапуска после ручного UPDATE в БД

✅ **Делай так:**
1. Проверь tenant_id в коде Events API ДО деплоя
2. После миграции БД — проверь один заказ через `/поиск`
3. После деплоя — дождись 30 сек нового заказа, проверь tenant_id в БД
4. Пока не уверен что данные изолированы — тести с заказами других клиентов
5. Если обновлял БД вручную — перезапусти контейнер

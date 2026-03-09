# ТЗ: Починить команду /опоздания (все тенанты)

> Статус: готово к работе  
> Дата: 05.03.2026  
> Приоритет: 🔴 Высокий

---

## Симптом

Команда `/опоздания` не отвечает ничем — ни у одного тенанта, ни в одном чате.

---

## Анализ: 3 причины

### Причина 1 — Модуль `late_queries` не назначен ни одному чату 🔴 (главная)

Команда `/опоздания` требует модуль `late_queries`. Если у чата его нет — бот **молча прерывает обработку**, без ответа пользователю:

```python
# app/jobs/arkentiy.py:2394
required_module = _CMD_MODULE.get(cmd)  # → "late_queries"
if required_module and not perms.has(required_module):
    continue  # тихо, без ответа
```

**Текущие модули чатов Шабурова** (`004_shaburov_onboarding.sql`):

| Чат | chat_id | Модули | `late_queries`? |
|-----|---------|--------|-----------------|
| Отчёты | -5128713915 | `reports, alerts` | ❌ |
| Аудит | -5114358382 | `audit` | ❌ |
| Поиск заказов | -5169819257 | `search` | ❌ |
| Опоздания Ижевск | -4860116340 | `late_alerts` | ❌ |
| Опоздания Зеленогорск | -5168619845 | `late_alerts` | ❌ |

Подписка Шабурова также **не содержит** `late_queries`:
```json
["audit","search","reports","late_alerts","alerts","iiko_to_sheets"]
```
Дополнительно: `"alerts"` — несуществующий модуль (нет в `ALL_MODULES`), должен быть `"late_alerts"`.

Добавить `late_queries` через `/доступ` невозможно: `access_manager` скрывает модули, не входящие в тариф.

---

### Причина 2 — `upsert_orders_batch` падает, `is_late` не пишется в БД 🟡

`upsert_orders_batch` (`app/database_pg.py:210`) вставляет колонки:

```sql
has_problem, bonus_accrued, return_sum, service_charge, cancellation_details
```

Но ни в одной из 5 миграций эти колонки **не создаются** (проверено поиском по всем `.sql`). Если на VPS они отсутствуют — каждый INSERT из Events API завершается ошибкой:

```python
# app/clients/iiko_bo_events.py:628
except Exception as e:
    logger.error(f"[{state.branch_name}] Ошибка сохранения в БД: {e}")  # поглощается!
```

Последствие: `is_late = true` никогда не попадает в `orders_raw` → историческая `/опоздания вчера` всегда возвращает "нет опозданий".

---

### Причина 3 — Пустой кэш точек = пустой результат real-time 🟡

`_handle_late` (режим без даты) фильтрует `_states` через `get_available_branches()`:

```python
branch_names_set = {b["name"] for b in get_available_branches(...)}
for branch_name, state in _states.items():
    if branch_name not in branch_names_set:
        continue  # пропускается, если кэш не загрузился
```

Если `_branches_cache[tenant_id]` пуст (ошибка при старте) → `branch_names_set = {}` → всё фильтруется → "✅ Активных опозданий нет" даже при реальных опозданиях.

---

## Диагностика на VPS (перед фиксом)

```bash
# 1. Есть ли ошибки сохранения в БД?
docker logs ebidoebi_app 2>&1 | grep "Ошибка сохранения в БД" | tail -10

# 2. Есть ли нужные колонки?
docker exec ebidoebi_db psql -U postgres -d postgres -c \
  "SELECT column_name FROM information_schema.columns
   WHERE table_name='orders_raw'
   AND column_name IN ('has_problem','bonus_accrued','return_sum','service_charge','cancellation_details');"

# 3. Есть ли записи с is_late=true?
docker exec ebidoebi_db psql -U postgres -d postgres -c \
  "SELECT tenant_id, COUNT(*) FROM orders_raw WHERE is_late=true GROUP BY 1;"

# 4. Какие модули реально стоят у чатов Шабурова?
docker exec ebidoebi_db psql -U postgres -d postgres -c \
  "SELECT chat_id, name, modules_json FROM tenant_chats
   WHERE tenant_id=(SELECT id FROM tenants WHERE slug='shaburov');"
```

---

## Что делать

### Шаг 1 — Миграция недостающих колонок

Создать `app/migrations/006_orders_extra_fields.sql`:

```sql
-- 006_orders_extra_fields.sql
-- Колонки, которые вставляет upsert_orders_batch, но отсутствуют в схеме

ALTER TABLE orders_raw ADD COLUMN IF NOT EXISTS has_problem       BOOLEAN DEFAULT false;
ALTER TABLE orders_raw ADD COLUMN IF NOT EXISTS bonus_accrued     DOUBLE PRECISION;
ALTER TABLE orders_raw ADD COLUMN IF NOT EXISTS return_sum        DOUBLE PRECISION;
ALTER TABLE orders_raw ADD COLUMN IF NOT EXISTS service_charge    DOUBLE PRECISION;
```

Миграция `cancellation_details` уже есть в `005_refactor_comments.sql` — проверить что применилась.

---

### Шаг 2 — Исправить подписку Шабурова (добавить `late_queries`, убрать опечатку `alerts`)

```sql
UPDATE subscriptions
SET modules_json = '["audit","search","reports","late_alerts","late_queries","iiko_to_sheets"]'::jsonb
WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'shaburov');
```

---

### Шаг 3 — Добавить `late_queries` чатам Шабурова

Минимум — чат "Поиск заказов" (уже используется для `/поиск`, логично добавить туда же):

```sql
UPDATE tenant_chats
SET modules_json = '["search","late_queries"]'::jsonb
WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'shaburov')
  AND chat_id = -5169819257;
```

Или "Отчёты" — если опоздания нужны там (обсудить с Шабуровым):

```sql
UPDATE tenant_chats
SET modules_json = '["reports","late_alerts","late_queries"]'::jsonb
WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'shaburov')
  AND chat_id = -5128713915;
```

---

### Шаг 4 — Перезапуск для обновления кэша

После SQL-изменений перезапустить сервис (кэш `_db_cfg` и `_branches_cache` заполняются при старте):

```bash
docker compose restart app
sleep 10
docker compose ps
docker compose logs app --tail=30
```

---

### Шаг 5 — Деплой миграции `006`

```bash
# Копируем миграцию на VPS
scp app/migrations/006_orders_extra_fields.sql arkentiy:/opt/ebidoebi/app/migrations/

# Пересборка контейнера (миграции применяются при init_db)
cd /opt/ebidoebi
docker compose build --no-cache && docker compose up -d
sleep 15
docker compose ps
docker compose logs app --tail=40
```

---

## Проверка после фикса

```bash
# 1. Нет ошибок сохранения?
docker logs ebidoebi_app 2>&1 | grep "Ошибка сохранения в БД" | wc -l

# 2. is_late стал заполняться?
docker exec ebidoebi_db psql -U postgres -d postgres -c \
  "SELECT tenant_id, COUNT(*) FROM orders_raw
   WHERE is_late=true AND date >= CURRENT_DATE - 1
   GROUP BY 1;"

# 3. /опоздания отвечает нормально?
# Отправить /опоздания в чат Шабурова, убедиться что получен ответ (не тишина)
```

---

## Файлы к изменению

| Файл | Действие |
|------|----------|
| `app/migrations/006_orders_extra_fields.sql` | **Создать** |
| БД: таблица `subscriptions` (tenant Шабуров) | SQL UPDATE — добавить `late_queries` |
| БД: таблица `tenant_chats` (чат Шабурова) | SQL UPDATE — добавить `late_queries` |

Файлы Python не меняются.

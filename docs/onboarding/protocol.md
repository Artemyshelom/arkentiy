# Протокол онбординга клиента — Аркентий

> Создан по итогам Сессии 42 (Шабуров, 2 марта 2026).
> Цель: стандартизировать ручной онбординг + фундамент для автоматического.

**Связанные документы:**
- [offboarding_city.md](offboarding_city.md) — протокол отключения города/точки

---

## 0. Чеклист «Что нужно от клиента» (собрать ДО начала)

| Поле | Пример | Где использовать |
|------|--------|-----------------|
| Имя / компания | «Никита Шабуров» | `tenants.name`, `contact` |
| Почта | shaburovn1991@gmail.com | `tenants.email`, логин в кабинет |
| Телеграм-ID | 400872656 | `tenant_users.user_id` |
| Телеграм-username | @papa_sektor | для документации |
| Города | Канск, Зеленогорск, Ижевск | `iiko_credentials.city` |
| iiko логин | lazarevich | `iiko_credentials.bo_login` |
| iiko пароль | 19121984 | `iiko_credentials.bo_password` |
| Адреса iiko BO | yobidoyobi-kansk.iiko.it/resto | `iiko_credentials.bo_url` |
| Telegram-группы | ID каждой группы + назначение | `tenant_chats` |
| Модули | аудит, поиск, отчёты, опоздания, алерты, выгрузка | `subscriptions.modules_json` |
| Google Sheet ID | 1IfY8GVZx... | записать в `tenant_events` / settings |
| Свой бот? | да/нет | `tenants.bot_token` |

**Как получить Telegram ID группы:** бот должен быть добавлен в группу, написать туда, `getUpdates` покажет `chat.id`.

**Как получить dept_id для iiko:**
```bash
# SHA1 пароля
python3 -c "import hashlib; print(hashlib.sha1(b'ПАРОЛЬ').hexdigest())"

# Auth
curl "https://BO_URL/api/auth?login=ЛОГИН&pass=ХЭШ"

# Список департаментов (XML ответ, парсим python)
curl "https://BO_URL/api/corporation/departments?key=TOKEN" | python3 -c "
import sys, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.read())
for d in root.findall('.//departmentDto'):
    print(d.findtext('id'), d.findtext('name'))
"
```

---

## 1. SQL-миграция — шаблон

> **Файл:** `app/migrations/00N_SLUG_onboarding.sql`
> Идемпотентный (безопасно запускать повторно).

### Известные грабли схемы БД

| Ошибка | Причина | Правильно |
|--------|---------|-----------|
| `ON CONFLICT (email)` | email не unique в tenants | `ON CONFLICT (slug)` |
| `period` не существует | нет такой колонны в subscriptions | убрать |
| `connection_fee_paid` не существует | нет такой колонны | убрать |
| `tenant_events` не существует | таблица не создана | убрать или создать |
| `asyncpg date parameter` | передаём строку | передавать `date.fromisoformat(d)` |

### Актуальная схема ключевых таблиц

**tenants:** `id, name, slug (unique), email, contact, password_hash, plan, status, bot_token, created_at, updated_at`

**subscriptions:** `id, tenant_id (unique), status, plan, modules_json, branches_count, amount_monthly, started_at, next_billing_at, grace_until, created_at, updated_at`

**iiko_credentials:** `id, tenant_id, branch_name, city, bo_url, bo_login, bo_password, dept_id, utc_offset, is_active, created_at`
- Unique: `(tenant_id, branch_name)`

**tenant_chats:** `id, tenant_id, chat_id, name, modules_json, city, is_active`
- Unique: `(tenant_id, chat_id)`
- `city`: NULL = все города; JSON-массив `["Канск","Зеленогорск"]` = фильтр по городам

**tenant_users:** `id, tenant_id, user_id, name, role, modules_json, city, is_active`
- Unique: `(tenant_id, user_id)`

### Шаблон SQL-миграции

```sql
-- ====================================================================
-- 00N_SLUG_onboarding.sql
-- Онбординг клиента: ИМЯ (tenant_id=N)
-- Города: ...
-- ====================================================================

-- 1. Tenant (unique = slug)
INSERT INTO tenants (name, slug, email, contact, password_hash, plan, status, created_at, updated_at)
VALUES (
    'Название',
    'slug',                          -- уникальный slug
    'email@example.com',
    'Имя Контакта',
    '$2b$12$...',                    -- bcrypt hash пароля
    'base',
    'active',
    now(), now()
)
ON CONFLICT (slug) DO UPDATE SET status = 'active', updated_at = now();

-- 2. Subscription
INSERT INTO subscriptions (tenant_id, status, plan, modules_json, branches_count, amount_monthly, started_at, created_at, updated_at)
SELECT id, 'active', 'base',
    '["audit","search","reports","late_alerts","alerts","iiko_to_sheets"]'::jsonb,
    3, 15000, now(), now(), now()
FROM tenants WHERE slug = 'slug'
ON CONFLICT (tenant_id) DO NOTHING;

-- 3. iiko credentials (одна строка на точку)
INSERT INTO iiko_credentials (tenant_id, branch_name, city, bo_url, bo_login, bo_password, dept_id, utc_offset, is_active, created_at)
SELECT id, 'Канск_1 Назв', 'Канск',
    'https://HOST.iiko.it/resto',
    'LOGIN', 'PASSWORD',
    'UUID-dept-id', 7, true, now()
FROM tenants WHERE slug = 'slug'
ON CONFLICT (tenant_id, branch_name) DO NOTHING;

-- 4. Telegram chats
-- Глобальный чат (city = NULL → все города)
INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
SELECT id, -XXXXXXXXX, 'Отчёты', '["reports","alerts"]'::jsonb, NULL, true
FROM tenants WHERE slug = 'slug'
ON CONFLICT (tenant_id, chat_id) DO UPDATE SET is_active = true, modules_json = '["reports","alerts"]'::jsonb;

-- Чат с фильтром по городу (city = JSON-массив)
INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
SELECT id, -XXXXXXXXX, 'Опоздания Канск', '["late_alerts"]'::jsonb, '["Канск"]', true
FROM tenants WHERE slug = 'slug'
ON CONFLICT (tenant_id, chat_id) DO UPDATE SET is_active = true, city = '["Канск"]', modules_json = '["late_alerts"]'::jsonb;

-- 5. Tenant user (admin)
INSERT INTO tenant_users (tenant_id, user_id, name, role, is_active)
SELECT id, TG_USER_ID, 'Имя', 'admin', true
FROM tenants WHERE slug = 'slug'
ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = 'admin', is_active = true;

-- Проверка итогов
SELECT 'tenants' as tbl, count(*) FROM tenants WHERE slug = 'slug'
UNION ALL
SELECT 'subscriptions', count(*) FROM subscriptions s JOIN tenants t ON t.id = s.tenant_id WHERE t.slug = 'slug'
UNION ALL
SELECT 'iiko_credentials', count(*) FROM iiko_credentials ic JOIN tenants t ON t.id = ic.tenant_id WHERE t.slug = 'slug'
UNION ALL
SELECT 'tenant_chats', count(*) FROM tenant_chats tc JOIN tenants t ON t.id = tc.tenant_id WHERE t.slug = 'slug'
UNION ALL
SELECT 'tenant_users', count(*) FROM tenant_users tu JOIN tenants t ON t.id = tu.tenant_id WHERE t.slug = 'slug';
```

### Запуск миграции

```bash
# 1. Скопировать файл на VPS
scp -i ~/.ssh/cursor_arkentiy_vps app/migrations/00N_SLUG.sql root@5.42.98.2:/opt/ebidoebi/app/migrations/

# 2. Выполнить через psql в контейнере
ssh -i ~/.ssh/cursor_arkentiy_vps root@5.42.98.2 "
cd /opt/ebidoebi
docker compose exec -T postgres psql -U ebidoebi -d ebidoebi < /opt/ebidoebi/app/migrations/00N_SLUG.sql
"
```

> ⚠️ `init_db` прогоняет ВСЕ миграции из папки при каждом старте.
> Миграция должна быть идемпотентной (ON CONFLICT DO NOTHING/UPDATE).

---

## 2. Генерация пароля для веб-кабинета

```python
import bcrypt
pwd = b"ПАРОЛЬ_КЛИЕНТА"
hashed = bcrypt.hashpw(pwd, bcrypt.gensalt(12))
print(hashed.decode())  # вставить в SQL
```

Или через Python в контейнере:
```bash
ssh root@5.42.98.2 "cd /opt/ebidoebi && docker compose exec -T app python3 -c \"
import bcrypt
print(bcrypt.hashpw(b'ПАРОЛЬ', bcrypt.gensalt(12)).decode())
\""
```

---

## 3. Деплой обновлённого кода (если нужен)

```bash
# Бэкап файлов, которые меняем
ssh -i ~/.ssh/cursor_arkentiy_vps root@5.42.98.2 "
TS=\$(date +%Y%m%d_%H%M%S)
cp /opt/ebidoebi/app/ФАЙЛ.py /opt/ebidoebi/app/ФАЙЛ.py.bak.\$TS
"

# SCP
scp -i ~/.ssh/cursor_arkentiy_vps app/ФАЙЛ.py root@5.42.98.2:/opt/ebidoebi/app/ФАЙЛ.py

# Rebuild
ssh -i ~/.ssh/cursor_arkentiy_vps root@5.42.98.2 "cd /opt/ebidoebi && docker compose up -d --build 2>&1 | tail -6"

# Проверка через 15-20 сек
sleep 20 && ssh -i ~/.ssh/cursor_arkentiy_vps root@5.42.98.2 "
cd /opt/ebidoebi
docker compose ps
docker compose logs app --tail=15
"
```

---

## 4. Бэкфилл исторических данных

**Три бэкфилла (делаются в порядке 1→2→3):**
1. `daily_stats` — выручка, скидки, COGS (из OLAP v2)
2. `orders_raw` — заказы, блюда, курьеры, время (5 фаз из OLAP v2)
3. Вычисление времён (avg_cooking_min, avg_wait_min, avg_delivery_min) — из orders_raw в daily_stats

---

### 4A. Бэкфилл `daily_stats` (выручка + скидки из OLAP)

Заполняет таблицу `daily_stats` — используется в `/отчёт`, `/статус`, утренних отчётах.

**Скрипт:** `app/onboarding/backfill_daily_stats_generic.py` — **generic для любого tenant'а**.

**Что заполняется:**
- Выручка (DishDiscountSumInt.withoutVAT)
- COGS % (ProductCostBase.Percent)
- Кол-во чеков (UniqOrderId.OrdersCount)
- Сумма скидок (DiscountSum)
- Самовывоз (по Delivery.ServiceType)

**Запуск:**

```bash
# На VPS
ssh arkentiy "cd /opt/ebidoebi && docker compose exec app \
  python -m app.onboarding.backfill_daily_stats_generic \
    --tenant-id 3 \
    --date-from 2025-02-01 \
    --date-to 2026-03-01 \
  2>&1 | tee /opt/ebidoebi/logs/backfill_daily_stats_tenant3.log"
```

**ETA:** ~10 минут за месяц (2 OLAP запроса в день × 30 дней).

**Возобновляемо:** UPSERT по (tenant_id, branch_name, date), безопасно перезапустить.

---

### 4B. Бэкфилл `orders_raw` (индивидуальные заказы)

Заполняет таблицу `orders_raw` — используется в `/поиск` (по телефону клиента).

**Скрипт:** `app/onboarding/backfill_orders_generic.py` — **generic для любого tenant'а**.

**5 фаз:** Phase 1 (по дням) → затем Phases 2-5 параллельно

| Фаза | Поле OLAP | Результат | Статус |
|------|----------|----------|--------|
| 1 | `Delivery.Number`, `Department`, `Delivery.CustomerPhone`, `Delivery.CancelCause`, `Delivery.ActualTime`, `Delivery.Address`, `Delivery.ServiceType` | core fields: `delivery_num`, `branch_name`, `client_phone`, `sum`, `date`, `actual_time`, `delivery_address`, `is_self_service`, `status`, `cancel_reason` | ✅ |
| 2 | `Delivery.Number`, `Department`, `DishName` | `items` (JSON с составом) | ✅ |
| 3 | `Delivery.Number`, `Department`, `WaiterName` | `courier` (имя курьера) | ✅ |
| 4 | `Delivery.Number`, `Department`, `Delivery.ExpectedTime` | `planned_time` (плановое время доставки) | ✅ |
| 5 | `Delivery.Number`, `Department`, `Delivery.CustomerName` | `client_name` (имя клиента, пропускаем GUEST*) | ✅ |

**Для нового клиента — меняем только параметры:**
```bash
python -m app.onboarding.backfill_orders_generic \
  --tenant-id 3 \
  --date-from 2025-02-01 \
  --date-to 2026-03-01 \
  --skip-cities "Ижевск"
```

**⚠️ После бэкфилла — проверить `tenant_id` и заполнение полей:**
```sql
-- Заказы НЕ должны быть с tenant_id=1
SELECT tenant_id, branch_name, COUNT(*)
FROM orders_raw
WHERE branch_name IN ('Ветка_1', 'Ветка_2')
GROUP BY tenant_id, branch_name;

-- Целевые метрики (для успешного бэкфилла):
-- - Все заказы должны иметь tenant_id = N
-- - 100% заказов с составом (items)
-- - ~95-98% с курьером (пропускаются отдельные serve)
-- - 100% с planned_time
-- - ~95-98% с именем (пропускаются анонимные GUEST*)
```

---

### 4C. Вычисление времён (avg_cooking_min, avg_wait_min, avg_delivery_min)

После заполнения `orders_raw` → вычисляем времена и обновляем `daily_stats`.

Через Python (безопаснее, вычисляет правильный avg_wait_min):
```bash
ssh arkentiy "cd /opt/ebidoebi && docker compose exec app python -c '
import asyncio
from datetime import date, timedelta
from app.database_pg import get_pool, aggregate_orders_for_daily_stats

async def main():
    pool = get_pool()
    tenant_id, branch, date_from = 3, \"Канск\", date(2025, 2, 1)
    
    for i in range(30):  # 30 дней
        d = date_from + timedelta(days=i)
        agg = await aggregate_orders_for_daily_stats(branch, d.isoformat(), tenant_id)
        await pool.execute(\"UPDATE daily_stats SET avg_cooking_min=\$1, avg_wait_min=\$2, avg_delivery_min=\$3 WHERE tenant_id=\$4 AND branch_name=\$5 AND date=\$6\",
            agg.get(\"avg_cooking_min\"), agg.get(\"avg_wait_min\"), agg.get(\"avg_delivery_min\"), tenant_id, branch, d)

asyncio.run(main())
'"
```

Или через SQL напрямую (если нужны все даты сразу):
```sql
UPDATE daily_stats d SET avg_cooking_min = t.avg_cooking_min, avg_wait_min = t.avg_wait_min, avg_delivery_min = t.avg_delivery_min
FROM (SELECT branch_name, date::text,
  AVG(CASE WHEN cooked_time != '' AND opened_at != '' AND sum >= 200
        AND EXTRACT(EPOCH FROM (cooked_time::timestamp - REPLACE(SUBSTR(opened_at,1,19),'T',' ')::timestamp))/60 BETWEEN 1 AND 120
      THEN EXTRACT(EPOCH FROM ...) END) as avg_cooking_min,
  -- аналогично для avg_wait_min и avg_delivery_min
  ...
FROM orders_raw WHERE tenant_id = 3 AND status != 'Отменена' GROUP BY branch_name, date::text) t
WHERE d.tenant_id = 3 AND d.branch_name = t.branch_name AND d.date::text = t.date_str;
```

| Поле | Тип | reportType | Где | Примечание |
|------|-----|-----------|-----|-----------|
| `Delivery.CustomerPhone` | measure | SALES | aggregateFields | Работает |
| `Delivery.CancelCause` | dimension | SALES | groupByRowFields | Null = не отменён |
| `Delivery.ActualTime` | dimension | SALES | groupByRowFields | Фактическое время |
| `DishName` | dimension | SALES | groupByRowFields | Для состава (Phase 2) |
| `WaiterName` | dimension | SALES | groupByRowFields | Курьер доставки (Phase 3) |
| `Delivery.ExpectedTime` | dimension | **DELIVERIES** | groupByRowFields | Плановое время (Phase 4) |
| `Delivery.CustomerName` | dimension | **DELIVERIES** | groupByRowFields | Имя клиента, GUEST* = анонимные (Phase 5) |

**⚠️ КРИТИЧЕСКИЕ НЮАНСЫ:**
- `OpenDate` в `groupByRowFields` → `Delivery.Number` становится NULL на Канске → **НЕ добавлять**
- `Delivery.ExpectedDeliveryTime` НЕ существует (confusion)
- `WaiterName` в Phase 1 обнуляет `DishDiscountSumInt` → Phase 3 отдельно
- `Delivery.CustomerName` может быть "GUEST12345" для анонимов → фильтруем

---

### Известные грабли OLAP

| Ошибка | Причина | Решение |
|--------|---------|---------|
| `Delivery.Number` = null | `OpenDate` добавлен в `groupByRowFields` | Убрать `OpenDate` из group fields |
| `DishDiscountSumInt` = 0 | `WaiterName` добавлен в `OLAP_ORDER_FIELDS` Phase 1 | `WaiterName` → отдельный Phase 3 |
| Timeout 60s на OLAP v2 | Медленный сервер iiko (Ижевск) | Добавить в `SKIP_CITIES` |
| `tenant_id=1` в orders_raw | Events API пишет до настройки маппинга | `UPDATE ... SET tenant_id=N` вручную |

---

## 5. Общий бот vs свой бот

### Архитектура мультитенантного бота

При старте (`main.py`):
- Тенанты С `bot_token` → каждый получает свой `polling_loop`
- Тенанты БЕЗ `bot_token` → обслуживаются общим ботом Ёбидоёби

При обработке каждого сообщения (`arkentiy.py`):
```python
# Группа → ищем tenant_id по chat_id
resolved = get_tenant_id_for_chat(chat_id)
# Личка → ищем tenant_id по user_id (admin lookup)
resolved = await get_tenant_id_by_admin(user_id)
_ctx_tenant_id.set(resolved if resolved else tenant_id)
```

### Если клиент хочет свой бот
1. Клиент создаёт бота через @BotFather
2. Записываем токен в БД:
```sql
UPDATE tenants SET bot_token = 'TOKEN' WHERE slug = 'SLUG';
```
3. Рестарт контейнера — новый polling loop запустится автоматически

### Если клиент на общем боте
Ничего не делать — работает автоматически через `chat_id → tenant_id` lookup.

---

## 6. Проверка после онбординга

```bash
# Проверяем данные в БД
ssh root@5.42.98.2 "cd /opt/ebidoebi && docker compose exec -T postgres psql -U ebidoebi -d ebidoebi -c \"
SELECT t.id, t.name, t.slug, t.status,
       (SELECT count(*) FROM iiko_credentials WHERE tenant_id = t.id) as branches,
       (SELECT count(*) FROM tenant_chats WHERE tenant_id = t.id) as chats,
       (SELECT count(*) FROM tenant_users WHERE tenant_id = t.id) as users
FROM tenants t WHERE t.slug = 'SLUG';
\""
```

**Тест-чеклист:**
- [ ] Бот отвечает на `/статус` из группы клиента → показывает ТОЛЬКО его точки
- [ ] `/отчёт` из группы клиента → видит только его данные
- [ ] Алерты опоздания → приходят в правильные городские группы
- [ ] Клиент не видит данные Ёбидоёби (изоляция тенантов)
- [ ] Ёбидоёби не видит данные клиента
- [ ] `/поиск ТЕЛЕФОН` → находит заказы клиента (проверить телефон из его базы)
- [ ] В настройках чата `/доступ` — показываются только его города, не Барнаул/Томск
- [ ] Недоступные модули помечены 🔒, при нажатии — "Недоступно в тарифе"
- [ ] `orders_raw` для веток клиента имеют правильный `tenant_id` (не 1!)

**Диагностика изоляции:**
```sql
-- Все данные клиента в orders_raw правильно изолированы?
SELECT tenant_id, COUNT(*) FROM orders_raw
WHERE branch_name IN ('Ветка_1', 'Ветка_2')
GROUP BY tenant_id;
-- Должна быть ТОЛЬКО одна строка с tenant_id клиента

-- daily_stats правильно изолирован?
SELECT tenant_id, COUNT(*) FROM daily_stats
WHERE branch_name IN ('Ветка_1', 'Ветка_2')
GROUP BY tenant_id;
```

---

## 7. Что идёт в автоматический онбординг (следующий шаг)

Веб-форма регистрации (`/register`) уже есть. Нужно добавить/проверить:

| Шаг | Сейчас | Автоматически |
|-----|--------|---------------|
| Создание тенанта | SQL вручную | ✅ `/api/register` |
| Подключение iiko | SQL вручную | ⚠️ `/api/test-iiko` есть, dept_id не получает авто |
| Добавление чатов | SQL вручную | ❌ нужно через бот `/подключить группу` |
| Бэкфилл | вручную | ❌ нужен фоновый job при активации |
| Пароль кабинета | bcrypt вручную | ✅ генерируется при регистрации |
| Оплата | вручную (`status='active'`) | ⚠️ ЮKassa подключена, не активирована |

### Что нужно доработать для полного автоматического онбординга

1. **dept_id из iiko** — при регистрации/тест-подключении iiko авто-запрашивать `/api/corporation/departments` и сохранять `dept_id`
2. **Добавление чатов через бот** — команда `/подключить` или автодетект при добавлении бота в группу (частично есть)
3. **Бэкфилл при активации** — фоновый job `backfill_on_activation` при `status: trial → active`
4. **Google Sheets** — автошаринг через Service Account при указании Sheet ID

---

## 8. SSH и доступ к VPS

```
VPS: 5.42.98.2
User: root
SSH key: ~/.ssh/cursor_arkentiy_vps
```

```bash
# Проверка доступа
ssh -i ~/.ssh/cursor_arkentiy_vps -o BatchMode=yes -o ConnectTimeout=10 root@5.42.98.2 "echo OK"

# Если не работает — key не в authorized_keys. Добавить через Timeweb Консоль:
cat ~/.ssh/cursor_arkentiy_vps.pub
# Вставить в консоль Timeweb:
echo "PUBKEY" >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
```

---

## 9. Журнал онбордингов

| Дата | Клиент | Города | Статус | Примечания |
|------|--------|--------|--------|------------|
| 02.03.2026 | Шабуров (slug: shaburov) | Канск, Зеленогорск, Ижевск | ✅ Active | Общий бот. Ижевск: OLAP v2 timeout |

---

## Быстрый старт следующего онбординга (7 шагов)

```
1. Собрать данные по таблице раздела 0
2. Получить dept_id для каждой точки (раздел 0)
3. Сгенерировать bcrypt-hash пароля (раздел 2)
4. Заполнить SQL-шаблон (раздел 1) → запустить на VPS
5. Бэкфилл daily_stats (раздел 4A)
6. Бэкфилл orders_raw (раздел 4B) + проверить tenant_id
7. Проверить по чеклисту (раздел 6)
```

Время: **~30-60 минут** при подготовленных данных (бэкфилл идёт в фоне).

**Типичные проблемы первого дня (из опыта Шабурова):**
| Симптом | Причина | Решение |
|---------|---------|---------|
| Бот молчит | Новый параметр в `Settings` отсутствует на VPS | Сравнить `config.py` local vs VPS, добавить недостающее |
| Чужие города в настройках чата | `tenant_cities` не передаётся в `cfg` | Проверить `get_access_config_from_db()` и `_resolve_tenant_id()` |
| `/поиск` возвращает чужие заказы | Нет фильтра по веткам когда `city_filter=None` | Проверить fallback в `_handle_search` |
| `/поиск` ничего не находит | `orders_raw` имеют `tenant_id=1` | SQL UPDATE (раздел 4B) |
| `/опоздания` показывает старые заказы | Нет фильтра `overdue_min > LATE_MAX_MIN` | Проверить `_handle_late` и `_handle_pickup` |

---

## ⚠️ Частые проблемы (Troubleshooting)

> На основе опыта с Shaburov (tenant_id=3). Полный разбор — `rules/integrator/lessons.md`.

### 1. Events API пишет заказы в неправильный `tenant_id`

**Симптом:**
```sql
SELECT DISTINCT tenant_id, COUNT(*) FROM orders_raw
WHERE branch_name IN ('Канск', 'Зеленогорск') GROUP BY tenant_id;
-- Возвращает: tenant_id=1 (НЕПРАВИЛЬНО!)
```
**Причина:** `BranchState` не содержит `tenant_id` → дефолт =1.  
**Решение:**
1. Убедиться что `BranchState` содержит `tenant_id`, передаётся из конфига ветки
2. Исправить данные в БД: `UPDATE orders_raw SET tenant_id = N WHERE branch_name IN (...) AND tenant_id = 1;`
3. Перезапустить контейнер после правки БД (in-memory кэш иначе перезапишет фикс)

---

### 2. Временные поля полностью пусты

**Симптом:** `opened_at` 8.9%, `cooked_time` 0%, `ready_time` 0%  
**Причина:** Events API не логировал время; OLAP не настроен или нет Phase 6 backfill.  
**Решение:** Запустить Phase 6 backfill (`app/onboarding/phase6_enrich_times.py`). Ожидаемый результат: 90%+ для всех.

---

### 3. OLAP field names неправильные

**Симптом:** `400 Bad Request: Field not found: Delivery.ReadyTime`  
**Правильные имена:**
| Поле | Правильное имя | НЕ ИСПОЛЬЗОВАТЬ |
|------|---------------|-----------------|
| Время готовки | `Delivery.CookingFinishTime` | `Delivery.CookingTime` |
| Время выдачи | `Delivery.BillTime` | `Delivery.ReadyTime` |
| Время открытия | `OpenTime` (root) | `Delivery.OpenTime` |
| Плановое | `Delivery.ExpectedTime` | `Delivery.ExpectedDeliveryTime` |

---

### 4. opened_at отсутствует на 90%+ заказов

**Причина:** Events API не логировал `opened_at` до подписки.  
**Решение:** Phase 8 recovery — восстановить из OLAP поля `OpenTime`. Ожидаемый результат: 100%.

---

### 5. `/статус` не показывает данные финансов

**Причина:** `get_branch_olap_stats()` вызывается без tenant context → использует hardcoded branches.  
**Решение:** Передавать ветки текущего тенанта явно: `get_branch_olap_stats(today, branches=tenant_branches)`.

---

### 6. `/повара` показывает 0 поваров для города

**Симптом:** Один из городов показывает 0 поваров, хотя они есть.  
**Причина:** Роли поваров (`ПС-АБ`, `ПБТ`) не попали в `_COOK_ROLE_PREFIXES`.  
**Решение:** Проверить реальные `roleName` через Events API, добавить в префиксы в `arkentiy.py`.

---

### Метрики качества данных (целевые)

| Поле | Минимум | Норма |
|------|---------|-------|
| `opened_at` | 90% | 100% (после Phase 8) |
| `cooked_time` | 85% | 92%+ |
| `ready_time` | 85% | 92%+ |
| `cooking_duration` | 85% | 92%+ |
| `total_duration` | 95% | 100% |

Если ниже минимума — остановить и отладить до запуска следующей фазы.

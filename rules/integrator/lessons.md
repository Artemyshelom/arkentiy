# Уроки и баги — Интегратор

> Всё что шло не так. Читать перед работой — экономит часы отладки.

---

## 🔴 [BUG] Daily Report: неправильный парсинг ISO timestamps (коррекция 4 марта 2026)

**Проблема:** Отчёт показывал:
- `Готовка: -5.7 мин` (отрицательное число!)
- `В пути: 1089.9 мин` (более 18 часов — явно ошибка)
- `Опозданий: нет данных`

**Причина:** В `app/database_pg.py` функции `aggregate_orders_for_daily_stats` неправильно парсились ISO timestamps:
```sql
-- НЕПРАВИЛЬНО:
EXTRACT(EPOCH FROM (cooked_time::timestamp - REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')::timestamp)) / 60

-- PostgreSQL не может конвертировать '2026-02-01T16:05:38.187' через ::timestamp
-- REPLACE() на текст и потом ::timestamp работает неправильно для миллисекунд
```

**Решение:** Использовать `TO_TIMESTAMP()` с явным форматом:
```sql
-- ПРАВИЛЬНО:
EXTRACT(EPOCH FROM (
  TO_TIMESTAMP(cooked_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
  - TO_TIMESTAMP(opened_at, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
)) / 60
```

**Применено:** Commit `1a86270` в `app/database_pg.py` линии 1104–1146

**Результат:** Завтрашний отчёт должен показать корректные времена (примерно 10–15 мин готовка, 50–60 мин доставка)

**Lesson:**  ISO datetime strings с миллисекундами (`.187`) требуют `TO_TIMESTAMP(..., format)`, не `::timestamp`

---

## ~~🟢 [ПРАКТИКА] 5-фазовый бэкфилл `orders_raw` через OLAP v2~~ ⚠️ АРХИВ

> Устарело в сессии 74. Сейчас используй `app/onboarding/backfill_new_client.py` — 5 шагов автоматически. Детали: `docs/onboarding/protocol.md` раздел 4.

---

## 🟢 [ПРАКТИКА] Dimension-поля в OLAP v2: `Delivery.ExpectedTime` и `Delivery.CustomerName` (Phase 4–5)

**Проблема:** Были уверены что `planned_time` невозможно получить из OLAP, оказалось неверно.

**Решение:** Dimension-поля `Delivery.ExpectedTime` и `Delivery.CustomerName` работают в OLAP v2, но ТОЛЬКО:
- В `groupByRowFields`, не в `aggregateFields`
- С `reportType: DELIVERIES`, не `SALES`

**Phase 4 — `Delivery.ExpectedTime` для `planned_time`:**
```python
OLAP_PLANNED_FIELDS = [
    "Delivery.Number",
    "Department", 
    "Delivery.ExpectedTime",  # ← ISO datetime: "2026-02-10T11:55:00"
]

body = {
    "reportType": "DELIVERIES",  # ← важно
    "buildSummary": "false",
    "groupByRowFields": OLAP_PLANNED_FIELDS,
    "aggregateFields": ["DishDiscountSumInt"],
    "filters": _date_filter(str(week_start), str(week_end)),
}
```
**Результат:** 7301/7301 заказов (100%) получили `planned_time`.

**Phase 5 — `Delivery.CustomerName` для `client_name`:**
```python
OLAP_CLIENT_NAME_FIELDS = [
    "Delivery.Number",
    "Department",
    "Delivery.CustomerName",  # ← "Павличенко Евгения" или "GUEST649036"
]

body = {
    "reportType": "DELIVERIES",  # ← важно
    "buildSummary": "false",
    "groupByRowFields": OLAP_CLIENT_NAME_FIELDS,
    "aggregateFields": ["DishDiscountSumInt"],
    "filters": _date_filter(str(week_start), str(week_end)),
}

# В коде: пропускаем GUEST*
if not name.startswith("GUEST"):
    client_name_map[(dept, str(int(num)))] = name
```
**Результат:** 7047/7301 заказов (~96%) получили имя (254 пропущены — анонимные).

**Ключевой урок:** Dimension-поля в OLAP v2 часто доступны, но требуют правильного `reportType` и группировки. Всегда проверяй в заголовке ответа какие поля доступны, не верь на слово что "невозможно".

---

## 🟡 [ОСТОРОЖНО] OLAP: `Delivery.Address` часто пусто или "Нет улицы" — это норма

**Наблюдение:** При бэкфилле Шабурова видно:
- ~23% адресов вообще пусто в OLAP v2
- ~69% из оставшихся содержат "ул. Нет улицы" (агрегаторы, служба доставки)
- Остаток (~31%) — нормальные адреса (ул. Некрасова, ул. Мира и т.д.)

**Причина:** iiko не передаёт адрес для:
- Заказов через агрегаторы (например, Яндекс.Еда отправляет сами)
- Заказов с независимой доставкой (ТК берут свой адрес)
- Технических заказов, пресетов

**Вывод:** Это **не ошибка бэкфилла** — это реальные данные. Не нужно ничего менять. Адреса, которые есть, приходят корректно.

---

## 🟢 [ПРАКТИКА] WaiterName = курьер в OLAP v2 доставки

**Поле:** `WaiterName` в `groupByRowFields` OLAP v2 (reportType=SALES).  
**В контексте доставки:** это курьер, который доставил заказ.  
**Проверено:** Канск, Зеленогорск (март 2026). Работает через `Delivery.Number` + `WaiterName` + `Department`.

```python
OLAP_ORDER_FIELDS = [
    "Delivery.Number", "Department", "Delivery.CustomerPhone",
    "Delivery.CancelCause", "Delivery.ActualTime",
    "Delivery.Address", "Delivery.ServiceType",
    "WaiterName",   # ← курьер доставки
]
# Результат: row.get("WaiterName") → имя курьера
```

**Важно:** `Delivery.ExpectedDeliveryTime` — НЕ существует (400 Unknown field на Канске и Зеленогорске).

**Правильное поле для planned_time: `Delivery.ExpectedTime`** — работает как dimension:
```python
OLAP_PLANNED_FIELDS = ["Delivery.Number", "Department", "Delivery.ExpectedTime"]
body = {
    "reportType": "DELIVERIES",  # НЕ SALES
    "groupByRowFields": OLAP_PLANNED_FIELDS,
    "aggregateFields": ["DishDiscountSumInt"],
    ...
}
# row.get("Delivery.ExpectedTime") → "2026-02-10T11:55:00"
```
Проверено: Канск, Зеленогорск, март 2026.

---

## 🟢 [ПРАКТИКА] OLAP v2: `Delivery.CustomerName` для имени клиента (Phase 5)

**Поле:** `Delivery.CustomerName` — dimension-поле в OLAP v2, работает как `groupByRowFields`:
```python
OLAP_CLIENT_NAME_FIELDS = ["Delivery.Number", "Department", "Delivery.CustomerName"]
body = {
    "reportType": "DELIVERIES",
    "groupByRowFields": OLAP_CLIENT_NAME_FIELDS,
    "aggregateFields": ["DishDiscountSumInt"],
}
# row.get("Delivery.CustomerName") → "Павличенко Евгения" или "GUEST12345"
```

**Нюансы:**
- Возвращает "GUEST" + ID для анонимных заказов (не нужны нам)
- Проверено: Канск, Зеленогорск (март 2026)
- Phase 5 обновила 7047 заказов (~96% от всех)

---

## 🔴 [КРИТИЧЕСКИЙ] OLAP v2: `OpenDate` в `groupByRowFields` ломает `Delivery.Number`

**Симптом:** `Delivery.Number` возвращается как `null` для всех строк. `/поиск` не работает, так как в таблице нет номеров заказов.  
**Причина:** На некоторых версиях iiko-сервера (например, Канск) добавление `OpenDate` в `groupByRowFields` приводит к тому, что агрегация по `Delivery.Number` перестаёт работать → поле null.

**Решение:** `OpenDate` НЕ добавлять в `groupByRowFields`. Дату берём из параметра фильтра (`filters.openDate.from`), передаём явно при парсинге.

**Правило:**
```python
# ❌ НЕЛЬЗЯ
OLAP_GROUP_FIELDS = ["Delivery.Number", "OpenDate", ...]  # Delivery.Number станет null

# ✅ МОЖНО — дата из фильтра
OLAP_GROUP_FIELDS = ["Delivery.Number", "Department", "Delivery.CustomerPhone", ...]
order_date = current_date  # из параметра цикла
```

---

## 🟡 [ОСТОРОЖНО] Данные нового клиента могут записываться с `tenant_id=1`

**Симптом:** `/поиск` не находит заказы нового клиента за первые дни работы.  
**Причина:** При старте Events API (real-time обновления заказов) новый клиент начинает писать в `orders_raw` ДО того, как маппинг `chat_id → tenant_id` прогрузился, или код не передаёт `tenant_id` явно → INSERT с дефолтным `tenant_id=1`.

**Диагностика:**
```sql
SELECT tenant_id, branch_name, COUNT(*) FROM orders_raw
WHERE branch_name IN ('Канск_1 Сов', 'Зеленогорск_1 Изы')
GROUP BY tenant_id, branch_name;
```

**Фикс:**
```sql
UPDATE orders_raw SET tenant_id=3
WHERE branch_name IN ('Канск_1 Сов','Зеленогорск_1 Изы','Ижевск_1 Авт') AND tenant_id=1;
```

**Правило:** После онбординга нового клиента — **всегда** проверить `orders_raw` на `tenant_id=1` для его веток.

---

## 🟡 [ОСТОРОЖНО] Ижевск (`yobidoyobi-izhevsk.iiko.it`) — OLAP v2 таймаутит

**Симптом:** Запрос OLAP v2 к Ижевску висит 60+ секунд → `httpx.ReadTimeout`.  
**Причина:** Сервер iiko для Ижевска медленный или на нём нет исторических данных.

**Решение при бэкфилле:** добавить в `SKIP_CITIES = {"Ижевск"}` и пропускать. Для Ижевска нужно уточнить у клиента правильный адрес сервера и `dept_id`.

---

## 🔴 [КРИТИЧЕСКИЙ] `/опоздания` показывает устаревшие/отменённые заказы из кэша

**Симптом:** В `/опоздания` висит заказ, который уже отменён. Отображается часами.  
**Причина:** Заказ хранится в in-memory `_states` со статусом "Новая" — событие отмены было пропущено (бот лежал или событие потерялось). Команда `/опоздания` не фильтровала заказы старше `LATE_MAX_MIN` (60 минут).

**Фикс:**
```python
# _handle_late и _handle_pickup в arkentiy.py
overdue_min = (now_local - planned_dt).total_seconds() / 60
if overdue_min <= 0 or overdue_min > LATE_MAX_MIN:  # добавить вторую часть
    continue
```

**Правило:** Любой список "активных опозданий" должен иметь верхний порог по времени. 60 минут — разумный максимум.

---

## 🔴 [КРИТИЧЕСКИЙ] Мультитенант: `get_daily_stats` без `tenant_id` → падает для других клиентов

**Симптом:** `/отчет` работает для основного тенанта (tenant_id=1), но не работает для других клиентов (tenant_id>1). Показывает "нет данных" хотя они есть в БД.  
**Причина:** Функции `get_daily_stats()` и `get_period_stats()` вызываются БЕЗ явной передачи `tenant_id` → используется значение по умолчанию `tenant_id=1`, даже если текущий контекст — другой тенант (например, tenant_id=3 для Шабурова).

**Масштаб:** Проявляется только при мультитенант-архитектуре. Первый внешний клиент (Шабуров, 02.03.2026) сразу натолкнулся на эту проблему.

**Код-антипаттерн:**
```python
# ❌ НЕПРАВИЛЬНО — всегда tenant_id=1
ds = await get_daily_stats(name, date_from)  
ds = await get_period_stats(name, date_from, date_to)
```

**Правильно:**
```python
# ✅ ПРАВИЛЬНО — используем текущий tenant из контекста
from app.ctx import ctx_tenant_id
_tid = ctx_tenant_id.get()
ds = await get_daily_stats(name, date_from, tenant_id=_tid)
ds = await get_period_stats(name, date_from, date_to, tenant_id=_tid)
```

**Фикс:**
1. В `_build_branch_report()` передавать `tenant_id` в оба вызова `get_daily_stats()` / `get_period_stats()`
2. В `_build_city_aggregate()` то же самое
3. В `get_available_branches()` добавить fallback на `settings.branches` если кэш пуст для тенанта (он может быть не заполнен при старте)
4. В `run_polling_loop()` явно загружать кэш точек для этого тенанта при старте

**Урок:** При мультитенант-архитектуре — **НИКОГДА** не полагаться на значения по умолчанию. Всегда:
- Брать `tenant_id` из `ctx_tenant_id`
- Передавать его явно во все функции БД
- Проверять в юнит-тестах (если будут) что функция срабатывает для разных тенантов

---

## 🔴 [КРИТИЧЕСКИЙ] Дублирование файлов и импорты после реорганизации

**Симптом:** `ImportError: cannot import name 'access' from 'app'` или бот не отвечает.  
**Причина:** После переноса `access.py` в `app/services/` остались:
1. **Дубликат** — `app/jobs/access_manager.py` (orphaned) при работающем `app/services/access_manager.py`
2. **Старые импорты** — `from app import access` вместо `from app.services import access`

**Масштаб:** Исправляли 3 раза. Каждый раз — потеря времени и токенов.

**ПРАВИЛА (нарушение = поломка):**
| Что | Правило |
|-----|---------|
| Путь access | Только `app/services/access.py`. Никогда `app/access.py` |
| Путь access_manager | Только `app/services/access_manager.py`. **Нет** `app/jobs/access_manager.py` |
| Импорт access | `from app.services import access`. **Нет** `from app import access` |
| Перед git push | `rg "from app import access" app/` — должен быть пусто |
| Перед git push | `find app -name "access_manager.py"` — только `app/services/access_manager.py` |

**Фикс:**
```bash
# Удалить orphaned дубликат
git rm app/jobs/access_manager.py

# Заменить во всех файлах
from app import access          → from app.services import access
from app import access as _access → from app.services import access as _access
```

**Урок:** При реорганизации (перемещении в services/, clients/, onboarding/) — ОДИН РАЗ обновить ВСЕ импорты и УДАЛИТЬ orphaned дубликаты. Проверить перед коммитом. Не пушить пока `rg "from app import"` не пусто по этим модулям.

---

## [АНТИПАТТЕРН] SCP локального файла поверх VPS-файла без сравнения

**Симптом:** после деплоя бот ведёт себя по-старому или хуже — новая функциональность есть, но работает неправильно.  
**Причина:** локальная версия файла отстаёт от VPS-версии. Например, на VPS уже обновлена `_format_order_compact`, а локально — нет. SCP перезаписывает обновлённый код устаревшим.  
**Пример (01.03.2026):** `jobs/arkentiy.py` — на VPS была новая `_format_order_compact` (🛵/🚶), локально — старая (👤/💰). SCP залил старую поверх новой. Формат сломался.  
**Фикс:**
```bash
# Перед SCP — всегда сравни
ssh ... "cat /opt/ebidoebi/app/jobs/arkentiy.py" > /tmp/vps_arkentiy.py
diff /tmp/vps_arkentiy.py app/jobs/arkentiy.py
```
**Урок:** Локальная версия ≠ VPS-версия. **Всегда.** При SCP нужно сначала посмотреть diff, потом копировать хирургически — только изменённые фрагменты, а не файл целиком.

---

## [АНТИПАТТЕРН] Мигрировать данные без перезапуска сервиса

**Симптом:** данные в БД есть, но бот их не видит — показывает старые значения или ведёт себя как будто таблица пустая.  
**Причина:** in-memory кэш (`_db_cfg` в `access.py`) заполняется **один раз при старте** через `get_access_config_from_db()`. Если данные добавлены в БД после запуска — кэш об этом не знает.  
**Фикс:** после любой ручной миграции данных — перезапустить сервис: `docker compose restart app`.  
**Урок:** миграция данных в БД ≠ обновление состояния живого сервиса. Сервис читает БД при старте, а дальше живёт в памяти.

---

## [АНТИПАТТЕРН] Не мигрировать tenant_chats при переходе SQLite → PG

**Симптом:** `/доступ` показывает «Чатов: 0», хотя в боте настроены группы.  
**Причина:** `tenant_chats` — оперативная таблица с настройками доступа, скрипт базовой миграции её не захватил.  
**Фикс:** после миграции явно проверять и переносить: `tenants`, `tenant_users`, `tenant_chats`, `tenant_modules`.  
**Урок:** при любом переносе БД — пробежаться по всем таблицам `tenant_*`, не только по данным заказов.

---

## asyncpg — Строгая типизация параметров

### [АНТИПАТТЕРН] Передавать ISO-строки вместо `datetime.date` / `datetime.datetime`

**Симптом:** `invalid input for query argument $N: '2026-02-26' ('str' object has no attribute 'toordinal')`  
**Причина:** asyncpg строго проверяет типы. `DATE` колонка ожидает `datetime.date`, `TIMESTAMPTZ` — `datetime.datetime` с tzinfo. ISO-строки не принимаются.  
**Фикс:** всегда конвертировать перед передачей:
```python
import datetime as _dt
def _to_date(s: str | None) -> _dt.date | None:
    return _dt.date.fromisoformat(s) if s else None

datetime.fromisoformat(ts_str.replace("Z", "+00:00"))  # для TIMESTAMPTZ
```
**Урок:** при портировании SQLite→PG добавляй `_to_date()` ко ВСЕМ параметрам DATE-колонок. Grep: `date = \$N` и `date < \$N`.

---

### [АНТИПАТТЕРН] `date::text = $N` для сравнения date-колонки со строкой

**Симптом:** PG не принимает строку как DATE, даже если в WHERE стоит `date = $1`.  
**Фикс:** либо `date::text = $1` (строка), либо `date = $1` с `_to_date(s)` (дата).  
**Урок:** `date::text = $1` — безопаснее когда пришла строка ISO и не хочется парсить. Но `date = $1` с `_to_date()` — правильнее семантически и использует индекс.

---

### [АНТИПАТТЕРН] SQLite BACKEND guards блокируют всю функциональность в PG

**Симптом:** модуль молча возвращает пустой результат или `return`, пользователь видит «ничего не найдено».  
**Причина:** при миграции SQLite→PG добавлялись guards `if BACKEND != "sqlite": return`.  
**Фикс:** портировать все SQLite-запросы на asyncpg, убрать guards.  
**Урок:** guards — временный костыль. Каждый добавленный guard — задолженность которую придётся платить.

---

### [АНТИПАТТЕРН] `?` плейсхолдеры в PG

**Симптом:** `asyncpg.exceptions.PostgresSyntaxError` при выполнении запроса с `?`.  
**Причина:** PostgreSQL использует `$1, $2, ...`, SQLite — `?`.  
**Фикс:** функция `_to_pg_sql()` в `marketing_export.py` конвертирует `?`→`$N` и булевы 0/1→true/false.

---

## iiko — Импорт банковских выписок (1CClientBankExchange)

### [ОГРАНИЧЕНИЕ iiko] «КоррСчет» и «Счёт» не контролируются из файла

**Симптом:** `КоррСчет=2.2.11.8` прописан в синтетическом документе, но iiko подставляет другое значение или игнорирует.  
**Причина:**  
- `КоррСчет` в формате 1CClientBankExchange — это **межбанковский корреспондентский счёт** (банка), а не счёт в плане счётов iiko. iiko его игнорирует при маппинге.  
- «Счёт» (левая часть проводки) iiko определяет **по маппингу р/с → iiko-счёт** в разделе «Финансы → Банковские счёта». Из файла это поле не управляется.  
- «Корр. счёт» (правая часть проводки) iiko определяет **по контрагенту** — запоминает после первой ручной установки.

**Фикс:**  
1. Завести в файле нового контрагента (уникальный ИНН, которого ещё нет в iiko).  
2. При первой загрузке iiko спросит счета → выставить нужные вручную → iiko запомнит.  
3. «Счёт списания» (левый счёт) — зависит от р/с в документе. Если нужен другой счёт — нужен другой р/с в файле (теоретически — виртуальный банковский счёт в iiko, но в текущих версиях это не поддерживается).

**Урок:** не пытаться управлять планом счётов iiko через поля 1CClientBankExchange. Только `КоррСчет` косвенно влияет через память по контрагенту. Для «Счёт списания» — только ручная настройка при первой загрузке нового контрагента.

---

### [БАГ] Лишний файл при межфилиальном переводе в выписке

**Симптом:** выписка по Абакану-1/2 → бот генерирует дополнительный файл Барнаул-1.  
**Причина:** в выписке содержится документ «перевод с Абакана-1 на Барнаул-1». `split_by_branch` видел р/с Барнаула в документе и считал его «своим».  
**Фикс:** фильтруем `our_accounts` только теми р/с, которые указаны в заголовке `СчетДебета`/`СчетКредита` блока `СекцияРасчСчет`. Если р/с нет в заголовке выписки — файл не создаём.

---

## iiko BO Events API

### [КРИТИЧЕСКИЙ] Событие вне порядка → неверный финальный статус заказа
**Симптом:** "33 новых заказа" когда реально 3-4. Закрытые заказы показываются как активные.  
**Причина:** iiko BO Events API возвращает события **не в хронологическом порядке**. `Закрыта` может прийти в XML до `В пути к клиенту` даже если физически позже.  
**Масштаб:** ~30-40% заказов имели неверный статус до фикса.  
**Фикс:**
```python
events_sorted = sorted(events_xml, key=lambda ev: ev.findtext("date", ""))
for ev in events_sorted:
    ...  # теперь обрабатываем в правильном порядке
```
**Урок:** всегда сортировать события по дате. Без этого вся логика статусов — мусор.

---

### [КРИТИЧЕСКИЙ] deliveryOrderEdited — overwrite → потеря курьера
**Симптом:** у всех курьеров 0 доставленных заказов (Колчеданцева Елизавета — 0 вместо 13).  
**Причина:** событие `deliveryOrderEdited` содержит только изменившиеся атрибуты. Если перезаписывать весь dict — теряем `deliveryCourier`, `deliverySum` и т.д.  
**Фикс:** merge strategy:
```python
existing = state.deliveries.get(num, {})
if attrs.get("deliveryCourier"):
    existing["courier"] = attrs["deliveryCourier"]
# ... не пересоздаём dict!
state.deliveries[num] = existing
```
**Урок:** при event-sourcing — всегда патчить существующий объект, не заменять целиком.

---

### Расхождение имён курьеров (опечатки в iiko)
**Симптом:** курьер есть в сессии, но заказы не привязываются (0 вместо 8+).  
**Причина:** "Кузницов Кирилл" (deliveryCourier) ≠ "Кузнецов Кирилл" (persSessionOpened.userName). Опечатки в iiko.  
**Фикс:** fuzzy token matching — токенизация + пересечение множеств (см. API_iiko.md).  
**Урок:** имена из двух источников (events vs sessions) почти всегда не совпадают точно.

---

### Роли поваров не распознаются → счётчик занижен
**Симптом:** Томск-1 показывает 1 повар, реально больше.  
**Причина:** роли `ПС-АБ` (Повар Сушист АБТОС) и `ПБТ` не попадали в `_COOK_ROLE_PREFIXES`.  
**Фикс:** добавить `"пс"` и `"пбт"` в префиксы.  
**Урок:** при неверных счётчиках — смотри реальные значения `roleName` из `persSessionOpened`. Коды ролей в каждом городе свои.

---

### cookingStatus — orderNum это INT, не строка
**Проблема:** `cookingStatusChangedToNext.orderNum` — целое число ("201"), а deliveryNumber — "Д-201" или "201".  
**Решение:** связывать через `int(order_num) == int(delivery_num.lstrip("Д-").strip())`.  

---

### /api/employees — не запускать при каждом polling
**Проблема:** запрос возвращает 23к+ сотрудников (~18 МБ XML). При 30-секундном polling это убивает сервер iiko.  
**Решение:** один раз при старте → `_employees_global[bo_url]`.

---

## iiko BO OLAP

### Исторические индивидуальные заказы — недоступны
**Проблема:** нужны данные по каждому заказу за прошлые дни.  
**Исследование (21.02.2026):** проверены все доступные эндпоинты:
- `/api/deliveries`, `/api/orders`, `/api/delivery/list` → 404
- Events API с `dateFrom`/`dateTo` → игнорирует параметры, отдаёт только текущий день
- Все 89 OLAP-пресетов → только агрегаты (1 строка = 1 день × 1 точка)  

**Вывод:** `orders_raw` можно заполнять только real-time (Events API). Исторически — только `daily_stats` из OLAP.  
**Урок:** не тратить время на поиск — проверено полностью.

---

### /api/reports/olap — сломан, не использовать
**Проблема:** любые параметры → 500 NullPointerException. POST → 405.  
**Решение:** `/service/reports/report.jspx?presetId=UUID` + JSESSIONID-cookie.

---

### JSESSIONID — читать из client.cookies, не из resp.cookies
**Проблема:** после POST /j_spring_security_check cookie есть в `client.cookies`, но не в `resp.cookies` (из-за redirect).  
**Фикс:**
```python
async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
    await client.post(...)
    cookie = client.cookies.get("JSESSIONID")  # ← отсюда, не из resp!
```

---

### XML с точками в именах тегов — findtext не работает
**Проблема:** `elem.findtext("DishDiscountSumInt.withoutVAT")` возвращает None — ElementTree трактует точку как разделитель пути.  
**Фикс:**
```python
for child in data_elem:
    if child.tag == "DishDiscountSumInt.withoutVAT":
        value = float(child.text or 0)
```

---

### XML-структура OLAP: данные внутри <data>, не в корне
**Проблема:** `for elem in root: if elem.tag == "Department"` — даёт 0 результатов.  
**Причина:** `<Department>` находится внутри `<data>`, а не на уровне `<report>`.  
**Фикс:**
```python
for data_elem in root.findall("data"):
    dept = data_elem.findtext("Department", "").strip()
```

---

### departmentIds в параметрах OLAP-пресетов не работает
**Проблема:** `&departmentIds=DEPT_ID` не фильтрует — возвращаются все точки.  
**Решение:** получать все → фильтровать клиентски по `<Department>` == `branch["name"]`.

---

### XML заголовок — удалять перед ET.fromstring()
**Проблема:** ответы начинаются с `<?xml-stylesheet ...?>` → ElementTree падает с ParseError.  
**Фикс:**
```python
xml_clean = xml_str.replace('<?xml-stylesheet type="text/xsl" href="report-view.xslt"?>', "")
root = ET.fromstring(xml_clean)
```

---

### Формат даты iiko BO — dd.MM.yyyy
**Проблема:** `2026-02-19` → 400 "Unparseable date".  
**Фикс:** `datetime.now().strftime("%d.%m.%Y")`

---

## iiko Cloud API

### Ключ параметра — organizationIds (множественное число), массив
**Проблема:** `{"organizationId": org_id}` → 400 "organizationIds is null".  
**Фикс:** `{"organizationIds": [org_id]}` — массив, даже если одна организация.

---

### Стоп-лист: productId без имени
**Проблема:** стоп-лист возвращает только `productId`.  
**Решение:** сначала `/nomenclature` → маппинг `{id → name}`.

---

### Delivery orders через Cloud API — не использовать для real-time
**Проблема:** фильтрует по `completeBefore` → ASAP-заказы без срока не попадают → 1-2 вместо десятков.  
**Решение:** iiko BO Events API.

---

## Docker / Деплой

### docker compose restart не применяет изменения кода
**Фикс:** только `docker compose up -d --build`. Restart — только для изменений `.env`.

---

### JSON строки в .env теряются
**Проблема:** `IIKO_ORG_IDS={"key": "val"}` в `.env` не передаётся корректно.  
**Решение:** `secrets/org_ids.json` + монтировать в docker, читать через `Path.read_text()`.

---

### SSH heredoc с кавычками — не работает
**Проблема:** передача Python-кода через `ssh host 'cat > /tmp/x.py << EOF ... EOF'` ломается на одинарных кавычках.  
**Решение:** Write скрипт локально → `scp` → `ssh python /tmp/x.py`.

---

## Telegram

### ID группы — с минусом
**Проблема:** `5160506328` не работает для группы.  
**Фикс:** `-5160506328` (отрицательный для супергрупп).

---

### Бот не может инициировать переписку
**Проблема:** бот не может написать пользователю первым.  
**Решение:** пользователь должен отправить `/start` боту.

---

### HTML parse_mode: символы < > & ломают парсер
**Фикс:** `html.escape(text)` для любых данных из внешних источников перед отправкой.

---

## Мультитенанта и изоляция данных

### [КРИТИЧЕСКИЙ] `/поиск` и `/выгрузка` без явного фильтра утекают данные других тенантов
**Симптом:** когда чат настроен на "все города" и пользователь пишет `/поиск число` без указания города — результаты содержат заказы других клиентов.  
**Причина (поиск):** переменная `city_branch_names` остаётся пустой, SQL-запрос выполняется без фильтра `branch_name = ANY($2)` → возвращаются заказы всех веток всех тенантов.  
**Причина (выгрузка):** `run_export()` не получает текущий `tenant_id`, `build_sql()` не добавляет обязательный фильтр → экспорт содержит клиентов всех тенантов.  
**Фикс (поиск):**
```python
# После логики city_branch_names:
if not city_branch_names:
    city_branch_names = [b["name"] for b in get_available_branches()]  # ctx_tenant_id уже установлен
```
**Фикс (выгрузка):**
```python
# В run_export после parse_query:
from app.ctx import ctx_tenant_id
tenant_branches = get_available_branches()  # текущего тенанта
if not params.get("branch") and not params.get("city"):
    params["_tenant_branch_names"] = [b["name"] for b in tenant_branches]

# В build_sql добавить фильтр во все CTE:
if tenant_branches:
    ph = ",".join("?" * len(tenant_branches))
    conditions.append(f"o.branch_name IN ({ph})")
    args.extend(tenant_branches)
```
**Масштаб:** 100% утечка данных для запросов без явного города.  
**Урок:** любой запрос к `orders_raw` должен иметь обязательный fallback к `get_available_branches()` если пользователь не указал город. `ctx_tenant_id` уже всегда установлен в polling loop, просто нужно его использовать.

---

## Python / Pydantic

### Pydantic v2: class Config и model_config несовместимы
**Фикс:** только `model_config = SettingsConfigDict(...)`, убрать `class Config`.

---

## Таймзона

### Частичные данные при выгрузке в конце дня
**Проблема:** если запись "сегодня" — рабочий день ещё не закончился, данные неполные.  
**Решение:** `yesterday = datetime.now(branch_tz(branch)) - timedelta(days=1)` — по TZ каждой точки.

---

### НЕ хардкодить timezone
**Плохо:** `BARNAUL_TZ`, `now_barnaul()` — удалены.  
**Хорошо:** `branch_tz(branch)` — из `utc_offset` в `branches.json`.

---

## ContextVar и мультитенантность

### asyncio.Task молча глотает AttributeError в `run_polling_loop`
**Симптом:** после деплоя бот вообще не отвечает, но в логах ничего не видно. Logs очищены или пусты.  
**Причина:** главный polling loop — это `asyncio.Task`, который создаётся в `@app.lifespan`. Если внутри `run_polling_loop()` происходит ошибка вне контекста try-except (например, `AttributeError`), она молча исчезает и Task завершается. Polling никогда не начинается, но контейнер считается здоров.  
**Пример (03.2026):** `settings.openclaw_enabled` отсутствовал в VPS `config.py` → строка `_openclaw_enabled = settings.openclaw_enabled` падала с AttributeError → polling loop не запускался → `getUpdates` никогда не вызывалась → бот молчал.  
**Фикс:**
```python
# В run_polling_loop / poll_analytics_bot оборачиваем содержимое в try-except:
async def run_polling_loop():
    try:
        # весь код loop
        ...
    except Exception as e:
        logger.error(f"[polling] Fatal error: {e}", exc_info=True)
        raise  # важно: чтобы Task завершился с ошибкой, видной в docker logs
```
**Урок:** когда бот молчит, сначала проверить `docker compose logs app --tail=100 | grep -i error`. Если `getUpdates` вообще не вызывается — проблема в инициализации, а не в обработке сообщений.

---

### global в `poll_analytics_bot` должна быть ПЕРЕД использованием
**Проблема (Python 3.11+):** строка `global _openclaw_enabled` в конце функции → Python 3.11 выпиливает это (требует объявления ДО использования).  
**Фикс:** переместить `global` объявления в начало функции:
```python
async def poll_analytics_bot(bot_token: str = "", tenant_id: int = 1) -> None:
    global _openclaw_enabled  # ← ДО первого использования!
    _ctx_tenant_id.set(tenant_id)
    ...
```

---

### В main.py lifespan НУЖНО загружать конфиги ВСЕх тенантов, не только тенанта 1
**Проблема:** `get_access_config_from_db(1)` → только Ёбидоёби загружается. Шабуров и другие клиенты не видны в `_access._db_cfg`.  
**Фикс:**
```python
# В lifespan / startup:
_tid_rows = await _pg_pool.fetch("SELECT id FROM tenants WHERE status = 'active'")
for _row in _tid_rows:
    _cfg = await get_access_config_from_db(_row["id"])
    _merged_access["chats"].update(_cfg.get("chats", {}))
    _merged_access["users"].update(_cfg.get("users", {}))
_access.update_db_cache(_merged_access)
```
**Урок:** без этого новые тенанты работают, но в старте приложения не инициализируются → первый запрос к `/доступ` или `/статус` медленнее, потому что нужен первый раз загруз конфига. А если TTL кэша истёк — отказ.

---

### Resolve tenant по chat_id в polling loop
**Паттерн:** каждое сообщение приходит в polling → нужно установить `ctx_tenant_id` ПЕРЕД обработкой.  
**Пример:**
```python
for update in updates:
    cb = update.get("callback_query")
    if cb:
        cb_chat_id = cb["message"]["chat"]["id"]
        tenant_id = get_tenant_id_for_chat(cb_chat_id)  # from _chat_tenant_map
        ctx_tenant_id.set(tenant_id)
        # теперь все функции видят правильный tenant_id
        await handle_callback(...)
```
**Урок:** ContextVar — не глобальное состояние, а **thread-local для async task**. Нужно устанавливать ВСЕ РАЗ перед использованием, иначе будет использовано значение из предыдущего запроса.

---

## Таймзона

### Частичные данные при выгрузке в конце дня
**Проблема:** если запись "сегодня" — рабочий день ещё не закончился, данные неполные.  
**Решение:** `yesterday = datetime.now(branch_tz(branch)) - timedelta(days=1)` — по TZ каждой точки.

---

### НЕ хардкодить timezone
**Плохо:** `BARNAUL_TZ`, `now_barnaul()` — удалены.  
**Хорошо:** `branch_tz(branch)` — из `utc_offset` в `branches.json`.

---

## Алерты и мониторинг

### Технические уведомления — только в личку, не в группу
**Проблема:** алерты "сервер запущен/упал" засоряли рабочий чат.  
**Решение:** `TELEGRAM_CHAT_MONITORING=255968113` (личка Артемия). Группа — только бизнес-отчёты.

---

## 🔴 [КРИТИЧЕСКИЙ] Multi-tenant: забывают передать `tenant_id` в queries

**Проблема:** Множество функций читают из БД БЕЗ фильтра `tenant_id`:

- **`/поиск` (_handle_search)** — показывала заказы ИЗ ЛЮБОГО тенанта (7 SQL запросов)
- **`/точные` (get_exact_time_orders)** — выгружала заказы всех тенантов
- **Другие команды** — потенциально тоже не фильтруют

**Корневая причина:** Дефолтный параметр `tenant_id: int = 1` в хелпер-функциях (`get_module_chats_for_city`, `get_exact_time_orders` и т.д.)

**Решение:**
1. ВСЕГДА добавлять `AND tenant_id = $N` в WHERE clause
2. Когда функция в контексте команды — брать `tenant_id` из `_ctx_tenant_id.get()`
3. Убрать дефолтные значения из хелперов — сделать ошибку если не передан

```python
# ДО (опасно — молчит)
async def get_exact_time_orders(branch_name, date_iso, tenant_id: int = 1) -> list[dict]:
    ...

# ПОСЛЕ (безопасно — бросает ошибку)
async def get_exact_time_orders(branch_name, date_iso, tenant_id: int | None = None) -> list[dict]:
    if tenant_id is None:
        raise ValueError("tenant_id must be specified explicitly")
    ...

# В команде (из контекста)
tenant_id = _ctx_tenant_id.get()
rows = await pool.fetch("SELECT ... WHERE tenant_id = $1 AND ...", tenant_id, ...)
```

**Чеклист для каждой новой функции:**
- [ ] Есть ли `AND tenant_id = ...` в WHERE?
- [ ] Если параметр `tenant_id` — нет ли дефолта?
- [ ] Если команда (из контекста) — используется ли `_ctx_tenant_id.get()`?

---

### Снапшот RT-данных для вечернего отчёта пт/сб
**Проблема:** вечерний отчёт в 00:30 субботы (данные пятницы) — live RT уже другой день, данные недоступны.  
**Решение:**
1. `job_save_rt_snapshot` запускается в 23:50 пт и сб (лок. время)
2. Читает из in-memory BranchState, ничего не запрашивает у iiko
3. Сохраняет в SQLite `daily_rt_snapshot(branch, date, delays_*, cooks_today, couriers_today)`
4. Вечерний отчёт в 00:30 читает снапшот из SQLite

---

## 🔴 [КРИТИЧЕСКИЙ] Events API: заказы нового клиента пишутся в tenant_id=1

**Симптом (Шабуров, март 2026):** Заказы веток Канска, Зеленогорска, Ижевска появлялись в БД с `tenant_id=1`, хотя должны быть `tenant_id=3`. Следовательно, `/поиск` не находил их (фильтр по tenant_id+branch_name не совпадал).

**Корневая причина:** `BranchState` не содержал `tenant_id`. Функция `upsert_orders_batch()` вызывалась с дефолтным `tenant_id=1`:
```python
# ДО (бага)
upsert_orders_batch(order_rows)  # tenant_id=1 по умолчанию

# ПОСЛЕ (фикс)
upsert_orders_batch(order_rows, tenant_id=state.tenant_id)
```

**Масштаб:** 473 заказа за несколько дней были записаны с неправильным `tenant_id`. Часть дублировалась (176 дублей были и в t1 и в t3).

**Фикс (март 2026):**
1. Добавить поле `tenant_id: int = 1` в `BranchState`
2. При инициализации ветки в `poll_all_branches()`:
   ```python
   if name not in _states:
       _states[name] = BranchState(
           bo_url=bo_url,
           branch_name=name,
           bo_login=branch.get("bo_login", ""),
           bo_password=branch.get("bo_password", ""),
           tenant_id=branch.get("tenant_id", 1),  # ← ИЗ ВЕТКИ!
       )
   ```
3. При сохранении в БД в `_save_to_db()`:
   ```python
   await upsert_orders_batch(order_rows, tenant_id=state.tenant_id)
   ```

**Проверка после деплоя:**
```sql
SELECT tenant_id, branch_name, COUNT(*) FROM orders_raw
WHERE branch_name IN ('Канск_1 Сов', 'Зеленогорск_1 Изы', 'Ижевск_1 Авт')
GROUP BY tenant_id, branch_name;
-- ожидаемый результат: все в tenant_id=3 (или нужный тенант)
```

**Урок:** Events API polling хранит состояние в глобальной памяти (`_states`). Если состояние не содержит `tenant_id` — он не может быть передан при сохранении. Всегда включай идентификатор контекста (tenant, user, branch) в каждый объект состояния.

---

## 🟡 [ОСТОРОЖНО] cancel_sync закрывает зависшие заказы ТОЛЬКО если они старше 1 дня

**Симптом (Шабуров, март 2026):** Заказы за вчеру (03.03) зависли в статусе "Новая", "В пути к клиенту", "Доставлена" — не закрывались.

**Корневая причина:** `cancel_sync` имеет две фазы:
- **Фаза 1:** обновляет отменённые заказы (с `CancelCause`) — покрывает сегодня + завтра
- **Фаза 2 (стабилизация):** закрывает зависшие `status NOT IN ('Закрыта', 'Отменена', 'Не подтверждена')` **ТОЛЬКО если `date < stale_cutoff`**, где `stale_cutoff = now_local - 1 день`

Это означает, что вчерашние зависшие **не закроются сегодня**, а закроются только завтра (когда вчера станет позапрошлым днём).

**Логика фильтрации:**
```python
stale_cutoff = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
stale_rows = await pool.fetch("""
    SELECT ... FROM orders_raw
    WHERE date::text < $1  # ← < stale_cutoff, не ≤
      AND status NOT IN ('Закрыта', 'Отменена', 'Не подтверждена')
""", stale_cutoff)
```

Если сегодня 04.03, то `stale_cutoff = '03.03'`, и запрос ловит только `date < '03.03'` (т.е. 02.03 и ранее). Вчерашний день (03.03) не попадает.

**Почему это намеренная логика:** 
- Вчера заказы могут быть ещё активными (система обновляется ночью)
- Гарантия, что день ТОЧНО закончился — только на следующий день

**Решение если срочно нужно закрыть вчерашние:**
```python
# Ручной скрипт (одноразовый)
yesterday = date.today() - timedelta(days=1)
stuck = await pool.fetch("""
    SELECT branch_name, delivery_num FROM orders_raw
    WHERE date = $1 AND status NOT IN ('Закрыта', 'Отменена', 'Не подтверждена')
""", yesterday)

# Для каждого зависшего — проверить в OLAP:
# - Если есть в OLAP без CancelCause → Закрыта (доставлен)
# - Если нет в OLAP → тоже Закрыта (день прошёл)
# - Если с CancelCause → Отменена + cancel_reason
```

**Урок:** Логика "зависшие закрываются со сдвигом на 1 день" — это защита от недоделанных данных за текущий день. При отладке новых клиентов нужно помнить, что вчерашние зависшие нормально закроются завтра, или закрыть вручную если критично.

---

## 🟡 [ОСТОРОЖНО] OLAP v2: поля без CancelCause = заказ завершён (не отменён)

**Симптом (Шабуров, март 2026):** После синхронизации cancel_sync статусы "Новая", "В пути к клиенту", "Доставлена" не изменились на "Закрыта".

**Корневая причина:** `cancel_sync` обновляет ТОЛЬКО заказы с `Delivery.CancelCause` (отменённые). Заказы БЕЗ причины отмены (т.е. успешно доставленные) остаются как есть, их никто не закрывает, потому что cancel_sync предполагает что Events API должен был прислать событие закрытия.

**Логика (Фаза 1 cancel_sync):**
```python
cancelled = await _fetch_cancelled_from_server(bo_url, ...)  # ← только Delivery.CancelCause != null
for c in cancelled:
    if c["branch_name"] in branch_names:
        all_cancelled.append(c)  # обновляем ДО UPDATE status='Отменена'
```

**Проблема:** если Events API был недоступен или перезагружался, события `deliveryOrderClosed` были пропущены → БД остаёт с промежуточным статусом, а OLAP v2 уже показывает что заказ завершён.

**Решение при диагностике:**
1. Запросить OLAP v2 за день с зависшими: какие заказы есть в OLAP?
2. Для каждого зависшего в БД — проверить в OLAP:
   ```python
   if delivery_num in olap_data and not olap_row.get("Delivery.CancelCause"):
       # заказ в OLAP БЕЗ отмены → он доставлен → UPDATE status='Закрыта'
   ```
3. Это можно добавить в Фазу 2 cancel_sync как дополнительную логику.

**Пример результата (Шабуров, 03.03):**
- 24 зависших нашлись в OLAP без `CancelCause` → закрыты как `Закрыта`
- 22 зависших не нашлись в OLAP вообще → тоже `Закрыта` (день прошёл)
- 0 заказов с `CancelCause` (они все уже были в cancel_sync обновлены в Фазе 1)

**Урок:** OLAP v2 — это источник истины для завершённых заказов. Если Events API отступил, OLAP может спасти ситуацию, но нужна логика "если заказа нет в OLAP или он там без причины отмены → закрыть как Закрыта".

---

## 🔴 [КРИТИЧЕСКИЙ] Миграция данных (tenant_id) без перезапуска сервиса = молчаливое несовпадение

**Симптом (Шабуров, март 2026):** Сделали `UPDATE orders_raw SET tenant_id=3 WHERE ...`, но `/поиск` всё равно не находит заказы.

**Причина:** In-memory кэш (`_states` в Events API) содержит заказы с неправильным `tenant_id`. SQL обновилась, а памяти — нет. Новые заказы пишутся правильно (после фикса tenant_id в BranchState), но старые — остаются в памяти с tenant_id=1.

**Фикс:** После ручного UPDATE в БД **обязательно** пересобрать и перезапустить контейнер:
```bash
docker compose build --no-cache app
docker compose up -d
```

**Дополнительный риск:** in-memory `_states` может быть синхронизирован в БД при следующем save cycle — если не перезапустить, он перезапишет наши исправления!

**Урок:** Никогда не обновлять критические данные (tenant_id, status, branch_name) в live системе БЕЗ перезапуска. Production-системы с in-memory кэшем требуют hard restart для гарантии консистентности.

---

## 🔴 [BUG] delay_stats считал опоздания с прошлого дня (март 2026)

**Симптом:** `/статус` показывал ≈326 мин среднего опоздания на 2 заказах (Канск).

**Причина:** `BranchState.delay_stats()` итерировал ВСЕ `deliveries` со статусом "Доставлена"/"Закрыта" без фильтрации по дате. `_states` накапливает заказы с момента последнего full reload (раз в 6 часов). Если full reload происходил в начале дня и iiko вернул события прошлой смены (часть рабочего дня пересекает полночь) — старые заказы с `planned_time` = вчера попадали в расчёт среднего.

**Фикс:** Добавлен date-фильтр в `delay_stats()`:
```python
today_local = (datetime.now(timezone.utc) + timedelta(hours=7)).date()
# ...
planned_date = datetime.fromisoformat(planned...).date()
if planned_date != today_local:
    continue
```

**Урок:** Любая RT-агрегация из `_states` (опоздания, выручка, чеки) должна фильтровать по `planned_time.date() == today_local`. `_states` — NOT day-scoped хранилище.

---

## 🔴 [БЕЗОПАСНОСТЬ] Проверка секретов перед git push

**Контекст:** 2 марта 2026 — OpenClaw токен утёк в публичный репо. GitGuardian поймал.

**Обязательные проверки перед каждым `git push`:**

```bash
# 1. Нет ли токенов в diff:
git diff HEAD~1 | grep -i "token\|password\|secret\|api_key"
# Результат: пусто, или только имена переменных в .env.example

# 2. Нет ли ключей в коде:
rg "IIKO_API_KEY|TELEGRAM_BOT_TOKEN|OPENCLAW_API_TOKEN|GOOGLE.*KEY" app/
# Результат: пусто ✅

# 3. .gitignore актуален:
cat .gitignore | grep -E "\.env|secrets|credentials"

# 4. .env не в staging:
git status | grep "\.env"
# Должно быть пусто (или только .env.example)
```

**Если токен скомпрометирован:**
1. Немедленно регенерировать в соответствующем сервисе
2. `git reset --soft HEAD~1` — откатить коммит
3. `git reset app/файл.py` — убрать файл с токеном из staging
4. Новый коммит без токена

**Урок:** Один утёкший токен = пересоздание всех ключей + инцидент. Проверка занимает 10 секунд, восстановление — часы.

---

## 🔴 [ИНФРАСТРУКТУРА] OpenClaw агент перестал отвечать после создания sub-agent

**Симптом (8 марта 2026):** После создания второго агента (`ops-consultant`) перестал работать основной агент `@murphsmartbot` (`accounts.default`). Сообщения в OpenClaw не получают ответа. Сервис `morf.service` запущен, порт 3000 отвечает.

**Диагностика:**
```bash
ssh morf 'journalctl -u morf.service -n 50 --no-pager'
# или
ssh morf 'PATH=/root/.nvm/versions/node/v22.22.0/bin:$PATH openclaw doctor'
```
Доктор покажет предупреждения о `channels.telegram` или сессиях. В логах при старте видны строки вида:
```
[telegram] [ops-consultant] starting provider (@borissmartbot)  ✅
# НО:
[telegram] [default] starting provider (@murphsmartbot)       ❌  — строки нет!
```

**Корневая причина:** При создании нового агента OpenClaw мигрировал структуру `channels.telegram` и сгенерировал новый `sessions.json` с legacy-ключами. Аккаунт `accounts.default` потерял рабочую сессию и не стартовал.

**Исправление:**
```bash
ssh morf
PATH=/root/.nvm/versions/node/v22.22.0/bin:$PATH openclaw doctor --fix
# Доктор пересоздаёт sessions.json и выводит: "Fixed N issues"

# Перезапустить сервис:
systemctl restart morf.service

# Проверить что оба провайдера стартовали:
journalctl -u morf.service -n 30 --no-pager | grep 'starting provider'
# Ожидаемый результат:
# [telegram] [ops-consultant] starting provider (@borissmartbot)
# [telegram] [default] starting provider (@murphsmartbot)
```

**Если `openclaw doctor --fix` не помогло:**
```bash
# Сбросить сессии вручную
rm /root/.openclaw/sessions.json
PATH=/root/.nvm/versions/node/v22.22.0/bin:$PATH openclaw doctor
systemctl restart morf.service
# При первом подключении OpenClaw попросит повторную авторизацию Telegram
```

**SSH-доступ к серверу:**
- Хост: `72.56.107.85`, алиас `morf`, пользователь `root`
- Ключ: `/Users/artemii/.ssh/id_ed25519` (с паролем) или `cursor_arkentiy_vps` (без пароля)
- Конфиг OpenClaw: `/root/.openclaw/`
- Сервис: `morf.service` (systemd)
- Node: `/root/.nvm/versions/node/v22.22.0/bin/`

**Урок:** После добавления/удаления агентов в OpenClaw **всегда** запускать `openclaw doctor --fix` и проверять что все провайдеры стартуют. Один агент может сломать другой через shared `sessions.json`.

---

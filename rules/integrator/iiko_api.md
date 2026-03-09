# iiko API — Справочник

> Два разных API. Всегда сверяйся сюда перед работой с iiko.

**Полный справочник endpoint'ов iiko BO:** `docs/reference/iiko_bo_api.md`

---

## ПРАВИЛО ВЫБОРА API

```
Стоп-листы, номенклатура              → iiko Cloud API (api-ru.iiko.services)
Все метрики (выручка, COGS, чеки,     → iiko BO OLAP v2 (/api/v2/reports/olap, token-auth, JSON)
  скидки, оплаты, доставка/самовывоз)    ← ОСНОВНОЙ СПОСОБ с 24.02.2026
Аудит скидок (пока)                   → iiko BO OLAP XML-пресеты (/service/, cookie-auth)
Real-time: заказы, статусы, смены     → iiko BO Events API (/api/events)
Отмены (Events API не отдаёт)         → iiko BO OLAP v2 (cancel_sync.py)
```

---

## 1. iiko Cloud API

- **Base URL:** `https://api-ru.iiko.services/api/1/`
- **Auth:** `POST /access_token` с `{"apiLogin": "..."}` → Bearer token, TTL ~1 час
- **Ключ:** `.env` → `IIKO_API_KEY`

**Что работает:**
- `GET /stop_lists` с `{"organizationIds": [org_id]}` — стоп-лист (payload: массив!)
- `GET /nomenclature` — номенклатура (нужна для маппинга productId → name)
- `GET /terminal_groups` — терминалы

**Что НЕ работает** (причина — org_ids настроены как KDS, не кассы):
- `/deliveries/by_delivery_date_and_status` — 0 результатов для ASAP-заказов
- `/reports/olap` — 403
- `/cash_shifts` — 403

**org_ids iiko Cloud (только Барнаул, остальных нет):**
```json
{
  "Барнаул_1 Ана": "0d5c0fd6-61b3-485f-8f04-8c5f05dbb91b",
  "Барнаул_2 Гео": "7780ce8e-3433-4a6e-96c7-50518b9051f3",
  "Барнаул_3 Тим": "a735f031-d404-4b7e-8bfd-b3765b80bee1",
  "Барнаул_4 Бал": "20d13466-e3b7-4c4e-9828-6a193a010512"
}
```

---

## 2. iiko Web BO API (resto)

- **Base URL:** у каждой точки СВОЙ сервер — из поля `bo_url` в `branches.json`
- **Старый общий сервер** `tomat-i-chedder-ebidoebi-co.iiko.it` — НЕ ИСПОЛЬЗОВАТЬ (данные с задержкой)
- **SSL:** всегда `httpx.AsyncClient(verify=False)` — сертификат не проходит стандартную проверку
- **Формат дат:** `dd.MM.yyyy` (российский!) — `19.02.2026`, НЕ ISO

### Два типа аутентификации

| Тип | Endpoint | Для чего |
|-----|---------|---------|
| **Токен** | `GET /api/auth?login=LOGIN&pass=SHA1(PASSWORD)` → UUID | `/api/*` endpoints |
| **Cookie** | `POST /j_spring_security_check` form-data → JSESSIONID | `/service/*` endpoints (OLAP) |

Логин/пароль: `.env` → `IIKO_BO_LOGIN=artemiish`, `IIKO_BO_PASSWORD`

**Единый менеджер:** `app/clients/iiko_auth.py` — кеширует и токены и cookie. Не создавать локальные кеши!

**Cookie-логин (важные детали, только для audit.py):**
```python
async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
    await client.post(f"{base_url}/j_spring_security_check",
                      data={"j_username": login, "j_password": password})
    cookie = client.cookies.get("JSESSIONID")  # читать отсюда, не из resp.cookies!
```
Cookie живёт ~30 мин. SHA1 пароля: `hashlib.sha1(password.encode()).hexdigest()`.

### Работающие /api/ endpoints

```
GET /api/auth?login=LOGIN&pass=SHA1HASH
GET /api/reports/sales?key=TOKEN&department=DEPT_ID&dateFrom=dd.MM.yyyy&dateTo=dd.MM.yyyy  ← НЕ ИСПОЛЬЗУЕТСЯ (выручка из OLAP v2)
GET /api/corporation/departments?key=TOKEN    → список отделов с dept_id
GET /api/employees?key=TOKEN                  → все сотрудники (23к+, ~18 МБ XML!) — только при старте!
GET /api/events?key=TOKEN                     → все события с начала дня
GET /api/events?from_rev=N&key=TOKEN          → события с ревизии N
```

**Не работает:** `/api/reports/olap` (500 NPE), cash_shifts (404), delivery orders (404)

### OLAP v2 API (JSON, программные отчёты)

```
POST /api/v2/reports/olap?key=TOKEN
Content-Type: application/json
```

**Основной способ получения метрик** (с 24.02.2026). Возвращает JSON (не XML). Token auth (не cookie).

**2 запроса заменяют 5 XML-пресетов** (54 → 27 HTTP-запросов, ~1.86с на все 9 серверов).

**Query 1 — core metrics** (1 строка на точку):
```json
{
  "reportType": "SALES",
  "buildSummary": "false",
  "groupByRowFields": ["Department"],
  "aggregateFields": ["DishDiscountSumInt.withoutVAT", "ProductCostBase.Percent",
                       "UniqOrderId.OrdersCount", "DiscountSum"],
  "filters": {
    "OpenDate.Typed": {
      "filterType": "DateRange",
      "periodType": "CUSTOM",
      "from": "2026-02-24",
      "to": "2026-02-25",
      "includeLow": "true",
      "includeHigh": "false"
    }
  }
}
```
Ответ: `{"data": [{"Department": "Абакан_1 Кир", "DishDiscountSumInt.withoutVAT": 166867, "ProductCostBase.Percent": 0.348, "UniqOrderId.OrdersCount": 104, "DiscountSum": 14010}]}`

**Query 2 — payment + delivery breakdown** (~10 строк на точку):
```json
{
  "groupByRowFields": ["Department", "PayTypes", "Delivery.ServiceType"],
  "aggregateFields": ["DishDiscountSumInt", "UniqOrderId"],
  ...
}
```
Ответ: `{"data": [{"Department": "...", "PayTypes": "Наличные", "Delivery.ServiceType": "COURIER", "DishDiscountSumInt": 25106, "UniqOrderId": 20}, ...]}`

**Query 3 — cancel sync** (используется в `cancel_sync.py`):
```json
{
  "groupByRowFields": ["Delivery.Number", "Delivery.CancelCause", "Department"],
  "aggregateFields": ["DishDiscountSumInt"],
  ...
}
```

**Проверенные поля (aggregateFields):**

| Поле | Что даёт | Тип |
|------|---------|-----|
| `DishDiscountSumInt.withoutVAT` | Выручка без НДС | float |
| `ProductCostBase.Percent` | COGS как доля (0.348 = 34.8%) | float |
| `UniqOrderId.OrdersCount` | Кол-во чеков | int |
| `DiscountSum` | Сумма скидок | float |
| `DishDiscountSumInt` | Сумма продаж (с НДС) | float |
| `UniqOrderId` | Кол-во уникальных заказов | int |
| `ProductCostBase.OneItem` | Себестоимость 1 позиции | float |

**Проверенные поля (groupByRowFields):**

| Поле | Что даёт | Пример |
|------|---------|--------|
| `Department` | Точка (филиал) | `Абакан_1 Кир` |
| `PayTypes` | Тип оплаты | `Наличные`, `Сбербанк`, `SailPlay Бонус` |
| `Delivery.ServiceType` | Тип заказа | `COURIER`, `PICKUP` |
| `OrderDiscount.Type` | Название скидки | `10% Ёби, Самовывоз` |
| `Delivery.Number` | Номер заказа | `291735` |
| `Delivery.CancelCause` | Причина отмены | `Отказ гостя` |
| `OrderType` | Тип заказа | `Доставка курьером`, `Доставка самовывоз`, `Доставка Яндекс` |
| `Delivery.SourceKey` | Источник заказа | `EVA iOS`, `EVA Web`, `EVA Android`, `Chibbis`, `EVO`, `yandex_food` |
| `Delivery.PrintTime` | Время печати на кухне | `2026-02-23T10:03:25.499` |
| `Delivery.CookingFinishTime` | Время готовности | `2026-02-23T13:49:33.018` |
| `Delivery.SendTime` | Время отправки курьеру | `2026-02-23T14:01:22.919` |
| `Delivery.ActualTime` | Фактическое время доставки | `2026-02-23T14:18:11.783` |
| `Delivery.BillTime` | Время создания чека | timestamp |
| `Delivery.CloseTime` | Время закрытия | timestamp |
| `Delivery.ConfirmTime` | Время подтверждения | timestamp |
| `WaiterName` | Оператор/официант | `Шумкин Антон` |
| `OpenDate.Typed` | Дата открытия | `2026-02-23` |
| `Storned` | Сторнирован | `TRUE` / `FALSE` |

**Поля для бэкфилла orders_raw (проверено на Канске и Зеленогорске, март 2026):**
| Поле | → колонка orders_raw | Примечание |
|------|---------------------|------------|
| `Delivery.Number` | `delivery_num` | int → str, null = пропускать |
| `Department` | `branch_name` | фильтровать по branch_names |
| `Delivery.CustomerPhone` | `client_phone` | телефон |
| `Delivery.CancelCause` | `cancel_reason` | null = не отменён |
| `Delivery.ActualTime` | `actual_time` | фактическое время |
| `Delivery.Address` | `delivery_address` | адрес доставки, ~23% может быть пусто из OLAP |
| `Delivery.ServiceType` | `is_self_service` | `PICKUP` → true |
| `WaiterName` | `courier` | курьер в контексте доставки |
| `DishName` | `items` (Phase 2) | блюдо, одна строка на позицию |
| `Delivery.CustomerName` | `client_name` (Phase 5) | имя клиента — пропускаются GUEST* |

**⚠️ Важно: `Delivery.CustomerName` — dimension (не measure):**
- Работает ТОЛЬКО в `groupByRowFields`, НЕ в `aggregateFields`
- Использовать `reportType: DELIVERIES`
- Возвращает "GUEST12345" для анонимных — эти фильтруются в Phase 5
- Проверено: Канск, Зеленогорск (март 2026)

**НЕ существуют (400 Unknown OLAP field):**
`Cooking`, `CookingTime`, `FullSum`, `LaborCost`, `Employee.Salary`, `Employee.Rate`,
`Delivery.Duration`, `Delivery.IsLate`, `Delivery.DelayMinutes`, `Delivery.IsSelfService`,
`Delivery.ExpectedDeliveryTime` (это другое поле — несуществующее),
`Delivery.PlannedTime`, `Delivery.DeliveryDate`

**reportType:** `SALES` (основной), `DELIVERIES` (работает), `TRANSACTIONS` (другие фильтры). `EMPLOYEES`, `LABOR` — не существуют.

**Формат дат:** ISO (`YYYY-MM-DD`), **НЕ** `dd.MM.yyyy` как в пресетах! `from` и `to` не должны быть равны (иначе 409).

**Используется:**
- `app/clients/iiko_bo_olap_v2.py` — агрегатные метрики (выручка, COGS, чеки, оплаты, скидки)
- `app/jobs/olap_enrichment.py` — per-order обогащение (payment, discount_type, source, timestamps)
- `app/jobs/cancel_sync.py` — синхронизация статуса "Отменена" каждые 3 мин

---

## 3. iiko BO OLAP-пресеты (DEPRECATED → OLAP v2)

> **DEPRECATED:** Основные потребители (daily_report, iiko_to_sheets, iiko_status_report) переведены на OLAP v2.
> Остаётся только в `audit.py` для анализа скидок.
>
> `/service/reports/report.jspx?presetId=UUID&dateFrom=dd.MM.yyyy&dateTo=dd.MM.yyyy` + Cookie: JSESSIONID
> Возвращает **все точки сразу** → фильтруй клиентски по `<Department>` = `branch["name"]`

**Листинг всех пресетов:**
```
GET /service/reports/report.jspx (без параметров)
```

**Рабочие пресеты:**

| Пресет | ID | Что даёт |
|--------|-----|---------|
| `.API-Статистика.` | `2c0c11d7-48fa-48e6-91b3-26f169587b09` | Выручка (со скидкой без НДС) + COGS% |
| `Общий отчет по доставкам` | `1f56b9d3-13ca-6044-0148-5e7f38cd001f` | Кол-во чеков |
| `Типы оплат` | `5a8842c5-4681-40ce-ba2b-133d39efbb93` | Разбивка по типам оплаты |
| `Отчет по скидкам` | `6a714099-1252-4c8c-a474-9151b79e375a` | Скидки по типам (сумма + типы) |
| `Доставка/самовывоз за месяц` | `81dfa241-55a9-4b0a-b6e7-d4bb48dad9d5` | PICKUP/COURIER разбивка |

**Ключевые XML-поля (у `.API-Статистика.`):**
- Выручка: `DishDiscountSumInt.withoutVAT`
- Себестоимость %: `ProductCostBase.Percent`

**Парсинг XML (критично — точки в тегах):**
```python
# Структура: <report><data><Department>...</Department><Tag.With.Dot>...</data></report>
for data_elem in root.findall("data"):
    dept = data_elem.findtext("Department", "").strip()
    for child in data_elem:
        if child.tag == "DishDiscountSumInt.withoutVAT":   # findtext не работает!
            revenue = float(child.text or 0)
```

**Типы оплат:**
- `Наличные` = наличные
- `SailPlay Бонус` = списанные бонусы лояльности
- `(без оплаты)` = тестовые/служебные → исключать из расчётов
- `Картой при получении`, `СБП`, `Сбербанк`, `Оплата на сайте` = безналичные

---

## 4. iiko BO Events API — Real-time

> Главное открытие. Позволяет получать real-time данные без плагинов на фронт.

### Принцип
```
Полная загрузка:      GET /api/events?key=TOKEN
                      → XML с событиями с начала дня + maxRevision
Инкрементальная:      GET /api/events?from_rev=N&key=TOKEN
                      → только новые события с ревизии N
Polling каждые 30с → инкрементальный поток
Раз в 6 часов → full reload для предотвращения дрейфа
```

### Структура XML события
```xml
<events maxRevision="12345">
  <event type="deliveryOrderEdited" revision="12340">
    <attr name="deliveryNumber" value="Д-001"/>
    <attr name="deliveryStatus" value="OnWay"/>
    <attr name="deliveryCourier" value="Иванов Иван"/>
    <attr name="deliverySum" value="1200.00"/>
    <attr name="deliveryDate" value="2026-02-20T15:30:00"/>        <!-- плановое время -->
    <attr name="deliveryActualTime" value="2026-02-20T15:34:00"/>  <!-- фактическое -->
  </event>
  <event type="persSessionOpened" revision="12341">
    <attr name="userId" value="uuid"/>
    <attr name="userName" value="Петров Пётр"/>
    <attr name="roleName" value="ПС-АБ"/>
    <attr name="openTime" value="2026-02-20T10:00:00"/>
  </event>
  <event type="persSessionClosed" revision="12342">
    <attr name="userId" value="uuid"/>
    <attr name="closeTime" value="2026-02-20T18:00:00"/>
  </event>
  <event type="cookingStatusChangedToNext" revision="12343">
    <attr name="orderNum" value="201"/>       <!-- целое число! -->
    <attr name="cookingStatus" value="Собран"/>  <!-- Приготовлено / Собран -->
  </event>
</events>
```

### Типы событий

| Тип | Когда | Ключевые атрибуты |
|-----|-------|------------------|
| `deliveryOrderCreated` | Создан заказ | deliveryNumber, deliveryStatus, deliveryCourier, deliverySum |
| `deliveryOrderEdited` | Изменён заказ | только изменившиеся поля! (см. merge strategy) |
| `persSessionOpened` | Сотрудник пришёл | userId, userName, roleName, openTime |
| `persSessionClosed` | Сотрудник ушёл | userId, closeTime |
| `cookingStatusChangedToNext` | Смена статуса кухни | orderNum (int!), cookingStatus |

### КРИТИЧНО: сортировка событий
```python
# iiko возвращает события НЕ в хронологическом порядке!
# Без сортировки 30-40% заказов имеют неверный финальный статус.
events_sorted = sorted(events_xml, key=lambda ev: ev.findtext("date", ""))
for ev in events_sorted:
    ...
```

### КРИТИЧНО: merge strategy для deliveryOrderEdited
```python
# deliveryOrderEdited содержит ТОЛЬКО изменившиеся поля — делай merge, не overwrite!
existing = state.deliveries.get(num, {})
attrs = {a.get("name"): a.get("value") for a in ev.findall("attr")}
if attrs.get("deliveryStatus"):
    existing["status"] = attrs["deliveryStatus"]
if attrs.get("deliveryCourier"):
    existing["courier"] = attrs["deliveryCourier"]
# ... и т.д.
state.deliveries[num] = existing
```

### Статусы доставки iiko

| Статус (в API) | Отображение | Активный? |
|----------------|-------------|-----------|
| `Новая` / `Не подтверждена` | Новый | ✅ |
| `Ждет отправки` | Ждёт отправки | ✅ |
| `В пути к клиенту` | В пути | ✅ |
| `Доставлена` | Доставлена | ❌ (закрытый) |
| `Закрыта` | Закрыта (оплачена) | ❌ (закрытый) |
| `Отменена` | Отменена | ❌ (закрытый) |

> **Важно:** "Закрыта" в контексте доставки = чек закрыт, не то же самое что "доставлена физически". Заказ может быть "Доставлена" → "Закрыта". Оба считаются delivered_today.

> **КРИТИЧНО:** Events API **никогда не отправляет статус "Отменена"**. Для отменённых заказов используется OLAP v2 (`cancel_sync.py`), который опрашивает `Delivery.CancelCause` каждые 3 минуты.

### Подсчёт состояния готовности (cookingStatus)
```
WAITING_STATUSES = {"Новая", "Не подтверждена", "Ждет отправки"}
Заказ в cooking_statuses[int(deliveryNumber)] → "Приготовлено" | "Собран"

orders_new:     WAITING + cookingStatus is None (не попал в кухню)
orders_cooking: WAITING + cookingStatus == "Приготовлено"
orders_ready:   WAITING + cookingStatus == "Собран"
```

Связка orderNum (int) с deliveryNumber: `int(order_num) == int(delivery_num)` — в кухонных событиях orderId — целое число без "Д-".

### Классификация ролей сотрудников

```python
_COOK_ROLE_PREFIXES = ("повар", "cook", "пс", "пбт")
_COOK_ROLE_SUBSTRINGS = ("сушист", "кухня", "kitchen")

_COURIER_ROLE_PREFIXES = ("курьер", "courier", "delivery")
_COURIER_ROLE_SUBSTRINGS = ("доставка",)
```

Реальные роли из iiko (примеры):
- `Повар сушист абтос` → cook (через "повар")
- `ПС-АБ` (Повар Сушист АБТОС) → cook (через "пс")
- `ПБТ` (система начисления поваров) → cook (через "пбт")
- `Курьер` → courier
- Не распознана → `None` (игнорируется)

> При неверных счётчиках — всегда смотри реальные коды ролей в `persSessionOpened.roleName`.

### Нечёткое сопоставление имён курьеров
```python
# Проблема: "Кузницов Кирилл" (deliveryCourier) ≠ "Кузнецов Кирилл" (сессия)
# Решение: токенизация + пересечение множеств
def _best_session_name(delivery_name: str) -> str:
    dtokens = frozenset(delivery_name.lower().split())
    best_name, best_score = delivery_name, 0
    for stokens, sname in session_tokens.items():
        score = len(dtokens & stokens)
        if score > best_score and score >= max(1, len(dtokens) - 1):
            best_score, best_name = score, sname
    return best_name
```

### /api/employees — тяжёлый запрос
- ~23000 сотрудников, ~18 МБ XML
- Загружать **один раз при старте**, кешировать в `_employees_global[bo_url]`
- Нужен для обогащения данных. Основные имена берём из `persSessionOpened.userName`

---

## Google Sheets API

| Параметр | Значение |
|----------|---------|
| SA email | `cursoraccountgooglesheets@cursor-487608.iam.gserviceaccount.com` |
| Таблица выгрузки | `1UM_pxA27p_utC8hw9AUDw5kN7VTJrDmxL_yjAuLB8GA` |
| Лист | `Выгрузка iiko` |
| Колонки (13) | Дата, Точка, Город, Выручка, Чеки, Средний чек, Нал, Безнал, Скидки, SailPlay, Самовывоз (чеков), COGS ₽, COGS % |

**Функции `google_sheets.py`:**
- `read_range`, `write_range`, `append_rows`, `ensure_sheet_exists`, `ensure_header`, `clear_range`
- `backup_file_to_drive(local_path, filename, folder_id)`

При 403 → добавить SA email в "Поделиться" → Редактор.

---

## Telegram Bot API

| Параметр | Значение |
|----------|---------|
| Токен | `.env` → `TELEGRAM_BOT_TOKEN` |
| Режим | Long polling (нет webhook) |
| parse_mode | HTML (всегда) |

Символы `< > &` в тексте → обязательно `html.escape()` перед отправкой.

# Инструкция для агента Борис — Stats API Аркентия

## Доступ

```
Base URL:  https://arkenty.ru/api/stats
Method:    GET
Auth:      Authorization: Bearer <TOKEN_FROM_SECRETS>
```

Все запросы — GET с параметром `metric` и опциональными фильтрами.

---

## Метрики

### 1. `metric=realtime` — текущее состояние точек

Данные из живого in-memory состояния (обновляются каждые ~30 сек из iiko Events).

```
GET /api/stats?metric=realtime
GET /api/stats?metric=realtime&city=Барнаул
GET /api/stats?metric=realtime&branch=Барнаул_1
```

**Ответ:**
```json
{
  "timestamp": "2026-03-08T15:43:34+00:00",
  "branches": [
    {
      "name": "Барнаул_1 Ана",
      "city": "Барнаул",
      "active_orders": 85,
      "late_orders": 33,
      "avg_cooking_time": 14,
      "avg_wait_time": null,
      "avg_delivery_time": 22
    }
  ]
}
```

Поля:
- `active_orders` — заказы в работе прямо сейчас
- `late_orders` — из них опоздавших (план уже прошёл)
- `avg_cooking_time` — среднее время приготовления (мин), null если нет данных
- `avg_wait_time` — среднее ожидание курьера (мин)
- `avg_delivery_time` — среднее время в пути (мин)

---

### 2. `metric=daily` — итоги дня

```
GET /api/stats?metric=daily                        # вчера, все точки
GET /api/stats?metric=daily&date=2026-03-07        # конкретная дата
GET /api/stats?metric=daily&date=2026-03-07&city=Томск
```

**Ответ:**
```json
{
  "date": "2026-03-07",
  "branches": [
    {
      "name": "Барнаул_1 Ана",
      "city": "Барнаул",
      "revenue": 530187,
      "checks": 288,
      "avg_check": 1841,
      "cogs_pct": 31.6,
      "discounts": 38287,
      "discount_pct": 7.2,
      "late_count": 25,
      "late_pct": 14.5,
      "avg_cooking_time": null,
      "avg_waiting_time": null,
      "avg_delivery_time": null,
      "avg_total_time": null,
      "new_customers": 12,
      "repeat_customers": 276
    }
  ],
  "totals": {
    "revenue": 3448561,
    "checks": 1817,
    "avg_check": 1898
  }
}
```

Поля:
- `revenue` — выручка в рублях
- `checks` — количество чеков (заказов)
- `avg_check` — средний чек
- `cogs_pct` — себестоимость в % от выручки
- `discounts` — сумма скидок в рублях
- `discount_pct` — скидки в % от выручки
- `late_count` — опозданий за день
- `late_pct` — % опозданий от доставок
- `avg_cooking_time` / `avg_waiting_time` / `avg_delivery_time` — среднее время по этапам (мин)
- `avg_total_time` — суммарное среднее время (мин)
- `new_customers` — новые клиенты (первый заказ в точке)
- `repeat_customers` — повторные клиенты

---

### 3. `metric=period` — агрегат за период

```
GET /api/stats?metric=period&from=2026-03-01&to=2026-03-07
GET /api/stats?metric=period&from=2026-03-01&to=2026-03-07&city=Абакан
```

По умолчанию: последние 7 дней.

**Ответ:** аналогично `daily`, но с полями `"from"` и `"to"` вместо `"date"`. Поля `avg_cooking_time` и т.д. — средние за период.

---

### 4. `metric=shifts` — смены сотрудников

```
GET /api/stats?metric=shifts                       # сегодня, все точки
GET /api/stats?metric=shifts&date=2026-03-08
GET /api/stats?metric=shifts&city=Барнаул
```

**Ответ:**
```json
{
  "date": "2026-03-08",
  "branches": [
    {
      "name": "Барнаул_1 Ана",
      "city": "Барнаул",
      "total": 31,
      "on_shift": 21,
      "roles": {
        "cook": {"total": 16, "on_shift": 11},
        "courier": {"total": 15, "on_shift": 10}
      }
    }
  ],
  "totals": {
    "total": 179,
    "on_shift": 161
  }
}
```

Поля:
- `total` — всего записей смен за дату
- `on_shift` — сотрудников, чья смена ещё открыта (сейчас на работе)
- `roles` — разбивка по ролям: `cook` (кухня), `courier` (курьеры)

---

## Фильтры

| Параметр | Тип | Описание |
|---|---|---|
| `city` | string | Подстрока в названии города: `Барнаул`, `Томск`, `Абакан`, `Черногорск` |
| `branch` | string | Подстрока в названии точки: `Барнаул_1`, `Ана`, и т.д. |
| `date` | YYYY-MM-DD | Конкретная дата (для daily, shifts) |
| `from` | YYYY-MM-DD | Начало периода (для period) |
| `to` | YYYY-MM-DD | Конец периода (для period) |

---

## Справочник точек

| Точка | Город |
|---|---|
| Барнаул_1 Ана | Барнаул |
| Барнаул_2 Гео | Барнаул |
| Барнаул_3 Тим | Барнаул |
| Барнаул_4 Бал | Барнаул |
| Томск_1 Яко | Томск |
| Томск_2 Дуб | Томск |
| Абакан_1 Кир | Абакан |
| Абакан_2 Аск | Абакан |
| Черногорск_1 Тих | Черногорск |

---

## Коды ошибок

| Код | Причина |
|---|---|
| 401 | Неверный или отсутствующий Bearer токен |
| 403 | Модуль `stats` не разрешён для токена |
| 429 | Превышен лимит 60 запросов в минуту |
| 400 | Неверный `metric` или формат даты |

---

## Примечания

- Все данные для tenant_id=1 (сеть Артемия: Барнаул, Томск, Абакан, Черногорск)
- `realtime` живёт в памяти — данные актуальны с задержкой ~30-100 сек
- `daily` / `period` — из PostgreSQL, данные за прошедшие дни полные, за сегодня могут быть неполными до конца дня
- Время в `timestamp` — UTC. Локальное время точек: UTC+7
- `avg_cooking_time` и другие t-метрики за прошлые дни пока `null` (обогащение в разработке)

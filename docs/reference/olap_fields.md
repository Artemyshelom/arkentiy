# OLAP Field Mapping — Справочник полей для разных версий iiko

> Практический справочник для диагностики доступных полей в iiko OLAP v2 для разных клиентов

---

## 📌 Структура OLAP запроса

```
GET /api/v2/reports/olap

Параметры:
- reportType: "SALES" или "DELIVERIES" (зависит от того, что нужно)
- buildStructure: true/false (показать структуру данных)
- measures: [поля для агрегации, SAL, COUNT и т.д.]
- dimensions/groupByRowFields: [по каким полям группировать]
```

**Правило:** Не все поля доступны в обоих reportType. Нужно тестировать.

---

## ✅ Подтвержденные field names (Shaburov, Ёбидоёби)

### Phase 6 Backfill — Временные поля

Для заполнения `orders_raw` полей: `opened_at`, `cooked_time`, `ready_time`, `send_time`, `service_print_time`

| Логическое поле | reportType | OLAP field name | Тип | Примечание |
|-----------------|-----------|-----------------|-----|-----------|
| **opened_at** | SALES | `OpenTime` | dimension (root-level) | Время открытия заказа (критично!) |
| **cooked_time** | SALES | `Delivery.CookingFinishTime` | dimension | Время завершения готовки |
| **ready_time** | SALES | `Delivery.BillTime` | dimension | Время выдачи (НЕ `Delivery.ReadyTime`!) |
| **send_time** | SALES | `Delivery.SendTime` | dimension | Время отправки курьером |
| **service_print_time** | SALES | `Delivery.PrintTime` | dimension | Время печати чека |

### Phase 1-5 Backfill — Основные поля

| Логическое поле | reportType | OLAP field name | Тип | Фаза | Примечание |
|-----------------|-----------|-----------------|-----|------|-----------|
| order_num | SALES | `Delivery.Number` | dimension | 1 | Номер заказа |
| branch_name | SALES | `Department` | dimension | 1 | Название ветки |
| sum | SALES | `DishDiscountSumInt.withoutVAT` | measure | 1 | Сумма заказа без скидок |
| status | SALES | `Delivery.CancelCause` | dimension | 1 | NULL = выполнен, else = причина отмены |
| actual_time | SALES | `Delivery.ActualTime` | dimension | 1 | Фактическое время доставки |
| delivery_address | SALES | `Delivery.Address` | dimension | 1 | Адрес доставки |
| is_self_service | SALES | `Delivery.ServiceType` | dimension | 1 | "Самовывоз" / "Доставка" |
| **items** | SALES | `DishName` | dimension | 2 | Состав блюд (Phase 2 отдельно) |
| **courier** | SALES | `WaiterName` | dimension | 3 | Курьер (Phase 3 отдельно!) |
| **planned_time** | DELIVERIES | `Delivery.ExpectedTime` | dimension | 4 | Плановое время доставки |
| **client_name** | DELIVERIES | `Delivery.CustomerName` | dimension | 5 | Имя клиента (GUEST* = анонимные) |
| client_phone | SALES | `Delivery.CustomerPhone` | measure | 1 | Телефон клиента |

### Финансовые поля (для daily_stats)

| Логическое поле | reportType | OLAP field name | Тип | Примечание |
|-----------------|-----------|-----------------|-----|-----------|
| revenue | SALES | `DishDiscountSumInt.withoutVAT` | measure | Сумма без скидок |
| discounts | SALES | `DiscountSum` | measure | Сумма скидок |
| check_count | SALES | `UniqOrderId.OrdersCount` | measure | Количество чеков |
| cogs_pct | SALES | `ProductCostBase.Percent` | measure | % себестоимости |

---

## ⚠️ Грабли и различия по версиям iiko

### Грабль 1: `Delivery.ReadyTime` vs `Delivery.BillTime`

| Поле | Возвращает | Проблема | Решение |
|------|-----------|----------|---------|
| `Delivery.ReadyTime` | Может быть NULL или ошибка 400 | В некоторых версиях не существует | Использовать `Delivery.BillTime` |
| `Delivery.BillTime` | Время оплаты/выдачи | Более надёжно | **ИСПОЛЬЗУЙ ЭТО** |

**Диагностика:** Если OLAP возвращает 400 на `ready_time`, переключиться на `BillTime`.

---

### Грабль 2: `OpenTime` (root-level) vs `Delivery.OpenTime`

| Поле | Тип | Возвращает | Проблема | Решение |
|------|-----|-----------|----------|---------|
| `OpenTime` | root-level dimension | Фактическое время открытия | Может быть пусто в старых системах | Использовать если доступно |
| `Delivery.OpenTime` | nested | Может отличаться | Не всегда совпадает с реальным открытием | Проверить оба варианта |

**Диагностика:** Если `OpenTime` возвращает < 50% — запросить у клиента какой field использует их iiko.

---

### Грабль 3: `groupByRowFields` содержит `OpenDate`

**Что происходит:**
```
Добавил в groupByRowFields: ["OpenDate", "Delivery.Number"]
Результат: Delivery.Number = NULL для всех записей
```

**Почему:** На некоторых иико installations `OpenDate` + `Delivery.Number` конфликтуют.

**Решение:** Не добавлять `OpenDate` в `groupByRowFields`, только в conditions если нужно фильтровать.

---

### Грабль 4: `WaiterName` в Phase 1 обнуляет `DishDiscountSumInt`

**Что происходит:**
```
Phase 1 запрос содержит: "DishDiscountSumInt.withoutVAT" + "WaiterName" в groupBy
Результат: DishDiscountSumInt.withoutVAT = 0 для всех
```

**Почему:** OLAP v2 не может агрегировать sum вместе с dimension `WaiterName` на некоторых версиях.

**Решение:** Запрос Phase 3 (курьер) делать **отдельно** от Phase 1 (выручка).

---

### Грабль 5: `Delivery.CustomerName` = "GUEST12345" для анонимов

**Что происходит:**
```sql
SELECT client_name FROM orders_raw WHERE tenant_id = 3 LIMIT 10;
-- "Иван", "Мария", "GUEST12345", "GUEST67890", ...
```

**Почему:** Клиент не указал имя при заказе, iiko генерирует GUEST*.

**Решение:** При валидации — фильтровать `WHERE client_name NOT LIKE 'GUEST%'`.

---

## 📊 Диагностический запрос для нового клиента

Используй этот Python скрипт для диагностики доступных полей:

```python
import asyncio
import httpx
from datetime import date

async def diagnose_olap(bo_url: str, bo_login: str, bo_password: str, branch_name: str):
    """
    Проверить доступные OLAP поля для конкретной ветки/клиента
    """
    
    client = httpx.AsyncClient()
    
    # Шаг 1: Авторизация
    auth_resp = await client.get(
        f"{bo_url}/api/auth",
        params={"login": bo_login, "pass": hashlib.sha1(bo_password.encode()).hexdigest()}
    )
    token = auth_resp.json().get("token")
    
    # Шаг 2: Запросить OLAP с buildStructure
    today = date.today().isoformat()
    olap_resp = await client.get(
        f"{bo_url}/api/v2/reports/olap",
        params={
            "key": token,
            "reportType": "SALES",
            "buildStructure": "true",
            "organization": branch_name,
            "from": today,
            "to": today,
            "measures": ["DishDiscountSumInt.withoutVAT"],
            "dimensions": []
        }
    )
    
    struct = olap_resp.json().get("structure", {})
    
    print("=== ДОСТУПНЫЕ ПОЛЯ OLAP ===")
    print(f"Branch: {branch_name}")
    print(f"Date: {today}")
    
    for category in ["measures", "dimensions"]:
        fields = struct.get(category, {})
        print(f"\n{category.upper()}:")
        for name, info in fields.items():
            print(f"  - {name}: {info}")
    
    await client.aclose()

# Запуск
asyncio.run(diagnose_olap(
    bo_url="https://BRANCH.iiko.it/resto",
    bo_login="LOGIN",
    bo_password="PASSWORD",
    branch_name="Branch_1"
))
```

**Как использовать:**
1. Заменить bo_url, bo_login, bo_password, branch_name
2. Запустить на VPS: `docker compose exec app python diagnose_olap.py`
3. Скопировать вывод полей
4. Сравнить с таблицей выше (в разделе ✅)

---

## 🔍 Процесс диагностики для нового клиента

**Шаг 1:** Запросить у клиента OLAP доступ и credentials
**Шаг 2:** Запустить диагностический скрипт (выше)
**Шаг 3:** Сравнить возвращённые поля с таблицей ✅
**Шаг 4:** Если не совпадают — добавить заметку ниже

---

## 📝 Известные различия по iiko версиям

### Версия 1 (старые клиенты)
- ✅ `Delivery.BillTime` (не ReadyTime)
- ✅ `OpenTime` на root-level
- ✅ Все поля доступны

### Версия 2 (Shaburov, Ёбидоёби)
- ✅ Всё работает как указано выше

### Версия ? (будущие клиенты)
- ❓ Заполнить после подключения

---

## 🚀 Checklist для нового клиента

- [ ] Получить credentials доступа к OLAP
- [ ] Запустить диагностический скрипт
- [ ] Документировать отличия field names (если есть)
- [ ] Обновить `phase6_shaburov.py` если names отличаются
- [ ] Тестировать Phase 6 на небольшом диапазоне (1 день)
- [ ] Если < 80% заполнения — вернуться к диагностике
- [ ] После успеха — добавить версию в раздел 📝 выше

---

**Последнее обновление:** 03 Марта 2026

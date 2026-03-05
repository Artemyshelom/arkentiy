# Troubleshooting онбординга — Типичные проблемы и решения

> На основе опыта с Shaburov (tenant_id=3)

---

## 🔴 Критические баги

### 1. Events API пишет заказы в неправильный `tenant_id`

**Симптом:**
```sql
SELECT DISTINCT tenant_id, COUNT(*) FROM orders_raw
WHERE branch_name IN ('Канск', 'Зеленогорск')
GROUP BY tenant_id;
-- Возвращает: tenant_id=1 (НЕПРАВИЛЬНО!)
```

**Причина:**
- `BranchState` не содержит `tenant_id` или используется дефолт
- Events API пишет в default `tenant_id=1` вместо правильного

**Решение:**
1. **Код:** Добавить `tenant_id` в `BranchState` и передавать при `upsert_orders_batch()`
   ```python
   class BranchState:
       tenant_id: int  # ← ДОБАВИТЬ
       ...
   ```

2. **Проверка:**
   ```sql
   -- После бэкфилла:
   UPDATE orders_raw SET tenant_id = 3 
   WHERE branch_name IN ('Канск', 'Зеленогорск') AND tenant_id = 1;
   ```

3. **На будущее:** Жестко требовать `tenant_id` в конфигурации ветки (см. `Протокол_онбординга.md`, раздел 0)

**Статус (Shaburov):** ✅ Завершено (Phase 1-5 + manual fix)

---

### 2. Временные поля полностью пусты

**Симптом:**
```sql
SELECT COUNT(*), 
       COUNT(opened_at) as opened_cnt,
       COUNT(cooked_time) as cooked_cnt,
       COUNT(ready_time) as ready_cnt,
       COUNT(send_time) as send_cnt
FROM orders_raw WHERE tenant_id = 3;

-- Возвращает: total=8010, opened_cnt=709, cooked_cnt=0, ready_cnt=0, send_cnt=0
```

**Причина:**
- Events API новый (не логировал время раньше)
- OLAP не настроен или недоступен
- Нет Phase 6 backfill

**Решение:**
1. **Phase 6 Backfill** → Запустить `phase6_shaburov.py` (или generic) на весь период
   ```bash
   docker compose exec app python -m app.onboarding.phase6_backfill \
     --tenant-id 3 \
     --date-from 2025-01-01 \
     --date-to 2026-03-03
   ```

2. **Проверка результата:**
   ```sql
   SELECT 
     ROUND(COUNT(opened_at)::numeric / COUNT(*) * 100, 1) as opened_pct,
     ROUND(COUNT(cooked_time)::numeric / COUNT(*) * 100, 1) as cooked_pct,
     ROUND(COUNT(ready_time)::numeric / COUNT(*) * 100, 1) as ready_pct,
     ROUND(COUNT(send_time)::numeric / COUNT(*) * 100, 1) as send_pct
   FROM orders_raw WHERE tenant_id = 3;
   -- Ожидается: 90%+ для всех
   ```

3. **Если < 85%:** Проверить OLAP field names (см. раздел 3 ниже)

**Статус (Shaburov):** ✅ Завершено (92-100%)

---

### 3. OLAP field names неправильные или не найдены

**Симптом:**
```
ERROR: 400 Bad Request from OLAP API
Field not found: Delivery.ReadyTime
```

или

```sql
SELECT COUNT(*) FROM orders_raw WHERE ready_time != '' AND tenant_id = 3;
-- Возвращает: 0 (хотя должны быть данные)
```

**Причина:**
- Field names отличаются между версиями iiko
- Используется скопированный query от другого клиента
- Версия OLAP поддерживает другие имена полей

**Примеры разных имён для одного поля:**

| Поле | Вариант 1 | Вариант 2 | Примечание |
|------|-----------|-----------|-----------|
| Время готовки | `Delivery.CookingFinishTime` | `Delivery.CookingTime` | Проверить iiko docs |
| Время выдачи | `Delivery.BillTime` | `Delivery.ReadyTime` | BillTime часто правильнее |
| Время открытия | `OpenTime` | `Delivery.OpenTime` | Root-level или nested |

**Решение:**

1. **Диагностика доступных полей:**
   ```bash
   # На VPS с временным diagnostic скриптом
   ssh arkentiy "cd /opt/ebidoebi && docker compose exec app python -c '
   import asyncio
   from datetime import date
   from app.clients.iiko_bo_olap_v2 import _fetch_from_server

   async def diagnose():
       # Запросить OLAP с пустыми filters, посмотреть какие поля есть
       result = await _fetch_from_server(
           \"https://BRANCH.iiko.it/resto\",
           {\"Branch1\"},
           \"2025-03-01\",
           \"2025-03-02\",
           include_delivery=False,
           bo_login=\"LOGIN\",
           bo_password=\"PASSWORD\"
       )
       print(\"Available fields:\", result.keys() if result else \"None\")

   asyncio.run(diagnose())
   '"
   ```

2. **После определения правильных names:** Обновить `phase6_shaburov.py` (или generic script)

3. **Перезапустить Phase 6** с правильными field names

**Статус (Shaburov):** ✅ Завершено (использовались корректные field names)

---

### 4. opened_at отсутствует на 90%+ заказов

**Симптом:**
```sql
SELECT COUNT(*), COUNT(opened_at) FROM orders_raw WHERE tenant_id = 3;
-- Возвращает: 8010 total, 709 opened_at = 8.9%
-- Не можем считать: cooking_duration, total_duration
```

**Причина:**
- Events API не логировал `opened_at` до недавно
- OLAP поле `OpenTime` не включалось в Phase 6

**Решение:**

1. **Phase 8 Recovery** → Восстановление из OLAP `OpenTime`:
   ```python
   # Добавить в backfill script
   UPDATE orders_raw SET opened_at = olap.open_time
   WHERE tenant_id = 3 AND opened_at IS NULL OR opened_at = ''
   ```

2. **Запустить:**
   ```bash
   docker compose exec app python -m app.onboarding.phase8_recovery \
     --tenant-id 3 \
     --field opened_at \
     --source olap.OpenTime
   ```

3. **Проверка:**
   ```sql
   SELECT COUNT(*), COUNT(opened_at) FROM orders_raw WHERE tenant_id = 3;
   -- Ожидается: 100% или > 95%
   ```

**Статус (Shaburov):** ✅ Завершено (100% из `OpenTime`)

---

### 5. `/статус` не показывает данные финансов для нового клиента

**Симптом:**
```
/статус Канск:
Revenue: —
Check count: —
COGS: —
```

**Причина:**
- `iiko_status_report.py` вызывает `get_branch_olap_stats()` без параметров
- Функция использует hardcoded `settings.branches` (только tenant_id=1)
- Новый клиент не в этом списке

**Решение:**

1. **В `app/jobs/iiko_status_report.py`:**
   ```python
   # ДО:
   olap = await get_branch_olap_stats(today)
   
   # ПОСЛЕ:
   from app.db import get_branches as get_available_branches
   branches = get_available_branches()  # Получить ветки текущего тенанта
   olap = await get_branch_olap_stats(today, branches=branches)
   ```

2. **В `app/clients/iiko_bo_olap_v2.py`:**
   ```python
   # Сигнатура функции
   async def get_branch_olap_stats(date: datetime, branches: list[dict] | None = None):
       if branches is None:
           branches = settings.branches  # Fallback
       
       # Дальше в функции: использовать branches параметр
       # И извлекать credentials из каждой ветки
   ```

3. **Деплой и проверка:**
   ```bash
   ssh arkentiy "cd /opt/ebidoebi && docker compose exec -T app \
     python -c \"from app.jobs.iiko_status_report import get_daily_report; print(await get_daily_report())\""
   ```

**Статус (Shaburov):** ✅ Завершено

---

### 6. `/повара` показывает 0 поваров для определённого города

**Симптом:**
```
/повара
├─ Канск: 3 повара
├─ Зеленогорск: 0 поваров  ← ПРОБЛЕМА
└─ Ижевск: 2 повара
```

**Причина:**
- Events API получает `roleName` для каждого работника
- Если `roleName` пусто в событии — система смотрит в `_employees_global` cache по `mainRoleCode`
- `_COOK_ROLE_PREFIXES` неполный (не содержит все локальные abbreviations)

**Примеры:**
```python
# ДО:
_COOK_ROLE_PREFIXES = ("повар", "cook", "пс", "пбт", "пов")

# ПОСЛЕ (для Shaburov):
_COOK_ROLE_PREFIXES = ("повар", "cook", "пс", "пбт", "пов", "пз", "кп")
# ПЗ = повар Зеленогорска
# КП = ещё один вариант повара
```

**Решение:**

1. **Запросить у клиента** — какие сокращения он использует для должностей в iiko
2. **Проверить в iiko API:**
   ```bash
   curl "https://BRANCH.iiko.it/api/employees?key=TOKEN" | python3 -c "
   import sys, json
   data = json.load(sys.stdin)
   for emp in data.get('employees', []):
       if emp.get('isStuff'):  # Только сотрудники
           print(f\"{emp['name']}: {emp.get('mainRoleCode', 'NO_ROLE')}\")
   "
   ```

3. **Добавить prefixes в `app/clients/iiko_bo_events.py`:**
   ```python
   _COOK_ROLE_PREFIXES = ("повар", "cook", "пс", "пбт", "пов", "пз", "кп")  # добавить локальные
   ```

4. **⚠️ Исключить админов:** Убедиться что админские роли (АЗ, admin) не в списке поваров

5. **Деплой:**
   ```bash
   scp -i ~/.ssh/cursor_arkentiy_vps app/clients/iiko_bo_events.py root@5.42.98.2:/opt/ebidoebi/app/
   ssh arkentiy "cd /opt/ebidoebi && docker compose restart app"
   sleep 15
   # Проверить /повара
   ```

**Статус (Shaburov):** ✅ Завершено (добавлены ПЗ, КП; АЗ исключена как администратор)

---

## ⚠️ Невысокие приоритеты (но бывают)

### 7. Отсутствуют заказы за первый день после подписки на Events API

**Симптом:**
```sql
SELECT date, COUNT(*) FROM orders_raw WHERE tenant_id = 3 GROUP BY date ORDER BY date;
-- 2025-01-01: 180 заказов
-- ...
-- 2026-03-02: 45 заказов
-- 2026-03-03: 3 заказов ← РЕЗКО упал, потом вообще ничего
```

**Причина:**
- Events API был перезагружен/переподписан на новый token
- Пропущены события между перезагрузкой и переподпиской

**Решение:**

1. Не критично — Phase 1-5 покрывает исторические данные из OLAP
2. Можно запустить manual cleanup job для зависших статусов
3. На будущее: не спешить с Events API в день подключения, дать 24+ часа стабильности

**Статус (Shaburov):** 🟡 Принято (не критично, т.к. есть OLAP backfill)

---

### 8. Отрицательные values в cooking_duration

**Симптом:**
```sql
SELECT AVG(EXTRACT(EPOCH FROM cooking_duration)/60) FROM orders_raw 
WHERE tenant_id = 3;
-- Результат: -120.5 минут (НЕПРАВИЛЬНО!)
```

**Причина:**
- `opened_at` > `cooked_time` (временной парадокс в данных)
- Обычно — баг в OLAP field mapping или системных часов

**Решение:**
1. Проверить OLAP field names (см. раздел 3)
2. Проверить `utc_offset` в `iiko_credentials` для ветки
3. Это **data quality issue**, не код-баг
4. На будущее: добавить validation check при Phase 7

**Статус (Shaburov):** 🟡 Документировано, не критично

---

## 📋 Чеклист диагностики для нового клиента

Использовать этот чеклист сразу после первого бэкфилла:

```sql
-- 1. Проверить tenant_id изоляцию
SELECT DISTINCT tenant_id FROM orders_raw 
WHERE branch_name IN ('Ветка_1', 'Ветка_2')
GROUP BY tenant_id;
-- Ожидается: ОДНА строка = tenant_id нового клиента

-- 2. Проверить заполнение критических полей
SELECT 
  COUNT(*) as total,
  COUNT(*) FILTER (WHERE order_num IS NOT NULL) as order_num_cnt,
  COUNT(*) FILTER (WHERE opened_at IS NOT NULL AND opened_at != '') as opened_at_cnt,
  COUNT(*) FILTER (WHERE cooked_time IS NOT NULL AND cooked_time != '') as cooked_time_cnt,
  COUNT(*) FILTER (WHERE ready_time IS NOT NULL AND ready_time != '') as ready_time_cnt,
  COUNT(*) FILTER (WHERE send_time IS NOT NULL AND send_time != '') as send_time_cnt
FROM orders_raw WHERE tenant_id = NEW_TENANT_ID;

-- 3. Проверить заполнение derived metrics
SELECT 
  COUNT(*) as total,
  COUNT(*) FILTER (WHERE cooking_duration IS NOT NULL) as cooking_dur_cnt,
  COUNT(*) FILTER (WHERE delivery_duration IS NOT NULL) as delivery_dur_cnt,
  COUNT(*) FILTER (WHERE total_duration IS NOT NULL) as total_dur_cnt
FROM orders_raw WHERE tenant_id = NEW_TENANT_ID;

-- 4. Проверить daily_stats финансы
SELECT COUNT(*) FROM daily_stats WHERE tenant_id = NEW_TENANT_ID AND revenue > 0;
-- Ожидается: > 80% дней с revenue

-- 5. Проверить по городам
SELECT branch_name, COUNT(*) as order_cnt,
  ROUND(COUNT(*) FILTER (WHERE opened_at != '')/COUNT(*)::numeric * 100, 1) as opened_pct
FROM orders_raw WHERE tenant_id = NEW_TENANT_ID
GROUP BY branch_name;
```

---

**Последнее обновление:** 03 Марта 2026

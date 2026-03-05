# Чеклист проверки данных — SQL диагностика для онбординга

> Готовые SQL запросы для быстрой проверки целостности данных после бэкфилла

---

## 🔍 Быстрая диагностика (1 минута)

Скопировать и запустить всё в одном batch:

```sql
-- ========== БЫСТРЫЙ ЧЕКЛИСТ ==========

-- 1. Проверить базовые метрики
SELECT 
  t.name as client,
  COUNT(o.id) as total_orders,
  COUNT(DISTINCT o.branch_name) as branches,
  MIN(o.date)::text as from_date,
  MAX(o.date)::text as to_date,
  COUNT(DISTINCT o.delivery_num) as unique_deliveries
FROM tenants t
LEFT JOIN orders_raw o ON t.id = o.tenant_id
WHERE t.id = NEW_TENANT_ID
GROUP BY t.id, t.name;

-- 2. Проверить tenant_id изоляцию
SELECT 
  'orders_raw isolation' as check_name,
  COUNT(DISTINCT tenant_id) as distinct_tenants,
  CASE WHEN COUNT(DISTINCT tenant_id) = 1 THEN '✅ PASS' ELSE '❌ FAIL' END as status
FROM orders_raw 
WHERE branch_name IN (SELECT branch_name FROM iiko_credentials WHERE tenant_id = NEW_TENANT_ID);

-- 3. Проверить заполнение критических полей (%)
SELECT 
  ROUND(COUNT(*) FILTER (WHERE delivery_num IS NOT NULL)::numeric / COUNT(*) * 100, 1) as order_num_pct,
  ROUND(COUNT(*) FILTER (WHERE opened_at IS NOT NULL AND opened_at != '')::numeric / COUNT(*) * 100, 1) as opened_at_pct,
  ROUND(COUNT(*) FILTER (WHERE cooked_time IS NOT NULL AND cooked_time != '')::numeric / COUNT(*) * 100, 1) as cooked_pct,
  ROUND(COUNT(*) FILTER (WHERE ready_time IS NOT NULL AND ready_time != '')::numeric / COUNT(*) * 100, 1) as ready_pct,
  ROUND(COUNT(*) FILTER (WHERE send_time IS NOT NULL AND send_time != '')::numeric / COUNT(*) * 100, 1) as send_pct,
  ROUND(COUNT(*) FILTER (WHERE actual_time IS NOT NULL AND actual_time != '')::numeric / COUNT(*) * 100, 1) as actual_time_pct
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID;

-- 4. Проверить derived metrics (%)
SELECT 
  ROUND(COUNT(*) FILTER (WHERE cooking_duration IS NOT NULL)::numeric / COUNT(*) * 100, 1) as cooking_dur_pct,
  ROUND(COUNT(*) FILTER (WHERE idle_time IS NOT NULL)::numeric / COUNT(*) * 100, 1) as idle_time_pct,
  ROUND(COUNT(*) FILTER (WHERE delivery_duration IS NOT NULL)::numeric / COUNT(*) * 100, 1) as delivery_dur_pct,
  ROUND(COUNT(*) FILTER (WHERE total_duration IS NOT NULL)::numeric / COUNT(*) * 100, 1) as total_dur_pct
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID;

-- 5. Проверить по городам
SELECT 
  branch_name,
  COUNT(*) as orders,
  ROUND(COUNT(*) FILTER (WHERE opened_at != '')::numeric / COUNT(*) * 100, 1) as opened_at_pct,
  ROUND(COUNT(*) FILTER (WHERE cooking_duration IS NOT NULL)::numeric / COUNT(*) * 100, 1) as cooking_dur_pct,
  ROUND(COUNT(*) FILTER (WHERE total_duration IS NOT NULL)::numeric / COUNT(*) * 100, 1) as total_dur_pct
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID
GROUP BY branch_name
ORDER BY branch_name;
```

---

## 📊 Detailed Checks (по этапам)

### Phase 1-5: Основные поля заказа

```sql
-- Проверить заполнение после Phase 1-5
SELECT 
  COUNT(*) as total,
  COUNT(*) FILTER (WHERE delivery_num IS NOT NULL) as delivery_num_cnt,
  COUNT(*) FILTER (WHERE branch_name IS NOT NULL) as branch_name_cnt,
  COUNT(*) FILTER (WHERE sum IS NOT NULL AND sum > 0) as sum_with_value_cnt,
  COUNT(*) FILTER (WHERE actual_time IS NOT NULL AND actual_time != '') as actual_time_cnt,
  COUNT(*) FILTER (WHERE client_phone IS NOT NULL AND client_phone != '') as client_phone_cnt,
  COUNT(*) FILTER (WHERE items IS NOT NULL AND items != '[]') as items_cnt,
  COUNT(*) FILTER (WHERE courier IS NOT NULL AND courier != '') as courier_cnt,
  COUNT(*) FILTER (WHERE planned_time IS NOT NULL AND planned_time != '') as planned_time_cnt,
  COUNT(*) FILTER (WHERE client_name IS NOT NULL AND client_name != '' AND client_name NOT LIKE 'GUEST%') as client_name_cnt
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID;

-- ✅ Ожидается: 
-- - delivery_num = 100%
-- - branch_name = 100%
-- - actual_time = 95-100%
-- - items = 100%
-- - courier = 95-98% (может быть null для самовывоза)
-- - planned_time = 100%
-- - client_name = 95-98% (фильтруем GUEST*)
```

### Phase 6: Временные поля

```sql
-- Проверить Phase 6 результаты
SELECT 
  COUNT(*) as total,
  COUNT(*) FILTER (WHERE opened_at IS NOT NULL AND opened_at != '') as opened_at_cnt,
  COUNT(*) FILTER (WHERE cooked_time IS NOT NULL AND cooked_time != '') as cooked_time_cnt,
  COUNT(*) FILTER (WHERE ready_time IS NOT NULL AND ready_time != '') as ready_time_cnt,
  COUNT(*) FILTER (WHERE send_time IS NOT NULL AND send_time != '') as send_time_cnt,
  COUNT(*) FILTER (WHERE service_print_time IS NOT NULL AND service_print_time != '') as service_print_time_cnt,
  -- Проверить min/max для разумности
  MIN(cooked_time) as min_cooked_time,
  MAX(cooked_time) as max_cooked_time
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID;

-- ✅ Ожидается: 
-- - opened_at >= 90% (критично!)
-- - cooked_time >= 85%
-- - ready_time >= 85%
-- - send_time >= 85%
```

### Phase 7: Derived Metrics

```sql
-- Проверить Phase 7 расчёты
SELECT 
  COUNT(*) as total,
  COUNT(*) FILTER (WHERE cooking_duration IS NOT NULL) as cooking_dur_cnt,
  COUNT(*) FILTER (WHERE idle_time IS NOT NULL) as idle_time_cnt,
  COUNT(*) FILTER (WHERE delivery_duration IS NOT NULL) as delivery_dur_cnt,
  COUNT(*) FILTER (WHERE total_duration IS NOT NULL) as total_dur_cnt,
  -- Проверить значения (в минутах)
  ROUND(EXTRACT(EPOCH FROM AVG(cooking_duration))/60.0, 1) as avg_cooking_min,
  ROUND(EXTRACT(EPOCH FROM AVG(idle_time))/60.0, 1) as avg_idle_min,
  ROUND(EXTRACT(EPOCH FROM AVG(delivery_duration))/60.0, 1) as avg_delivery_min,
  ROUND(EXTRACT(EPOCH FROM AVG(total_duration))/60.0, 1) as avg_total_min
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID AND date >= '2025-01-01';

-- ✅ Ожидается: 
-- - cooking_duration >= 90%
-- - total_duration >= 95% (должен быть 100% если opened_at + actual_time OK)
-- - avg_cooking_min: 10-30 мин (разумное значение)
-- - avg_delivery_min: 25-60 мин (разумное значение)
```

### Phase 8: Recovery критических полей

```sql
-- Проверить opened_at после Phase 8
SELECT 
  COUNT(*) as total,
  COUNT(*) FILTER (WHERE opened_at IS NOT NULL AND opened_at != '') as opened_at_cnt,
  ROUND(COUNT(*) FILTER (WHERE opened_at IS NOT NULL AND opened_at != '')::numeric / COUNT(*) * 100, 1) as opened_at_pct
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID;

-- ✅ Ожидается: 
-- - opened_at >= 95% (можно 100%)
```

---

## 🏢 Daily Stats Проверка

```sql
-- Проверить финансовые данные в daily_stats
SELECT 
  COUNT(*) as total_records,
  COUNT(*) FILTER (WHERE revenue > 0) as revenue_filled,
  COUNT(*) FILTER (WHERE check_count > 0) as check_count_filled,
  COUNT(*) FILTER (WHERE cogs_pct > 0) as cogs_filled,
  COUNT(*) FILTER (WHERE avg_cooking_min > 0) as avg_cooking_filled,
  COUNT(*) FILTER (WHERE avg_delivery_min > 0) as avg_delivery_filled,
  -- Проверить диапазоны значений
  ROUND(AVG(revenue), 0) as avg_revenue,
  ROUND(AVG(avg_cooking_min), 1) as avg_cooking_min_value,
  ROUND(AVG(avg_delivery_min), 1) as avg_delivery_min_value
FROM daily_stats 
WHERE tenant_id = NEW_TENANT_ID;

-- ✅ Ожидается: 
-- - revenue_filled >= 80%
-- - check_count_filled >= 80%
-- - avg_revenue: > 5000 (зависит от клиента)
```

---

## ⚠️ Проверка на аномалии и ошибки

```sql
-- Проверить на отрицательные durations (ошибка в данных)
SELECT 
  COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM cooking_duration) < 0) as negative_cooking,
  COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM delivery_duration) < 0) as negative_delivery,
  COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM total_duration) < 0) as negative_total
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID AND date >= '2025-01-01';

-- ⚠️ Если > 0: проблема с OLAP field mapping (см. Troubleshooting)
```

```sql
-- Проверить на очень большие durations (> 24 часов)
SELECT 
  COUNT(*) as total,
  COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM delivery_duration) > 86400) as delivery_over_24h,
  COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM total_duration) > 86400) as total_over_24h
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID AND date >= '2025-01-01';

-- ⚠️ Если > 1%: нужна проверка данных (может быть нормально для поздних доставок)
```

```sql
-- Проверить на пропущенные заказы (бреши в номерах)
SELECT 
  CURRENT_DATE as check_date,
  branch_name,
  COUNT(*) as delivery_count,
  COUNT(DISTINCT DATE(CAST(actual_time AS TIMESTAMP))) as days_with_orders
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID AND date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY branch_name
ORDER BY branch_name;

-- ⚠️ Если days_with_orders < 25 за 30 дней: проверить Events API логи
```

---

## 📋 Финальный чеклист перед production

```sql
-- ========== ФИНАЛЬНЫЙ ЧЕКЛИСТ ==========

-- 1. ✅ tenant_id изоляция
SELECT COUNT(DISTINCT tenant_id) as tenant_count
FROM orders_raw 
WHERE branch_name IN (SELECT branch_name FROM iiko_credentials WHERE tenant_id = NEW_TENANT_ID);
-- ДОЛЖНО БЫТЬ: 1

-- 2. ✅ Минимальный объём данных
SELECT COUNT(*) as order_count FROM orders_raw WHERE tenant_id = NEW_TENANT_ID;
-- ДОЛЖНО БЫТЬ: >= 100 (хотя бы для диагностики)

-- 3. ✅ Критические поля >= 90%
SELECT 
  ROUND(COUNT(*) FILTER (WHERE opened_at != '')::numeric / COUNT(*) * 100, 1) as opened_at_pct,
  ROUND(COUNT(*) FILTER (WHERE actual_time != '')::numeric / COUNT(*) * 100, 1) as actual_time_pct,
  ROUND(COUNT(*) FILTER (WHERE total_duration IS NOT NULL)::numeric / COUNT(*) * 100, 1) as total_dur_pct
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID;
-- ВСЕ ДОЛЖНЫ БЫТЬ >= 90%

-- 4. ✅ Финансовые данные в daily_stats
SELECT COUNT(*) FROM daily_stats 
WHERE tenant_id = NEW_TENANT_ID AND revenue > 0;
-- ДОЛЖНО БЫТЬ: >= 80% от total дней

-- 5. ✅ Нет дубликатов
SELECT delivery_num, COUNT(*) as cnt 
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID 
GROUP BY delivery_num 
HAVING COUNT(*) > 1 
LIMIT 5;
-- ДОЛЖНО БЫТЬ: 0 результатов

-- 6. ✅ Нет очевидных ошибок в временах
SELECT COUNT(*) as count_issues
FROM orders_raw 
WHERE tenant_id = NEW_TENANT_ID 
  AND (
    EXTRACT(EPOCH FROM cooking_duration) < 0
    OR EXTRACT(EPOCH FROM delivery_duration) < 0
    OR EXTRACT(EPOCH FROM total_duration) < 0
  );
-- ДОЛЖНО БЫТЬ: 0 (или < 1% от total)

-- ИТОГ: Если ВСЕ чеки пройдены → ✅ READY FOR PRODUCTION
```

---

## 🚀 Как быстро проверить нового клиента

1. **После Phase 1-5:** Запустить раздел "Phase 1-5" выше
2. **После Phase 6:** Запустить раздел "Phase 6" выше
3. **После Phase 7:** Запустить раздел "Phase 7" выше
4. **После Phase 8:** Запустить раздел "Phase 8" выше
5. **Финально:** Запустить "Финальный чеклист" выше

**Если все green — готово к production!**

---

**Последнее обновление:** 03 Марта 2026

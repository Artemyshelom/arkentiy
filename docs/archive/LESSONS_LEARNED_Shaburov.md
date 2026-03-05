# Полное руководство по онбордингу нового клиента — Experience from Shaburov (tenant_id=3)

> **Ключевой принцип:** Отсутствующие исторические данные = проблема. Новый клиент должен иметь ВСЕ данные, которые были накопленны у старых клиентов. Не игнорируем, не откладываем — решаем прямо при онбординге.

## 📋 Проблемы найденные и решённые (Shaburov case)

### 1. ❌ Multi-tenancy Bug в Events API
**Проблема:** Events API писал заказы Shaburov в `tenant_id=1` вместо `tenant_id=3`
- **Причина:** `BranchState` не содержал `tenant_id`, использовался дефолт `tenant_id=1`
- **Масштаб:** 473 заказа за 3+ дня, 176 дублей в обеих БД
- **Решение:** Добавить `tenant_id` в `BranchState`, передавать в `upsert_orders_batch()`
- **Автоонбординг:** ✅ Жестко требуем `tenant_id` в конфигурации ветки

### 2. ❌ Временные поля (opened_at, cooked_time, ready_time, send_time, service_print_time)
**Проблема:** У нового клиента временные поля пусты (Events API не логировал, OLAP не настроен)
- **opened_at:** 8.9% (709/8010)
- **cooked_time:** 0% (новые Events API)
- **ready_time:** 0% (новые Events API)
- **send_time:** 0% (новые Events API)

**Решение:**
1. **Phase 6:** Бэкфил из OLAP v2 (проверенные field names)
2. **Phase 8:** Восстановление `opened_at` из OLAP поля `OpenTime` (100%)
3. **OLAP field mapping (критично!):**
   - `Delivery.CookingFinishTime` → `cooked_time`
   - `Delivery.BillTime` → `ready_time` (NOT `Delivery.ReadyTime`!)
   - `Delivery.SendTime` → `send_time`
   - `Delivery.PrintTime` → `service_print_time`
   - `OpenTime` → `opened_at` (это root поле, не `Delivery.*`)

**Итог:** 7639 заказов заполнено, 92-100% покрытие

**Автоонбординг:**
```
RULE: Если new_client.olap_available:
  1. Определить точные field names в OLAP (может отличаться от iiko версии)
  2. Запустить Phase 6 + Phase 8 backfill
  3. Валидировать: opened_at должен быть 100%, остальные > 85%
```

### 3. ❌ Отсутствие данных за первый день (03.03)
**Проблема:** После подписки Shaburov на Events API — остановка на 1+ часа, пропущены события
- **Статусы:** 46 заказов остались в "Новая", "В пути", "Доставлена"
- **Причина:** Events API был перезагружен, Events subscriber переподписался на новый refresh token

**Решение:**
1. **cancel_sync job** — закрывает только отменённые заказы (фаза 1) и зависшие > 1 дня (фаза 2)
2. **Manual cleanup script** — для зависших за последний день
3. **Проверка перед онбордингом:** Убедиться что Events API стабилен 24+ часа

**Автоонбординг:**
```
RULE: После подписки на Events API:
  1. Жди 24 часа стабильности
  2. Запусти диагностику стоп-статусов: SELECT status, COUNT(*) WHERE status IN ('Новая', 'В пути', '...')
  3. Если > 1% — debug Events API логи
```

### 4. ❌ Отсутствие opened_at в исторических данных
**Проблема:** У 91% заказов Shaburov нет `opened_at` (только новые через Events API)
- **Невозможно считать:** `cooking_duration`, `total_duration`
- **OLAP решение:** Поле `OpenTime` возвращает 100% данных

**Решение:**
```sql
-- Phase 8: Восстановление из OLAP
UPDATE orders_raw SET opened_at = olap.OpenTime 
WHERE tenant_id = 3 AND opened_at IS NULL
```

**Итог:** opened_at заполнена 100%, `total_duration` работает для всех заказов

**Автоонбординг:**
```
RULE: Если opened_at < 90%:
  1. Попытка 1: fetch из OLAP.OpenTime
  2. Если нет — попытка 2: opened_at = cooked_time - 15min (дефолт估算)
  3. Если нет — opened_at = date (только дата)
```

### 5. ❌ Duration метрики не считались правильно
**Проблема:** `cooking_duration`, `total_duration` были пусты (нет `opened_at`)
- **Shaburov:** 4.1% `cooking_duration`, 8.9% `total_duration`
- **Невозможно:** Анализировать скорость готовки по станциям

**Решение:** Phase 7 + Phase 8 (opened_at) → **92% `cooking_duration`, 100% `total_duration`**

## 🚀 Phases of Backfill (Standardized)

### Phase 1-5: Initial Data Load (Existing)
- Sync desde OLAP v2 (основные поля)
- Sync из Events API (текущее состояние)
- Sync из бизнес-системы (платежи, бонусы)

### Phase 6: OLAP Time Fields Enrichment ✅
```python
# Fetch из OLAP и заполнить:
- opened_at (OpenTime)
- cooked_time (Delivery.CookingFinishTime)
- ready_time (Delivery.BillTime or Delivery.ReadyTime)
- send_time (Delivery.SendTime)
- service_print_time (Delivery.PrintTime)

# Ожидание: > 85% для всех
```

### Phase 7: Calculate Derived Metrics ✅
```python
# После заполнения base time fields:
- cooking_duration = cooked_time - opened_at
- idle_time = ready_time - cooked_time
- delivery_duration = actual_time - send_time
- total_duration = actual_time - opened_at

# Ожидание: > 95% для delivery_duration, >= 90% для остальных
```

### Phase 8: Recovery Missing Critical Fields ✅
```python
# Если поле < 90% заполнено:
- opened_at: fetch OpenTime из OLAP или estimate
- других fields: fetch из OLAP или дефолт NULL
```

## 🔍 Validation Checklist (Before Production)

| Field | Min % | Check | Fix |
|-------|-------|-------|-----|
| `order_num` | 100 | SELECT COUNT(*) FILTER (WHERE delivery_num IS NULL) | N/A — ошибка |
| `status` | 100 | SELECT DISTINCT status | Валидация статусов |
| `actual_time` | 98 | OLAP sync | ✅ обычно 100% |
| `opened_at` | 90 | Phase 8 backfill | Заполнить из OpenTime |
| `cooked_time` | 85 | Phase 6 backfill + Events | ✅ обычно 90%+ |
| `ready_time` | 85 | Phase 6 backfill + Events | ✅ обычно 90%+ |
| `send_time` | 85 | Phase 6 backfill + Events | ✅ обычно 90%+ |
| `cooking_duration` | 85 | Phase 7 calculate | Проверить после Phase 8 |
| `total_duration` | 95 | Phase 7 calculate | Должен быть 100% если opened_at + actual_time OK |

## 🐛 Critical Bugs to Prevent

| Bug | Detection | Prevention |
|-----|-----------|-----------|
| Events API пишет в `tenant_id=1` | Мониторинг tenant_id в orders_raw по событиям | Жестко require tenant_id в BranchState |
| OLAP field names неправильные | 400 ошибка при бэкфиле | Диагностика OLAP полей перед backfill |
| opened_at отсутствует | < 50% заполнения | Phase 8 automated recovery |
| Status зависание (Новая, В пути 24+ часа) | SELECT WHERE created_at < 24h AND status NOT IN (...) | Automated stale order detection |
| Дублирование из Events API | UNIQUE constraint violations | Идемпотентность upsert + tenant_id |

## 📝 Configuration Template for New Client

```yaml
new_client:
  tenant_id: 3
  name: "Shaburov"
  
  branches:
    - name: "Kansk_1"
      bo_url: "https://yobidoyobi-kansk.iiko.it/resto"
      bo_login: "lazarevich"
      bo_password: "<encrypted>"
      tenant_id: 3  # CRITICAL: явно указать
      
    - name: "Zelenogorsk_1"
      bo_url: "https://ebidoebi-zelenogorsk-shaburov.iiko.it/resto"
      bo_login: "lazarevich"
      bo_password: "<encrypted>"
      tenant_id: 3  # CRITICAL: явно указать
  
  onboarding:
    phases: [1, 2, 3, 4, 5, 6, 7, 8]
    olap_field_mapping:
      opened_at: "OpenTime"
      cooked_time: "Delivery.CookingFinishTime"
      ready_time: "Delivery.BillTime"  # NOT ReadyTime!
      send_time: "Delivery.SendTime"
      service_print_time: "Delivery.PrintTime"
    
    validation:
      opened_at_min_pct: 90
      cooked_time_min_pct: 85
      ready_time_min_pct: 85
      send_time_min_pct: 85
      total_duration_min_pct: 95
```

## 🤖 Automated Onboarding Flow (Future)

```
1. Create tenant_id in DB
2. Register branches with explicit tenant_id
3. Start Events API subscriber (with branch tenant_id mapping)
4. Wait 24h for stability check
5. Phase 1-5: Load historical data (OLAP)
6. Phase 6: OLAP time fields backfill → validate > 85%
7. Phase 7: Calculate durations → validate > 90%
8. Phase 8: Recovery missing fields → validate > 95%
9. Smoke tests:
   - SELECT * FROM orders_raw WHERE tenant_id = X LIMIT 10
   - Проверить: все required fields заполнены
   - SELECT cooking_duration FROM orders_raw WHERE tenant_id = X AND cooking_duration IS NOT NULL LIMIT 5
10. Mark as READY_FOR_PRODUCTION
```

## 📊 Metrics that Changed (Shaburov Before vs After)

| Metric | Before Onboarding | After Phases 1-8 | Change |
|--------|---|---|---|
| opened_at | 8.9% | 100% | +91.1pp |
| cooked_time | 0% | 92% | +92pp |
| cooking_duration | 0% | 92% | +92pp |
| total_duration | 0% | 100% | +100pp |
| Ready for Analytics | ❌ | ✅ | ✅ |

## 🔑 Key Learnings for Auto-Onboarding

1. **OLAP field names differ between iiko versions** — Нельзя hardcode, нужна диагностика
2. **Events API needs stabilization period** — 24h before validating data
3. **Multi-tenancy must be enforced at data entry** — Не default, явно в конфиге
4. **Historical data recovery is non-negotiable** — Без неё аналитика бесполезна
5. **Validation gates at каждом phase** — Не пробивать дальше если что-то < требуемого %
6. **Idempotency in backfill scripts** — Они должны работать несколько раз без ошибок

---

**Session where discovered:** Session 49-51 (4 March 2026)
**Client:** Shaburov (tenant_id=3, Kansk + Zelenogorsk + Izhevsk)
**Total orders backfilled:** 7,639
**Final data quality:** 92-100% across all critical fields

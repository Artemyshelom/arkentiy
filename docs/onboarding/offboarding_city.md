# Протокол отключения города от подписки

> Создан по итогам отключения Ижевска (Шабуров, 09.03.2026).
> Применять при полном отключении города/точки от мониторинга Аркентия.

---

## Когда применять

- Клиент закрыл точку или продал её
- Клиент временно приостанавливает мониторинг города
- iiko-сервер недоступен и не восстановится
- Клиент снизил тариф (убирает города из подписки)

**Не путать с временным отключением алертов** — для этого есть `/тишина`.

---

## Чеклист отключения

### 1. БД: выключить точку

```sql
-- Отключить из мониторинга (Events API перестанет опрашивать после рестарта)
UPDATE iiko_credentials
SET is_active = false, updated_at = now()
WHERE tenant_id = <tenant_id> AND branch_name = '<ветка>';

-- Проверить
SELECT branch_name, is_active FROM iiko_credentials
WHERE tenant_id = <tenant_id>;
```

### 2. БД: отключить Telegram-чаты города

```sql
-- Чаты, привязанные к конкретному городу
UPDATE tenant_chats
SET is_active = false
WHERE tenant_id = <tenant_id> AND city::text ILIKE '%<Город>%';

-- Проверить
SELECT chat_id, name, is_active, city FROM tenant_chats
WHERE tenant_id = <tenant_id>;
```

### 3. Migration: зафиксировать is_active=false навсегда

**Критично!** Миграции запускаются при каждом рестарте контейнера. Если в SQL-файле онбординга клиента стоит `is_active=true` с `ON CONFLICT DO NOTHING` — строка останется отключённой. Но если кто-то когда-то удалит строку из БД и рестартует — она создастся заново с `is_active=true`.

Поэтому: найти в `app/migrations/00N_SLUG_onboarding.sql` INSERT этой точки и изменить:

```sql
-- БЫЛО:
INSERT INTO iiko_credentials (..., is_active, ...)
VALUES (..., true, ...)
ON CONFLICT (tenant_id, branch_name) DO NOTHING;

-- НАДО:
-- Город отключён (is_active=false) — точка выведена из подписки DD.MM.YYYY
INSERT INTO iiko_credentials (..., is_active, ...)
VALUES (..., false, ...)
ON CONFLICT (tenant_id, branch_name) DO UPDATE SET is_active = false;
```

Аналогично для `tenant_chats` в том же файле:
```sql
-- БЫЛО:
ON CONFLICT (tenant_id, chat_id) DO UPDATE SET is_active = true, ...

-- НАДО:
-- Чат отключён вместе с городом DD.MM.YYYY
ON CONFLICT (tenant_id, chat_id) DO UPDATE SET is_active = false, ...
```

### 4. Перезапустить контейнер

```bash
ssh arkentiy 'cd /opt/ebidoebi && docker compose up -d --build app'
```

Убедиться что в логах при первом тике **нет** URL отключённого сервера:

```bash
ssh arkentiy 'docker logs $(docker ps -qf name=ebidoebi-integrations) --since 2m 2>&1 | grep -v getUpdates | grep "HTTP Request"'
```

### 5. Очистить мусорные данные (если успели накопиться)

Если между выключением `is_active` и рестартом контейнера прошло время — могли накопиться строки.

```sql
-- Проверить
SELECT count(*) FROM orders_raw WHERE branch_name = '<ветка>';
SELECT count(*) FROM shifts_raw WHERE branch_name = '<ветка>';
SELECT count(*) FROM hourly_stats WHERE branch_name = '<ветка>';

-- Удалить (в транзакции)
BEGIN;
DELETE FROM orders_raw   WHERE branch_name = '<ветка>' AND tenant_id = <tenant_id>;
DELETE FROM shifts_raw   WHERE branch_name = '<ветка>' AND tenant_id = <tenant_id>;
DELETE FROM hourly_stats WHERE branch_name = '<ветка>' AND tenant_id = <tenant_id>;
-- daily_stats обычно можно оставить — исторические данные за прошлые дни не вредят
COMMIT;
```

### 6. Обновить документацию

- [ ] `docs/onboarding/registry.md` — обновить запись клиента: убрать город из активных, добавить в «отключённые» с датой
- [ ] Деплой: `git commit -m "fix: отключить <Город> (<Клиент>)"` + `git push`

---

## Что НЕ делать

❌ **Не удалять строку из `iiko_credentials`** — при следующем рестарте migration вставит её заново с `is_active=true` (если только не изменить сам SQL файл как в п.3).

❌ **Не менять только SQL в migration без изменения БД** — изменение в файле вступит в силу только после следующего рестарта контейнера.

❌ **Не перезапускать контейнер без п.3** — после рестарта migration снова включит точку, если в файле `is_active=true`.

---

## Почему Ижевск продолжал писать данные после is_active=false

Случай из практики (09.03.2026):

1. В БД поставили `is_active=false` для Ижевска
2. **Контейнер не перезапускали** → `_branches_cache` в памяти остался прежним (с Ижевском)
3. `poll_all_branches()` каждые 30 сек брал ветки из памяти → продолжал опрашивать Ижевск
4. После рестарта `load_branches_cache()` прочитал `WHERE is_active=true` → Ижевск исключён
5. **Но**: migration 004 при каждом старте делал `ON CONFLICT DO NOTHING` для `tenant_chats` Ижевска с `is_active=true` → если чат был активен, строка не менялась

**Вывод:** `is_active=false` в БД работает **только после рестарта контейнера** + migration должен гарантировать `is_active=false` через `DO UPDATE SET is_active=false`.

---

## Журнал отключений

| Дата | Клиент | Город | Причина | Статус |
|------|--------|-------|---------|--------|
| 09.03.2026 | Шабуров (T3) | Ижевск | iiko-сервер недоступен, OLAP timeout | ✅ Отключён |

# Onboarding — бэкфилл данных

## Скрипты

| Скрипт | Таблица | Источник |
|--------|---------|---------|
| `backfill_new_client.py` | **Мастер, 5 шагов** | — |
| `backfill_orders_generic.py` | `orders_raw` | iiko OLAP |
| `backfill_daily_stats_generic.py` | `daily_stats` (revenue, cash, noncash) | iiko OLAP |
| `backfill_timing_stats.py` | `daily_stats` (тайминги, new_customers) | orders_raw (БД) |
| `backfill_shifts_generic.py` | `shifts_raw` | iiko schedule API |
| `backfill_hourly_stats.py` | `hourly_stats` | orders_raw + shifts_raw (БД) |
| `set_chat_avatars.py` | аватарки чатов | iiko |

### Порядок шагов

```
1 → orders_raw          (iiko OLAP)
2 → daily_stats OLAP    (iiko OLAP)
3 → daily_stats timing  (БД, orders_raw)
4 → shifts_raw          (iiko API)   ← должен быть ДО шага 5
5 → hourly_stats        (БД)
```

---

## Как запустить

> Скажи Копилоту: «дозаполни тенанта N с YYYY-MM-DD» — он выдаст готовую команду для конкретного кейса.

### Новый клиент / полный бэкфилл

```bash
ssh arkentiy && cd /opt/ebidoebi
screen -S bf_tenantN

docker compose exec app python -m app.onboarding.backfill_new_client \
    --tenant-id N \
    --date-from YYYY-MM-DD \
    --date-to YYYY-MM-DD

# Ctrl+A, D — выйти без остановки
# Вернуться: screen -r bf_tenantN
```

Если один из городов недоступен — добавь `--skip-cities "НазваниеГорода"`.

### Дозаполнить только конкретные таблицы

```bash
# shifts + hourly (orders и daily уже есть)
docker compose exec app python -m app.onboarding.backfill_new_client \
    --tenant-id N --date-from YYYY-MM-DD --date-to YYYY-MM-DD --steps 4,5

# только тайминги daily_stats (avg_cooking_min, new_customers и т.д.)
docker compose exec app python -m app.onboarding.backfill_timing_stats \
    --tenant-id N --date-from YYYY-MM-DD --date-to YYYY-MM-DD

# только shifts_raw
docker compose exec app python -m app.onboarding.backfill_shifts_generic \
    --tenant-id N --date-from YYYY-MM-DD --date-to YYYY-MM-DD

# только hourly_stats (shifts_raw уже должны быть!)
docker compose exec app python -m app.onboarding.backfill_hourly_stats \
    --tenant-id N --date-from YYYY-MM-DD --date-to YYYY-MM-DD
```

---

## Диагностика — что уже есть по тенанту

```sql
SELECT
  (SELECT MIN(date)||' - '||MAX(date) FROM orders_raw   WHERE tenant_id=N) as orders,
  (SELECT MIN(date)||' - '||MAX(date) FROM daily_stats  WHERE tenant_id=N) as daily,
  (SELECT MIN(date)||' - '||MAX(date) FROM shifts_raw   WHERE tenant_id=N) as shifts,
  (SELECT MIN(hour::date)||' - '||MAX(hour::date) FROM hourly_stats WHERE tenant_id=N) as hourly;
```

---

## Архив

`archive/` — устаревшие скрипты (не удалять, нужны как справочник).

# Справочник БД Аркентия

**Модуль:** `app/database_pg.py` — PostgreSQL: все таблицы, UPSERT-функции, инициализация пула.

## Таблицы

### Мультитенант
| Таблица | Назначение |
|---------|-----------|
| `tenants` | Клиенты SaaS: slug (уник.), email, plan, status, bot_token |
| `subscriptions` | Тариф клиента: модули, кол-во точек, дата оплаты |
| `iiko_credentials` | Точки по тенанту: bo_url, bo_login, dept_id, utc_offset, is_active |
| `tenant_chats` | Telegram-чаты тенанта: chat_id, modules_json, city |
| `tenant_users` | Пользователи тенанта: tg user_id, role, is_active |

### Данные (все с tenant_id)
| Таблица | Назначение |
|---------|-----------|
| `orders_raw` | Заказы: статус, курьер, тайминги, состав, источник — Events API + OLAP обогащение |
| `daily_stats` | OLAP-итоги дня: выручка, чеки, COGS, скидки, тайминги |
| `shifts_raw` | Смены сотрудников: employee_id, role_class, clock_in/out |
| `hourly_stats` | Почасовая статистика (из orders_raw + shifts_raw); начало часа в UTC (TIMESTAMPTZ) |

### Real-time / служебные
| Таблица | Назначение |
|---------|-----------|
| `iiko_tokens` | Кэш токенов iiko BO (TTL ~15 мин) |
| `job_logs` | История запусков задач |
| `competitor_snapshots` | Скрапинг конкурентов: дата, статус |
| `competitor_menu_items` | Меню конкурентов: позиция, цена, история |

## Правила

- **UPSERT:** `INSERT ... ON CONFLICT DO UPDATE SET ...` (asyncpg). `INSERT OR REPLACE` — SQLite, не использовать
- **Не удаляй строки** — помечай `is_active=false` или `deleted_at`
- **Читай `database_pg.py`** перед добавлением таблицы — там шаблон
- **Новая таблица** → SQL-миграция в `app/migrations/`, функция в `database_pg.py`
- **Запросы из job-модулей** — через функции `database_pg.py`, не raw SQL inline
- **Изоляция тенанта:** `tenant_id` обязателен во всех запросах к бизнес-таблицам — антипаттерн `get_daily_stats()` без tenant_id является ошибкой

## hourly_stats — инвариант временных зон

**Миграция 016** (15.03.2026): `hour` → `TIMESTAMPTZ aware UTC`.

### Инвариант (КРИТИЧНО)

- `orders_raw.opened_at`, `shifts_raw.clock_in/out` — **TEXT** в **местном времени филиала** (KSK UTC+7)
- `hourly_stats.hour` — **TIMESTAMPTZ**, хранит **UTC aware instant**
  - Семантика: начало часа по **местному времени** филиала, сохранённое как UTC
  - Пример: local 15:00 KSK = UTC 08:00 → `2026-03-08 08:00:00+00`
  - `hour AT TIME ZONE 'Asia/Krasnoyarsk'` → `2026-03-08 15:00:00` (местное)

### Python контракт

- `aggregate_hour(hour_utc)` — параметр всегда **aware UTC datetime**, иначе `AssertionError`
- `get_hourly_stats(hour_from, hour_to)` — input = **naive LOCAL datetime** (calendar date)
  - `'2026-03-08'` интерпретируется как `2026-03-08 00:00 KSK` → конвертируется в UTC для WHERE
  - Функция сама конвертирует: `dt.replace(tzinfo=DEFAULT_TZ).astimezone(tz.utc)`
- API ответ `_build_hourly()` — `hour.astimezone(DEFAULT_TZ).isoformat()` возвращает с tz-info (local)

### Session timezone

Оба asyncpg пула имеют `server_settings={'timezone': 'UTC'}`:
- `app/database_pg.py:init_db()` — main pool
- `app/onboarding/backfill_hourly_stats.py:init_db()` — backfill pool

Это гарантирует что `hour::date`, `EXTRACT()` и текстовое отображение всегда в UTC. Для local — явный `AT TIME ZONE 'Asia/Krasnoyarsk'`.

### Когда сломается (checklist)

- ❌ Пишешь `hour_start.replace(tzinfo=None)` → коллизия с aware UTC в TIMESTAMPTZ
- ❌ Пишешь `datetime.now(timezone.utc)` в job_recalc без итерации по LOCAL calendar day → дыра в данных
- ❌ Забыл `server_settings={'timezone': 'UTC'}` в новом пуле → сравнения по `hour::date` сломаются
- ❌ Читаешь `get_hourly_stats('2026-03-08 15:00', ...)` ожидая UTC → неправильный диапазон (забыл что это local input)

### Мониторинг

Precheck перед изменением:
```sql
SELECT COUNT(*), COUNT(DISTINCT (tenant_id, branch_name, hour))
FROM hourly_stats;
-- Должны быть равны (UNIQUE на (tenant_id, branch_name, hour))
```

Round-trip sanity check:
```sql
SELECT hour, hour AT TIME ZONE 'Asia/Krasnoyarsk' AS local_hour, orders_count
FROM hourly_stats
WHERE (hour AT TIME ZONE 'Asia/Krasnoyarsk')::date = CURRENT_DATE - 1
LIMIT 5;
-- local_hour должна быть в рабочее время (10:00–23:00 KSK), не в ночь
```

# Справочник БД Аркентия

**Модуль:** `app/database.py` — все таблицы, UPSERT-функции, инициализация, пул.

## Таблицы

| Таблица | Назначение |
|---------|-----------|
| `iiko_tokens` | Кэш токенов iiko BO (TTL ~15 мин) |
| `orders_raw` | Заказы из Events API: статус, курьер, время, опоздание |
| `shifts_raw` | Смены: employee_id, role_class, clock_in/out по точкам |
| `daily_stats` | OLAP-итоги дня по точкам (выручка, чеки, с/с) |
| `daily_rt_snapshot` | RT-итоги дня: delays + staff (утренние и пт/сб отчёты) |
| `job_logs` | История запусков задач |
| `telegram_queue` | Очередь TG сообщений (retry) |
| `stoplist_state` | Хэши стоп-листов (дедупликация алертов) |
| `report_updates` | Флаги изменения данных |
| `competitor_snapshots` | Скрапинг конкурентов: дата, статус |
| `competitor_menu_items` | Меню конкурентов: позиция, цена, история |

Полная схема с полями → `99_Системное/Интегратор/Архитектура.md`.

## Правила

- **UPSERT:** `INSERT ... ON CONFLICT DO UPDATE SET ...` (PostgreSQL). `INSERT OR REPLACE` НЕ работает
- **Не удаляй строки** — помечай `is_active=0` или `deleted_at`
- **Читай `database.py`** перед добавлением таблицы — там шаблон
- **Новая таблица** → в `database.py`, не отдельный файл
- **Запросы из job-модулей** — через функции `database.py`, не raw SQL inline

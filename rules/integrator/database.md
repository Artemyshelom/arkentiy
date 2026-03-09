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
| `hourly_stats` | Почасовая статистика (из orders_raw + shifts_raw) |

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

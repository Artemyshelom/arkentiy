# Onboarding — Подключение новых клиентов

Скрипты и инструменты для автоматизации процесса подключения новых SaaS-клиентов.

## Содержимое

- `backfill_shaburov.py` — Пример: бэкфилл OLAP-данных для Shaburov (Канск, Зеленогорск)
- `backfill_daily.py` — Шаблон для ежедневного синхронизационного бэкфилла

## Как использовать

1. Скопируй и адаптируй `backfill_shaburov.py` под нового клиента
2. Обнови параметры: `tenant_id`, `branch_names`, `iiko_url`
3. Запусти: `python -m app.onboarding.backfill_<client_slug>`
4. Проверь логи в `/opt/ebidoebi/logs/`

## Миграции

Используй шаблон из `docs/rules/migration-template.sql` для создания SQL миграции нового клиента.

## Пример подключения клиента

```python
# 1. Миграция
app/migrations/00X_<client>.sql

# 2. Бэкфилл (если нужны исторические данные)
python -m app.onboarding.backfill_<client>

# 3. Проверка в боте
/статус — должны видеться только его города
/доступ — только его чаты
```

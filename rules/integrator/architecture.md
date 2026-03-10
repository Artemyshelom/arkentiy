# Архитектура — Проект «Аркентий»

> Актуально: март 2026. Мультитенант: Артемий (tenant_id=1, 9 точек) + Шабуров (tenant_id=3, 2 активных города). PostgreSQL.

---

## Файловая структура на VPS

```
/opt/ebidoebi/
├── app/
│   ├── main.py                  # FastAPI + APScheduler: регистрация всех jobs
│   ├── config.py                # Pydantic settings, читает .env
│   ├── database_pg.py           # PostgreSQL: все таблицы, UPSERT-функции, пул
│   ├── ctx.py                   # ContextVar tenant_id — текущий тенант в запросе
│   ├── clients/
│   │   ├── iiko_auth.py         # Единый менеджер токенов/куки iiko (ОБЯЗАТЕЛЬНО использовать)
│   │   ├── iiko_bo_olap_v2.py   # iiko BO OLAP v2 (основной, JSON, token-auth)
│   │   ├── iiko_bo_events.py    # iiko BO Events API (real-time: заказы, смены)
│   │   ├── iiko.py              # iiko Cloud API (стоп-лист, номенклатура)
│   │   ├── telegram.py          # Telegram Bot API
│   │   ├── google_sheets.py     # Google Sheets API v4 + Drive API v3
│   │   └── tbank_reconciliation.py  # Сверка эквайринга ТБанк
│   ├── jobs/
│   │   ├── arkentiy.py          # Диспетчер бота — все команды, мультитенант
│   │   ├── iiko_status_report.py
│   │   ├── iiko_to_sheets.py
│   │   ├── daily_report.py
│   │   ├── late_alerts.py
│   │   ├── hourly_stats.py      # Почасовая статистика → hourly_stats
│   │   ├── olap_enrichment.py   # Обогащение orders_raw из OLAP (тайминги, источник)
│   │   ├── cancel_sync.py       # Синхронизация статуса «Отменена» каждые 3 мин
│   │   └── competitor_monitor.py
│   ├── services/                # Бизнес-логика: access_manager, auth
│   ├── routers/                 # FastAPI endpoints: cabinet, payments, stats, auth
│   ├── onboarding/              # Скрипты бэкфилла (backfill_new_client.py и др.)
│   ├── monitoring/
│   │   └── healthcheck.py
│   └── migrations/              # SQL-миграции (Alembic not used — прямые .sql файлы)
├── secrets/
│   ├── api_keys.json            # Ключи API (iiko, TG, Sheets и др.)
│   └── google-service-account.json
├── web/                         # Личный кабинет (HTML/JS, Jinja2)
├── .env                         # Переменные окружения
├── Dockerfile
└── docker-compose.yml
```

---

## Статус модулей

| Модуль | Статус | Заметки |
|--------|--------|---------|
| `arkentiy.py` | ✅ Работает | Диспетчер — все команды, мультитенант |
| `iiko_bo_events.py` | ✅ Работает | Real-time, polling 30с, все точки |
| `iiko_bo_olap_v2.py` | ✅ Работает | Основной OLAP (JSON, token-auth) |
| `iiko_to_sheets.py` | ✅ Работает | Ежедневная выгрузка в Sheets |
| `daily_report.py` | ✅ Работает | Утро + вечер, мультитенант |
| `late_alerts.py` | ✅ Работает | Алерты задержек >15/30/45 мин |
| `hourly_stats.py` | ✅ Работает | Почасовая статистика |
| `olap_enrichment.py` | ✅ Работает | Обогащение заказов из OLAP |
| `cancel_sync.py` | ✅ Работает | Синхронизация отмены каждые 3 мин |
| `competitor_monitor.py` | ✅ Работает | Пн 10:00 МСК |
| `healthcheck.py` | ✅ Работает | /health + TG-уведомление в личку Артемию |
| `sheets_to_tg.py` | ⚠️ Не тестировался | Нужна таблица "Сводка" |
| `iiko_stoplist.py` | 🔴 Отключён | Закомментирован в main.py |
| `pre_meeting.py` | 🔴 Отключён | Нет MYMEET_API_KEY |
| `task_tracker.py` | 🔴 Отключён | Нет BITRIX24_INCOMING_WEBHOOK |

---

## Расписание Jobs (APScheduler, МСК)

| Job | Когда (МСК) | Примечание |
|-----|------------|------------|
| Аркентий polling | каждые 3 сек | Все команды бота |
| Events polling | каждые 30 сек | Real-time, все точки всех тенантов |
| cancel_sync | каждые 3 мин | Отменённые заказы из OLAP |
| Алерты задержек | каждые 2 мин | >15/30/45 мин |
| Утренний отчёт | 09:25 | Данные вчера из БД |
| OLAP → Sheets | 09:26 | iiko OLAP → Google Sheets |
| Аудит | 09:27 | Подозрительные операции |
| Мониторинг конкурентов | пн 12:00 | Парсинг цен |
| Бэкап БД | 02:00 | `/opt/ebidoebi/backups/` |

---

## Конфиг точек (branches.json)

9 точек, у каждой свой сервер iiko BO:

| Точка | bo_url | Примечание |
|-------|--------|------------|
| Барнаул_1 Ана | `yobidoyobi-barnaul.iiko.it/resto` | |
| Барнаул_2 Гео | `yobidoyobi-barnaul-2.iiko.it/resto` | |
| Барнаул_3 Тим | `ebidoebi-barnaul-3.iiko.it/resto` | |
| Барнаул_4 Бал | `ebidoebi-barnaul-baltiiskaya.iiko.it/resto` | |
| Абакан_1 Кир | `ebidoebi-abakan.iiko.it/resto` | |
| Абакан_2 Аск | `ebidoebi-abakan-2.iiko.it/resto` | |
| Томск_1 Яко | `yobidoyobi-tomsk.iiko.it/resto` | |
| Томск_2 Дуб | `ebidoebi-tomsk-2.iiko.it/resto` | |
| Черногорск_1 Тих | `ebidoebi-chernogorsk-tihonova.iiko.it/resto` | |

**Добавить новую точку:** строка в `branches.json` с `bo_url`, `dept_id`, `city`, `utc_offset`. Код не трогать.

---

## Telegram боты и права доступа

### Боты проекта

| Бот | Token ID | Файл | Статус |
|-----|----------|------|--------|
| **Аркентий — Диспетчер** (`@arkentybot`) | `8479820766` | `arkentiy.py` | ✅ Активен — единственный бот |
| Арсений | `8392478039` | `telegram_commands.py` | 🔴 Отключён |

### Пользователи и права в Аркентии

| ID | Имя | Должность | Права |
|----|-----|-----------|-------|
| `255968113` | Артемий | Admin | Все команды, личка + группы |
| `822559806` | Илья | Менеджер | Все команды + Kyrgyz режим |
| `1332224372` | Светлана | Опер директор | Все команды (только группы) |
| `1011547016` | Кристина | Рег управляющий | Все команды (только группы) |
| `874186536` | Светлана | Менеджер ОКК | Только `/поиск`, `/помощь` (только группы) |

**Правила доступа:**
- Личка с ботом → только Артемий. Остальные — только в группах.
- В группах: неизвестный пользователь → бот молчит.
- Итоговые права = пересечение ограничений группы × ограничений пользователя.

### Группы и разрешённые команды

| Чат | Разрешённые команды |
|-----|---------------------|
| Личка | Только Артемий — все команды |
| Группа поиска (`5149932144`) | `/поиск`, `/помощь` |
| Группа аналитики (`5262858990`) | `/день`, `/опоздания`, `/помощь` |
| Другая группа | Все команды (fallback) |

### Команды Аркентия (`arkentiy.py`)

| Команда | Источник | Описание |
|---------|----------|----------|
| `/статус [фильтр]` | in-memory | Текущий статус всех точек |
| `/повара [фильтр]` | in-memory | Повара на смене |
| `/курьеры [фильтр]` | in-memory | Курьеры со статистикой |
| `/поиск <запрос>` | SQLite `orders_raw` | Поиск заказа по номеру |
| `/день [дата]` | SQLite | Сводка за день по точкам |
| `/опоздания [дата]` | SQLite | Список опоздавших заказов |
| `/помощь` | — | Справка |

**Kyrgyz режим для Ильи (`822559806`):**
- Первое сообщение за день → случайное приветствие на киргизском
- Команды `/статус`, `/повара`, `/курьеры` → подтверждение на киргизском
- Трекинг через `_greeted_today: dict[int, str]` (in-memory, {user_id: date_iso})

### Telegram функции (`clients/telegram.py`)
- `alert(text)` → группа (критические операционные алерты)
- `report(text)` → группа (ежедневные отчёты)
- `monitor(text)` → **личка Артемию через Аркентий** (старт сервера, ошибки задач)
- `error_alert(job_name, error)` → вызывает `monitor()`

**`monitor()` использует токен Аркентия** (`telegram_analytics_bot_token`), отправляет в `TELEGRAM_CHAT_MONITORING=255968113` (личка Артемия). Heartbeat отключён — уведомление только при старте.

---

## BranchState — структура real-time данных точки

```python
@dataclass
class BranchState:
    bo_url: str
    branch_name: str
    revision: int = 0
    deliveries: dict = {}       # deliveryNumber → {status, courier, sum, planned_time, actual_time}
    sessions: dict = {}         # user_id → {role_class, name, opened_at, closed_at}
    employees: dict = {}        # user_id → {name, role, role_class}
    cooking_statuses: dict = {} # order_num_int → "Приготовлено"|"Собран"

    # Свойства (вычисляются из deliveries/sessions):
    active_orders          # не в CLOSED_DELIVERY_STATUSES
    delivered_today        # Доставлена + Закрыта
    orders_before_dispatch # WAITING_STATUSES (Новая, Не подтверждена, Ждет отправки)
    orders_cooking         # WAITING + cookingStatus=="Приготовлено"
    orders_ready           # WAITING + cookingStatus=="Собран"
    orders_on_way          # "В пути к клиенту"
    cooks_on_shift         # role_class="cook" И closed_at is None
    couriers_on_shift      # role_class="courier" И closed_at is None
    total_cooks_today      # все кто открывал смену (включая ушедших)
    total_couriers_today   # то же для курьеров
    delay_stats            # {late_count, total_delivered, avg_delay_min}
```

**get_branch_rt(branch_name)** → возвращает словарь из всех свойств или `None` если данные не загружены.

**Fallback при рестарте / rollover:** если Events API даёт 0 событий (начало дня UTC+7), `_seed_sessions_from_db` подгружает смены из `shifts_raw`.

---

## PostgreSQL таблицы (актуально)

Полный справочник → `rules/integrator/database.md`

Ключевые группы:
- **Мультитенант:** `tenants`, `subscriptions`, `iiko_credentials`, `tenant_chats`, `tenant_users`
- **Данные:** `orders_raw`, `daily_stats`, `shifts_raw`, `hourly_stats`
- **Real-time:** `iiko_tokens`, `job_logs`, `competitor_snapshots`, `competitor_menu_items`

---

## Мультитенантность

| tenant_id | Клиент | Города (активные) |
|-----------|--------|------------------|
| 1 | Артемий (Ёбидоёби) | Барнаул (4), Абакан (2), Томск (2), Черногорск (1) |
| 3 | Шабуров | Канск, Зеленогорск |

**Изоляция:** все бизнес-таблицы имеют `tenant_id`. Конфиг точек — в `iiko_credentials` (не в `branches.json`).  
**Текущий тенант в запросе:** `app/ctx.py`, ContextVar `_ctx_tenant_id`.

---

## Конфиг: bank_accounts.json (per-tenant)

**Расположение:** `/opt/ebidoebi/secrets/bank_accounts.json` (на VPS, не в git, монтируется как volume)

**Предназначение:** маппинг банковских счетов → филиалы для обработки выписок и сверки эквайринга.

**Структура:**
```json
{
  "1": {
    "label": "Артемий",
    "acquiring_corr_account": "2.2.11.8",
    "commission_counterpart_inn": "11111111",
    "commission_counterpart_name": "КОМИССИЯ ЭКВАЙРИНГ",
    "accounts": {
      "40802810271710001923": {
        "label": "Томск-1",
        "short": "Т1 Яко",
        "city": "Томск",
        "iiko_branch": "Томск_1 Яко"
      },
      "40802810971710001922": {
        "label": "Томск-2",
        "short": "Т2 Дуб",
        "city": "Томск",
        "iiko_branch": "Томск_2 Дуб"
      }
    }
  },
  "3": {
    "label": "Шабуров",
    "acquiring_corr_account": "...",
    "accounts": { ... }
  }
}
```

**Правило масштабирования:** добавить нового тенанта = добавить новый ключ (`"<tenant_id>"`) в корень JSON. Код (`bank_statement.py`) менять не нужно.

---

## Архитектура: Backfill-оркестратор (5 шагов)

**Цель:** заполнить историческую аналитику для нового тенанта (orders, daily_stats, shifts, hourly_stats).

**Мастер-скрипт:** `app/onboarding/backfill_new_client.py`

```
┌─ step 1 ──┐  OrdersBackfiller      [iiko OLAP → orders_raw]
│  ORDERS   │  2-6 месяцев, 5-20K заказов
└───────────┘

┌─ step 2 ──┐  DailyStatsBackfiller  [iiko OLAP → daily_stats]
│ DAILY     │  cash/noncash разбивка
└───────────┘

┌─ step 3 ──┐  inline в backfill_new_client  [orders_raw → daily_stats тайминги]
│ TIMING    │  service_time, delivery_time (из Event API)
└───────────┘

┌─ step 4 ──┐  ShiftsBackfiller      [iiko schedule API → shifts_raw]
│ SHIFTS    │  историческое расписание сотрудников (до 5000+ смен)
└───────────┘

┌─ step 5 ──┐  HourlyStatsBackfiller [orders_raw + shifts_raw → hourly_stats]
│ HOURLY    │  почасовая аналитика (DDD, средние чеки, кол-во заказов)
└───────────┘
```

**Особенность:** каждый компонент (`OrdersBackfiller`, `ShiftsBackfiller` и т.д.) может запускаться **самостоятельно** (в `app/onboarding/<name>.py` есть `if __name__ == "__main__"`), а может быть вызван через `backfill_new_client.py`.

**Пример вызова step 1 отдельно:**
```bash
python3 -m app.onboarding.backfill_orders_generic <tenant_id> <branch_name> 2025-12-01
```

**Пример полного бэкфилла:**
```bash
python3 app/onboarding/backfill_new_client.py 3 yobidoyobi-kansk
# Автоматически запустит все 5 шагов последовательно
```

---

# Аркентий — SaaS-бот для автоматизации доставки на базе iiko

> **Techcore:** Python 3.11, FastAPI, APScheduler, PostgreSQL, Docker  
> **Пилотный клиент:** Ёбидоёби (4 города: Абакан, Барнаул, Томск, Черногорск)  
> **Статус:** Production ✅

---

## Что это

**Аркентий** — автоматизация управления доставкой через Telegram-бот. Интегрирует данные из **iiko BO** (система управления ресторанами), **Google Sheets** (для отчётов), **Telegram** (интерфейс), с перспективой расширения на **Битрикс24** и другие CRM.

### Основные команды бота

| Команда | Что делает |
|---------|-----------|
| `/статус [город]` | Real-time статус смены: количество доставок, опоздания, среднее время |
| `/смены [дата]` | История смен по точкам с разбором по сотрудникам |
| `/отчёты` | Ежедневные отчёты по выручке, чекам, себестоимости (из OLAP iiko) |
| `/стоп-лист [город]` | Алерты при пополнении стоп-листа (позиции недоступны) |
| `/конкуренты` | Мониторинг меню конкурентов (Яндекс Еда, 2GIS) |

---

## Архитектура

```
VPS (5.42.98.2)
├── Docker
│   ├── Python 3.11 + FastAPI (app)
│   ├── PostgreSQL (database)
│   └── APScheduler (jobs)
├── /opt/ebidoebi/app/
│   ├── main.py              — registry jobs (FastAPI + scheduler)
│   ├── clients/             — интеграции (iiko, Google, Telegram)
│   ├── jobs/                — периодические задачи
│   ├── database.py          — ORM + pool connections
│   └── utils/               — shared utilities
├── /opt/ebidoebi/secrets/
│   ├── .env                 — secrets & config
│   ├── branches.json        — конфиг точек (9 шт)
│   └── org_ids.json         — iiko org ID для каждой точки
└── /opt/ebidoebi/.git/      — backup in git

Локально (Cursor)
├── Разработка кода (app/)
├── SCP → VPS
├── Docker build
└── Git push
```

**Ключевые модули:**
- `app/clients/iiko_bo_events.py` — реал-тайм события доставок (Events API)
- `app/clients/iiko_bo_olap.py` — аналитика по дням (OLAP v2)
- `app/jobs/late_alerts.py` — алерты при опозданиях (15, 30, 45 мин)
- `app/jobs/daily_report.py` — ежедневные отчёты в Sheets & Telegram
- `app/jobs/competitors.py` — скрапинг конкурентов

**База данных:**
- `orders_raw` — заказы из Events API (статус, время, опоздание)
- `shifts_raw` — смены сотрудников по точкам
- `daily_stats` — OLAP-итоги дня (выручка, чеки, с/с)
- `daily_rt_snapshot` — RT-снимок для утренних отчётов
- `telegram_queue` — очередь сообщений (retry logic)
- И ещё 5+ для конкурентов, токенов, логов

Полная схема → [`99_Системное/Интегратор/Архитектура.md`](../../99_Системное/Интегратор/Архитектура.md)

---

## Запуск локально

### 1. Подготовка

```bash
# Клонируй репо
git clone git@github.com:Artemyshelom/arkentiy.git
cd arkentiy

# Создай .env (скопируй .env.example и заполни)
cp .env.example .env
# Добавь: IIKO_API_KEY, DATABASE_URL, TELEGRAM_TOKEN, GOOGLE_SA_JSON и т.д.
```

### 2. Development режим (без Docker)

```bash
# Создай venv
python3.11 -m venv venv
source venv/bin/activate

# Установи зависимости
pip install -r requirements.txt

# Запусти (локально, без Docker)
python app/main.py
```

### 3. Docker (как на VPS)

```bash
# Build
docker compose build

# Run
docker compose up -d

# Логи
docker compose logs -f app

# Проверка
docker compose ps  # должен быть "healthy"
```

---

## Деплой на VPS

**Протокол обязателен.** Нарушение = поломка production (живые заказы, живые деньги).

### Схема

```
Cursor (локально)
  ↓ [Edit code]
  ↓ [SCP + backup]
  ↓
VPS (5.42.98.2)
  ↓ [docker compose build --no-cache]
  ↓ [docker compose up -d]
  ↓ [check healthy + logs]
  ↓ [git add + push]
  ↓
GitHub (artemyshelom/arkentiy)
```

### Шаги

```bash
# 1. Разведка (что уже есть на VPS)
ssh -i ~/.ssh/artemii_vps root@5.42.98.2 "ls -la /opt/ebidoebi/app/"

# 2. Бэкап на VPS (ДО заливки)
ssh ... "cp /opt/ebidoebi/app/jobs/daily_report.py /opt/ebidoebi/app/jobs/daily_report.py.bak.$(date +%Y%m%d_%H%M%S)"

# 3. SCP (только НОВЫЕ файлы или ДЕЛЬТА, не всё целиком)
scp -i ~/.ssh/artemii_vps app/jobs/daily_report.py root@5.42.98.2:/opt/ebidoebi/app/jobs/

# 4. Сборка на VPS
ssh ... "cd /opt/ebidoebi && docker compose build --no-cache && docker compose up -d"

# 5. Проверка
sleep 10
ssh ... "docker compose ps"                      # healthy?
ssh ... "docker compose logs app --tail=20"      # нет ERROR?

# 6. Git push
ssh ... "cd /opt/ebidoebi && git add app/ && git commit -m 'feat: ...' && git push"
```

**Запрещено:**
- ❌ Копировать локальный файл целиком поверх VPS-файла без сравнения
- ❌ Перезаписывать `.env` целиком — только дописывать новые переменные
- ❌ Деплоить и уходить — всегда жди `healthy`

Полная инструкция → [`.cursor/rules/интегратор.mdc`](https://github.com/Artemyshelom/rules/blob/main/cursor/интегратор.mdc)

---

## Структура проекта

```
arkentiy/
├── app/
│   ├── main.py                  — FastAPI + scheduler registry
│   ├── database.py              — ORM, таблицы, UPSERT логика
│   ├── database_pg.py           — PostgreSQL backend
│   ├── db.py                    — proxy (SQLite или PG)
│   ├── clients/
│   │   ├── iiko_bo_events.py    — real-time заказы
│   │   ├── iiko_bo_olap.py      — аналитика iiko OLAP
│   │   ├── competitor_scraper.py — парсинг конкурентов
│   │   └── google_sheets.py     — экспорт в Sheets
│   ├── jobs/
│   │   ├── late_alerts.py       — алерты при опозданиях
│   │   ├── daily_report.py      — ежедневные отчёты
│   │   ├── competitors.py       — мониторинг конкурентов
│   │   └── ...
│   └── utils/
│       ├── iiko_auth.py         — авторизация в iiko BO
│       ├── telegram.py          — helper для Telegram API
│       └── formatting.py        — форматирование сообщений
├── docs/
│   ├── CHANGELOG.md             — что обновлено (user-friendly)
│   ├── Модули_и_команды_бота.md — описание команд
│   ├── Дорожная карта.md        — roadmap
│   ├── BACKLOG.md               — что планируем
│   └── specs/                   — UX specs для TG-бота и веб
├── web/
│   ├── index.html               — лендинг
│   ├── cabinet/                 — личный кабинет
│   ├── js/                      — frontend логика
│   ├── css/                     — стили
│   └── data/
│       └── chain.json           — конфиг сети
├── tests/                       — юнит-тесты (pytest)
├── dev/                         — debug скрипты (в .gitignore)
│
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

**Не в git:** `.env`, `secrets/`, `*.bak.*`, `dev/` (см. `.gitignore`)

---

## Разработка

### Новый модуль (job, client, утилита)

1. Создай в соответствующей папке (`app/jobs/`, `app/clients/`, `app/utils/`)
2. Добавь в `app/main.py` (реестр):
   ```python
   from app.jobs.my_new_job import my_new_job
   scheduler.add_job(my_new_job, "interval", hours=1, id="my_new_job")
   ```
3. Напиши тесты в `tests/test_my_new_job.py`
4. Обновить документацию:
   - `docs/CHANGELOG.md` — для пользователя
   - `docs/Модули_и_команды_бота.md` — для разработчика

### Debug скрипт

Не в корне! Создай в `dev/test_название.py`:
```python
"""
Тест: проверка авторизации в iiko Cloud API
Как запускать: python dev/test_iiko_auth.py
"""
# твой код тут
```

Скрипты в `dev/` автоматически в `.gitignore` — не волнуйся об случайном коммите.

---

## Документация

| Файл | Для кого | Что там |
|------|----------|--------|
| `docs/CHANGELOG.md` | **Пользователи** | Что обновлено (язык: команды бота, новые фичи, баги) |
| `docs/Модули...md` | **Разработчики** | Описание команд, модулей, API |
| `docs/Дорожная карта.md` | **PM** | Планы на квартал, фазы развития |
| `docs/BACKLOG.md` | **Разработчики** | Очередь задач (что не начинали) |
| `docs/specs/` | **UX/Разработчики** | Макеты и требования перед кодом |

**Правило:** перед реализацией UX-задачи — сначала spec, потом код. Не додумывай интерфейс сам.

---

## Интеграции

| Сервис | Статус | Что делает |
|--------|--------|-----------|
| **iiko BO Events API** | ✅ Production | Real-time заказы, смены, события доставки |
| **iiko BO OLAP v2** | ✅ Production | Аналитика по дням: выручка, чеки, себестоимость |
| **Telegram** | ✅ Production | Команды бота, алерты, отчёты |
| **Google Sheets** | ✅ Production | Экспорт отчётов (ежедневно, еженедельно) |
| **Яндекс Еда** | ✅ Production (скрапинг) | Мониторинг меню и цен конкурентов |
| **2GIS** | ✅ Production (скрапинг) | Мониторинг меню и цен конкурентов |
| **Битрикс24** | 🟡 Планируется | CRM интеграция (тск, сделки, контакты) |

---

## Контакты & Links

- **Repo:** https://github.com/Artemyshelom/arkentiy
- **Cursor Rules:** https://github.com/Artemyshelom/rules/tree/main/cursor
- **VPS:** `5.42.98.2` (Timeweb Cloud)
- **Main DB:** PostgreSQL в Docker
- **Telegram Bot:** @EbidoebiBotDev (dev), @EbidoebiBotProd (prod)
- **Alert Chat:** `-5160506328` (Ёбидоёби управление)
- **Monitoring:** `@artemii` (255968113, личка Артемия)

---

## 5 КРИТИЧЕСКИХ ПРАВИЛ

1. **Events API → сортировка по дате:** iiko возвращает события не в хронологическом порядке. Без сортировки 30-40% заказов имеют неверный финальный статус.

2. **deliveryOrderEdited → только merge:** событие содержит только изменённые поля. Использовать `update()`, не overwrite целиком.

3. **OLAP через /service/, не /api/:** `/api/reports/olap` сломан (500). Правильно: `GET /service/reports/report.jspx?presetId=UUID`.

4. **Клиентская фильтрация OLAP:** `departmentIds` не работает → получать все точки → фильтровать по `<Department>` = `branch["name"]`.

5. **XML с точками в тегах:** `findtext("Tag.With.Dots")` не работает в ElementTree. Итерировать: `for child in elem: if child.tag == "Tag.With.Dots"`.

---

## Разработчикам

Перед любой новой задачей прочитай:
- [`99_Системное/Интегратор/Архитектура.md`](../../99_Системное/Интегратор/Архитектура.md) — структура, модули, расписание
- [`99_Системное/Интегратор/API_iiko.md`](../../99_Системное/Интегратор/API_iiko.md) — iiko API (обязательно для iiko-задач!)
- [`99_Системное/Интегратор/Уроки_и_баги.md`](../../99_Системное/Интегратор/Уроки_и_баги.md) — известные ловушки
- [`docs/Журнал.md`](docs/Журнал.md) — что уже сделано по сессиям
- [`docs/Архив/Журнал_интегратора_legacy.md`](docs/Архив/Журнал_интегратора_legacy.md) — архив старого журнала

**Cursor Rules:** [`github.com/Artemyshelom/rules`](https://github.com/Artemyshelom/rules) — настройки Cursor IDE для этого проекта.

---

**Last updated:** 1 марта 2026

# Журнал изменений — Интегратор

> Что было сделано и когда. Читать чтобы понять текущее состояние системы.
>
> С 1 марта 2026 основной журнал ведётся в `02_Проекты/Аркентий/docs/Журнал.md`.
> Этот файл оставлен как архив предыдущих сессий.

---

## Сессия 38 — 1 марта 2026 (Форматирование /поиск + порядок в репо)

### Что сделано

**Форматирование `/поиск` — единый стиль по всем типам запросов:**
- Обнаружена проблема: в `jobs/arkentiy.py` поиск по телефону (`query_type == "phone"`) имел собственный инлайн-форматтер вместо `_format_order_compact`. Плюс локальная версия `_format_order_compact` была устаревшей (без 🛵/🚶).
- Обновлена `_format_order_compact` в `jobs/arkentiy.py` — теперь соответствует `app/arkentiy.py`: двухстрочный формат, иконка типа заказа (🛵 доставка / 🚶 самовывоз), город без `_N_XXX`, сумма без пробелов, опоздание с минутами (`🔴 +15м`).
- Phone-блок переписан: имя клиента один раз в заголовке, заказы через `_format_order_compact(r)`, пустые строки между заказами.
- Коммиты: `ffc9486` (первый деплой, некорректный — старая функция была перезаписана SCP), `53163d7` (исправлен).

**Порядок в репозитории:**
- Удалены `test_staff.py` и `test_yobi.py` из корня (старые debug-скрипты).
- `.gitignore` обновлён: добавлены `dev/` и `test_*.py`.
- Правило в `интегратор.mdc`: dev-скрипты только в `dev/`, никогда в корне.
- `.cursor/rules/` и `.cursorrules` перенесены из Аркентия в `github.com/Artemyshelom/rules/cursor/`.
- Создан `README.md` для Аркентий-репо.

### Антипаттерн (зафиксирован)
**SCP поверх VPS-файла без сравнения.** При деплое `jobs/arkentiy.py` локальная версия (со старой `_format_order_compact`) перезаписала VPS-версию (с новой). Итог — пользователь увидел сломанный формат. Нарушение протокола ШАГ 1 + ШАГ 3. Добавить в `Уроки_и_баги.md`.

### Изменённые файлы
- `app/jobs/arkentiy.py` → обновлена `_format_order_compact`, переписан phone-блок
- `.gitignore` → добавлены `dev/`, `test_*.py`
- `README.md` → создан с нуля

---

## Сессия 37 — 28 февраля 2026 (Аудит + фиксы расчёта опозданий)

### Что сделано
- **Аудит @пм → @аудитор**: полный аудит кода по опозданиям (`iiko_bo_events.py`, `late_alerts.py`, `arkentiy.py`) — найдено 7 проблем.
- **Фикс `delay_stats` (RT):** `payment_changed`-заказы теперь исключаются и из in-memory статистики (`/статус`), а не только из OLAP-агрегатов. Логика: пропускаем доставки у которых `"смен"` в комментарии — то же условие что в `_delivery_to_row`.
- **Рефакторинг `/смены`:** сырой SQL вынесен из `arkentiy.py` в `database_pg.py::get_payment_changed_orders` и `database.py::get_payment_changed_orders`. В хендлере теперь один вызов `from app.db import get_payment_changed_orders`.

### Технический долг (зафиксирован, не сделан)
| # | Что | Файл | Приоритет |
|---|---|---|---|
| 1 | `_parse_dt` определяется внутри цикла в `delay_stats` | `iiko_bo_events.py:213` | 🟡 |
| 2 | `staff_list` использует VPS-дату (UTC+3), а не UTC+7 точек | `iiko_bo_events.py:237` | 🟠 |
| 3 | `fresh_start` пересчитывается в двойном цикле | `late_alerts.py:167` | 🟡 |
| 4 | Порог в `delay_stats` (>0 мин) расходится с порогом алертов (15 мин) | `iiko_bo_events.py:218` | 🟡 |
| 5 | SQL-инъекция через f-string с `days` | `database_pg.py:1334,1359` | 🟡 |
| 6 | Проверить `webhook_secret` на VPS | `.env` | 🟡 |

### Изменённые файлы (локально, требуют деплоя)
- `app/clients/iiko_bo_events.py` → `delay_stats` фильтрует `payment_changed`
- `app/database_pg.py` → добавлена `get_payment_changed_orders`
- `app/database.py` → добавлена `get_payment_changed_orders`
- `app/jobs/arkentiy.py` → `_handle_payment_changes` использует `app.db`

---

## Сессия 36 — 28 февраля 2026 (payment_changed + личный кабинет)

### Что сделано (через OpenClaude, не Cursor)
- **`feat: исключение смен оплаты из статистики опозданий`** (`a713d9b`):
  - Добавлен флаг `payment_changed` в `orders_raw` (PG: `BOOLEAN DEFAULT false`, SQLite: `INTEGER DEFAULT 0`)
  - Определение: `LOWER(comment) LIKE '%смен%'`
  - В `aggregate_orders_for_daily_stats`: `late_count`, `late_pickup_count`, `avg_late_min` — `payment_changed` исключён
  - Новый счётчик `payment_changed_count`
  - Новая команда `/смены [фильтр] [дата]` — список заказов со сменой оплаты
  - Миграция `002_payment_changed.sql` с частичным индексом
  - Обновлено 491 историческая запись
- **`feat: личный кабинет клиента (MVP)`** (`8e0d37c`):
  - JWT авторизация (`/api/cabinet/auth/login`)
  - Страницы: dashboard, подписка, подключения, платежи, настройки
  - Адаптивная вёрстка, hamburger-меню на мобильном
  - `app/routers/cabinet.py` (228 строк) + `web/cabinet/` HTML/JS

### Статус после сессии
Задеплоено на VPS, healthy. Git push: `a713d9b`, `8e0d37c`, `de82d69`.

---

## Сессия 35 — 27 февраля 2026 (Аудит проекта + фикс модуля audit в access.py)

### Что сделано
- **Комплексный аудит Аркентия** (@пм → @аудитор + @ux-бот): проверено 34 py-файла, 4 specs, архитектура, база знаний.
- **Фикс [КРИТИЧНО]:** `audit` добавлен в `ALL_MODULES` и `MODULE_LABELS` в `access.py`. Раньше модуль работал в боте (`/аудит` проверяет `perms.has("audit")`), но не был виден в системе прав — чатам нельзя было его выдать через `/доступ`.

### Технический долг (зафиксирован, не сделан)
| # | Что | Файл | Приоритет |
|---|---|---|---|
| 1 | SQL-инъекция через f-string с `days` | `database_pg.py:1334,1359` | 🟡 |
| 2 | Проверить `webhook_secret` на VPS | `.env` | 🟡 |
| 3 | `LOCAL_UTC_OFFSET = 7` хардкод → читать из config | `cancel_sync.py:25` | 🟡 |
| 4 | CORS `allow_origins=["*"]` → ограничить перед релизом кабинета | `main.py:277` | 🟡 |
| 5 | `_states` импорт из iiko_bo_events в cancel_sync → публичный API | `cancel_sync.py:19` | 🟡 |
| 6 | `save_bank_statement_log` в PG — заглушка, logger.debug → warning | `database_pg.py:1197` | 🟢 |
| 7 | Дублирование SQL-логики точного времени в 4 местах | `database.py` + `database_pg.py` | 🟢 |
| 8 | Specs отсутствуют для 14 из 17 команд — `/отчёт`, `/опоздания`, `/курьеры` в приоритете | `specs/tg/` | 🟢 |

### Статус после сессии
Критичный баг исправлен. Требуется деплой `access.py`.

---

## Сессия 34 — 27 февраля 2026 (Постмиграционные фиксы: доступ чатов + выписки менеджеров)

### Что сделано
- **Диагностика `/доступ` — Чатов: 0**: `_db_cfg` (in-memory кэш access.py) заполняется при старте. Чаты были мигрированы в PG уже после запуска бота → кэш остался пустым. Фикс: `docker compose restart app` → кэш загрузился с 8 активными чатами.
- **Выписки от менеджеров игнорировались** в чате ФИНАНСЫ: `get_permissions(chat_id, user_id)` до перезапуска видел пустой `_db_cfg["chats"]` → падал в `.env fallback` → давал `ALL_MODULES - {"finance"}` → `perms.has("finance") = False` → файл молча дропался. После перезапуска ФИНАНСЫ-чат (`modules: ["finance"]`) появился в кэше → любой участник чата получает finance-права → файлы обрабатываются.
- **Root-cause**: не отсутствие кода, а порядок событий: миграция данных без перезапуска сервиса.

### Статус после сессии
Все 6 ранее нерабочих команд `/поиск`, `/отчет`, `/тбанк`, `/выгрузка`, `/аудит`, `/доступ` — работают. Банковские выписки принимаются от любого участника чата ФИНАНСЫ.

---

## Сессия 33 — 26 февраля 2026 (Финальная зачистка: выгрузка + миграция чатов)

### Что сделано
- `marketing_export.py`: добавлен хелпер `_pg_args(args)` — конвертирует ISO-строки `YYYY-MM-DD` в `datetime.date` перед передачей в `pool.fetch`. `/выгрузка` теперь работает полностью.
- Инструментация отлажена, подтверждена логами (`args_before: ['2026-02-01', ...]` → `args_after: [datetime.date(2026, 2, 1), ...]`), затем удалена.
- `tenant_chats` (PostgreSQL): мигрированы все 10 чатов из SQLite. `/доступ` теперь показывает Чатов: 10.

**Git:** `8272326..53f2c5b`

---

## Сессия 32 — 26 февраля 2026 (Полный порт на PG — 3 модуля + tbank + date-фикс)

### Что сделано
**Модули — убраны все BACKEND guards, полноценный PG:**
- `audit.py`: `_detect_from_orders_raw` и `_detect_unclosed_in_transit` — переписаны на `asyncpg` (`pool.fetch`, `date::text = $1`, `is_self_service = false`). Убран `import aiosqlite`. `/аудит` теперь работает.
- `cancel_sync.py`: обе фазы (отменённые + зависшие заказы) — переписаны на `pool.execute`, `$N` плейсхолдеры, `date::text < $1`. `/cancel_sync` теперь работает.
- `marketing_export.py`: добавлен хелпер `_to_pg_sql()` (конвертирует `?`→`$N`, SQLite-бул 0/1 → true/false), выполнение через `pool.fetch`. `/выгрузка` теперь работает.

**database_pg.py:**
- `save_audit_events_batch`: `created_at` ISO-строка → `datetime.fromisoformat()` для TIMESTAMPTZ
- `save_tbank_registry_log`, `upsert_online_payment`, `confirm_online_payment`, `confirm_payout`, `record_chargeback`, `get_payout_delayed`, `get_pending_payments`, `get_overdue_payments`, `get_tracking_summary` — полноценные PG реализации вместо стабов
- `record_data_update`, `get_updates_for_date`, `clear_updates_for_date`, `save_rt_snapshot`, `get_rt_snapshot`, `clear_audit_events`, `get_audit_events` — все date-параметры обёрнуты в `_to_date()`

**Таблицы в PostgreSQL (новые):**
- `online_payments` — создана, мигрировано 665 записей из SQLite
- `tbank_registry_logs` — создана, мигрировано 41 запись

**Git:** `4a7..c9a`

---

## Сессия 31 — 26 февраля 2026 (Миграция исторических данных + date-тип фиксы)

### Что сделано
- Создан и выполнен `migrate_to_pg.py` внутри контейнера: мигрировано 377k+ `orders_raw`, 4k `daily_stats`, 661 `shifts_raw`
- Добавлен хелпер `_to_date()` в `database_pg.py` — конвертирует ISO str в `datetime.date` для asyncpg
- Применён ко всем DATE-колонкам: `upsert_orders_batch`, `upsert_shifts_batch`, `close_stale_shifts`, `upsert_daily_stats_batch`, `get_daily_stats`, `get_today_shifts`
- Добавлены недостающие колонки в PG: `has_problem`, `problem_comment`, `bonus_accrued`, `return_sum`, `service_charge`, `cancel_comment` (ALTER TABLE)
- `/поиск` и `/отчет` — работают

---

## Сессия 30 — 26 февраля 2026 (Переключение прода на PostgreSQL)

### Бэкап перед изменением
- `docker-compose.yml` → добавление сервиса postgres
- `.env` → DATABASE_URL → postgresql, PG_PASSWORD добавлен

### Что сделано
- `docker-compose.yml` обновлён: добавлен сервис `postgres` (mirror.gcr.io/library/postgres:16-alpine, pgdata volume, healthcheck)
- `DATABASE_URL` переключён на `postgresql://ebidoebi:...@postgres:5432/ebidoebi`
- `database_pg.py` дополнен: портированы `aggregate_orders_today`, `aggregate_orders_for_daily_stats`, `get_period_stats`, `get_exact_time_orders` (PG-время через EXTRACT EPOCH); добавлены стабы tbank-функций
- Оба контейнера `healthy`, polling работает, jobs запланированы (9 задач)
- Git: `0fb44fc`

---

## Сессия 29 — 26 февраля 2026 (PostgreSQL SaaS migration — деплой на прод)

### Бэкап перед изменением (timestamp: 20260226_212732)
- `app/database.py` → BACKEND sentinel, get_branches, get_active_tenants_with_tokens
- `app/config.py` → dynamic branches from db/json
- `app/main.py` → multi-bot polling loop via get_active_tenants_with_tokens
- `app/jobs/arkentiy.py` → ContextVar tenant_id/bot_token, _last_update_id dict
- все jobs/*.py и clients/*.py → from app.db import (proxy), BACKEND guards
- `app/monitoring/healthcheck.py` → asyncpg pool check

### Что сделано
- Новые файлы: `app/db.py` (прокси-модуль SQLite↔PG), `app/database_pg.py` (asyncpg, in-memory cache, seed), `app/migrations/001_initial.sql` (схема PG)
- Прод работает на SQLite (DATABASE_URL не изменён) — изменения backward-compatible, BACKEND="sqlite"
- Все 20 файлов залиты через scp, docker build --no-cache, контейнер healthy, логи чистые
- Git: `bfd1f00` (27 files changed, 4162 insertions)

---

## Сессия 28 — 26 февраля 2026 (humor.py: LLM-реплика в утреннем отчёте)

### Бэкап перед изменением
- `app/jobs/arkentiy.py` → убираем KYRGYZ-мод
- `app/jobs/daily_report.py` → добавляем LLM-реплику
- `app/config.py` → добавляем поле humor_model

### Что сделано
- Создан новый модуль `app/jobs/humor.py`: async вызов OpenRouter (`anthropic/claude-3-5-haiku`), функция `get_morning_quip(branch, rev, chk, late_pct, avg_late_min) → str | None`. Таймаут 5 сек, при ошибке возвращает None.
- Системный промпт с few-shot примерами: стиль ЧБД (Щербаков/Тамби) + иногда Ганвест-слова (фа/втфа/шнейне/пепе) как финальный аккорд. Не чаще 1 раза из 6.
- `daily_report.py`: вызов `get_morning_quip()` после формирования отчёта по каждой точке. Если вернула строку — добавляется курсивом в конец сообщения. Если API упал — отчёт приходит без реплики.
- `arkentiy.py`: удалены `_KYRGYZ_GREETINGS`, `_KYRGYZ_ACKS`, `_ILYA_ID`, `_greeted_today`, `_check_and_mark_ilya_greeting()`, все вызовы `random.choice(_KYRGYZ_*)`, неиспользуемый `import random`.
- `config.py`: добавлено поле `humor_model: str = "anthropic/claude-3-5-haiku"`.
- Деплой: healthy, логи чистые. Git: `f75aad3`.

---

## Сессия 27 — 26 февраля 2026 (/статус v2: сводка + edit_message навигация)

### Бэкап перед изменением
- `app/jobs/arkentiy.py` → переработка `_handle_status` + новый `stat:` callback
- `app/jobs/iiko_status_report.py` → строка времён (вариант А)

### Что сделано

**Задача:** переработать `/статус` — убрать пачку сообщений, добавить сводку по всем точкам с edit_message навигацией. UX аудит и spec в `specs/tg/status_v2.md`.

**`arkentiy.py`:**
- Добавлен `import asyncio` на уровне модуля
- Добавлен `_status_cache: dict[tuple[int, int], dict]` + `_STATUS_CACHE_MAX = 50` — аналог `_search_cache` для `/статус`
- Добавлена функция `_status_summary_line(data)` — форматирует строку точки в сводке (имя, выручка, чеки, иконка опоздания)
- Добавлена функция `_build_status_summary(results)` → `(str, list)` — собирает сводное сообщение и клавиатуру с кнопками per-точка + "Обновить"
- Переработан `_handle_status`:
  - Параллельный сбор данных: `asyncio.gather(*[_safe_get(b) for b in filtered])`
  - Одна точка → карточка напрямую + кнопка `stat:refresh:{name}`, id кешируется
  - Несколько точек → сводка + кнопки + кэш по `(chat_id, msg_id)`
- Новый callback `stat:` перед `srch:` в `poll_analytics_bot`:
  - `stat:branch:{name}` → edit_message карточки из кэша
  - `stat:back` → edit_message сводки из кэша
  - `stat:refresh` → asyncio.gather по всем точкам кэша → обновить сводку + кэш → edit_message
  - `stat:refresh:{name}` → одиночный refresh одной точки → edit_message карточки

**`iiko_status_report.py`:**
- Строка времён (вариант А): `готовка 18 → ожидание 8 → в пути 22 мин` (было: `Готовка: 18 | Ожидание: 8 | В пути: 22 мин`)

**Spec:** `02_Проекты/Аркентий/specs/tg/status_v2.md` — создан

**Деплой:** `docker compose build --no-cache && docker compose up -d` → healthy, git push → `2ec8826`

---

## Сессия 26 — 26 февраля 2026 (/поиск v2: edit_message навигация)

### Бэкап перед изменением
- `app/jobs/arkentiy.py` → редизайн `_handle_search` + новый `srch:` callback

### Что сделано

**Задача:** переработать модуль `/поиск` — убрать пачку сообщений, добавить edit_message навигацию, тип оплаты, время опоздания.

**Изменения в `arkentiy.py`:**

1. **`_search_cache: dict[tuple[int, int], dict]`** — новый in-memory кэш `(chat_id, msg_id) → {text, keyboard, rows, back_label}`. Лимит 100 записей (FIFO).

2. **`_format_order_card`** — добавлен `payment_type` в строку суммы: `2 691 ₽ · Наличные`. Маппинг значений iiko → читаемые названия.

3. **`_format_order_compact`** — время опоздания: `🔴 +37 мин` вместо просто `🔴`.

4. **`_handle_search` — полная переработка:**
   - Определение типа поиска: `numeric` (по номеру), `phone` (10-11 цифр), `text` (по тексту)
   - Один результат → сразу карточка, без навигации
   - Несколько результатов → одно сообщение, кнопки по 2-3 в ряд
   - Поиск по номеру (N филиалов) — сводка + кнопки `📋 Барнаул_1 Ана`
   - Поиск по телефону — имя клиента один раз в шапке, список без дублирования
   - `_send_with_keyboard_return_id` → сохраняем msg_id в кэш

5. **Новый callback `srch:`:**
   - `srch:card:{delivery_num}:{row_idx}` → `_edit_message` с карточкой + кнопка назад
   - `srch:back` → `_edit_message` из кэша (восстанавливает список)
   - Если кэш устарел → fallback на `_handle_search`

6. **Старый callback `order:`** — оставлен для обратной совместимости (старые кнопки в чате).

**Добавлено в COLS запросов:** `payment_type` (уже была в БД, не была в SELECT).

**Статус:** задеплоено, healthy, git push выполнен (4afeb61).

---

## Сессия 25 — 26 февраля 2026 (фикс сверки: только счета из выписки)

### Что сделано

**Задача:** убрать шум в отчёте сверки эквайринга — бот показывал расхождения по всем городам даже если загружена выписка только по одному.

**Причина:** `reconcile_acquiring` строил `branches_by_city` из всего `bank_accounts.json` (все 8 р/с). Для счетов, которых нет в выписке, `bank_gross = 0`, но iiko возвращал реальные суммы → ложные ⚠️.

**Фикс:**
- `bank_statement.py` — добавлен параметр `statement_accounts: set[str] | None = None` в `reconcile_acquiring`. В цикле построения `branches_by_city` добавлена проверка: `if statement_accounts and acc not in statement_accounts: continue`
- `arkentiy.py` — передаём `statement_accounts=set(result["parsed"].accounts)` при вызове `reconcile_acquiring`

**Результат:** сверка показывает только те филиалы, чьи р/с реально заявлены в загруженной выписке.

**Статус:** задеплоено, healthy, git не пушился (мелкий хотфикс).

---

## Сессия 24 — 26 февраля 2026 (комиссия по эквайрингу в выписке)

### Что сделано

**Задача:** исправить загрузку синтетических документов на комиссию по эквайрингу в iiko.

**Баги и фиксы:**

1. **Отсутствовал ИНН плательщика** — iiko давал предупреждение «ИНН текущего торгового предприятия не равен». Синтетический документ не содержал `ПлательщикИНН`, `Плательщик`, `ПолучательИНН`, `Получатель` и банковских реквизитов.
   - **Фикс:** расширен `AcquiringEntry` полями `our_inn`, `our_name`, `our_kpp`, `bank_bik`, `bank_korshet`, `bank_name`, `sbr_account`, `sbr_inn`, `sbr_name`. В `parse_acquiring` они извлекаются из реальных эквайринговых документов. В `generate_1c_file` вписываются в синтетический документ.

2. **Конфликт контрагента** — Сбербанк уже имел маппинг счетов в iiko (под «возмещение»), из-за чего `КоррСчет=2.2.11.8` игнорировался и подставлялся неверный счёт. Поле `КоррСчет` в 1CClientBankExchange iiko использует для межбанковского кор/счёта, а не для счёта в плане счётов.
   - **Фикс:** введён новый контрагент `КОМИССИЯ ЭКВАЙРИНГ` с ИНН `11111111`. iiko его не знает → спрашивает счета при первой загрузке → запоминает. `КоррСчет=2.2.11.8` теперь добавляется в документ как подсказка.

3. **«Счёт списания» = 1.02.9 не запоминается** — iiko определяет «Счёт» исходя из маппинга `р/с → iiko-счёт` в настройках банковских счетов, а не из файла и не из памяти по контрагенту. Виртуальный банковский счёт в iiko добавить нельзя. 
   - **Статус:** ограничение iiko, не решается из файла. Нужна ручная установка «Счёт» при каждой загрузке ИЛИ смириться с «Денежные средства, банк» в этом поле. Корр. счёт и контрагент — автоматические.

**Изменённые файлы:**
- `app/jobs/bank_statement.py`:
  - `AcquiringEntry` — добавлены поля реквизитов для синтетического документа
  - `parse_acquiring` — извлечение реквизитов из эквайрингового документа
  - `generate_1c_file` — полный блок полей синтетического документа (ИНН, банк, контрагент, `КоррСчет`)
  - Добавлены функции `load_acquiring_corr_account`, `load_commission_counterpart`
  - Параметры `acquiring_corr_account`, `commission_counterpart_inn`, `commission_counterpart_name` пробрасываются через `process`
- `secrets/bank_accounts.json` — добавлены поля:
  - `acquiring_corr_account: "2.2.11.8"` (Комиссия по эквайрингу)
  - `commission_counterpart_inn: "11111111"`
  - `commission_counterpart_name: "КОМИССИЯ ЭКВАЙРИНГ"`

**Урок для `Уроки_и_баги.md`:** `КоррСчет` в 1CClientBankExchange — межбанковский кор/счёт, iiko его не использует для маппинга в плане счётов. «Счёт» = маппинг р/с в настройках iiko, не контролируется из файла.

**Статус:** задеплоено, healthy. Git не пушился (только `secrets/` — в `.gitignore`).

---

## Сессия 23 — 25 февраля 2026 (трекер онлайн-оплат ТБанк)

### Что сделано

**Задача:** stateful сверка реестров ТБанк с iiko — трекер онлайн-оплат.

**Новые файлы:**
- `app/jobs/tbank_reconciliation.py` — парсер xlsx, движок сверки, Telegram-отчёт
- `secrets/tbank_branches.json` — маппинг имён листов ТБанк → iiko Department

**Изменённые файлы:**
- `app/database.py` — таблицы `online_payments` (трекер состояний) и `tbank_registry_logs` (аудит), функции upsert/confirm/get_pending/get_overdue
- `app/clients/iiko_bo_olap_v2.py` — новая функция `get_online_orders()`: OLAP v2 запрос с `Delivery.Number` + фильтр `PayTypes = "Оплата на сайте"`
- `app/jobs/arkentiy.py` — автодетект xlsx файлов, обработчик `_handle_tbank_registry()`
- `requirements.txt` — добавлен `openpyxl==3.1.5`

**Архитектура:**
- Каждый онлайн-заказ из iiko регистрируется в трекере со статусом `pending`
- При загрузке реестра ТБанк заказы подтверждаются (`confirmed` / `mismatch` / `missing_in_iiko`)
- Заказы pending > 4 дней = просроченные, отображаются в отчёте
- Автодетект: xlsx с заголовком "Уникальный идентификатор транзакции"

**Статус:** задеплоено, healthy, git pushed.

**TODO:** маппинг точек (`tbank_branches.json`) — плейсхолдеры, нужен реальный реестр Артемия для заполнения.

---

## Сессия 22 — 25 февраля 2026 (расширение фильтров /выгрузка)

### Что сделано

**Задача:** добавить поддержку сложных маркетинговых сегментов в `/выгрузка` через свободный текстовый запрос.

**Изменён только один файл:** `app/jobs/marketing_export.py`

**Новые параметры LLM (добавлены в `_SYSTEM_PROMPT` и `build_sql()`):**

| Параметр | Назначение |
|----------|-----------|
| `min_orders_in_period` / `max_orders_in_period` | Кол-во заказов клиента внутри date_from/date_to |
| `min_total_orders` / `max_total_orders` | Кол-во заказов клиента за всё время в БД |
| `exclude_period_from` / `exclude_period_to` | Исключить клиентов с заказами в этом периоде |
| `payment_type` | Фильтр по типу оплаты (наличные/карта/онлайн) |
| `source` | Фильтр по источнику заказа |
| `has_problem` | Фильтр по наличию жалобы |
| `unique_clients_only` | Один ряд на клиента (последний заказ) |

**Новые CTE в `build_sql()`:**
- `period_orders` — COUNT заказов клиента внутри заданного диапазона дат (добавляется только при `min/max_orders_in_period`)
- `excluded_phones` — DISTINCT телефоны с заказами в исключаемом периоде (добавляется только при `exclude_period_from/to`)
- Оба CTE добавляются условно — нет лишней нагрузки если фильтры не используются

**Новые столбцы в CSV:**
- `Заказов за период` (из CTE `period_orders`)
- `Тип оплаты` (поле `payment_type`)
- `Источник заказа` (поле `source`)

**Обновлены:** `_build_params_summary()`, `_build_filename()`, подсказка пользователю при пустом запросе.

### Деплой
- Бэкап `app/jobs/marketing_export.py` на VPS
- SCP → `docker compose build --no-cache && docker compose up -d`
- Статус: `healthy`, ошибок в логах нет
- Git push: коммит `a72c507`

### Изменённые файлы
- `app/jobs/marketing_export.py` — +257 строк (новые CTE, фильтры, параметры LLM)

---

## Сессия 21 — 25 февраля 2026 (сверка эквайринга банк vs iiko + логирование выписок)

### Что сделано

**Задача:** автоматическая сверка Сбер-эквайринга из банковской выписки с данными iiko OLAP v2. Логирование обработок выписок в БД.

**Новая функция `get_payment_breakdown` в `app/clients/iiko_bo_olap_v2.py`:**
- OLAP v2 запрос с `groupBy=[Department, PayTypes]`, `agg=[DishDiscountSumInt]`
- Возвращает `{dept_name: {pay_type: amount}}` для всех точек за период
- Параллельные запросы по серверам (как существующие функции)

**Новая async-функция `reconcile_acquiring` в `app/jobs/bank_statement.py`:**
- Сверка: bank_gross vs iiko (PayTypes: «Картой при получении» + «Сбербанк»)
- Конвертация DD.MM.YYYY → ISO, date_to + 1 день (iiko exclusive)
- Группировка по городам, расхождения любого размера помечаются ⚠️
- Итоги: суммарная комиссия с процентом, счётчик ✅/⚠️

**Интеграция в `arkentiy.py`:**
- `_handle_bank_statement` вызывает reconcile после отправки файлов
- Если iiko недоступен — файлы всё равно отправятся, ошибка сверки логируется отдельно

**Таблица `bank_statement_logs` в `database.py`:**
- Поля: processed_at, user_id, chat_id, filename, date_from, date_to, total_docs, total_files
- `init_bank_statement_tables()` вызывается из `init_db()`
- `save_bank_statement_log()` — INSERT после обработки

### Деплой
- Бэкап 4 файлов (olap_v2, bank_statement, arkentiy, database)
- SCP → build --no-cache → healthy + чистые логи

### Изменённые файлы
- `app/clients/iiko_bo_olap_v2.py` — +35 строк (get_payment_breakdown)
- `app/jobs/bank_statement.py` — +100 строк (reconcile_acquiring, вспомогательные)
- `app/jobs/arkentiy.py` — +20 строк (reconcile + db log в _handle_bank_statement)
- `app/database.py` — +40 строк (init_bank_statement_tables, save_bank_statement_log)

---

## Сессия 20 — 24 февраля 2026 (cancel_sync + улучшение карточки заказа)

### Что сделано

**Задача:** убрать ложные алерты об опоздании и неверный статус для отменённых заказов в `/поиск`. Корень проблемы: iiko Events API никогда не отправляет статус "Отменена".

**Новый модуль `app/jobs/cancel_sync.py`:**
- Job запускается каждые 3 минуты (APScheduler IntervalTrigger)
- Авторизация через `GET /api/auth` на каждый BO-сервер (hash SHA1 пароля)
- Запрос `POST /api/v2/reports/olap?key=TOKEN` — JSON body, возвращает JSON (не XML)
- `groupByRowFields: [Delivery.Number, Delivery.CancelCause, Department]`
- Дата `from/to` в ISO формате (YYYY-MM-DD), `includeLow: true, includeHigh: false`
- Фильтр: только строки где `Delivery.CancelCause != null`
- Группировка по `bo_url` → 5 уникальных серверов вместо 9 запросов
- `UPDATE orders_raw SET status='Отменена', cancel_reason=?, updated_at=? WHERE delivery_num=? AND status != 'Отменена'`
- Обновляет in-memory `_states[branch].deliveries[num]['status']` для немедленного эффекта в алертах
- Первый запуск: 22 заказа обновлено за раз

**`app/jobs/arkentiy.py` — улучшение карточки `/поиск`:**
- Отменённые заказы: `❌ **Статус: ОТМЕНЕНА** (причина)` — жирный, с причиной из `cancel_reason`
- Строка "опаздывает N м" для отменённых скрыта, показывается `—`
- Определение отмены: `status.lower() in ("отменена", "отменён")`

**`app/clients/iiko_bo_events.py`:**
- Убрана debug-инструментация (гипотезы B и AC из сессии отладки)

**Деплой:**
- Два раунда деплоя (cancel_sync + arkentiy hotfix), оба `healthy`
- Git push: `3a8b723`

### Бэкапы перед изменением (24.02.2026)
- `app/main.py` → `.bak.20260224_*`
- `app/clients/iiko_bo_events.py` → `.bak.20260224_*`
- `app/jobs/arkentiy.py` → `.bak.20260224_*`

### Новые открытия / в API_iiko.md
- **OLAP v2 API** (`/api/v2/reports/olap`) — рабочий JSON-endpoint (в отличие от `/api/reports/olap` → 500)
- Даты в OLAP v2: ISO `YYYY-MM-DD`, `from != to` (иначе 409)
- `Delivery.CancelCause` = причина отмены (`null` если не отменён)
- Events API **никогда не шлёт "Отменена"** — это системное ограничение iiko, не баг

---

## Сессия 19 — 23 февраля 2026 (Маркетинговый экспорт /выгрузка)

### Что сделано

**Задача:** реализовать команду `/выгрузка` в Аркентии для отдела маркетинга.

**Новый модуль `app/jobs/marketing_export.py`:**
- NLP-парсинг свободного запроса через OpenRouter (модель `google/gemini-2.5-flash`)
- Поддерживаемые фильтры: новый/старый клиент, дата, опоздание, сумма, состав блюд, город, конкретный филиал
- Базовый порог опоздания: 5 мин (переопределяется в запросе)
- Определение нового клиента: `MIN(date) per client_phone == date заказа`
- CSV с BOM (UTF-8) — корректно открывается в Excel
- `sendDocument` в Telegram — возвращает файл с читаемым именем
- Fallback при ошибке OpenRouter — показывает понятное сообщение

**`app/jobs/arkentiy.py`:**
- Добавлен `_get_marketing_ids()` + `_is_marketing_authorized()`
- Обработчик `elif cmd in ("выгрузка", "export")` — вызывает `run_export()`
- Обновлён HELP_TEXT с примерами

**`app/config.py`:**
- `openrouter_api_key`, `openrouter_model`, `telegram_marketing_ids`

**`tools/test_marketing_export.py`:**
- Тест-скрипт для pre-deploy проверки: конфиг → OpenRouter → SQL → CSV → БД

**Деплой:**
- Задеплоено на VPS, контейнер healthy, ошибок нет
- OPENROUTER_API_KEY добавлен в `.env` на VPS
- TELEGRAM_MARKETING_IDS — пока пустой (нужно заполнить chat_id маркетолога)
- Git push: `054ade1`

### Бэкапы перед изменением (23.02.2026 07:50 UTC)
- `app/jobs/arkentiy.py` → `.bak.20260223_075026`
- `app/config.py` → `.bak.20260223_075026`

---

## Сессия 18 — 22 февраля 2026 (Категории блюд + Ёбидоёби)

### Что сделано

**Задача:** добавить 3 города Ёбидоёби для скрапинга + поддержку категорий блюд в пайплайне.

**Парсер (`app/clients/competitor_scraper.py`):**
- Новый метод `_scrape_playwright_sections()` — итерирует `<section>`-теги, берёт категорию из заголовка, блюда из карточек внутри
- Обновлена `_js_result_to_items()` — вытаскивает поле `category` из JS-результатов
- Обновлена `scrape_competitor()` — `section_selector` проверяется первым, до `card_selector`

**competitors.json:**
- Добавлены 3 новых города: `Новосибирск`, `Канск`, `Иркутск` (Ёбидоёби, Angular SSR, секционная структура)
- Обновлён `Суши Даром` (добавлены `section_selector` + `category_selector` → категории из секций)
- Обновлён `СушиSELL` (добавлены `section_selector` + `category_selector: .category-settings__title`)

**database.py:** `get_all_competitor_items_by_snapshot` — добавлено поле `category` в SELECT и ORDER BY

**competitor_sheets.py:**
- `_build_pivot` перестроен → `{категория → {блюдо → {дата → цена}}}` + `cat_order`
- `_write_competitor_sheet` — строки-разделители категорий (серый фон, курсив), блюда внутри каждой, Δ-форматирование только для строк блюд
- `_write_summary_sheet` — адаптирован под новый формат пивота

**Результат тестов на VPS:**
- Ёбидоёби Новосибирск: 133 позиции, категории Наборы / Роллы и суши / Темпура / Ёнигири / Премиум / ...
- Суши Даром: 168 позиций, категории ШОК Цена / Сеты / Запеченные роллы / Жареные роллы / ...

**Деплой:** бэкап × 3 файлов → scp × 3 → `build --no-cache` → `healthy` ✅ → git push `4380ea9`

**Следующий шаг:** расшарить таблицы на SA для городов Новосибирск/Канск/Иркутск и протестировать `/конкуренты`.

---

## Сессия 17 — 22 февраля 2026 (Sheets-экспорт конкурентов)

### Что сделано

**Задача:** автоматически выгружать меню конкурентов из БД в Google Sheets после каждого скрапинга.

**Реализовано:**
- `secrets/competitors.json` → Мир Суши переведён в `active: false` (парсинг сайта сломан, отложено)
- `secrets/competitor_sheets.json` → создан маппинг `{город: spreadsheet_id}`, добавлен Томск
- `app/config.py` → добавлено свойство `competitor_sheets` (читает `competitor_sheets.json`)
- `app/database.py` → добавлены три функции:
  - `get_competitor_names()` — уникальные (city, name) с данными в БД
  - `get_all_competitor_items_by_snapshot(city, name)` — все позиции по всем слепкам
  - `get_competitor_last_snapshot(city, name)` — дата и кол-во позиций последнего слепка
- `app/jobs/competitor_sheets.py` → новый модуль экспорта. Структура каждой Sheets-таблицы:
  - `⚙️ Конкуренты` — тех. лист (сотрудники вводят название/сайт, скрипт заполняет статус/дату/позиций). Защита заголовка и авто-столбцов; A:B редактируемы.
  - `[Имя]` — пивот меню (блюда × даты слепков + Δ-столбец). Условное форматирование: зелёный при подешевении, красный при подорожании. NEW/REM для новых/удалённых блюд. Заморозка строки 1 и столбца A.
  - `Сводка` — агрегат по конкурентам: слепков, дата, позиций, средняя цена, Δ ср. цены.
  - Если в тех. листе конкурент без данных в БД → оранжевая подсветка + TG-алерт Артемию.
  - Все авто-листы защищены (только SA может писать).
- `app/jobs/competitor_monitor.py` → в конец `job_monitor_competitors()` добавлен вызов `export_all_competitors_to_sheets()`
- `app/jobs/arkentiy.py` → добавлена команда `/конкуренты` (только для admin): запускает экспорт из БД без re-scrape

**Деплой:** бэкап × 4 файлов → scp × 5 → `build --no-cache` → `healthy` ✅ → git push `af76e6a`

**Хотфикс (в той же сессии):**
- Обнаружено: `export_all_competitors_to_sheets()` выгружал Мир Суши (историческая запись в БД, хотя `active: false`)
- Добавлен фильтр: строим `inactive` set из `settings.competitors` → пропускаем при экспорте
- Удалён мусорный снапшот Суши Даром из БД (id=4, дата 21.02 — до фикса парсера)
- Суши Даром перескраплен вручную: 168 нормальных позиций → сохранено в БД (id=6)
- git push `dbc75a2`

**Следующий шаг:** расшарить Sheets-таблицу Томска на SA (`cursoraccountgooglesheets@cursor-487608.iam.gserviceaccount.com`) и протестировать `/конкуренты`.

---

## Сессия 16 — 22 февраля 2026 (Фикс парсера конкурентов)

### Что сделано

**Проблема:** в `competitor_menu_items` попадал мусор — "от", "-50%", "NEW АКЦИЯ", "НОВИНКА", "300г" вместо реальных названий блюд.

**Диагностика:**
- `_DOM_EXTRACTOR_JS` не фильтровал имена совсем — промо-бейджи проходили насквозь
- `_TEXT_EXTRACTOR_JS` фильтровал только `^%`, не ловил `-50%` (начинается с `-`) и `NEW`
- "от" (2 символа) проходило min_length=2, не было в черном списке
- Для Суши Даром в `competitors.json` уже были `card_selector`/`name_selector` — но код их не читал

**Фикс (`app/clients/competitor_scraper.py`):**
- Добавлен `_BAD_NAME_RE` + `_is_valid_name()` — Python-фильтр, применён в обоих методах разбора
- `_TEXT_EXTRACTOR_JS`: расширен фильтр — `[-\d]*%`, `new\b`, `от$`, `до$`, `цена`, `length < 3`
- `_DOM_EXTRACTOR_JS`: добавлены те же проверки перед `results.push` (раньше не было ничего)
- Новый метод `_scrape_playwright_selectors()` — Playwright + точные CSS-селекторы из конфига
- `scrape_competitor()`: новая ветка `if card_selector → _scrape_playwright_selectors()` перед generic

**Деплой:** бэкап → scp → `build --no-cache` → `healthy` ✅ → git push `de93640`

**Не входит в эту сессию:** Мир Суши — реальные названия (Сет Германия, Сет Австрия) не грузятся. Это не фильтрация, а парсинг сайта. Требует отдельного исследования URL и структуры.

---

## Сессия 15 — 21 февраля 2026 (Утренний отчёт из БД)

### Что сделано

**Проблема:** вечерний и утренний отчёты работали из `_states` (in-memory). При рестарте контейнера ночью — данные по опозданиям и персоналу терялись.

**Анализ (что оказалось лучше, чем думали):**
- RT-данные (опоздания, персонал) уже сохранялись в `daily_rt_snapshot` вечерним отчётом → утренний уже читал из БД ✅
- Финансовые данные (выручка, чеки, с/с, скидки) запрашивались у iiko BO свежо каждое утро — надёжно, но зависимость от iiko в момент отправки

**Что реализовали:**

1. **`database.py`** — расширена схема `daily_stats`:
   - Новые колонки: `cogs_pct REAL`, `sailplay REAL`, `discount_sum REAL`, `discount_types TEXT (JSON)`
   - Миграция: `ALTER TABLE daily_stats ADD COLUMN ...` для существующих БД
   - Новая функция `get_daily_stats(branch_name, date_iso) → dict | None`
   - `upsert_daily_stats_batch` обновлён под новые колонки

2. **`daily_report.py`** — вечерний отчёт теперь сохраняет OLAP-данные в `daily_stats`:
   - После получения данных от iiko BO → `upsert_daily_stats_batch(...)` с полным набором полей
   - Сохранение вынесено вне `if/elif` — выполняется всегда (и для `days_ago=0`, и для `days_ago>0`)

3. **`daily_report.py`** — утренний отчёт теперь читает из `daily_stats` как приоритет:
   - Сначала `get_daily_stats(name, date_iso)` для каждой точки
   - Если все точки покрыты БД → iiko BO не запрашивается вообще
   - Если БД покрывает частично → `get_all_branches_stats` только для недостающих, мердж через `{**iiko_stats, **db_stats}`

**Итог:** утренний отчёт теперь не зависит от iiko BO при условии, что вечерний отчёт прошёл успешно.

---

## Сессия 14 — 21 февраля 2026 (Inline-кнопки в поиске)

### Что сделано

**Inline-кнопки для раскрытия заказа из компактного списка:**

Проблема: при поиске по телефону/адресу бот отдавал компактный список из 10-20 заказов без возможности посмотреть детали конкретного.

Реализация:
- Новый хелпер `_send_with_keyboard(chat_id, text, keyboard)` — отправляет сообщение с `inline_keyboard`
- Новый хелпер `_answer_callback(callback_id)` — подтверждает нажатие (убирает loader)
- Компактный список теперь отправляется **одним сообщением** с кнопками под текстом: `[ 📋 Открыть #131592 (Томск) ]` — по одной кнопке на заказ
- В `poll_analytics_bot` добавлена обработка `callback_query`: при нажатии кнопки `order:XXXXX` → авторизация пользователя → `_handle_search(chat_id, XXXXX)` → полная карточка в чат

---

## Сессия 13 — 21 февраля 2026 (Хотфиксы алертов опоздания)

### Что сделано

**Хотфикс 1: Отменённые заказы в алертах**

Отменённые заказы показывались как опаздывающие, т.к. их статус не входил в `CLOSED_DELIVERY_STATUSES` (например, статус вроде "Отказ клиента" или аналогичный).

Решение: заменили **blacklist** (пропускать закрытые) на **whitelist** (алертить только явно активные):
```python
ACTIVE_DELIVERY_STATUSES = frozenset({
    "Новая", "Не подтверждена", "Ждет отправки",
    "В пути к клиенту", "В процессе приготовления",
})
```

**Хотфикс 2: Парсинг даты**

iiko Events API отдаёт `planned_time` в формате `2026-02-21T22:00:00.000` (с `T` и миллисекундами), а парсер ждал `%Y-%m-%d %H:%M:%S` → `ValueError` на каждом заказе → алерты не отправлялись.

Фикс: `clean = planned_raw.replace("T", " ").split(".")[0]`

**Хотфикс 3: Формат алерта**

- Убрана строка `💳 Тип оплаты` — в Events API не заполняется, всегда прочерк
- Добавлена `💰 Сумма заказа` (поле `sum` есть в Events)

---

## Сессия 12 — 21 февраля 2026 (Алерты опоздания заказов)

### Что сделано

**Новый job `app/jobs/late_alerts.py`:**

- Запускается каждые 2 минуты (APScheduler, `id='late_alerts'`)
- Читает `_states` из `iiko_bo_events.py` (in-memory RT данные)
- Для каждого активного (не закрытого) заказа доставки (не самовывоз):
  - Если текущее время UTC+7 > `planned_time` + 15 мин → алерт
  - Деduplication: `_alerted: dict[tuple, datetime]` — каждый заказ оповещается не более 1 раза в сутки, записи чистятся каждые 24 ч

**Формат алерта:**
```
🚨 Опоздание +X мин — Томск_1 Яко

#119505
👤 Иван Иванов
📞 +79001234567
💳 Наличные
🗺 ул. Ленина, 1
📦 Статус: в пути к клиенту
🛵 Курьер: Рафаил Надиров
```

**Маппинг городов → чат ID** (в `CITY_ALERT_CHAT` в коде):

| Город | Чат | Статус |
|-------|-----|--------|
| Томск | `5252075754` | ✅ Активен |
| Барнаул | — | ⏳ Ждём чат ID |
| Абакан | — | ⏳ Ждём чат ID |
| Черногорск | — | ⏳ Ждём чат ID |

Шлёт через **аналитический бот** (`TELEGRAM_ANALYTICS_BOT_TOKEN`).

**Фикс `_delivery_to_row` в `iiko_bo_events.py`:**

После добавления новых полей схемы (`send_time`, `service_print_time`, `cooking_to_send_duration`, `pay_breakdown`) — Events API не знал об этих полях и падал с `binding parameter` ошибкой. Добавлено явное `None` для всех 4 полей в `_delivery_to_row()`.

**Как добавить новый город:** открыть `app/jobs/late_alerts.py`, в словарь `CITY_ALERT_CHAT` вставить `"Барнаул": <chat_id>`.

---

## Сессия 11 — 21 февраля 2026 (MVP завершён: поиск, UX, финализация)

### Что сделано

**Фикс отображения состава заказа (`_format_order_card`):**

Обнаружено два формата поля `items` в БД:
- **Real-time (Events API):** `"Яса Сиса; Палочки × 2; ..."` — строка через `;`
- **Бэкфилл (OLAP):** `[{"name": "...", "qty": 1, "sum": 50.0}]` — JSON

Старый рендер знал только первый формат → старые заказы показывали сырой JSON. Добавлен fallback: сначала пробуем `json.loads()`, если не JSON — старый сплит по `;`.

**Индикация опоздания для недоставленных заказов:**

Если заказ ещё не доставлен (`actual_time` пустой), но текущее время уже прошло плановое — показываем `⏳ ещё не доставлен | ⚠️ опаздывает X м`. Время сравнивается с учётом таймзоны: VPS = UTC, iiko = UTC+7.

**Комментарий к заказу в карточке:**

Добавлена строка `💬 <комментарий>` под временем доставки. Поле `comment` добавлено в SELECT запроса поиска.

**Умный приоритет поиска (вариант A):**

Проблема: `/поиск 199505` находил 7 заказов — номер встречался как подстрока в телефонах других клиентов.

Логика теперь:
- Запрос из только цифр → сначала точное совпадение по `delivery_num` → полные карточки
- Если есть другие совпадения (телефон / адрес / состав) → компактный список внизу с пометкой "Ещё найдено X в других полях"
- Запрос с буквами / `+` → обычный поиск по всем полям как раньше

**МВП аналитического бота завершён.**

---

## Сессия 10 — 21 февраля 2026 (Бэкфилл orders_raw 2025–2026)

### Что сделано

**Анализ структуры `preset_artemy` — обнаружен cross-product:**

iiko OLAP, когда в пресете одновременно стоят `PayTypes` (измерение) и `DishName` (измерение), делает cross-product:
- 1 строка = 1 тип оплаты × 1 блюдо
- `DishAmountInt` = доля этого блюда, оплаченная данным методом (например `0.172` = SailPlay оплатил 17.2% суммы)
- `DishDiscountSumInt` = сумма за блюдо по данному методу оплаты

Чтобы получить реальные данные — нужно суммировать по всем PayTypes в рамках (заказ × блюдо). Менять порядок столбцов в пресете **не решает проблему** — причина в типе поля (dimension), а не в порядке.

**Плюс:** благодаря cross-product мы бесплатно получаем разбивку по методам оплаты — суммируя `DishDiscountSumInt` по PayType для заказа.

**Расширена схема `orders_raw` — теперь 33 колонки:**

| Новое поле | Тип | Что это |
|-----------|-----|---------|
| `send_time` | TEXT | Время отправки курьеру (`Delivery.SendTime`) |
| `service_print_time` | TEXT | Время сервисной печати блюда → отправка на кухню |
| `cooking_to_send_duration` | INTEGER | Длительность от сервисной печати до отправки (мин) |
| `pay_breakdown` | TEXT (JSON) | Разбивка по методам: `{"Наличные": 1103, "SailPlay Бонус": 229}` |

`items` уже существовал — теперь заполняется: `[{"name": "БИЧ", "qty": 1, "sum": 1149.0}]`

**Переписан `parse_and_aggregate` в бэкфилл-скрипте:**

- Группировка по `(dept, delivery_num / no_num_{open_time})`
- Суммирование `DishDiscountSumInt` и `DishAmountInt` по всем PayTypes → реальная сумма и количество
- `items` = JSON-список блюд с правильными qty (целые) и суммами
- `pay_breakdown` = JSON-словарь по методам оплаты
- Все тайминговые поля (`service_print_time`, `send_time`, `cooking_to_send_duration`) извлекаются

**Обновлён `app/database.py`:**

- 4 новых поля добавлены в миграцию (`ALTER TABLE ADD COLUMN`)
- Обновлён `upsert_orders_batch` — включены новые поля в INSERT OR REPLACE
- Применено в рабочий контейнер через `docker cp`

**Запущен бэкфилл `orders_raw` с 01.01.2025:**

- Диапазон: **01.01.2025 → 20.02.2026** (416 дней)
- Скорость: ~4 сек/день (2 сек rate-limit + запрос)
- Время выполнения: ~30 мин
- Прогресс: `/app/data/backfill_orders_progress.json` (возобновляемый)
- Лог: `/tmp/backfill_orders_v2.log`
- Существующие записи за 2026 год обновятся через UPSERT с новыми полями

**Пример итоговых данных (заказ #51386, Черногорск):**

| Поле | Значение |
|------|----------|
| Сумма | 1 332 ₽ |
| Наличные | 1 103 ₽ |
| SailPlay Бонус | 229 ₽ |
| Блюдо | БИЧ × 1 = 1 149 ₽ |
| Доставка | 150 ₽ |
| Сервисный сбор | 33 ₽ |

---

## Сессия 9 — 21 февраля 2026 (Бэкфилл daily_stats + разведка API)

### Что сделано

**Разведка исторических данных iiko BO:**

Исследованы все доступные источники для получения исторических индивидуальных заказов:

| Источник | Результат |
|----------|-----------|
| `/api/deliveries`, `/api/orders`, `/api/delivery/list` | 404 — нет в этой версии iiko BO |
| Events API с параметрами `dateFrom`/`dateTo` | Игнорирует параметры, отдаёт только текущий день |
| 89 OLAP-пресетов | Только агрегаты (1 строка = 1 день × 1 точка), нет индивидуальных заказов |
| **Вывод** | `orders_raw` исторически заполнить невозможно |

**Бэкфилл `daily_stats` из OLAP-пресетов:**

- Добавлена функция `upsert_daily_stats_batch()` в `app/database.py`
- Написан скрипт `backfill_daily_stats.py` с прогрессом и rate-limit (2 сек/день)
- Диапазон: **01.12.2024 → вчера** (~447 дней × 9 точек = ~4023 строк)
- Прогресс сохраняется в `/app/data/backfill_daily_stats_progress.json` (возобновляемый)
- Запущен в фоне, завершён за ~18 минут

**Итог бэкфилла (финальная строка из логов):**
- **4011 строк**, 2024-12-01 → 2026-02-20
- Суммарно **409 837 заказов** по всей сети
- Docker образ пересобран: `database.py` с `upsert_daily_stats_batch()` теперь в образе

**Что заполняется в `daily_stats` из OLAP:**

| Поле | Источник |
|------|----------|
| `orders_count` | PRESET_ORDER_SUMMARY |
| `revenue` | PRESET_API_STATS |
| `avg_check` | revenue / orders_count |
| `delivery_count` | orders_count − pickup_count |
| `pickup_count` | PRESET_DELIVERY_TYPES |
| `late_count`, `late_percent`, `avg_late_min` | 0 (нет источника в OLAP) |
| `cooks_count`, `couriers_count` | 0 (нет исторических смен) |

**Новые пресеты обнаружены (для будущего использования):**

| Пресет | UUID | Поля |
|--------|------|------|
| Доставки по курьерам | `1f56b9d3-...0020` | Courier, DelayAvg, WayDurationAvg, OrdersCount |
| Доставки по гостям | `1f56b9d3-...0025` | CustomerName, OrdersCount, Sum |
| Доставки по часам | `1f56b9d3-...0022` | HourClose, OrdersCount, Sum |
| Причины отмен | `1f56b9d3-...0026` | CancelCause, OrdersCount |
| ОПиУ для выгрузки | `43a229fc-...` | Account.Name/Type, Sum.Incoming/Outgoing |

---

## Сессия 8 — 21 февраля 2026 (Расширение схемы orders_raw)

### Что сделано

**Расширена схема `orders_raw` — 29 колонок (было 17):**

Новые поля из **Events API** (заполняются в real-time):
| Колонка | Источник | Описание |
|---------|----------|----------|
| `comment` | `deliveryComment` | Комментарий к заказу |
| `operator` | `deliveryOperator` | Кто принял заказ |
| `opened_at` | дата события `deliveryOrderCreated` | Время открытия (для почасовой выручки) |
| `has_problem` | `deliveryHasProblem` | Флаг проблемы (0/1) |
| `problem_comment` | `deliveryProblemComment` | Комментарий к проблеме / отмене |

Новые поля для **OLAP бэкфилла** (пока NULL, заполнятся при бэкфилле):
| Колонка | Описание |
|---------|----------|
| `payment_type` | Тип оплаты |
| `bonus_accrued` | Начисленные бонусы |
| `source` | Источник заказа (приложение, сайт, агрегатор) |
| `return_sum` | Сумма возврата |
| `service_charge` | Сервисная печать |
| `cancel_reason` | Причина отмены |
| `cancel_comment` | Комментарий к отмене (также = `problem_comment`) |

**Отменённые заказы** — в той же таблице `orders_raw`, фильтруются по `status IN ('Отменена', 'Удалена', 'Отменена: Не подтверждена')`.

**`month`** — не хранится отдельно, вычисляется: `strftime('%Y-%m', date)`.

**Миграция:** `ALTER TABLE orders_raw ADD COLUMN ...` — цикл по всем новым колонкам в `init_analytics_tables()`. Существующие БД обновляются автоматически при старте.

**Проверено после деплоя:**
- 525 заказов с `comment`, 758 с `operator` (из incremental poll за ~30 сек)
- `opened_at` заполняется только для новых заказов (для истории — бэкфилл)
- OLAP-поля пустые — заполнятся при бэкфилле

---

## Сессия 7 — 21 февраля 2026 (Самовывоз, группы, доступы)

### Что сделано

**Корректный расчёт опозданий для самовывоза (`iiko_bo_events.py`, `database.py`):**
- Добавлена константа `PICKUP_READY_BUFFER_MINUTES = 5`
- В `BranchState` добавлен `ready_times: dict` — хранит timestamp события `cookingStatus=Собран`
- В `_process_events`: при `cookingStatus == "Собран"` захватывается timestamp и пробрасывается в `state.deliveries[num]["ready_time"]`
- `_delivery_to_row` разделяет логику:
  - Самовывоз: `is_late = (ready_time + 5 мин) > planned_time` (опоздание кухни)
  - Доставка: `is_late = actual_time > planned_time + 3 мин` (старая логика, без изменений)
- `database.py`: добавлена колонка `ready_time TEXT` в `orders_raw` + `ALTER TABLE` миграция + обновлён `upsert_orders_batch`

**Переработан `/опоздания` (`analytics_bot.py`):**
- Разделение доставки и самовывоза в одном блоке на точку
- Самовывоз показывается только если есть опоздания — без шума "0 из X"
- Формат строки самовывоза: `план: ЧЧ:ММ готов: ЧЧ:ММ` вместо курьера
- Длинные сообщения разбиты: header + одно сообщение на точку (Telegram limit 4096)

**Group-based роутинг команд (`analytics_bot.py`):**
- Добавлена функция `_allowed_commands(chat_id)` — ограничивает команды по группе
- Группа `5149932144` → только `/поиск`, `/помощь`
- Группа `5262858990` → только `/день`, `/опоздания`, `/помощь`
- Личка / остальные → все команды

**Доступы:**
- Добавлен пользователь `874186536` (менеджер ОКК) в `TELEGRAM_ALLOWED_IDS`
- Текущий список: `255968113, 1332224372, 822559806, 1011547016, 874186536`

---

## Сессия 6 — 21 февраля 2026 (Аналитический бот)

### Что сделано

**Второй Telegram-бот (аналитический):**
- Токен: `TELEGRAM_ANALYTICS_BOT_TOKEN` → добавлен в `.env` и `config.py`
- Файл: `app/jobs/analytics_bot.py` (454 строки)
- Зарегистрирован в `main.py` как job `analytics_bot` (polling каждые 3 сек)
- Не трогает первого бота — независимый polling loop с отдельным `_last_update_id`

**Команды бота:**

Real-time (из in-memory BranchState, как у первого):
- `/статус [фильтр]` — состояние всех точек
- `/повара [фильтр]` — повара на смене
- `/курьеры [фильтр]` — курьеры со статистикой

Из БД (SQLite `orders_raw`):
- `/поиск <номер>` — поиск заказа по номеру доставки (LIKE)
- `/день [дата]` — агрегированная сводка за день по всем точкам (выручка, заказы, опоздания)
- `/опоздания [дата]` — список опоздавших заказов с деталями

Форматы даты: `вчера`, `21.02.2026`, `2026-02-21`, пусто = сегодня

**APScheduler:** 12 задач (было 11)

---

## Сессия 5 — 21 февраля 2026 (Data Warehouse: SQLite analytics)

### Что сделано

**Расширен `app/database.py`:**
- Добавлены таблицы: `orders_raw`, `shifts_raw`, `daily_stats`
- `orders_raw` PK: `(branch_name, delivery_num)` — вся история заказов
- `shifts_raw` PK: `(branch_name, employee_id, clock_in)` — все смены
- `daily_stats` PK: `(branch_name, date)` — агрегаты (пока пустая, заполняется позже)
- Добавлены индексы: `idx_orders_date`, `idx_orders_branch_date`, `idx_shifts_branch_date`
- Добавлены функции: `upsert_orders_batch`, `upsert_shifts_batch`, `query_orders`
- `init_analytics_tables()` вызывается из `init_db()` — таблицы создаются автоматически

**Модифицирован `app/clients/iiko_bo_events.py`:**
- `_process_events` теперь возвращает `(changed_deliveries, changed_sessions)` — set'ы изменившихся ключей
- Добавлены helper-функции: `_delivery_to_row`, `_session_to_row`
- Добавлена `_save_to_db(state, delivery_nums=None, session_ids=None)`:
  - `None` = сохранить всё (вызывается после full load)
  - Иначе = только изменившиеся (incremental, каждые 30 сек)
- `_full_load`: после обработки событий → `await _save_to_db(state)` (batch UPSERT всего)
- `_incremental_poll`: после событий → `await _save_to_db(state, changed_d, changed_s)`

**Результат после деплоя (проверено):**
- `orders_raw`: 554 строки по всем 9 точкам
- `shifts_raw`: 114 строк (повара + курьеры)
- Все данные реального текущего дня уже в БД

### Следующие шаги
1. **Backfill** — залить историю за 3-6 мес. через OLAP API
2. **Telegram-команды** — `/поиск [номер]` и `/инфо [дата]` через SQL
3. **daily_stats** — джоб ежедневной агрегации из `orders_raw`

---

## Сессия 4 — Февраль 2026 (документация и структура)

### Что сделано

**Реструктуризация знаний агента:**
- Создана база знаний `99_Системное/Интегратор/` с 4 файлами
- `Архитектура.md` — файловая структура VPS, модули, расписание, BranchState, SQLite
- `API_iiko.md` — полный справочник по iiko Cloud, BO OLAP, Events API
- `Уроки_и_баги.md` — все накопленные баги и антипаттерны с кодом-примерами
- `Журнал.md` — этот файл, история по сессиям

**Рефакторинг интегратор.mdc:**
- Урезан с 1174 строк до 148 строк — только навигатор и поведенческие правила
- Добавлены 5 критических технических правил (Events sort, merge, OLAP cookie, etc.)
- Добавлены разделы: ЗАДАЧИ, ВЫБОР СТЕКА, РАБОЧИЙ ПРОЦЕСС, ПРАВИЛА, САМООБУЧЕНИЕ, СВЯЗЬ С КОМАНДОЙ
- Детальное техническое содержимое перенесено в KB-файлы (читаются по необходимости)

**Принцип:** `.mdc` всегда в контексте → должен быть лёгким. KB-файлы читаются агентом по запросу задачи.

---

## Сессия 3 — Февраль 2026 (финальная)

### Что сделано

**Real-time данные (Events API):**
- Реализован `iiko_bo_events.py` — event sourcing через `/api/events`, polling 30 сек
- `BranchState` — in-memory состояние каждой точки (заказы, смены, кулинарные статусы)
- Детализация активных заказов: `orders_new` / `orders_cooking` / `orders_ready` / `orders_on_way`
- `delivered_today` — доставленные за день (Доставлена + Закрыта)
- Подсчёт задержек: `delay_stats` → `{late_count, total_delivered, avg_delay_min}`
- `total_cooks_today` / `total_couriers_today` — все кто работал за день

**Telegram-команды:**
- `/статус [фильтр]` — выручка + OLAP + RT: задержки, скидки по типам, детальные заказы, персонал
- `/повара [точка]` — поварá на смене: ФИО, время прихода/ухода
- `/курьеры [точка]` — курьеры: ФИО, смена, доставлено сегодня, активных заказов

**Отчёты и данные:**
- Вечерний 🌙 и утренний ☀️ — одинаковый формат (delays + staff)
- Расписание пт/сб: вечерний в 00:30 лок. следующего дня
- `job_save_rt_snapshot` — сохраняет RT в 23:50 пт/сб без OLAP-запросов
- `daily_rt_snapshot` таблица в SQLite
- `iiko_to_sheets.py` — инкрементальная выгрузка + перепроверка прошлого дня
- `healthcheck.py` — тихие часы (23-07), уведомления только при падении/восстановлении

**Доступ и безопасность:**
- Личка: только admin (255968113)
- Группа: `TELEGRAM_ALLOWED_IDS` = 255968113, 1332224372, 822559806, 1011547016
- Технические алерты → только личка Артемия (`TELEGRAM_CHAT_MONITORING=255968113`)

**Kyrgyz режим для Ильи (822559806):**
- Первое сообщение за день → случайное приветствие на киргизском
- Команды → подтверждение на киргизском перед выполнением

### Исправленные баги (критические)
1. **События не по порядку** → добавлена сортировка по `<date>` в `_process_events`
2. **overwrite вместо merge** → `deliveryOrderEdited` теперь патчит только пришедшие поля
3. **Fuzzy match имён** → токенизация для сопоставления курьеров из разных источников
4. **Роли поваров** → добавлены `"пс"`, `"пбт"` в `_COOK_ROLE_PREFIXES`

---

## Сессия 2 — Февраль 2026

### Что сделано

**Переход на индивидуальные серверы iiko BO:**
- Каждая точка → свой `bo_url` в `branches.json`
- Отказ от общего сервера `tomat-i-chedder` (данные с задержкой)
- 9 точек: 4 Барнаул (один сервер), 2 Абакан, 2 Томск, 1 Черногорск

**Events API — первая реализация:**
- Подключение `/api/events` для real-time данных (заказы, смены)
- Команды `/повара` и `/курьеры`
- Классификация ролей (повара / курьеры)

**Отчёты:**
- Добавлены задержки (`delay_stats`) в `/статус` и ежедневные отчёты
- Добавлены скидки по типам (`discount_types`)
- Emoji: `🔴` если есть задержки, `✅` если нет

---

## Сессия 1 — Февраль 2026 (MVP)

### Что сделано

**Базовая инфраструктура:**
- VPS 5.42.98.2, Docker, FastAPI, APScheduler
- `.env`, `branches.json`, Dockerfile, docker-compose.yml

**Интеграции:**
- iiko BO OLAP-пресеты → выручка, чеки, типы оплат, скидки, COGS, самовывоз
- Google Sheets → ежедневная выгрузка 13 метрик, инкрементальный трекинг
- Telegram бот → long polling, `/статус`, базовые отчёты

**Обнаружено:** OLAP `/api/reports/olap` сломан → переключились на `/service/reports/report.jspx` с cookie-auth.

---

## Следующие шаги (backlog)

| Приоритет | Задача | Статус |
|-----------|--------|--------|
| 🔴 Высокий | Алерты о критических задержках (>15 мин) в чат | Не начато |
| 🔴 Высокий | Алерты о стоп-листах | Отключён (был рабочий) |
| 🔴 Высокий | Бэкфилл исторических данных (1 год) через OLAP API | Не начато |
| 🟠 Средний | Агрегация `daily_stats` (scheduled job по итогам дня) | Не начато |
| 🟡 Средний | Ролевой доступ: управляющие видят только свои точки | ✅ Реализовано (сессия 5) |
| 🟡 Средний | Контроль незакрытых смен (алерт если смена открыта >12 ч) | Не начато |
| 🟢 Низкий | Битрикс24 задачи → Telegram | Нет ключа |
| 🟢 Низкий | MyMeet саммари встреч | Нет ключа |

---

## Сессия 4 — 22.02.2026 | Inbox-бот ("Второй мозг")

**Новый проект:** `06_Проекты/InboxBot/` — полностью отдельная кодовая база от Аркентия.

**VPS:** `/opt/inbox-bot/` (порт 8001), не пересекается с `/opt/ebidoebi/`.

### Что сделано (Фаза 1 — Ядро)

- Создана структура проекта: FastAPI + polling loop, config, SQLite (только `inbox_polling`)
- `app/clients/telegram.py` — getUpdates, sendMessage, getFile, downloadFile
- `app/jobs/inbox_bot.py` — хэндлеры для всех типов: text, voice, audio, photo, video, video_note, document; пересланное с комментарием группируется в один блок с `💬 Контекст:`
- Запись в Markdown-дневники: `data/inbox/YYYY-MM/YYYY-MM-DD.md`, медиа в `media/YYYY-MM-DD/`
- Авторизация: только `INBOX_BOT_ADMIN_ID` (255968113)
- Docker: `build --no-cache` → `healthy` с первого запуска

### Дорожная карта (следующие фазы)

| Фаза | Что | Статус |
|------|-----|--------|
| 2 | AssemblyAI: транскрипция голосовых | ✅ Готово |
| 3 | Триггеры (без LLM) → Google Calendar + Telemost | Не начато |
| 4 | ChromaDB + embeddings (RAG-база) | Не начато |
| 5 | Батч LLM Gemini Flash 2.5: теги + weekly дайджест | Не начато |

**Фаза 2 детали:**
- `app/clients/assemblyai.py` — REST API: upload → create transcript → polling
- Язык: `ru`, punctuate + format_text включены
- Голосовые: бот пишет "⏳ Транскрибирую...", потом "✅ Сохранено и транскрибировано (0:30) / 145 симв."
- Если `ASSEMBLYAI_API_KEY` не задан — сохраняется без транскрипта, без ошибки
- Порт 8001 закрыт наружу (убран из `ports:` docker-compose)

---

## Сессия 5 — 23.02.2026 | Система управления доступом + /выгрузка маркетинг

### Маркетинговый модуль (/выгрузка) — финализация и деплой

**Задеплоено ранее в сессии:**
- `app/jobs/marketing_export.py` — команда `/выгрузка <запрос>`, OpenRouter (Gemini 2.5 Flash), SQL с CTE, CSV с BOM
- Фильтрация по филиалу: Gemini получает таблицу всех точек с алиасами (Томск-1 → Томск_1 Яко)
- Порог опоздания по умолчанию: 5 мин (переопределяется в запросе)
- Проверено live: работает, CSV выгружается

### Система управления доступом — полная реализация

**Проблема:** хардкод прав в `arkentiy.py` (группы по частичному ID, TELEGRAM_ALLOWED_IDS), нет интерфейса для добавления новых чатов без деплоя.

**Решение:**

| Файл | Что |
|------|-----|
| `app/access.py` | Слой прав: `Permissions` dataclass, `get_permissions()`, `save_config()`, hot-reload по mtime файла |
| `app/jobs/access_manager.py` | Telegram UI: `/доступ`, inline-кнопки, управление чатами/юзерами, автодетект новых чатов |
| `app/jobs/arkentiy.py` | Рефакторинг: убран хардкод, используется `access.get_permissions()`, city-фильтр в handlers, `/доступ` команда, `my_chat_member` автодетект |
| `secrets/access_config.json` (VPS) | Начальный конфиг `{"admins":[255968113],"chats":{},"users":{}}` |

**Архитектура прав:**
- Иерархия: `admins > chats[chat_id] > users[user_id]`
- Hybrid fallback: если entity не в конфиге → проверяем `.env` (backward compat)
- Модули: `late_alerts`, `late_queries`, `search`, `reports`, `marketing`, `finance`, `admin`
- city-фильтр: чат привязан к городу → данные автоматически фильтруются по городу в `/опоздания`, `/день`, `/статус`, `/поиск`

**Telegram UI `/доступ`:**
- Главный экран: чаты сгруппированы по городам, счётчик юзеров
- Экран чата: toggle модулей (2 в ряд), выбор города, удаление
- Экран пользователей: аналогично
- Диалоговое добавление чата/юзера (шаги: ID → название)
- Автодетект: бот добавлен в группу → алерт в личку admin с кнопками «Зарегистрировать» / «Игнорировать»
- Toast-уведомления при нажатии кнопок (что включилось/выключилось)

**Бэкапы перед деплоем:** `arkentiy.py.bak.20260223_*` на VPS.

**Статус:** задеплоено, `healthy`, git push → `31aa221`.

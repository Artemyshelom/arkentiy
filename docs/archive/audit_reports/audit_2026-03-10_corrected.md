# Скорректированный аудит проекта Аркентий

Дата: 2026-03-10  
Основа: аудит слабой модели → перепроверка с походом в код.

---

## Вердикт по каждому пункту исходного аудита

| # | Claim | Вердикт | Обоснование |
|---|-------|---------|-------------|
| **1** | Секреты в docs | **Подтверждён** | Boris токен в `docs/specs/boris_api_prompt.md:8`, `docs/journal.md:575,589`. OpenCLAW токен в `docs/journal.md:1772`. |
| **2** | Мультитенантность не доведена | **Подтверждён** | 50+ функций с `tenant_id: int = 1`. Две функции вообще без tenant_id. `_states` без tenant scoping. Stats API fallback на 1. |
| **B1** | Reset password: поле `new_password` vs `password` | **Подтверждён** | Frontend: `new_password`. Backend Pydantic: `password`. Запрос отвергается. |
| **B2** | Reset password: email ведёт не на тот URL | **ЛОЖНЫЙ** | `StaticFiles(html=True)` автоматически резолвит `/reset-password` → `web/reset-password.html`. Nginx проксирует всё. |
| **B3** | Payment retry без auth | **Подтверждён** | `web/payment/fail.html` POST без JWT. Endpoint требует `Depends(get_tenant_id)`. Retry = 401. |
| **B4** | Cabinet test_iiko: нет `bo_password`, фейковый time | **Подтверждён** | SQL: `SELECT bo_url, bo_login`. `cred.get("bo_password")` = None. `response_time_ms: 500` — хардкод. |
| **B5** | Chat cities: сохраняет только первый | **Подтверждён** | `city = data.cities[0]`. БД-колонка `city` — скаляр. |
| **7** | Onboarding iiko test: SHA1("") | **Подтверждён** | Визард не собирает пароль. Тест с хешем пустой строки. |
| **8** | Дублирование OLAP-слоя | **Подтверждён, не срочно** | `iiko_bo_olap_v2.py` + `olap_queries.py` — архитектурный долг. |
| **9** | `verify=False` везде | **Частично ложный** | Все 20+ случаев — iiko API. В `rules/integrator/iiko_api.md` зафиксировано: сертификат iiko не проходит проверку. Осознанный компромисс. |
| **10-12** | Архитектурные наблюдения | **Справедливы** | Описательные пункты, не actionable. |
| **C1** | JWT в localStorage | **Верно, приемлемо** | Стандартный компромисс для SPA без SSR. |
| **C2** | Pricing на клиенте | **Верно, не баг** | Backend — source of truth. |

---

## Что НУЖНО делать

### Блок 1: Сломанные пользовательские flow (правятся за сессию)

#### 1. Reset password field mismatch
- **Файл:** `app/routers/auth.py:36`
- **Проблема:** `ResetPasswordRequest.password`, а фронт шлёт `new_password`
- **Решение:** Переименовать в backend `password` → `new_password` (фронт уже задеплоен, менять его дороже)
- **Объём:** 1 файл, ~3 строки

#### 2. Payment retry без auth
- **Файл:** `web/payment/fail.html`
- **Проблема:** POST на `/api/payments/create` без Authorization header. Endpoint требует JWT.
- **Решение:** Вариант А — читать JWT из localStorage + header. Вариант Б (лучше) — новый endpoint `/api/payments/retry` по `payment_id`, без JWT, пересоздаёт платёж по данным из БД.
- **Объём:** 1-2 файла

#### 3. Cabinet test_iiko
- **Файл:** `app/routers/cabinet.py:475`
- **Проблема:** SQL не читает `bo_password`; `response_time_ms` захардкожен
- **Решение:** Добавить `bo_password` в SELECT. Замерить реальное время (`time.monotonic()` до/после запроса).
- **Объём:** 1 файл, ~5 строк

#### 4. Chat cities
- **Файл:** `app/routers/cabinet.py:550`
- **Проблема:** Берёт только `data.cities[0]`, остальные теряются
- **Решение:** Зависит от бизнес-требования. Если чат = один город — упростить фронт (radio вместо checkbox). Если нужно несколько — миграция `city → cities_json`.
- **Требуется решение:** от владельца продукта

---

### Блок 2: Секреты (критично)

#### 5. Вычистить токены из docs
- **Файлы:**
  - `docs/specs/boris_api_prompt.md:8` — Boris Bearer token
  - `docs/journal.md:575,589` — Boris Bearer token
  - `docs/journal.md:1772` — OpenCLAW token
- **Решение:** Заменить реальные значения на `<REDACTED>` или `brs_EXAMPLE...`
- **Объём:** 3 замены в 2 файлах

#### 6. Ротация токенов
- **Действие:** После очистки docs — перевыпустить Boris API key и OpenCLAW token на VPS
- **Объём:** Ручная работа, ~10 минут

---

### Блок 3: Мультитенантность — точечные правки (средний объём)

#### 7. Функции без tenant_id
- **Файлы:**
  - `app/database_pg.py:1097` — `aggregate_orders_for_daily_stats(branch_name, date_iso)` — нет tenant_id
  - `app/database_pg.py:1823` — `get_payment_changed_orders(branch_names, date_iso)` — нет tenant_id
  - `app/jobs/olap_pipeline.py:407` — вызов без tenant_id
  - `app/jobs/arkentiy.py:2057` — вызов без tenant_id
- **Решение:** Добавить `tenant_id: int` (без дефолта) + `AND tenant_id = $N` в SQL
- **Риск:** Cross-tenant data leakage при совпадении branch_name между tenants

#### 8. _states без tenant scoping
- **Файл:** `app/clients/iiko_bo_events.py:69`
- **Проблема:** `_states: dict[str, BranchState]` — ключ `branch_name`. При совпадении имён у разных tenants — коллизия.
- **Решение:** Ключ `f"{tenant_id}:{branch_name}"`. Пробросить `state.tenant_id` в `close_stale_shifts` (строка ~872) и `_seed_sessions_from_db` (строка ~785).
- **Объём:** 1 файл, ~10 строк

#### 9. Stats API fallback
- **Файл:** `app/routers/stats.py:485`
- **Проблема:** `tenant_id = token_meta.get("tenant_id", 1)` — если нет tenant_id в токене, молча берёт 1
- **Решение:** `if "tenant_id" not in token_meta: raise HTTPException(403, "No tenant_id in token")`
- **Объём:** 1 файл, ~3 строки

---

## Что НЕ нужно делать

| Предложение аудита | Почему не нужно |
|---|---|
| Убирать `verify=False` | Осознанный компромисс для iiko BO API. Зафиксировано в `rules/integrator/iiko_api.md`. Их сертификат не проходит стандартную проверку. |
| JWT из localStorage в HttpOnly cookie | Переписать весь auth-слой (cabinet, wizard, login). Для текущего масштаба — overkill. |
| Сводить OLAP к одному слою | Архитектурный долг, но ничего не ломает. После критичных правок. |
| Синхронизировать все docs | Трудоёмко, не влияет на работу. Инкрементально при касании модулей. |
| Менять pricing на фронте | Backend — source of truth. Фронт показывает preview. |
| Finding B2 — менять URL в email | URL рабочий. `StaticFiles(html=True)` резолвит `/reset-password` → `reset-password.html`. |
| Убирать `tenant_id: int = 1` из 50+ функций | Не баг, пока вызовы передают явно. Системный рефакторинг без немедленной отдачи. Зафиксировать как долг. |
| Убирать `ctx_tenant_id` default=1 | Polling loop ставит контекст явно. Риск только при добавлении новых handler без контекста. |

---

## Итого: 9 конкретных правок

- **Блок 1** (пункты 1-4) — сломанные flow → чинить **сейчас**
- **Блок 2** (пункты 5-6) — секреты → чинить **сейчас** + ротация руками
- **Блок 3** (пункты 7-9) — tenant isolation → чинить **в ближайшем цикле**

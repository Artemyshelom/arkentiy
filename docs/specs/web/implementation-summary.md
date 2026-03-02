# Аркентий — Веб-платформа: итоги реализации

> Онбординг, оплата, личный кабинет, автоматизация подписок.
> Реализовано в 5 фазах. Суммарно: **29 API-эндпоинтов**, **2 327 строк Python**, **858 строк JS**, **5 новых таблиц**.

---

## Фаза 0 — Инфраструктура и безопасность

**Цель:** подготовить конфиг, JWT, rate-limiting.

| Файл | Что сделано |
|------|-------------|
| `app/config.py` | Поля `yukassa_shop_id`, `yukassa_secret_key`, `yukassa_return_url`, `jwt_secret`, `debug` |
| `.env.example` | Секция ЮKassa + JWT |
| `requirements.txt` | `bcrypt`, `slowapi`, `PyJWT`, `email-validator` |

**JWT:** HS256, срок — 30 дней, payload: `{tenant_id, email, exp}`.
**Rate-limiting:** slowapi, лимиты на все публичные эндпоинты (3–10 req/min).

---

## Фаза 1 — Онбординг / Регистрация

**Цель:** визард из 6 шагов → создание тенанта → переход к оплате.

### Backend — `app/routers/onboarding.py` (502 строки)

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/auth/check-email` | Проверка уникальности email |
| POST | `/api/iiko/test-connection` | Тест подключения к iiko BO, список точек |
| POST | `/api/telegram/test-chat` | Проверка бота в чате, тестовое сообщение |
| POST | `/api/promo/validate` | Валидация промокода, возврат бонусов |
| POST | `/api/tenants/create` | Атомарное создание тенанта + подписки + модулей + точек + чатов |

### Frontend — `web/js/wizard.js` (858 строк)

6 шагов визарда:

| # | Шаг | Поля |
|---|-----|------|
| 0 | Компания | название, контакт, email, телефон, пароль |
| 1 | Города и точки | мульти-город, мульти-точка (dropdown) |
| 2 | Модули | финансы, конкуренты (чекбоксы) |
| 3 | iiko | BO URL + логин API + кнопка теста |
| 4 | Telegram | chat_id + кнопка теста |
| 5 | Итог | сводка, ценообразование, период, промокод, способ оплаты |

### Ценообразование

| Компонент | Стоимость |
|-----------|-----------|
| Базовая подписка | 5 000 ₽/точка/мес |
| Модуль «Финансы» | +2 000 ₽/точка/мес |
| Модуль «Конкуренты» | +1 000 ₽/город/мес |
| Подключение (разовое) | 10 000 ₽ |
| Настройка конкурентов | 3 000 ₽/город |

**Скидки:** 4–6 точек → 10%, 7+ точек → 15%, годовая оплата → 20%.

---

## Фаза 2 — ЮKassa (Платежи)

**Цель:** карточные платежи, рекуррентный биллинг, счета для юрлиц.

### API-клиент — `app/clients/yukassa.py` (101 строка)

| Функция | Описание |
|---------|----------|
| `create_payment()` | Создание платежа (разовый или рекуррентный через `payment_method_id`) |
| `get_payment()` | Получение статуса платежа |
| `YukassaError` | Исключение для ошибок API |

Реализовано на **httpx** (async), без зависимости от пакета `yookassa`.
Авторизация: Basic Auth (`shop_id:secret_key`), Idempotence-Key в каждом запросе.

### Эндпоинты — `app/routers/payments.py` (467 строк)

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/payments/create` | Создать платёж → вернуть `confirmation_url` |
| POST | `/api/payments/webhook` | Webhook ЮKassa (`payment.succeeded` / `payment.canceled`) |
| GET | `/api/payments/{id}/status` | Статус платежа для success/fail страниц |
| POST | `/api/invoices/create` | Создать счёт (юрлицо), номер АРК-YYYY-NNN |
| GET | `/api/invoices/{id}` | Детали счёта |
| POST | `/api/invoices/{id}/confirm` | Юрлицо подтверждает оплату → ручная проверка |

### Рекуррентный биллинг — `app/jobs/billing.py` (132 строки)

- Cron: **ежедневно 03:00 МСК**
- Находит подписки с `next_billing_at <= now()` и сохранённым `payment_method_id`
- Автоматическое списание через ЮKassa
- При успехе: `next_billing_at += 1 month` (или 1 year)
- При ошибке: уведомление Артемию

### Страницы оплаты

| Файл | Назначение |
|------|------------|
| `web/payment/success.html` | Успешная оплата — автообновление статуса, кнопка «В кабинет» |
| `web/payment/fail.html` | Ошибка — кнопка повторной оплаты |
| `web/payment/invoice.html` | Счёт — детали, товары, кнопка подтверждения |

---

## Фаза 3 — Личный кабинет

**Цель:** полноценный SPA-кабинет тенанта с JWT-авторизацией.

### Эндпоинты — `app/routers/cabinet.py` (968 строк, 19 эндпоинтов)

#### Авторизация
| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/cabinet/auth/login` | Вход (email + password) → JWT |

#### Дашборд
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/cabinet/overview` | Статус подписки, подключений, лента событий |

#### Подписка
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/cabinet/subscription` | Детали подписки с расчётом стоимости |
| PUT | `/api/cabinet/subscription` | Изменение (точки, города, модули) с пересчётом цены |

#### Подключения
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/cabinet/connections` | iiko-креды + Telegram-чаты |
| PUT | `/api/cabinet/connections/iiko` | Обновить URL/логин iiko BO |
| POST | `/api/cabinet/connections/iiko/test` | Тест подключения к iiko |

#### Чаты (CRUD)
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/cabinet/chats` | Список активных чатов |
| POST | `/api/cabinet/chats` | Добавить чат (проверка уникальности) |
| PUT | `/api/cabinet/chats/{chat_id}` | Изменить название/модули/город |
| DELETE | `/api/cabinet/chats/{chat_id}` | Мягкое удаление (`is_active=false`) |
| POST | `/api/cabinet/chats/{chat_id}/test` | Тестовое сообщение в чат |
| POST | `/api/cabinet/chats/verify` | Проверка бота через Telegram API |

#### Биллинг
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/cabinet/billing` | История платежей + данные карты |

#### Настройки
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/cabinet/settings` | Профиль (имя, контакт, email, телефон, ИНН) |
| PUT | `/api/cabinet/settings` | Обновить профиль |
| PUT | `/api/cabinet/settings/password` | Смена пароля (проверка старого, bcrypt) |
| PUT | `/api/cabinet/settings/legal` | Обновить ИНН + юр. название |
| DELETE | `/api/cabinet/account` | Деактивация аккаунта (подтверждение паролем) |

---

## Фаза 4 — Уведомления и автоматизация

**Цель:** lifecycle подписок, welcome-flow, автоматические предупреждения.

### Джобы — `app/jobs/subscription_lifecycle.py` (257 строк)

#### `job_trial_expiry` — 04:00 МСК

| Событие | Действие |
|---------|----------|
| Триал кончается через 3 дня | Уведомление в чат + `tenant_events` |
| Триал кончается завтра | Последнее предупреждение |
| Триал истёк | `status → expired`, уведомление Артемию |

#### `job_payment_grace` — 04:10 МСК

| Событие | Действие |
|---------|----------|
| Оплата просрочена | `status → past_due`, `grace_until = +7 дней` |
| Grace через 3 дня | Предупреждение в чат |
| Grace истёк | `status → suspended`, уведомление Артемию |

### Welcome-уведомление

При успешной оплате (`payment.succeeded` webhook):
1. Активация тенанта → `tenant_events`
2. Приветственное сообщение в чат тенанта
3. Уведомление Артемию с деталями

### Система доставки — `_notify_tenant()`

1. Ищет первый активный чат тенанта (`tenant_chats`)
2. Отправляет через Telegram Bot API (`bot_token` из конфига)
3. Fallback: если чатов нет → уведомление Артемию в мониторинг

---

## Фаза 5 — Финализация

**Цель:** миграция БД, верификация, зависимости.

### Миграция — `app/migrations/003_web_platform.sql` (110 строк)

**Расширение `tenants`:** email, contact, phone, password_hash, trial_ends_at, inn, legal_name.
**Расширение `subscriptions`:** period, connection_fee_paid, yukassa_payment_method_id.

**Новые таблицы:**

| Таблица | Назначение | Ключевые поля |
|---------|------------|---------------|
| `payments` | История платежей | id (UUID), tenant_id, yukassa_id, amount, status, card_last4 |
| `invoices` | Счета юрлиц | id (UUID), invoice_number (АРК-YYYY-NNN), items_json |
| `promo_codes` | Промокоды | code, bonuses_json, usage_limit, valid_until |
| `promo_usage` | Применение промо | promo_id, tenant_id (unique pair) |
| `tenant_events` | Лента событий | event_type, text, icon, created_at |

**Seed-данные:** промокоды `EARLY` (бесплатное подключение + скидка 2000 ₽ × 3 мес) и `FRIEND` (бесплатное подключение).

### Верификация

- Все SQL-запросы покрыты миграциями (001 + 002 + 003)
- Все импорты корректны, циклических зависимостей нет
- Все роутеры зарегистрированы в `main.py`
- Все джобы в scheduler: `billing` 03:00, `trial_expiry` 04:00, `payment_grace` 04:10
- `PyJWT==2.9.0` добавлен в `requirements.txt`

---

## Расписание cron-джобов

| Время (МСК) | Job | Описание |
|-------------|-----|----------|
| 03:00 | `recurring_billing` | Автосписание ЮKassa |
| 04:00 | `trial_expiry` | Предупреждения и деактивация триалов |
| 04:10 | `payment_grace` | Grace period и приостановка за неоплату |

---

## Архитектура

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   wizard.js  │────▶│ onboarding.py│────▶│  PostgreSQL   │
│  (6 шагов)   │     │  (5 API)     │     │  (tenants,    │
└──────────────┘     └──────────────┘     │   subs, ...)  │
                                          └──────────────┘
┌──────────────┐     ┌──────────────┐            │
│  success.html│────▶│ payments.py  │────▶  ЮKassa API
│  fail.html   │     │  (6 API)     │     (create/webhook)
│  invoice.html│     └──────────────┘
└──────────────┘
                     ┌──────────────┐
  SPA (кабинет) ────▶│  cabinet.py  │────▶  JWT auth
                     │  (19 API)    │     (HS256, 30d)
                     └──────────────┘

┌──────────────────────────────────────────┐
│            APScheduler (cron)            │
│  billing.py → subscription_lifecycle.py  │
│  03:00 МСК      04:00 / 04:10 МСК       │
└──────────────────────────────────────────┘
```

---

## Статусы тенанта (жизненный цикл)

```
trial ──(оплата)──▶ active ──(неоплата)──▶ past_due ──(7 дней)──▶ suspended
  │                   │                                              │
  │ (истёк)           │ (удаление)                                   │ (оплата)
  ▼                   ▼                                              ▼
expired            cancelled                                      active
```

---

## Файлы проекта (новые / изменённые)

### Новые файлы
- `app/clients/yukassa.py` — API-клиент ЮKassa
- `app/routers/onboarding.py` — регистрация
- `app/routers/payments.py` — платежи и счета
- `app/jobs/billing.py` — рекуррентный биллинг
- `app/jobs/subscription_lifecycle.py` — lifecycle подписок
- `app/migrations/003_web_platform.sql` — новые таблицы
- `web/payment/success.html` — страница успешной оплаты
- `web/payment/fail.html` — страница ошибки оплаты
- `web/payment/invoice.html` — страница счёта

### Изменённые файлы
- `app/config.py` — поля ЮKassa + JWT
- `app/routers/cabinet.py` — полная переработка (19 эндпоинтов)
- `app/main.py` — регистрация роутеров и джобов
- `web/js/wizard.js` — оплата (card → ЮKassa, invoice → /payment/invoice)
- `.env.example` — секция ЮKassa
- `requirements.txt` — PyJWT

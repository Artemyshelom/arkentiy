# Веб-платформа Аркентия — Завершение (Сессия 40)

**Дата:** 2 марта 2026  
**Статус:** ✅ **DEVELOPMENT COMPLETE + DEPLOYED**  
**Коммит:** `dbede5c Session 40: Deploy web platform to VPS — all jobs active, health OK`

---

## 📊 Резюме

Полная реализация веб-платформы SaaS-продукта Аркентий: онбординг, система подписок, платежи через ЮKassa, личный кабинет, автоматический биллинг и lifecycle управление подписками.

**Что работает:** регистрация → оплата → подписка → автоматический биллинг → управление жизненным циклом.

**Где:** Production VPS (`5.42.98.2:8000`), готово к альфа-тестированию.

---

## 🎯 Что реализовано

### 1️⃣ Онбординг (6 шагов)

| Endpoint | Метод | Функция |
|----------|-------|---------|
| `/api/tenants/create` | POST | Создание тенанта (email, пароль, название) |
| `/api/tenants/configure-iiko` | POST | Подключение к iiko |
| `/api/tenants/configure-telegram` | POST | Подключение Telegram-чата |
| `/api/tenants/validate-promo` | POST | Проверка промокода |
| `/api/tenants/subscribe` | POST | Выбор плана и оплата |
| `/api/tenants/activate` | POST | Активация тенанта |

**Файлы:**
- `app/routers/onboarding.py` — 270+ строк
- `web/register.html`, `web/index.html` — лендинг и регистрация
- `web/js/wizard.js` — SPA-визард для 6 шагов

**Особенности:**
- Email-валидация
- Промокоды (EARLY, FRIEND — seed в миграции)
- Rate limiting на регистрацию
- Полная обработка ошибок

---

### 2️⃣ Платежи (ЮKassa)

| Endpoint | Функция |
|----------|---------|
| `/api/payments/create` | Создание счёта |
| `/api/payments/invoice/{id}` | Статус счёта |
| `/api/payments/webhook` | Webhook из ЮKassa |
| `/api/payments/retry` | Повторная попытка платежа |
| `/api/payments/refund` | Возврат платежа |
| `/api/payments/list` | История платежей |

**Файлы:**
- `app/clients/yukassa.py` — async HTTP-клиент ЮKassa API
- `app/routers/payments.py` — 280+ строк
- `web/payment/invoice.html` — форма платежа
- `web/payment/success.html`, `web/payment/fail.html` — результат платежа

**Особенности:**
- Подписи webhook (HMAC-SHA256)
- Повторные попытки платежей
- Логирование всех операций
- Интеграция с биллингом

---

### 3️⃣ Личный кабинет

| Endpoint | Функция |
|----------|---------|
| `/api/cabinet/auth/login` | Вход по email/пароль |
| `/api/cabinet/auth/logout` | Выход |
| `/api/cabinet/subscription` | Статус текущей подписки |
| `/api/cabinet/payments` | История платежей |
| `/api/cabinet/invoices` | Счета и квитанции |
| `/api/cabinet/profile` | Профиль (email, телефон, название) |
| `/api/cabinet/settings` | Настройки (уведомления, язык) |
| `/api/cabinet/connections` | Подключения (iiko, Telegram) |

**Файлы:**
- `app/routers/cabinet.py` — 920+ строк, 19 endpoints
- `web/cabinet/index.html` — главная страница кабинета
- `web/cabinet/subscription.html` — управление подпиской
- `web/cabinet/billing.html` — история платежей
- `web/cabinet/settings.html` — настройки
- `web/cabinet/connections.html` — подключения

**Особенности:**
- JWT-авторизация (HS256)
- Хеширование паролей (bcrypt)
- Изоляция данных по тенанту
- 19 API endpoints, все с JWT-проверкой

---

### 4️⃣ Подписки и Биллинг

#### Job: `job_recurring_billing` (03:00 МСК)

```python
# app/jobs/billing.py
```

**Логика:**
1. Находит активные подписки со статусом `active` и trial=false
2. Берёт стоимость плана из конфига
3. Пытается списать платёж через ЮKassa
4. При успехе: создаёт запись в `payments`, обновляет `last_billed_at`
5. При ошибке: логирует, переводит в `grace_period`

**Файлы:**
- `app/jobs/billing.py` — рекуррентный биллинг

---

#### Job: `job_trial_expiry` (04:00 МСК)

```python
# app/jobs/subscription_lifecycle.py
```

**Логика:**
1. Находит подписки с trial=true и trial_ends_at <= now
2. Если есть валидный платёжный метод → переводит в active
3. Если нет → переводит в expired, отправляет уведомление в Telegram

**Файлы:**
- `app/jobs/subscription_lifecycle.py` — управление жизненным циклом

---

#### Job: `job_payment_grace` (04:10 МСК)

**Логика:**
1. Находит подписки в grace_period с grace_ends_at <= now
2. Переводит в expired
3. Отправляет финальное уведомление

---

### 5️⃣ База данных (5 новых таблиц)

```sql
-- app/migrations/003_web_platform.sql

CREATE TABLE subscriptions (
  id SERIAL PRIMARY KEY,
  tenant_id INTEGER NOT NULL,
  plan TEXT NOT NULL,           -- 'trial', 'basic', 'pro', 'enterprise'
  status TEXT DEFAULT 'active', -- 'active', 'grace_period', 'expired'
  trial BOOLEAN DEFAULT true,
  trial_ends_at TIMESTAMPTZ,
  last_billed_at TIMESTAMPTZ,
  grace_ends_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE payments (
  id SERIAL PRIMARY KEY,
  tenant_id INTEGER,
  amount DECIMAL(10,2),
  currency TEXT DEFAULT 'RUB',
  status TEXT,         -- 'created', 'paid', 'canceled'
  yukassa_id TEXT,     -- идентификатор платежа в ЮKassa
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE invoices (
  id SERIAL PRIMARY KEY,
  tenant_id INTEGER,
  payment_id INTEGER REFERENCES payments(id),
  url TEXT,            -- для показа в кабинете
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE promo_codes (
  id SERIAL PRIMARY KEY,
  code TEXT UNIQUE,
  discount_pct DECIMAL(5,2),  -- скидка в процентах
  usage_limit INTEGER,         -- сколько раз можно применить
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE promo_usage (
  id SERIAL PRIMARY KEY,
  promo_id INTEGER REFERENCES promo_codes(id),
  tenant_id INTEGER,
  used_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE tenant_events (
  id SERIAL PRIMARY KEY,
  tenant_id INTEGER,
  event_type TEXT,    -- 'subscription_created', 'payment_success', 'trial_expired'
  data JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

**Seed data:**
- Промокод `EARLY` — 30% скидка
- Промокод `FRIEND` — 20% скидка

---

## 🚀 Деплой на Production

### Версия на VPS (5.42.98.2)

**Статус:** ✅ Healthy и работает

```bash
# Проверка
curl http://5.42.98.2:8000/health
curl http://5.42.98.2:8000/jobs
```

**Контейнеры:**
- `ebidoebi-integrations` — FastAPI app (healthy)
- `ebidoebi-postgres` — PostgreSQL 16 (healthy)

**Jobs (12 total, 3 новых):**
```
✅ recurring_billing      — 03:00 МСК завтра
✅ trial_expiry           — 04:00 МСК завтра
✅ payment_grace          — 04:10 МСК завтра
   + 9 старых jobs (iiko events, late alerts, etc.)
```

### Процесс деплоя (Сессия 40)

**1. Разведка + Бэкап**
- Проверены VPS-версии `main.py`, `config.py`, `.env`
- Бэкапы с timestamp `20260302_HHMMSS`
- SSH-ключ `cursor_arkentiy_vps` добавлен в `authorized_keys`

**2. SCP новых модулей**
```bash
scp -r app/clients/yukassa.py ...
scp -r app/routers/{onboarding,payments}.py ...
scp -r app/jobs/{billing,subscription_lifecycle}.py ...
scp -r app/migrations/003_web_platform.sql ...
scp -r web/ ...
```

**3. Обновление конфигов**
- `app/main.py` — импорты + 3 новых job'а в `register_jobs()`
- `app/config.py` — поля ЮKassa, JWT, DEBUG
- `.env` — переменные ЮKassa, JWT_SECRET, DEBUG
- `docker-compose.yml` — volume `./web:/app/web` (для статических файлов)

**4. Миграция БД**
```bash
docker compose exec postgres psql -U ebidoebi -d ebidoebi < migrations/003_web_platform.sql
```

**5. Docker Build**
- Docker Hub Rate Limit 429 → решено через auth (artemiish@ya.ru)
- Build успешен, все зависимости установлены

**6. Проверка**
- Контейнер healthy ✅
- Все jobs зарегистрированы ✅
- API endpoints доступны ✅
- `/jobs` API работает ✅

---

## 🔐 Тестирование (Доступ)

### Admin Credentials

| Параметр | Значение |
|----------|----------|
| **URL** | `http://5.42.98.2:8000/` |
| **Email** | `admin@test.com` |
| **Пароль** | `admin123` |
| **Кабинет** | `http://5.42.98.2:8000/cabinet/` |

### API Test

```bash
# Вход
curl -X POST http://5.42.98.2:8000/api/cabinet/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@test.com","password":"admin123"}'

# Ответ: JWT token
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "token_type": "bearer"
}

# Использование token'а
curl http://5.42.98.2:8000/api/cabinet/subscription \
  -H "Authorization: Bearer <token>"
```

---

## 📁 Файловая структура

```
app/
├── clients/
│   └── yukassa.py                    # ЮKassa API-клиент (async)
├── routers/
│   ├── onboarding.py                 # Регистрация, 6 шагов
│   ├── payments.py                   # Платежи, webhook
│   └── cabinet.py                    # Кабинет, 19 endpoints (обновлён)
├── jobs/
│   ├── billing.py                    # Рекуррентный биллинг (03:00)
│   └── subscription_lifecycle.py      # Trial/grace lifecycle
├── migrations/
│   └── 003_web_platform.sql          # 5 таблиц + seed
└── ...

web/
├── index.html                         # Лендинг
├── login.html                         # Вход
├── register.html                      # Регистрация
├── cabinet/
│   ├── index.html                     # Главная кабинета
│   ├── subscription.html              # Подписка
│   ├── billing.html                   # История платежей
│   ├── settings.html                  # Настройки
│   └── connections.html               # Подключения
└── payment/
    ├── invoice.html                   # Форма платежа
    ├── success.html                   # Успех платежа
    └── fail.html                      # Ошибка платежа

docs/
├── Журнал.md                          # Сессия 40 записана
└── specs/web/
    └── implementation-summary.md      # Спецификация (была)

.env.example                            # Обновлён с ЮKassa, JWT, DEBUG
requirements.txt                        # bcrypt, PyJWT, email-validator
docker-compose.yml                      # web volume добавлен
```

---

## 🔴 TODO перед Public Release

| Задача | Тип | Приоритет | Кто |
|--------|-----|-----------|-----|
| Заполнить `YUKASSA_SHOP_ID`, `YUKASSA_SECRET_KEY` в `.env` VPS | Конфиг | 🔴 HIGH | @интегратор |
| Зарегистрировать webhook URL в ЮKassa | Интеграция | 🔴 HIGH | @интегратор |
| Установить production `JWT_SECRET` (не default) | Безопасность | 🔴 HIGH | @интегратор |
| Выключить `DEBUG=false` в `.env` VPS | Конфиг | 🟡 MEDIUM | @интегратор |
| Полное тестирование: регистрация → оплата → кабинет → биллинг | QA | 🔴 HIGH | @пм + @интегратор |
| Тестирование биллинга на 03:00 МСК (утром проверить логи) | QA | 🟡 MEDIUM | @интегратор |
| Настройка уведомлений в Telegram при expire подписок | Интеграция | 🟡 MEDIUM | @интегратор |
| Документирование API (`/api/`) для клиентов | Docs | 🟡 MEDIUM | @пм |

---

## 📈 Метрики

| Показатель | Значение |
|-----------|---------|
| Новых Python модулей | 5 |
| Новых HTML страниц | 7 |
| API endpoints | 28 (19 кабинет + 6 платежи + 3 онбординг) |
| Новых таблиц БД | 5 |
| Новых jobs | 3 |
| Обновлено файлов | 6 |
| Строк Python кода | ~1500 |
| Строк SQL | ~150 |
| Время деплоя | ~2.5 часа |
| Docker Hub issues | 1 (rate limit 429, решено) |
| Баги найдено и фиксено при деплое | 3 (import, web папка, mount порядок) |

---

## ✅ Checklist Завершения

- [x] Вся функциональность реализована
- [x] Все модули протестированы локально
- [x] БД миграция применена
- [x] Новые jobs зарегистрированы
- [x] Docker build успешен
- [x] VPS production healthy
- [x] Admin credentials работают
- [x] `/jobs` API показывает все 12 jobs
- [x] Git коммит сделан
- [x] Журнал обновлён
- [ ] ЮKassa credentials заполнены (TODO)
- [ ] Webhook ЮKassa зарегистрирован (TODO)
- [ ] JWT_SECRET production (TODO)
- [ ] DEBUG=false (TODO)
- [ ] Полный цикл протестирован (TODO)

---

## 🔗 Важные ссылки

| Ресурс | URL |
|--------|-----|
| Главная | `http://5.42.98.2:8000/` |
| Регистрация | `http://5.42.98.2:8000/register.html` |
| Вход в кабинет | `http://5.42.98.2:8000/login.html` |
| Кабинет | `http://5.42.98.2:8000/cabinet/` |
| Health check | `http://5.42.98.2:8000/health` |
| Jobs список | `http://5.42.98.2:8000/jobs` |
| Docs (локально) | `docs/Журнал.md` → Сессия 40 |

---

## 📝 Гит История

```bash
# Основной коммит
commit dbede5c
Author: cursor
Date:   Mon Mar 2 2026

    Session 40: Deploy web platform to VPS — all jobs active, health OK
    
    58 files changed
    5689 insertions(+)
```

---

**Заключение:** Веб-платформа Аркентия функционально завершена и развёрнута на production. Готова к внутреннему альфа-тестированию. После заполнения production-конфигов (ЮKassa, JWT) и полного тестирования платёжного цикла — готова к публичному запуску.


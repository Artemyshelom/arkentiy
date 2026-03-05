# Журнал изменений — Архив 2025

> Архив сессий 1–39. Актуальные сессии 40–53 в `docs/journal.md`.

---

## Сессия 39 — 1 марта 2026 (Веб-платформа: онбординг, оплата, кабинет)

### Что сделано

Реализована полная веб-платформа Аркентия — онбординг, оплата, личный кабинет, lifecycle подписок.

- **Фаза 0:** `app/config.py` — поля ЮKassa + JWT. Зависимости: `bcrypt`, `slowapi`, `PyJWT`, `email-validator`.
- **Фаза 1:** `app/routers/onboarding.py` — 5 API: регистрация тенанта за 6 шагов. `web/js/wizard.js` — SPA-визард.
- **Фаза 2:** `app/clients/yukassa.py` — async API-клиент ЮKassa (httpx). `app/routers/payments.py` — 6 API: оплата, webhook, счета. `app/jobs/billing.py` — рекуррентный биллинг (03:00 МСК).
- **Фаза 3:** `app/routers/cabinet.py` — 19 API, JWT-авторизация, полный CRUD кабинета.
- **Фаза 4:** `app/jobs/subscription_lifecycle.py` — trial/grace lifecycle (04:00, 04:10 МСК).
- **Фаза 5:** `app/migrations/003_web_platform.sql` — 5 новых таблиц: `payments`, `invoices`, `promo_codes`, `promo_usage`, `tenant_events`. Seed: промокоды `EARLY`, `FRIEND`.

### Исправлено

- `app/routers/onboarding.py` — SyntaxError в f-string с backslash (строка 486). Вынесено в переменную `first_payment_fmt`.

### Проверка

- `python3 -m compileall app` — ✅ чисто, ошибок нет.

### Изменённые файлы

**Новые:** `app/clients/yukassa.py`, `app/routers/onboarding.py`, `app/routers/payments.py`, `app/jobs/billing.py`, `app/jobs/subscription_lifecycle.py`, `app/migrations/003_web_platform.sql`, `web/payment/success.html`, `web/payment/fail.html`, `web/payment/invoice.html`  
**Изменённые:** `app/config.py`, `app/routers/cabinet.py`, `app/main.py`, `web/js/wizard.js`, `.env.example`, `requirements.txt`

### Статус

Синтаксически чисто. До релиза: заполнить `YUKASSA_SHOP_ID`/`YUKASSA_SECRET_KEY` на VPS, применить миграцию 003, зарегистрировать webhook URL в ЮKassa, установить production `JWT_SECRET`, выключить `DEBUG=false`.

---

## Миграция журнала (1 марта 2026)

- Новый основной путь: `02_Проекты/Аркентий/docs/Журнал.md`
- Архив предыдущих журналов: `02_Проекты/Аркентий/docs/Архив/`
- Файлы в архиве:
  - `Журнал_ИИ_автоматизаций.md`
  - `Журнал_интегратора_legacy.md`

Рекомендуемый формат записи:

```markdown
## Сессия N — ДД месяц ГГГГ (краткий заголовок)

### Что сделано
- ...

### Изменённые файлы
- ...

### Статус после сессии
- ...
```

# Аркентий — Документация

> Навигация по документации проекта. Все файлы на латинице, структура по категориям.

---

## Навигация

### Живые документы (корень)

| Файл | Назначение |
|------|-----------|
| [journal.md](journal.md) | Технический журнал сессий — что сделано, где, зачем |
| [CHANGELOG.md](CHANGELOG.md) | Пользовательский changelog — что видит клиент |
| [BACKLOG.md](BACKLOG.md) | Задачи в работе и очереди |
| [roadmap.md](roadmap.md) | Стратегия, 30/90-дневные приоритеты |

---

### Онбординг клиентов → `onboarding/`

| Файл | Назначение |
|------|-----------|
| [onboarding/protocol.md](onboarding/protocol.md) | Пошаговый протокол подключения + troubleshooting |
| [onboarding/registry.md](onboarding/registry.md) | Реестр подключённых клиентов |
| [onboarding/data_checklist.md](onboarding/data_checklist.md) | SQL-запросы для проверки качества данных |

---

### Справочники → `reference/`

| Файл | Назначение |
|------|-----------|
| [reference/modules.md](reference/modules.md) | Команды бота, модули, права доступа |
| [reference/olap_fields.md](reference/olap_fields.md) | Маппинг OLAP полей → колонки БД, грабли iiko |
| [reference/git_workflow.md](reference/git_workflow.md) | Git-процесс, репозитории, когда пушить |

---

### RAG Поиск по коду → `codesearch/`

| Файл | Назначение |
|------|-----------|
| [codesearch/README.md](codesearch/README.md) | **Навигация** по поиску кода |
| [codesearch/QUICK_START.md](codesearch/QUICK_START.md) | Как использовать `/api/codesearch` (для агентов) |
| [codesearch/IMPLEMENTATION.md](codesearch/IMPLEMENTATION.md) | Техника: pgvector, HNSW, Jina AI, SOCKS5 прокси |
| [codesearch/FINAL_REPORT.md](codesearch/FINAL_REPORT.md) | Итоговый отчёт: индекс 1748 чанков, пример поисков |

---

### Правила для интегратора → `rules/integrator/`

> Авторитетные источники истины для разработки. Читать по необходимости — не загружать весь контекст.

| Файл | Когда читать |
|------|-------------|
| [rules/integrator/lessons.md](../rules/integrator/lessons.md) | **ПЕРЕД отладкой** — все антипаттерны и критические баги |
| [rules/integrator/iiko_api.md](../rules/integrator/iiko_api.md) | Перед работой с iiko — 60+ полей, Events API, OLAP |
| [rules/integrator/architecture.md](../rules/integrator/architecture.md) | Jobs расписание, BranchState, таблицы БД |
| [rules/integrator/access_architecture.md](../rules/integrator/access_architecture.md) | Система прав, модули, /доступ |
| [rules/integrator/deploy.md](../rules/integrator/deploy.md) | **ПЕРЕД деплоем** — пошаговый протокол |
| [rules/integrator/database.md](../rules/integrator/database.md) | Схема таблиц, UPSERT-правила |
| [rules/integrator/infrastructure.md](../rules/integrator/infrastructure.md) | VPS, SSH, пути, Google SA |
| [rules/integrator/stack.md](../rules/integrator/stack.md) | Выбор технологии (Python vs GAS vs n8n) |
| [rules/integrator/team_agents.md](../rules/integrator/team_agents.md) | Агенты, роли, specs |

---

### Specs → `specs/`

Техзадания на фичи. Структура: `specs/tg/` (Telegram) и `specs/web/` (веб).

---

### Архив → `archive/`

Старые документы, снапшоты, legacy. Не вести — только хранить.

- [archive/journal_2025.md](archive/journal_2025.md) — сессии 1–39
- [archive/LESSONS_LEARNED_Shaburov.md](archive/LESSONS_LEARNED_Shaburov.md) — кейс-стади онбординга Шабурова

---

## Быстрые маршруты

| Задача | Куда идти |
|--------|----------|
| Встал баг | [rules/integrator/lessons.md](../rules/integrator/lessons.md) |
| Деплою | [rules/integrator/deploy.md](../rules/integrator/deploy.md) |
| Подключаю клиента | [onboarding/protocol.md](onboarding/protocol.md) |
| Работаю с iiko API | [rules/integrator/iiko_api.md](../rules/integrator/iiko_api.md) |
| Что нового у пользователей | [CHANGELOG.md](CHANGELOG.md) |
| Стратегия / приоритеты | [roadmap.md](roadmap.md) |
| Что в работе | [BACKLOG.md](BACKLOG.md) |

---

## Структура проекта

```
arkentiy/
├── app/          — backend: jobs, clients, services, routers
├── web/          — frontend: личный кабинет
├── docs/         — эта папка
│   ├── onboarding/
│   ├── reference/
│   ├── specs/
│   └── archive/
├── rules/
│   └── integrator/   — правила и справочники для интегратора
└── ...
```
3. Создать `secrets/branches.json` с конфигом точек
4. Запустить: `docker compose up -d --build`
5. Проверить: `curl http://localhost:8000/health`

---

## 🏗️ Архитектура

- `app/` — backend (FastAPI + APScheduler + Telegram bot)
- `web/` — frontend личного кабинета
- Мультитенантность: каждый клиент (tenant) имеет свой набор точек и настроек
- Изоляция данных на уровне БД
- Личный кабинет с JWT-авторизацией

---

## 🔌 API

| Endpoint | Метод | Зачем |
|----------|-------|-------|
| `/health` | GET | Статус сервиса |
| `/jobs` | GET | Список фоновых задач |
| `/run/{job_id}` | POST | Запустить задачу вручную |
| `/cabinet/` | GET | Веб-интерфейс личного кабинета |
| `/api/cabinet/*` | POST | API кабинета |

---

## 📚 Полный список документов

**User-facing (для пользователей бота):**
- [CHANGELOG.md](CHANGELOG.md) — история обновлений
- [Модули_и_команды_бота.md](Модули_и_команды_бота.md) — описание команд
- [specs/](specs/) — UX спецификации

**Техническая документация (для команды):**
- [Журнал.md](Журнал.md) — техническая история
- [Дорожная карта.md](Дорожная%20карта.md) — стратегия развития
- [BACKLOG.md](BACKLOG.md) — текущие задачи
- [Уроки_и_баги.md](Уроки_и_баги.md) — справочник ошибок

**Справочники интегратора:**
- [Архив/](Архив/) — архив старых документов и исторических отчётов

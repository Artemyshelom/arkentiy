# GitHub и бэкап

## Репозитории

| Проект | Репозиторий | Назначение |
|--------|------------|------------|
| **Аркентий** | [Artemyshelom/arkentiy](https://github.com/Artemyshelom/arkentiy) | Основной сервер (этот проект) |
| **Арсений** | [Artemyshelom/arseny-assistent](https://github.com/Artemyshelom/arseny-assistent) | Отдельный проект, независимый |

**Важно:** Аркентий и Арсений — разные независимые проекты. Не создавай общих зависимостей. Взаимодействие только через API/webhook/Telegram.

## Когда пушить

```bash
ssh arkentiy \
  "cd /opt/ebidoebi && git add app/ Dockerfile docker-compose.yml && git commit -m 'feat: описание' && git push"
```

| Пушить | Не пушить |
|--------|----------|
| Новый модуль / интеграция | Мелкий хотфикс (потом батчем) |
| Крупная фича, рефакторинг | Правка одной строки в debug |
| После healthy + чистых логов | Экспериментальный код |

**Порядок:** сначала healthy + логи → потом git push. Не раньше.

**В git:** только код (`app/`, `Dockerfile`, `docker-compose.yml`).
**Не в git:** `.env`, `secrets/`, `*.bak.*`.

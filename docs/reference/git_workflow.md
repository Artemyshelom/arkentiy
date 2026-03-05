# Git — Рабочий процесс и репозитории

> **Правило:** История git должна отражать реальность. Все правки на VPS → в git.

---

## Репозитории

| Проект | Репозиторий | Назначение |
|--------|------------|------------|
| **Аркентий** | [Artemyshelom/arkentiy](https://github.com/Artemyshelom/arkentiy) | Основной сервер (этот проект) |
| **Арсений** | [Artemyshelom/arseny-assistent](https://github.com/Artemyshelom/arseny-assistent) | Отдельный проект, независимый |

**Важно:** Аркентий и Арсений — разные независимые проекты. Не создавай общих зависимостей.

---

## Рабочий процесс

```bash
git pull origin main    # Всегда тяни перед работой
```

### Новая фича / рефакторинг
```
1. Локально разрабатываешь
2. Коммитишь: git add → git commit -m "feat: ..."
3. Пушишь: git push origin main
4. На VPS: git pull && docker compose up -d --build
```

### Срочный баг в проде
```
1. Исправляешь прямо на VPS (через SSH)
2. Тестируешь (docker logs, curl)
3. Коммитишь на VPS: git add -A → git commit → git push
4. Локально: git pull (синхронизируешь)
```

---

## После любой правки на VPS

**КРИТИЧНО:** Не оставлять незакоммиченные изменения.

```bash
# На VPS
git add -A
git commit -m "fix: описание что исправил"
git push origin main
```

---

## Деплой из локалки

После пуша в GitHub:
```bash
ssh arkentiy "cd /opt/ebidoebi && git pull && docker compose up -d --build"

# Проверка
ssh arkentiy "cd /opt/ebidoebi && docker compose ps && docker compose logs app --tail=20"
```

---

## Что коммитить

| Коммитить | НЕ коммитить |
|----------|-------------|
| Новый модуль / интеграция | `.env`, `secrets/`, `*.bak.*` |
| Крупная фича, рефакторинг | Экспериментальный код |
| После healthy + чистых логов | Правка одной строки в debug |

**В git:** только код — `app/`, `Dockerfile`, `docker-compose.yml`, `docs/`, `web/`.  
**Порядок:** сначала healthy + чистые логи → потом `git push`.

---

## Чеклист

| Этап | Проверка |
|------|----------|
| Перед работой | `git pull origin main` |
| После разработки локально | `git add`, `git commit`, `git push` |
| После фикса на VPS | Исправил → тестировал → `git add -A && git commit && git push` |
| Перед деплоем | `git status` — всё чисто? |
| После деплоя | `docker compose ps` — контейнер healthy? |
| После дня работы | Локально — `git pull` для синхронизации |

---

**История git = источник истины.** Откат в любой момент: `git revert <commit>` или `git reset --hard <hash>`.

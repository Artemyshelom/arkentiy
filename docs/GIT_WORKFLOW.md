# Git Workflow — Аркентий

> **Правило:** История git должна отражать реальность. Все правки на VPS → в git.

---

## 📋 Перед началом работы

```bash
git pull origin main    # Всегда тяни перед работой
```

---

## 🏗️ Где и как работать

### Новая фича / рефакторинг
```
1. Локально разрабатываешь
2. Коммитишь: git add → git commit -m "..."
3. Пушишь: git push origin main
4. На VPS: git pull && docker compose up -d --build
```

### Срочный баг в проде / дебаг
```
1. Исправляешь прямо на VPS (через SSH)
2. Тестируешь на VPS (docker logs, curl)
3. Если работает — коммитишь эту версию в git
```

---

## ✅ После любой правки на VPS

**КРИТИЧНО:** Не оставлять незакоммиченные изменения.

```bash
# На VPS
git add -A
git commit -m "fix: описание что исправил"
git push origin main
```

Потом локально:
```bash
git pull origin main    # Синхронизируешь локал с VPS
```

---

## 🚀 Деплой из локалки

**После пуша в GitHub:**

```bash
# На VPS
ssh arkentiy "cd /opt/ebidoebi && git pull && docker compose up -d --build"

# Проверка
ssh arkentiy "cd /opt/ebidoebi && docker compose ps && docker compose logs app --tail=20"
```

---

## 📊 Чеклист: как не ошибиться

| Этап | Проверка |
|------|----------|
| **Перед работой** | `git pull origin main` |
| **После разработки локально** | `git add`, `git commit`, `git push` |
| **После фикса на VPS** | Исправил → тестировал → `git add -A && git commit && git push` |
| **Перед деплоем** | `git status` — всё чисто? |
| **После деплоя** | `docker compose ps` — контейнер healthy? |
| **После дня работы** | Локально — `git pull` для синхронизации |

---

## 🎯 История git = источник истины

- ✅ Коммит на VPS → запушится в GitHub
- ✅ Локальный коммит → запушится в GitHub  
- ✅ Все видят кто, когда, зачем изменил
- ✅ Можно откатиться на любой момент

---

## 🔴 Если забыл коммитить на VPS

```bash
# На VPS проверяешь что изменилось
git status

# Если есть изменения — коммитишь
git add -A
git commit -m "fix: что-то"

# Пушишь
git push origin main

# Локально синхронизируешь
git pull origin main
```

---

**Последнее обновление:** 5 Марта 2026

# GIT-FIRST WORKFLOW — Аркентий

> **Критическое правило:** Всё только через локал и git. На VPS прямые правки ЗАПРЕЩЕНЫ.

---

## 📋 Правильный порядок разработки

```
1. Изменяй ЛОКАЛЬНО
   ↓
2. Коммитируй в git (git add → git commit)
   ↓
3. Пушь на GitHub (git push origin main)
   ↓
4. VPS тянет из GitHub (git pull origin main)
   ↓
5. Деплой на VPS (docker compose build --no-cache && up -d)
   ↓
6. Проверка (curl, docker logs, статус)
```

---

## ✅ ЧТО МОЖНО

| Действие | Место | Как |
|----------|-------|-----|
| Редактировать код | Локально | `code main.py` |
| Тестировать | Локально | `docker compose up -d` |
| Коммитить | Локально | `git commit -m "..."` |
| Пушить | Локально | `git push origin main` |
| Проверять logs | VPS | `ssh arkentiy "docker logs ..."` |
| Смотреть процессы | VPS | `ssh arkentiy "docker ps"` |
| Откатывать из бэкапа | VPS | `ssh arkentiy "cp file.bak file"` (экстренно) |

---

## ❌ ЧТО ЗАПРЕЩЕНО

| Действие | Почему |
|----------|--------|
| **Редактировать на VPS через SSH** | `echo "код" >> file.py` — нет истории |
| **Коммитить с VPS в git** | `git commit` на сервере — потеря синхронизации |
| **Создавать файлы на VPS** | Потом нельзя откатить, нельзя увидеть в истории |
| **Пушить с VPS на GitHub** | VPS только pulls, не pushes |
| **Редактировать в nano/vim на VPS** | Без контроля версий → потеря кода |

---

## 🔄 Если срочный фикс на VPS

**Даже если сервер упал, не редактируй прямо там!**

**Правильно:**

```bash
# 1. Локально исправляешь
code app/main.py

# 2. Коммитишь
git add app/main.py
git commit -m "fix: ошибка в main.py"

# 3. Пушишь
git push origin main

# 4. На VPS
ssh arkentiy "cd /opt/ebidoebi && git pull origin main && docker compose build --no-cache && up -d"

# 5. Проверяешь
ssh arkentiy "cd /opt/ebidoebi && docker compose logs app --tail=20"
```

**Неправильно:**

```bash
❌ ssh arkentiy "sed -i 's/bug/fix/g' /opt/ebidoebi/app/main.py"
❌ ssh arkentiy "echo 'import os' >> /opt/ebidoebi/app/main.py"
❌ ssh arkentiy "cd /opt/ebidoebi && git commit -m 'fix on server'"
```

---

## 🎯 Почему это важно

| Выгода | Описание |
|--------|----------|
| **История** | Git логирует кто, когда, зачем (blame, bisect) |
| **Откат** | Любой момент → `git revert` или `git reset` |
| **Синхронизация** | Все разработчики видят одно и то же |
| **Воспроизводимость** | Можно восстановить баг, который был месяц назад |
| **Код-ревью** | Pull requests перед merge |
| **CI/CD** | Автоматическое тестирование перед деплоем |

---

## 📊 Статус-чек: ты делаешь правильно?

- [ ] Редактирую файлы только локально
- [ ] Коммитю все изменения в git
- [ ] Пушу на GitHub перед деплоем
- [ ] VPS тянет из GitHub (git pull)
- [ ] При экстренном фиксе — локальное изменение → commit → push → pull на VPS
- [ ] На VPS редактирую только конфиги (.env) если критично, но потом обновляю локально
- [ ] История git отражает реальность кода

---

## 🚨 Экстренные ситуации

### Сценарий 1: Сервер упал, нужно срочно исправить

**ПРАВИЛЬНО:**
1. Локально исправляешь
2. Git commit + push
3. VPS pull + rebuild

**НЕПРАВИЛЬНО:**
1. SSH в prod
2. `sed -i` или `nano` исправление
3. `git commit` на сервере

### Сценарий 2: Позабыл закоммитить перед деплоем

**Не пушь на VPS без git!**
```bash
git status                    # Проверь что не закоммичено
git add файлы
git commit -m "..."
git push origin main
# Только ПОТОМ деплой
```

### Сценарий 3: Нужно изменить .env на VPS

**Это OK (конфиги, секреты):**
```bash
ssh arkentiy "cat >> /opt/ebidoebi/.env << 'EOF'
NEW_VAR=value
EOF"
```

**Но запомни:**
- `.env` не в git (для security)
- Если нужна новая переменная в коде → закоммить в `.env.example`

---

**Последнее обновление:** 5 Марта 2026

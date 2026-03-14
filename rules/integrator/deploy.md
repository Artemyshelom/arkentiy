# Протокол деплоя Аркентия (v2)

> Живые заказы, живые деньги. Git — источник истины, VPS всегда тянет из него.

## Рабочий процесс

```
Cursor (локально) → ветка → commit → push → merge в main → VPS git pull → restart
```

**Запрещено:** редактировать файлы напрямую на VPS через SSH или заливать через scp минуя git. 
Если файл изменён — он должен быть в коммите. Это и есть история + возможность отката.

---

## ШАГ 0 — Ветка (НОВОЕ)

Перед началом работы — создать ветку от main:

**В Cursor:** внизу слева кликнуть на `main` → Create new branch → название

**Названия:**
— `fix/что-чиним` — баг-фикс
— `feature/что-добавляем` — новая функция
— `refactor/что-переделываем` — рефакторинг

Одна задача = одна ветка. Дофиксы той же задачи — в ту же ветку.

---

## ШАГ 1 — Разведка (только если меняешь существующий файл)

Перед изменением существующего файла — убедиться что main актуален:

```bash
git checkout main
git pull
git checkout -b fix/my-fix
```

Если есть сомнения что VPS и git разошлись:

```bash
ssh arkentiy "cd /opt/ebidoebi && git status && git log --oneline -3"
```

---

## ШАГ 2 — Разработка и коммит

Пишешь код локально в Cursor. Перед коммитом — проверь что не трогаешь `.env` и не перезаписываешь секреты.

**Через Source Control в Cursor:**
1. Написать сообщение коммита в поле Message
2. Нажать Commit (✓)
3. Нажать Publish Branch / Push

**Через терминал:**
```bash
git add .
git commit -m "fix: краткое описание что изменилось и зачем"
git push -u origin fix/my-fix
```

Хороший commit message = замена ручного журнала. Пиши понятно.

---

## ШАГ 3 — Мерж в main (НОВОЕ)

Когда задача готова к деплою:

**Через Cursor (Source Control):**
1. Переключиться на main (внизу слева → кликнуть → main)
2. Три точки (...) → Branch → Merge Branch → выбрать свою ветку
3. Push

**Через терминал:**
```bash
git checkout main
git pull
git merge fix/my-fix
git push
```

Если конфликт — Cursor подсветит, выбрать нужный вариант, сохранить, закоммитить.

---

## ШАГ 4 — Деплой на VPS

```bash
ssh arkentiy "cd /opt/ebidoebi && git pull && docker compose restart"
```

**Когда `restart` достаточно:**
— Изменения только в Python-коде (монтируется через volume)
— Не менял `requirements.txt`, `Dockerfile`, `docker-compose.yml`

**Когда нужен `build`:**
— Добавил новую библиотеку в `requirements.txt`
— Изменил `Dockerfile` или `docker-compose.yml`
```bash
ssh arkentiy "cd /opt/ebidoebi && git pull && docker compose build --no-cache && docker compose up -d"
```

---

## ШАГ 5 — Проверка

```bash
sleep 15
ssh arkentiy "cd /opt/ebidoebi && docker compose ps"          # всё running?
ssh arkentiy "cd /opt/ebidoebi && docker compose logs --tail=30"  # нет ERROR/ImportError?
```

Если контейнер в состоянии `Restarting` — немедленный откат:

```bash
ssh arkentiy "cd /opt/ebidoebi && git revert HEAD --no-edit && git push && docker compose restart"
```

---

## Правила для .env и секретов

- **Никогда** не перезаписывать `.env` целиком
- Новые переменные — только дописывать в конец:
 ```bash
 ssh arkentiy "cat >> /opt/ebidoebi/.env << 'EOF'
 НОВАЯ_ПЕРЕМЕННАЯ=значение
 EOF"
 ```
- `.env` не коммитить в git (он в `.gitignore`)

---

## Минорное обновление (хотфикс)

Мелкий фикс (1-2 файла, понятно что делает) — можно сразу в main без ветки:

```bash
git commit -m "fix: ..." && git push
ssh arkentiy "cd /opt/ebidoebi && git pull && docker compose restart"
# подождать 15 сек, проверить логи
```

Большие задачи (рефакторинг, несколько файлов) — только через ветку.

---

## Правило пятницы

— Мелкие фиксы — можно деплоить
— Рефакторинг / большие изменения — не деплоить в пятницу после обеда (на понедельник-вторник)

---

## Запрещено

- Деплоить в обход git (scp, прямое редактирование на VPS)
- Деплоить и уходить — всегда жди running и проверяй логи
- Перезаписывать `.env` целиком
- Держать на VPS локальные изменения вне git (`git status` должен быть чистым)

# Протокол деплоя Аркентия

> Живые заказы, живые деньги. Git — источник истины, VPS всегда тянет из него.

## Рабочий процесс

```
Cursor (локально) → git commit → git push → VPS git pull → docker build
```

**Запрещено:** редактировать файлы напрямую на VPS через SSH или заливать через scp минуя git.  
Если файл изменён — он должен быть в коммите. Это и есть история + возможность отката.

---

## ШАГ 1 — Разведка (только если меняешь существующий файл)

Перед изменением существующего файла — убедиться что локальная версия актуальна:

```bash
git pull origin main
```

Если есть сомнения что VPS и git разошлись:

```bash
ssh arkentiy "cd /opt/ebidoebi && git status && git log --oneline -3"
```

---

## ШАГ 2 — Разработка и коммит

Пишешь код локально в Cursor. Перед коммитом — проверь что не трогаешь `.env` и не перезаписываешь секреты.

```bash
git add .
git commit -m "fix: краткое описание что изменилось и зачем"
git push origin main
```

Хороший commit message = замена ручного журнала. Пиши понятно.

---

## ШАГ 3 — Деплой на VPS

```bash
ssh arkentiy "cd /opt/ebidoebi && git pull origin main && docker compose build --no-cache && docker compose up -d"
```

**Важно:** `docker compose restart` НЕ применяет изменения кода, только `.env`. Всегда используй `up -d`.

---

## ШАГ 4 — Проверка

```bash
sleep 15
ssh arkentiy "cd /opt/ebidoebi && docker compose ps"         # всё healthy?
ssh arkentiy "cd /opt/ebidoebi && docker compose logs app --tail=30"  # нет ERROR/ImportError?
```

Если контейнер в состоянии `Restarting` — немедленный откат:

```bash
ssh arkentiy "cd /opt/ebidoebi && git revert HEAD --no-edit && git push origin main && docker compose build --no-cache && docker compose up -d"
```

Или если нужно откатить конкретный файл:

```bash
ssh arkentiy "cd /opt/ebidoebi && git checkout HEAD~1 -- app/jobs/file.py && docker compose build --no-cache && docker compose up -d"
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

Одна функция, одна строка — тот же процесс, просто быстрее:

```bash
git commit -m "fix: ..." && git push
ssh arkentiy "cd /opt/ebidoebi && git pull && docker compose up -d --build"
# подождать 15 сек, проверить логи
```

Никаких .bak файлов, никакого журнала вручную. Git помнит всё.

---

## Запрещено

- Деплоить в обход git (scp, прямое редактирование на VPS)
- Деплоить и уходить — всегда жди `healthy` и проверяй логи
- Перезаписывать `.env` целиком
- Держать на VPS локальные изменения вне git (`git status` должен быть чистым)

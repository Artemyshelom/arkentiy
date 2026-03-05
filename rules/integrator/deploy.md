# Протокол деплоя Аркентия

> Нарушение = поломка продакшена. Живые заказы, живые деньги.

## Рабочий процесс разработки

```
Cursor (локально) → scp → VPS → git push origin main
```

**Запрещено:** редактировать файлы напрямую на VPS через SSH. Локальная версия = источник истины.

## ШАГ 1 — Разведка

До того как написал хоть одну команду scp:

```bash
ssh arkentiy "ls -la /opt/ebidoebi/app/"
ssh arkentiy "ls -la /opt/ebidoebi/app/clients/"
ssh arkentiy "ls -la /opt/ebidoebi/app/jobs/"
ssh arkentiy "cat /opt/ebidoebi/.env"
ssh arkentiy "cat /opt/ebidoebi/app/main.py"
```

Спроси себя:
- Файл уже есть на VPS? Чем отличается от локального?
- Можно добавить только новые строки, не трогая старые?
- Локальная версия ≠ VPS-версия. **Всегда.**

## ШАГ 2 — Бэкап каждого файла который будет изменён

```bash
# Бэкап
ssh arkentiy \
  "cp /opt/ebidoebi/app/X.py /opt/ebidoebi/app/X.py.bak.$(date +%Y%m%d_%H%M%S)"

# Оставить только две последние версии
ssh arkentiy \
  "ls -t /opt/ebidoebi/app/X.py.bak.* 2>/dev/null | tail -n +3 | xargs rm -f"
```

**Сначала бэкап → потом scp.** Не наоборот.

**Запиши в `docs/Журнал.md`:**
```
### YYYY-MM-DD — бэкап перед изменением
- `app/jobs/xxx.py` → что меняется и зачем
```

## ШАГ 3 — Вшивка, а не замена

| Файл | Как правильно |
|------|--------------|
| Новый `.py` модуль | Просто копируй — его раньше не было |
| `main.py` | Прочитай VPS-версию → добавь только новые строки |
| `config.py` | Прочитай VPS-версию → добавь только новые поля |
| `.env` | Дописывай: `cat >> .env`, не перезаписывай целиком |
| `database.py` | Прочитай VPS-версию → добавь только новые таблицы/функции |

```bash
# Правильно: дописать в конец .env
ssh ... "cat >> /opt/ebidoebi/.env << 'EOF'
НОВАЯ_ПЕРЕМЕННАЯ=значение
EOF"

# Правильно: сравнить перед заливкой
ssh ... "cat /opt/ebidoebi/app/main.py" > /tmp/vps_main.py
diff /tmp/vps_main.py локальный_main.py
```

## ШАГ 4 — Проверка после деплоя

```bash
sleep 10
ssh ... "cd /opt/ebidoebi && docker compose ps"                    # healthy?
ssh ... "cd /opt/ebidoebi && docker compose logs app --tail=20"    # ERROR/ImportError?
```

Если `Restarting` — немедленный откат:
```bash
ssh ... "cp /opt/ebidoebi/app/X.py.bak.* /opt/ebidoebi/app/X.py"
ssh ... "cd /opt/ebidoebi && docker compose up -d --build"
```

## Запрещено без явной команды

- Заливать файл на VPS без бэкапа
- Копировать локальный файл целиком поверх VPS без сравнения
- Перезаписывать `.env` целиком
- Считать что локальный файл = VPS-файл
- Деплоить и уходить — всегда жди `healthy` и проверяй логи

## Команды деплоя

```bash
# SCP файла на VPS
scp -i ~/.ssh/cursor_arkentiy_vps app/jobs/new_module.py arkentiy:/opt/ebidoebi/app/jobs/

# Build и запуск
ssh arkentiy \
  "cd /opt/ebidoebi && docker compose build --no-cache && docker compose up -d"

# ВАЖНО: docker compose restart НЕ применяет изменения кода, только .env
```

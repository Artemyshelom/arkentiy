# ТЗ: Автоматическая аватарка чата при регистрации

**Модуль:** `access_manager.py`

---

## Задача

При регистрации чата через `/доступ` автоматически устанавливать аватарку бота в этом чате исходя из названия группы.

---

## Маппинг название → аватарка

| Паттерн в названии | Файл |
|-------------------|------|
| `Поиск` | `assets/avatars/search.jpg` |
| `Опозд` | `assets/avatars/late.jpg` |
| `Отчёт` / `Отчет` | `assets/avatars/reports.jpg` |
| `Финанс` | `assets/avatars/finance.jpg` |
| `Маркетинг` | `assets/avatars/marketing.jpg` |

Паттерн: `re.search(pattern, chat_title, re.IGNORECASE)`

**Без fallback** — если паттерн не совпал, аватарку не меняем.

---

## Логика

```python
from pathlib import Path
from aiogram.types import FSInputFile

AVATAR_MAP = [
    (r'поиск', 'search.jpg'),
    (r'опозд', 'late.jpg'),
    (r'отч[её]т', 'reports.jpg'),
    (r'финанс', 'finance.jpg'),
    (r'маркетинг', 'marketing.jpg'),
]

async def set_chat_avatar(bot: Bot, chat_id: int, chat_title: str):
    """Установить аватарку бота в чате по названию"""
    
    for pattern, filename in AVATAR_MAP:
        if re.search(pattern, chat_title, re.IGNORECASE):
            avatar_path = Path('assets/avatars') / filename
            if avatar_path.exists():
                try:
                    await bot.set_chat_photo(
                        chat_id=chat_id,
                        photo=FSInputFile(avatar_path)
                    )
                    logger.info(f"Avatar set for chat {chat_id}: {filename}")
                except TelegramBadRequest as e:
                    if "not enough rights" in str(e).lower():
                        logger.warning(f"No rights to set avatar in {chat_id}")
                    else:
                        logger.error(f"Failed to set avatar: {e}")
            break
```

---

## Точка встраивания

`app/jobs/access_manager.py` — после успешной регистрации чата:

```python
# После добавления чата в БД
await set_chat_avatar(bot, chat_id, chat_title)
```

---

## Файлы (уже в репо)

```
assets/avatars/
├── search.jpg      # 🔍 Поиск
├── late.jpg        # ❗ Опоздания  
├── reports.jpg     # 📊 Отчёты
├── finance.jpg     # 💰 Финансы
└── marketing.jpg   # ⭐ Маркетинг
```

---

## Ограничения

- Бот должен быть **админом** с правом `can_change_info`
- Если прав нет → warning в лог, не падаем

---

## Тесты

1. Создать группу "Поиск заказов Барнаул" → зарегистрировать → аватарка search.jpg
2. Создать группу "Опоздания Томск" → аватарка late.jpg
3. Создать группу "Тест" (без паттерна) → аватарка не меняется
4. Бот без прав админа → warning в логах, не падает

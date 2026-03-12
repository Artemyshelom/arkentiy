# RAG-поиск кодовой базы Аркентия 🔍

**Дата реализации:** 11 марта 2026 | **Статус:** ✅ Production Ready

Здесь вся документация по семантическому поиску по коду и документации.

---

## 📖 Документация

| Файл | Для кого | О чём |
|------|----------|-------|
| **[QUICK_START.md](QUICK_START.md)** | Пользователи (Мёрф, Станислав) | Как использовать поиск, примеры, FAQ |
| **[IMPLEMENTATION.md](IMPLEMENTATION.md)** | Разработчики | Полная инструкция, диагностика, техдетали |
| **[FINAL_REPORT.md](FINAL_REPORT.md)** | Менеджеры, заинтересованные | Результаты, статистика, планы |

---

## 🚀 Быстрый старт

### Поиск через curl
```bash
curl "http://localhost:8000/api/codesearch?q=cancel_sync&limit=3" \
  -H "Authorization: Bearer <ключ>"
```

### Поиск через Python
```python
import requests
r = requests.get(
    "http://localhost:8000/api/codesearch",
    params={"q": "отправить алерт", "limit": 3},
    headers={"Authorization": "Bearer <ключ>"}
)
for res in r.json()["results"]:
    print(f"{res['score']:.2f} {res['file']}")
```

---

## 📊 Что индексировано

```
✅ 114 файлов (75 Python + 39 Markdown)
✅ 1748 чанков кода и документации
✅ HNSW индекс в PostgreSQL
✅ Поиск <100мс
```

---

## 👥 Доступ

| Пользователь | Ключ | Модули |
|---------------|------|--------|
| мёрф_agent | `9316e4336ef197bc30d11fd8a9dbe73c0fa4372281ed826d` | codesearch |
| stanislaw_agent | `4ecbfe65d40f4519aca53014675afcc700687b6be34ece7d` | codesearch |
| admin | `$ADMIN_API_KEY` из .env | все |

---

## 🔗Связанные файлы в проекте

- `app/tools/reindex_code.py` — индексатор
- `app/routers/codesearch.py` — HTTP endpoint
- `app/migrations/013_code_chunks.sql` — таблица БД
- `docs/journal.md` — история реализации

---

**Вопросы?** → [QUICK_START.md](QUICK_START.md) (FAQ) или [IMPLEMENTATION.md](IMPLEMENTATION.md) (диагностика)

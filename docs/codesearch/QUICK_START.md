# Быстрый старт — RAG-поиск кодовой базы

**Статус:** ✅ Работает | **Доступ:** Мёрф, Станислав, админы

---

## Что это?

Поищешь "как сделать алерт про опоздание" → выдаст куски кода и документации, где это уже сделано. Как Google, но для вашего кода.

---

## Как использовать

### Шаг 1: Найди свой API ключ
```bash
# В secrets/api_keys.json:
мёрф_agent:      9316e4336ef197bc30d11fd8a9dbe73c0fa4372281ed826d
stanislaw_agent: 4ecbfe65d40f4519aca53014675afcc700687b6be34ece7d

# Или используй ADMIN_API_KEY из .env
```

### Шаг 2: Выполни поиск

**Через curl:**
```bash
curl "http://localhost:8000/api/codesearch?q=cancel_sync&limit=3" \
  -H "Authorization: Bearer <твой_ключ>"
```

**Через Python:**
```python
import requests

response = requests.get(
    "http://localhost:8000/api/codesearch",
    params={
        "q": "отправить алерт про опоздание",
        "limit": 5,
        "type": "py",      # только Python (опция)
        "module": "jobs"   # только jobs модуль (опция)
    },
    headers={"Authorization": "Bearer <твой_ключ>"}
)

# Вывод результатов
for result in response.json()["results"]:
    print(f"{result['score']:.2f} {result['file']}")
    print(result['content'][:200] + "...\n")
```

### Шаг 3: Результат
```json
{
  "query": "отправить алерт",
  "count": 3,
  "results": [
    {
      "file": "app/jobs/late_alerts.py",
      "type": "py",
      "module": "jobs",
      "score": 0.655,
      "content": "async def send_alert(...): ..."
    },
    ...
  ]
}
```

---

## Параметры поиска

| Параметр | Обязательно | Значения | Пример |
|----------|-----------|----------|---------|
| `q` | ✅ Да | Любой текст | `q=cancel_sync` |
| `limit` | ❌ Нет (default: 5) | 1-10 | `limit=3` |
| `type` | ❌ Нет | `py` или `md` | `type=py` |
| `module` | ❌ Нет | `jobs`, `routers`, `services`, `clients` | `module=jobs` |

---

## Примеры поисков

```bash
# Базовый поиск
curl "http://localhost:8000/api/codesearch?q=late_alerts&limit=5"

# Только документация (markdown)
curl "http://localhost:8000/api/codesearch?q=как подключить клиента&type=md&limit=3"

# Только из jobs модуля
curl "http://localhost:8000/api/codesearch?q=send_alert&module=jobs"

# Комбинированно
curl "http://localhost:8000/api/codesearch?q=cancel%20sync&type=py&module=jobs&limit=2"
```

---

## Что индексировано?

```
✅ 114 файлов
   ├─ 75 Python файлов (app/)
   └─ 39 Markdown файлов (docs/)

✅ 1748 чанков (смысловых блоков)
   ├─ 838 Python чанков
   └─ 910 Markdown чанков

✅ Модули:
   ├─ jobs       (14 job'ов)
   ├─ routers    (10 endpoint'ов)
   ├─ services   (8 сервисов)
   ├─ clients    (6 интеграций)
   └─ ...и другие
```

---

## Как это работает?

```
Твой запрос "cancel_sync отмена"
         ↓
    Jina AI (768-мерный вектор)
    [через SOCKS5 Frankfurt для обхода блокировки]
         ↓
    PostgreSQL HNSW индекс
    [поиск по косинусному расстоянию]
         ↓
    Top-5 результатов (0-1 score)
```

**Ключевые свойства:**
- 🧠 **По смыслу:** ищет похожие блоки, не по попаданию слов
- 🌍 **Мультиязычно:** русский + английский одинаково
- ⚡ **Быстро:** <100мс благодаря HNSW индексу
- 🔒 **Безопасно:** Bearer token auth, как GitHub

---

## Типичные ошибки

| Ошибка | Решение |
|--------|---------|
| `"detail":"Требуется авторизация"` | Добавь заголовок: `-H "Authorization: Bearer KEY"` |
| `"detail":"Неверный ключ"` | Проверь ключ в `secrets/api_keys.json` |
| `Internal Server Error (500)` | Контейнер упал: `docker logs ebidoebi-integrations` |
| `Empty response` | Проверь контейнер: `docker ps \| grep ebidoebi` |
| `Очень медленный (>2 сек)` | Нормально при первом запросе (загружается Jina модель) |

---

## FAQ

**Q: Как добавить новый файл в индекс?**
A: Добавь в `app/`, `docs/` или `rules/`, закоммитай в git, и при следующем деплое переиндексируется автоматически.

**Q: Почему не находит нужное?**
A: Поиск по смыслу. Попробуй переформулировать запрос.

**Q: Как часто обновляется?**
A: При каждом деплое (git push → Docker build → переиндексация).

**Q: Сколько это стоит?**
A: Батчинг экономит. Индексация 114 файлов ≈ $0.05, поиск ≈ <$0.001/запрос.

**Q: Безопасно ли?**
A: Да, Bearer token auth. Ключи можно генерить и отзывать как в GitHub.

---

## Подробнее

👉 **Техдетали?** → [IMPLEMENTATION.md](IMPLEMENTATION.md)
👉 **Результаты и статистика?** → [FINAL_REPORT.md](FINAL_REPORT.md)
👉 **Главная страница?** → [README.md](README.md)

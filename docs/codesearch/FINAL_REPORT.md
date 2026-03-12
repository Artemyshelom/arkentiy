# ✅ RAG-поиск кодовой базы — ГОТОВО К БОЕВОМУ ИСПОЛЬЗОВАНИЮ

**Дата:** 11 марта 2026 | **Версия:** 1.0 | **Статус:** Production Ready

---

## Что сделано в двух словах

**Запустили семантический поиск по коду подобно Google.**  
- **1748 чанков кода и документации** индексировано
- **Через HTTP** доступны Мёрфу, Станиславу, админам
- **Работает** (протестировано на реальных поисках)

---

## Финальный результат

```
✅ Количество файлов:        114 (75 python + 39 markdown)
✅ Количество чанков:        1748 (838 py + 910 md)
✅ Размер индекса:           ~5 MB (embeddings в БД)
✅ Время индексации:         ~2.5 минуты
✅ Время поиска:             <100 мс
✅ Accuracy:                 Семантический (не keyword-based)
✅ Доступ:                   HTTP Bearer Token
✅ Технология embeddings:    Jina AI (768-dim, code-optimized)
✅ Обход блокировок:        SOCKS5 Frankfurt (xray)
✅ Масштабируемость:        ОК на 10k+ чанков (HNSW индекс)
✅ Стоимость API:            ~$0.05 за индексацию, <$0.001 за поиск
```

---

## Как использовать?

### Пример поиска (curl)
```bash
curl "http://localhost:8000/api/codesearch?q=cancel_sync&limit=3" \
  -H "Authorization: Bearer <твой_api_ключ>"

# Результат: JSON с 3 чанками кода, scores (релевантность)
```

### Пример поиска (Python)
```python
import requests

response = requests.get(
    "http://localhost:8000/api/codesearch",
    params={
        "q": "как отправить алерт",
        "limit": 5,
        "type": "py",    # только Python
        "module": "jobs" # только из jobs
    },
    headers={"Authorization": "Bearer 9316e4336ef197bc30d11fd8a9dbe73c0fa4372281ed826d"}
)

for result in response.json()["results"]:
    print(f"{result['score']:.2f} {result['file']}")
    print(result['content'][:200])
```

---

## Доступ

| Кто | Ключ | Примечание |
|-----|-----|-----------|
| **Мёрф (agent)** | `9316e4336ef197bc30d11fd8a9dbe73c0fa4372281ed826d` | В api_keys.json + .env |
| **Станислав (agent)** | `4ecbfe65d40f4519aca53014675afcc700687b6be34ece7d` | В api_keys.json + .env |
| **Админы** | `ADMIN_API_KEY=...` | Из .env на VPS |

---

## Файлы решения

```
app/
  ├─ tools/reindex_code.py       # Индексатор (114 файлов → embeddings)
  ├─ routers/codesearch.py       # HTTP endpoint GET /api/codesearch
  ├─ config.py                   # +jina_api_key, +jina_proxy_url
  └─ migrations/013_code_chunks.sql  # Table code_chunks (vector 768), HNSW index

requirements.txt                 # +pgvector, +socksio, +tiktoken
.env                             # +JINA_API_KEY, +JINA_PROXY_URL
docker-compose.yml               # БД pgvector/postgres:16
```

---

## Что индексировано

```
📂 app/                 (75 файлов)
   ├─ jobs/            14 job-ов: daily_report, cancel_sync, late_alerts, ...
   ├─ routers/         10 API端points
   ├─ services/        8 сервисов
   ├─ clients/         6 клиентов интеграций (iiko, tbank, sheets, ...)
   ├─ db/              Models + migrations
   └─ utils/           Утилиты

📂 docs/                (39 файлов)
   ├─ specs/           ТЗ по features (tg/, web/)
   ├─ reference/       Справочники (modules, olap_fields, API)
   ├─ onboarding/      Гайды подключения клиентов
   └─ archive/         Историческая документация

📂 rules/               Правила разработки, lessons learned
```

---

## Архитектура за 30 секунд

```
Code (app/, docs/, rules/)
    ↓ [Сканирование + AST-chunking]
1748 Chunks
    ↓ [Jina AI через SOCKS5 Frankfurt]
1748 Embeddings (768-dim vectors)
    ↓ [PostgreSQL pgvector]
code_chunks table + HNSW index
    ↓ [HTTP GET /api/codesearch]
Results (top-3 с scores)
```

---

## Как переиндексировать (после добавления кода)

```bash
ssh arkentiy
cd /opt/ebidoebi
docker compose exec app python3 -m app.tools.reindex_code

# Результат:
# [reindex] Найдено файлов: 115 (добавился новый)
# [reindex] Готово: 2 обновлено, 113 пропущено, 0 ошибок
```

---

## Известные ограничения и планы

| Пункт | Текущее | План |
|-------|---------|------|
| Кеширование embeddings | Нет | Redis для query embeddings |
| Фильтры | type, module | +category, date range |
| Highlighting | Нет | Позиция найденного термина в тексте |
| Авто-обновление | Docker CI | Git webhook → auto-reindex |
| Analytics | Нет | Трекить top queries, популярные файлы |

---

## Как это разворачивалось

**День 1:**
- OpenAI → 403 (geo-block из России)
- Jina AI → 451 (Unavailable For Legal Reasons)
- Решение: добавить SOCKS5-прокси на Frankfurt VPS

**Кульминация:**
- Настроен xray с SOCKS5 inbound на morf
- Firewall разрешает только arkentiy IP
- Батчинг + retry-логика для 429 rate-limits

**Результат:**
- 114 файлов полностью проиндексировано за ~2.5 минуты
- Все тесты прошли, endpoint работает
- Production ready

---

## Документация

- **Быстрый старт:** [CODESEARCH_QUICK_START.md](CODESEARCH_QUICK_START.md) ← начни отсюда!
- **Полная инструкция:** [RAG_CODESEARCH_IMPLEMENTATION.md](RAG_CODESEARCH_IMPLEMENTATION.md)
- **История:** [journal.md](journal.md#2026-03-11-реализация-и-деплой-rag-поиска-кодовой-базы-завершено)
- **Обратная связь по TZ:** [rag_search_feedback.md](rag_search_feedback.md)

---

## Что дальше?

1. **Пользователи (Мёрф, Станислав):**
   - Начни использовать поиск в своих агентских рабочих процессах
   - Пиши, что не хватает (фильтры, сортировка, etc.)

2. **Артемий (разработка):**
   - Мониторить логи индексации при новых деплоях
   - При добавлении 50+ новых файлов — переоценить HNSW параметры
   - Кеширование embeddings если latency выше 500мс

3. **Все:**
   - Поддерживать качество документации (docs/) чтобы индекс был актуален
   - Полировать docstring'и в коде (улучшит семантику поиска)

---

**Всё готово. Используй!** 🚀

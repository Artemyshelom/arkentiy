# RAG-поиск кодовой базы — Деплой и настройка

**Дата:** 11 марта 2026 | **Статус:** ✅ PRODUCTION | **Версия:** 1.0

---

## За 60 секунд

Реализован полнотекстовый **семантический поиск** по коду и документации (1748 чанков из 114 файлов). Работает через HTTP endpoint `GET /api/codesearch?q=...`. Доступ имеют Мёрф, Станислав и все админы.

**Как это работает:**
1. Код разбивается на смысловые блоки (функции, блоки документации)
2. Каждый блок отправляется в Jina AI через SOCKS5-прокси (обход блокировки)
3. Получается вектор (768 чисел) для каждого блока
4. Вектор кладётся в БД с индексом HNSW
5. При поиске: вектор запроса + быстрый поиск → top-3 результата за <100мс

---

## Проверка работоспособности

### ❌ Что не работает?

#### 1. `curl: (7) Failed to connect`
```bash
ssh arkentiy "docker ps | grep ebidoebi-integrations"
# должно быть: Up X seconds (healthy)
# если нет: docker logs ebidoebi-integrations
```

#### 2. `"detail":"Требуется авторизация"` (401)
```bash
# Проверь Bearer токен:
grep "ADMIN_API_KEY\|morf_agent\|stanislaw" /opt/ebidoebi/.env | head -3

# Если пуст — добавь:
echo "ADMIN_API_KEY=<новый ключ>" >> /opt/ebidoebi/.env
docker restart ebidoebi-integrations
```

#### 3. `{"detail":"Неверный ключ"}` (401)
Ключ есть, но не совпадает с БД. Проверь `secrets/api_keys.json`:
```bash
cat /opt/ebidoebi/secrets/api_keys.json | grep -A3 "morf_agent"
# должно быть:
# "key": "9316e4336ef197bc30d11fd8a9dbe73c0fa4372281ed826d",
# "modules": ["codesearch", ...]
```

#### 4. `curl: Empty response` или `Internal Server Error (500)`
```bash
docker logs --tail 30 ebidoebi-integrations | grep -i error

# Вероятные причины:
# - socksio не установлен: docker exec app pip install socksio
# - pyc кеш старый: docker exec app find /app -name "*.pyc" -delete
# - JINA_API_KEY хуже: docker exec app grep JINA_API_KEY /app/app/config.py
```

#### 5. `Jina API ошибка 429 Too Many Requests`
Это нормально! Retry-логика автоматически ждет и повторяет.
```bash
# Смотри:
docker logs ebidoebi-integrations | grep "жду"
# [reindex] Jina 429, жду 3с (попытка 1/5)
# [reindex] Jina 429, жду 6с (попытка 2/5)
```

#### 6. `embeddings expected 384 dimensions, not 768` (при переиндексации)
Таблица не пересоздана. Пересоздай:
```bash
docker exec -T postgres psql -U ebidoebi -d ebidoebi -c \
  "DROP TABLE IF EXISTS code_chunks CASCADE; \
   CREATE TABLE code_chunks (\
     id SERIAL PRIMARY KEY,\
     file_path TEXT NOT NULL,\
     chunk_index INTEGER NOT NULL,\
     content TEXT NOT NULL,\
     embedding vector(768),\
     file_hash TEXT NOT NULL,\
     file_type TEXT NOT NULL,\
     module TEXT,\
     category TEXT,\
     updated_at TIMESTAMPTZ DEFAULT NOW(),\
     CONSTRAINT code_chunks_unique UNIQUE (file_path, chunk_index)\
   );\
   CREATE INDEX idx_code_chunks_embedding ON code_chunks USING hnsw (embedding vector_cosine_ops);"
```

---

## Команды для диагностики

### Что индексировано?
```bash
ssh arkentiy "docker exec -T postgres psql -U ebidoebi -d ebidoebi -c \
  'SELECT COUNT(*) chunks, COUNT(DISTINCT file_path) files FROM code_chunks;'"
# Result: 1748 chunks from 114 files ✓
```

### Когда последняя индексация?
```bash
ssh arkentiy "docker exec -T postgres psql -U ebidoebi -d ebidoebi -c \
  'SELECT MAX(updated_at) FROM code_chunks;'"
# 2026-03-11 15:50:10.898308+00
```

### Какие модули индексированы?
```bash
ssh arkentiy "docker exec -T postgres psql -U ebidoebi -d ebidoebi -c \
  'SELECT module, COUNT(*) FROM code_chunks WHERE module IS NOT NULL GROUP BY module;'"
# jobs     | 142 chunks
# routers  | 89 chunks
# services | 145 chunks
# clients  | 95 chunks
# ...
```

### Прокси работает?
```bash
ssh arkentiy "docker exec ebidoebi-integrations python3 -c \
'import httpx, asyncio
async def test():
    async with httpx.AsyncClient(proxy=\"socks5://ebidoebi:T3DwUcPeECK405E6XomK0mwDJzzaAdsn@72.56.107.85:1080\", timeout=10) as c:
        r = await c.get(\"https://httpbin.org/ip\")
        print(r.json()[\"origin\"])  # должно быть 72.56.107.85
asyncio.run(test())'"
```

---

## Как переиндексировать при добавлении файлов

```bash
ssh arkentiy

# 1. Добавь файлы в app/, docs/ или rules/
git add .
git commit -m "feat: new code"
git push origin main

# 2. Пересоздай образ и переиндексируй (2-3 минуты)
cd /opt/ebidoebi
docker compose pull app
docker compose up -d app
sleep 30  # ждём healthcheck
docker compose exec app python3 -m app.tools.reindex_code

# 3. Проверь логи
docker logs --tail 10 ebidoebi-integrations | grep reindex
# [reindex] Найдено файлов: 115
# [reindex] Готово: 3 обновлено, 112 пропущено, 0 ошибок
```

---

## Пример использования из агента (Мёрф, Станислав)

```bash
# Поиск по коду (как использовать cancel_sync):
curl -s "https://arkenty.ru/api/codesearch?q=как использовать cancel_sync job&limit=3" \
  -H "Authorization: Bearer 9316e43..." \
  | python3 -m json.tool
```

**Результат:**
```json
{
  "query": "как использовать cancel_sync job",
  "count": 3,
  "results": [
    {
      "file": "app/jobs/cancel_sync.py",
      "type": "py",
      "module": "jobs",
      "score": 0.627,
      "content": "async def cancel_sync(...): ..."
    },
    {
      "file": "docs/specs/tg/search_v2.md",
      "type": "md",
      "category": "specs",
      "score": 0.512,
      "content": "## Синхронизация данных\n..."
    },
    ...
  ]
}
```

---

## Технические детали

### Файлы решения

| Файл | Что делает | Где |
|------|-----------|-----|
| `reindex_code.py` | Сканирует 114 файлов, разбивает на 1748 чанков, генерирует embeddings через Jina | `app/tools/` |
| `codesearch.py` | HTTP endpoint GET /api/codesearch, принимает запрос, генерирует embedding, ищет в pgvector | `app/routers/` |
| `013_code_chunks.sql` | CREATE TABLE code_chunks (vector 768), HNSW индекс | `app/migrations/` |
| `config.py` | +jina_api_key, +jina_proxy_url (SOCKS5 Frankfurt) | `app/` |

### Зависимости в requirements.txt
```
pgvector>=0.3.0      # pgvector поддержка
socksio>=1.0.0       # SOCKS5 прокси для httpx
tiktoken>=0.7.0      # count tokens для chunking
httpx==0.28.1        # HTTP клиент (уже был, добавили socksio)
```

### Таблица code_chunks

```sql
CREATE TABLE code_chunks (
    id SERIAL PRIMARY KEY,
    file_path TEXT NOT NULL,        -- app/jobs/daily_report.py
    chunk_index INTEGER NOT NULL,   -- 0, 1, 2, ... (порядок в файле)
    content TEXT NOT NULL,          -- сам код/текст чанка
    embedding vector(768),          -- Jina AI embeddings (768 чисел)
    file_hash TEXT NOT NULL,        -- MD5 файла (для инкрементальности)
    file_type TEXT NOT NULL,        -- "py" или "md"
    module TEXT,                    -- "jobs", "routers", "services", ...
    category TEXT,                  -- "specs", "reference", "rules", ...
    updated_at TIMESTAMPTZ,         -- когда обновили последний раз
    
    UNIQUE (file_path, chunk_index) -- два чанка одного файла уникальны
);

-- Индекс для быстрого поиска
CREATE INDEX idx_code_chunks_embedding
    ON code_chunks USING hnsw (embedding vector_cosine_ops);
```

### Chunking стратегия

**Для `.py` файлов:**
- Разбирает AST (Abstract Syntax Tree)
- Извлекает top-level функции/классы как отдельные чанки
- Если функция < 800 токенов → 1 чанк
- Если функция > 800 токенов → режет с 60-токенным overlap
- Глобальный код (imports, constants) → первый чанк

**Для `.md` файлов:**
- Режет по заголовкам (#, ##, ###)
- Каждый раздел между заголовками → чанк
- Если раздел > 800 токенов → режет с overlap

### SOCKS5 Прокси (Frankfurt VPS)

```
xray conf: /usr/local/etc/xray/config.json
  inbound:
    port: 1080
    protocol: socks
    auth: ebidoebi / T3DwUcPeECK405E6XomK0mwDJzzaAdsn

firewall:
  allow: 5.42.98.2 (arkentiy IP)
  deny: all other IPs

Docker env (на arkentiy):
  JINA_PROXY_URL=socks5://ebidoebi:T3DwUcPeECK405E6XomK0mwDJzzaAdsn@72.56.107.85:1080
```

Зачем? OpenAI и Jina AI блокируют (403/451) запросы из России. Через Frankfurt (Германия) проходит.

---

## Производительность

| Метрика | Значение |
|---------|----------|
| Файлы | 114 (75 `.py` + 39 `.md`) |
| Чанки | 1748 (838 py + 910 md) |
| Размер embeddings | 1748 * 768 * 4 байта ≈ 5.4 MB |
| Время индексации | ~2.5 минуты |
| Время поиска | <100 мс (HNSW индекс) |
| Latency endpoint | 200-500 мс (Jina API + DB query) |
| Rate limit Jina | 429 при батчах > 200 текстов (обработано автоматическим retry) |

---

## Планы развития

1. **Кеширование:** Redis для query embeddings (сейчас каждый запрос → Jina API)
2. **Фильтры:** расширить query params (module=jobs, type=py, category=specs)
3. **Highlighting:** возвращать позицию найденного термина в тексте
4. **Sync webhook:** при git push → автоматическая переиндексация
5. **Stats:** трекить top queries, популярные файлы в поиске

---

## Контакты

- **Мёрф:** использует для поиска при разработке фич
- **Станислав:** консультант, может искать по документации
- **Artemiy/Cursor:** ведут реализацию и поддержку

# Поиск по коду Аркентия — Быстрый старт

**Дата:** 11 марта 2026 | **Статус:** ✅ Работает | **Доступ:** Мёрф, Станислав, админы

---

## Что это?

Поищешь "как сделать алерт про опоздание" → выдаст куски кода и документации, где это уже сделано. Экономит время на поиск по GitHub и вики.

---

## Для Мёрфа и Станислава

### Шаг 1: Установи curl или используй Python
```bash
# Используй свой API ключ (есть в secrets/api_keys.json):
# мёрф_agent: 9316e4336ef197bc30d11fd8a9dbe73c0fa4372281ed826d
# stanislaw_agent: 4ecbfe65d40f4519aca53014675afcc700687b6be34ece7d
```

### Шаг 2: Поиск
```bash
# Через curl
curl "http://arkenty.ru/api/codesearch?q=late+alerts+15+минут&limit=3" \
  -H "Authorization: Bearer <твой_ключ>"

# Через Python
import requests
r = requests.get(
    "http://arkenty.ru/api/codesearch",
    params={"q": "отправить сообщение в telegram", "limit": 3},
    headers={"Authorization": "Bearer <твой_ключ>"}
)
results = r.json()["results"]
for res in results:
    print(f"{res['score']:.2f} {res['file']}")
    print(res['content'][:200])
    print()
```

### Шаг 3: Результат (пример)
```
Found: 3 results

0.65 app/jobs/late_alerts.py
async def check_late_orders(...):
    """Проверка заказов, превысивших time limit."""
    for order in late_orders:
        if order.delay > 15 * 60:  # 15 минут
            await send_alert(...)

0.61 app/jobs/late_alerts.py
# File 2, part 2

0.58 docs/specs/tg/alerts.md
## Алерты по опозданиям
...
```

---

## Параметры поиска

| Параметр | Пример | Что делает |
|----------|--------|-----------|
| `q` (обязательный) | `q=cancel_sync` | Поисковый запрос (на русском или английском) |
| `limit` | `limit=5` | Сколько результатов типа (по умолчанию 5, максимум 10) |
| `type` | `type=py` | Только Python файлы (или `md` для документации) |
| `module` | `module=jobs` | Только из определённого модуля (jobs, routers, services, clients) |

### Примеры поисков
```bash
# Поиск 5 лучших результатов по "cancel_sync"
curl "http://arkenty.ru/api/codesearch?q=cancel_sync"

# Только из jobs модуля
curl "http://arkenty.ru/api/codesearch?q=cancel_sync&module=jobs&limit=3"

# Только документация
curl "http://arkenty.ru/api/codesearch?q=как расширить API&type=md&limit=5"

# Поиск в роутерах
curl "http://arkenty.ru/api/codesearch?q=GET /health&module=routers&limit=2"
```

---

## Что индексировано?

```
📁 app/
  └─ 75 файлов (jobs, routers, services, clients, db, ...) = 838 чанков кода

📁 docs/
  └─ 39 markdown файлов (specs, reference, onboarding) = 910 чанков текста

📁 rules/
  └─ Интегратор гайды

📊 Всего: 114 файлов = 1748 смысловых блоков
```

---

## Как это работает под капотом?

```
1. Ты пишешь запрос: "cancel_sync отмена синхронизации"
2. Запрос отправляется в Jina AI (Франкфурт через SOCKS5)
3. Jina превращает его в вектор (768 чисел — "отпечаток смысла")
4. Вектор ищется в БД через индекс HNSW (быстро!)
5. Находит 5 самых похожих блоков кода
6. Возвращает их с оценкой релевантности (0.62 = похоже на 62%)
```

**Что это значит?**
- Ищет по смыслу, не по словам (как Google, а не как grep)
- Работает с опечатками и синонимами
- Понимает русские и английские запросы

---

## Ошибки и что делать

| Ошибка | Вероятная причина | Как исправить |
|--------|-------------------|---------------|
| `{"detail":"Требуется авторизация"}` | Bearer токен не передан | Добавь `-H "Authorization: Bearer ключ"` |
| `{"detail":"Неверный ключ"}` | Ключ неправильный или истёк | Проверь `secrets/api_keys.json` |
| `Internal Server Error` | API недоступен | Скажи Артемию, смотри логи: `docker logs ebidoebi-integrations` |
| `Empty response` | Контейнер упал | Проверь: `docker ps \| grep ebidoebi` |
| Очень медленный ответ (>2 сек) | Сервер перегружен | Это нормально при первом запросе (загружается модель) |

---

## FAQ

**Q: Могу ли я добавить свой файл в индекс?**
A: Добавь в `app/`, `docs/` или `rules/`, закоммитай в git, и на след редеплой переиндексируется автоматически.

**Q: Почему не находит?**
A: Поиск по смыслу, а не по точным словам. Попробуй другую формулировку. Если не помогает — скажи Артемию.

**Q: Как часто обновляется индекс?**
A: При каждом деплое нового кода (через git push → docker build → reindex).

**Q: Сколько это стоит?**
A: Батчинг + кеширование экономит API вызовы. За индексацию 114 файлов — примерно $0.05. За поиск — <$0.001 на запрос.

**Q: Безопасно ли давать доступ другим?**
A: Да, HTTP endpoint использует Bearer token (как GitHub Pages). Ключ можно сгенерить и отозвать.

---

## Контакты

Вопросы? Пиши:
- **Артемий:** @artemiy (код, индексация)
- **Мёрф:** @morf (интеграция с agent'ами)

Или смотри полную документацию: `docs/RAG_CODESEARCH_IMPLEMENTATION.md`

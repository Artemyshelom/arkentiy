"""
marketing_export.py — команда /выгрузка для маркетинга.

Поток:
  1. /выгрузка <запрос на русском>
  2. OpenRouter (Gemini 2.5 Flash) → структурированные JSON-параметры
  3. SQL-запрос к orders_raw (параметры → WHERE, CTEs для new/old + total_orders)
  4. CSV-файл → Telegram sendDocument

Определение «новый клиент»:
  Дата первого заказа клиента в нашей БД == дата заказа из запроса → Новый
  Иначе → Старый

Порог опоздания по умолчанию: 5 минут.
"""

import csv
import io
import json
import logging
import re
from datetime import date, datetime
from typing import Optional

import aiosqlite
import httpx

from app.config import get_settings
from app.database import DB_PATH

logger = logging.getLogger(__name__)
settings = get_settings()

DEFAULT_LATE_MINUTES = 5  # базовый порог опоздания (мин)

# ---------------------------------------------------------------------------
# OpenRouter / LLM
# ---------------------------------------------------------------------------

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

_SYSTEM_PROMPT = """Ты помощник, который извлекает параметры фильтрации из запроса на русском языке.

Контекст: система учёта заказов доставки суши «Ёбидоёби» (4 города: Барнаул, Абакан, Томск, Черногорск).
В базе хранятся заказы с полями: клиент (имя, телефон), адрес доставки, дата заказа,
сумма заказа, состав блюд, опоздание (есть/нет, минут).

Верни ТОЛЬКО валидный JSON (без markdown-блоков, без пояснений) с полями:
{{
  "customer_type": "new" | "old" | null,
  "date": "YYYY-MM-DD" | null,
  "date_from": "YYYY-MM-DD" | null,
  "date_to": "YYYY-MM-DD" | null,
  "only_late": true | false | null,
  "min_late_minutes": <число> | null,
  "min_order_sum": <число> | null,
  "max_order_sum": <число> | null,
  "items_contains": ["название блюда", ...] | null,
  "branch": "<точное название филиала из таблицы ниже>" | null,
  "city": "Барнаул" | "Абакан" | "Томск" | "Черногорск" | null
}}

Таблица филиалов — используй ТОЧНОЕ название из колонки «branch_name»:
{branch_table}

Правила:
- "новый клиент" / "новые" → customer_type: "new"; "старый" / "постоянный" → customer_type: "old"
- Дата "14.02" без года → текущий год. "вчера" → вчера. "сегодня" → сегодня.
- "опоздание >15 минут" → only_late: true, min_late_minutes: 15
- "опоздание >5 минут" или просто "опоздание" (без числа) → only_late: true, min_late_minutes: null
  (базовый порог 5 мин применяется автоматически на стороне сервера)
- "без опоздания" / "вовремя" → only_late: false
- Блюда: список если упомянуты конкретные позиции, иначе null
- Если назван конкретный филиал → заполни branch (точное название) и оставь city: null
- Если назван только город → заполни city, оставь branch: null
- null = нет фильтра по этому полю

Текущая дата: {today}"""


def _build_branch_table(branches: list[dict]) -> str:
    """Строит таблицу филиалов с алиасами для системного промпта Gemini."""
    lines = ["branch_name         | город      | алиасы (любой из них → этот филиал)",
             "--------------------|------------|-------------------------------------"]
    for b in branches:
        name = b["name"]          # "Томск_1 Яко"
        city = b.get("city", "")
        # Алиасы: числовые варианты + короткое имя
        parts = name.split("_")   # ["Томск", "1 Яко"]
        if len(parts) == 2:
            num_part = parts[1].split()[0]   # "1"
            short    = parts[1].split()[1] if len(parts[1].split()) > 1 else ""  # "Яко"
            city_lo  = city.lower()
            # Генерируем алиасы
            aliases = [
                f"{city_lo}{num_part}",        # томск1
                f"{city_lo}-{num_part}",       # томск-1
                f"{city_lo}_{num_part}",       # томск_1
                f"{city_lo[:3]}{num_part}",    # том1 / бар1 / аба1 / чер1
                f"{city_lo[:3]}-{num_part}",   # том-1
            ]
            if short:
                aliases.append(short.lower())  # яко / дуб / ана / гео / тим / бал / кир / аск / тих
            aliases_str = ", ".join(aliases)
        else:
            aliases_str = name.lower()
        lines.append(f"{name:<20}| {city:<10}| {aliases_str}")
    return "\n".join(lines)


async def parse_query_with_openrouter(query_text: str) -> dict:
    """Вызывает OpenRouter для парсинга свободного запроса в структурированные параметры."""
    api_key = settings.openrouter_api_key
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY не настроен в .env")

    model = settings.openrouter_model or "google/gemini-2.5-flash"
    today = date.today().strftime("%d.%m.%Y")
    branch_table = _build_branch_table(settings.branches)
    system = _SYSTEM_PROMPT.format(today=today, branch_table=branch_table)

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{_OPENROUTER_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://ebidoebi.ru",
                "X-Title": "Ebidoebi Marketing Bot",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": query_text},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            },
        )
        r.raise_for_status()

    content = r.json()["choices"][0]["message"]["content"]
    # На случай если LLM всё же завернул в markdown-блок
    content = re.sub(r"```(?:json)?\s*|\s*```", "", content).strip()
    params = json.loads(content)
    logger.info(f"[marketing_export] parsed params: {params}")
    return params


# ---------------------------------------------------------------------------
# SQL Builder
# ---------------------------------------------------------------------------

def _get_branches_for_city(city: str) -> list[str]:
    """Возвращает список branch_name для указанного города."""
    return [
        b["name"]
        for b in settings.branches
        if b.get("city", "").lower() == city.strip().lower()
    ]


def build_sql(params: dict) -> tuple[str, list]:
    """
    Строит SQL-запрос к orders_raw на основе параметров из LLM.
    Возвращает (sql, args).

    CTE customer_first: первый заказ клиента в БД (для new/old).
    CTE customer_total: всего заказов клиента в БД.
    """
    conditions: list[str] = []
    args: list = []

    # Исключаем отменённые заказы
    conditions.append("o.status NOT IN ('Отменён', 'Отменен', 'Cancelled', 'Отмена')")
    # Только строки с телефоном (иначе клиент неидентифицирован)
    conditions.append("o.client_phone IS NOT NULL AND TRIM(o.client_phone) != ''")

    # --- Дата ---
    if params.get("date"):
        conditions.append("o.date = ?")
        args.append(params["date"])
    else:
        if params.get("date_from"):
            conditions.append("o.date >= ?")
            args.append(params["date_from"])
        if params.get("date_to"):
            conditions.append("o.date <= ?")
            args.append(params["date_to"])

    # --- Опоздание ---
    only_late = params.get("only_late")
    min_late = params.get("min_late_minutes")

    if only_late is True:
        threshold = min_late if min_late is not None else DEFAULT_LATE_MINUTES
        conditions.append("o.is_late = 1 AND COALESCE(o.late_minutes, 0) >= ?")
        args.append(threshold)
    elif only_late is False:
        conditions.append("o.is_late = 0")
    else:
        # only_late не указан, но min_late_minutes может быть задан явно
        if min_late is not None:
            conditions.append("COALESCE(o.late_minutes, 0) >= ?")
            args.append(min_late)

    # --- Сумма заказа ---
    if params.get("min_order_sum") is not None:
        conditions.append("o.sum >= ?")
        args.append(params["min_order_sum"])
    if params.get("max_order_sum") is not None:
        conditions.append("o.sum <= ?")
        args.append(params["max_order_sum"])

    # --- Состав блюд ---
    items_list = params.get("items_contains") or []
    for item in items_list:
        conditions.append("LOWER(o.items) LIKE ?")
        args.append(f"%{item.lower()}%")

    # --- Филиал или город ---
    # branch имеет приоритет над city (конкретная точка точнее)
    branch = params.get("branch")
    city = params.get("city")
    if branch:
        conditions.append("o.branch_name = ?")
        args.append(branch)
    elif city:
        branch_names = _get_branches_for_city(city)
        if branch_names:
            placeholders = ",".join("?" * len(branch_names))
            conditions.append(f"o.branch_name IN ({placeholders})")
            args.extend(branch_names)

    where_clause = " AND ".join(conditions)

    inner_sql = f"""
        WITH customer_first AS (
            SELECT client_phone, MIN(date) AS first_order_date
            FROM orders_raw
            WHERE client_phone IS NOT NULL AND TRIM(client_phone) != ''
            GROUP BY client_phone
        ),
        customer_total AS (
            SELECT client_phone, COUNT(*) AS total_orders
            FROM orders_raw
            WHERE client_phone IS NOT NULL AND TRIM(client_phone) != ''
            GROUP BY client_phone
        )
        SELECT
            o.branch_name,
            o.delivery_num,
            o.client_name,
            o.client_phone,
            o.delivery_address,
            o.date,
            COALESCE(o.sum, 0)             AS sum,
            o.is_late,
            COALESCE(o.late_minutes, 0)    AS late_minutes,
            CASE
                WHEN cf.first_order_date = o.date THEN 'Новый'
                ELSE 'Старый'
            END AS customer_type,
            COALESCE(ct.total_orders, 1)   AS total_orders
        FROM orders_raw o
        LEFT JOIN customer_first cf ON o.client_phone = cf.client_phone
        LEFT JOIN customer_total ct  ON o.client_phone = ct.client_phone
        WHERE {where_clause}
    """

    # Фильтр по типу клиента — применяем снаружи CTE (иначе нарушится логика first_order_date)
    customer_type = params.get("customer_type")
    if customer_type == "new":
        sql = f"SELECT * FROM ({inner_sql}) t WHERE t.customer_type = 'Новый' ORDER BY t.date DESC, t.branch_name"
    elif customer_type == "old":
        sql = f"SELECT * FROM ({inner_sql}) t WHERE t.customer_type = 'Старый' ORDER BY t.date DESC, t.branch_name"
    else:
        sql = f"SELECT * FROM ({inner_sql}) t ORDER BY t.date DESC, t.branch_name"

    return sql, args


# ---------------------------------------------------------------------------
# CSV Generation
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "Категория",
    "Имя клиента",
    "Телефон",
    "Адрес доставки",
    "Номер заказа",
    "Точка",
    "Дата заказа",
    "Сумма заказа, руб",
    "Опоздание",
    "Минут опоздания",
    "Всего заказов в базе",
]


def _build_csv(rows: list[dict]) -> bytes:
    """Генерирует CSV-файл. UTF-8 с BOM — корректно открывается в Excel."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=_CSV_FIELDS,
        extrasaction="ignore",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "Категория": r["customer_type"],
            "Имя клиента": r["client_name"] or "—",
            "Телефон": r["client_phone"],
            "Адрес доставки": r["delivery_address"] or "—",
            "Номер заказа": r["delivery_num"],
            "Точка": r["branch_name"],
            "Дата заказа": r["date"] or "—",
            "Сумма заказа, руб": int(float(r["sum"])) if r["sum"] else 0,
            "Опоздание": "Да" if r["is_late"] else "Нет",
            "Минут опоздания": int(float(r["late_minutes"])) if r["is_late"] else 0,
            "Всего заказов в базе": r["total_orders"] or 0,
        })
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def _build_filename(params: dict) -> str:
    """Читаемое имя файла по параметрам запроса."""
    parts = ["выгрузка"]

    ct = params.get("customer_type")
    if ct == "new":
        parts.append("новые")
    elif ct == "old":
        parts.append("старые")

    d = params.get("date")
    if d:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            parts.append(dt.strftime("%d.%m"))
        except Exception:
            parts.append(d)
    elif params.get("date_from") or params.get("date_to"):
        d_from = (params.get("date_from") or "")[:10]
        d_to = (params.get("date_to") or "")[:10]
        if d_from and d_to:
            parts.append(f"{d_from}_по_{d_to}")
        elif d_from:
            parts.append(f"с_{d_from}")
        else:
            parts.append(f"по_{d_to}")

    only_late = params.get("only_late")
    min_late = params.get("min_late_minutes")
    if only_late is True:
        threshold = min_late if min_late is not None else DEFAULT_LATE_MINUTES
        parts.append(f"опоздание{threshold}+")
    elif only_late is False:
        parts.append("вовремя")

    branch = params.get("branch")
    city = params.get("city")
    if branch:
        # "Томск_1 Яко" → "томск1_яко"
        parts.append(branch.lower().replace(" ", "_").replace("_", "", 1).replace(" ", ""))
    elif city:
        parts.append(city.lower())

    parts.append(date.today().strftime("%Y%m%d"))
    return "_".join(parts) + ".csv"


def _build_params_summary(params: dict) -> str:
    """Текстовое описание применённых фильтров для сообщения в Telegram."""
    lines = []

    ct = params.get("customer_type")
    if ct == "new":
        lines.append("👤 Тип: <b>Новые клиенты</b>")
    elif ct == "old":
        lines.append("👤 Тип: <b>Старые клиенты</b>")

    d = params.get("date")
    if d:
        lines.append(f"📅 Дата: <b>{d}</b>")
    elif params.get("date_from") or params.get("date_to"):
        d_from = params.get("date_from") or "—"
        d_to = params.get("date_to") or "—"
        lines.append(f"📅 Период: <b>{d_from} — {d_to}</b>")

    only_late = params.get("only_late")
    min_late = params.get("min_late_minutes")
    if only_late is True:
        threshold = min_late if min_late is not None else DEFAULT_LATE_MINUTES
        lines.append(f"⏱ Опоздание: <b>≥ {threshold} мин</b>")
    elif only_late is False:
        lines.append("⏱ Опоздание: <b>нет (вовремя)</b>")

    if params.get("min_order_sum") is not None:
        lines.append(f"💰 Сумма от: <b>{params['min_order_sum']} руб</b>")
    if params.get("max_order_sum") is not None:
        lines.append(f"💰 Сумма до: <b>{params['max_order_sum']} руб</b>")

    items = params.get("items_contains")
    if items:
        lines.append(f"🍱 Содержит: <b>{', '.join(items)}</b>")

    branch = params.get("branch")
    city = params.get("city")
    if branch:
        lines.append(f"📍 Филиал: <b>{branch}</b>")
    elif city:
        lines.append(f"🏙 Город: <b>{city}</b>")

    return "\n".join(lines) if lines else "(без дополнительных фильтров)"


# ---------------------------------------------------------------------------
# Telegram helpers (локальные, независимы от arkentiy)
# ---------------------------------------------------------------------------

async def _send_text(bot_url: str, chat_id: int, text: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{bot_url}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
            if not r.json().get("ok"):
                logger.error(f"[marketing_export] sendMessage error: {r.text[:300]}")
    except Exception as e:
        logger.error(f"[marketing_export] _send_text: {e}")


async def _send_document(
    bot_url: str, chat_id: int, data: bytes, filename: str, caption: str
) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{bot_url}/sendDocument",
            data={
                "chat_id": str(chat_id),
                "caption": caption,
                "parse_mode": "HTML",
            },
            files={"document": (filename, data, "text/csv; charset=utf-8")},
        )
        resp = r.json()
        if not resp.get("ok"):
            raise RuntimeError(f"Telegram sendDocument: {resp.get('description')}")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run_export(chat_id: int, query_text: str, bot_url: str) -> None:
    """
    Точка входа — вызывается из arkentiy.py при команде /выгрузка.
    bot_url: f"https://api.telegram.org/bot{TOKEN}"
    """
    if not query_text.strip():
        await _send_text(
            bot_url, chat_id,
            "❓ Укажи параметры запроса.\n\n"
            "Примеры:\n"
            "<code>/выгрузка новые клиенты 14.02 опоздание &gt;15 минут</code>\n"
            "<code>/выгрузка старые клиенты Барнаул февраль</code>\n"
            "<code>/выгрузка все клиенты с опозданием вчера</code>"
        )
        return

    await _send_text(bot_url, chat_id, "⏳ Обрабатываю запрос...")

    # 1. Парсинг запроса через OpenRouter
    try:
        params = await parse_query_with_openrouter(query_text)
    except Exception as e:
        logger.error(f"[marketing_export] OpenRouter error: {e}", exc_info=True)
        await _send_text(
            bot_url, chat_id,
            f"❌ Не удалось распознать запрос: <code>{e}</code>\n\n"
            "Проверь настройку OPENROUTER_API_KEY."
        )
        return

    # 2. Строим SQL
    try:
        sql, args = build_sql(params)
    except Exception as e:
        logger.error(f"[marketing_export] build_sql error: {e}", exc_info=True)
        await _send_text(bot_url, chat_id, f"❌ Ошибка формирования запроса: <code>{e}</code>")
        return

    # 3. Выполняем запрос к БД
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cursor:
                rows = [dict(r) for r in await cursor.fetchall()]
    except Exception as e:
        logger.error(f"[marketing_export] DB error: {e}\nSQL: {sql}\nArgs: {args}", exc_info=True)
        await _send_text(bot_url, chat_id, f"❌ Ошибка запроса к базе данных: <code>{e}</code>")
        return

    if not rows:
        summary = _build_params_summary(params)
        await _send_text(
            bot_url, chat_id,
            f"📭 Ничего не найдено по запросу:\n\n{summary}\n\n"
            "<i>Возможно, данные за этот период ещё не накоплены в базе.</i>"
        )
        return

    # 4. Генерируем CSV
    try:
        csv_bytes = _build_csv(rows)
        filename = _build_filename(params)
    except Exception as e:
        logger.error(f"[marketing_export] CSV build error: {e}", exc_info=True)
        await _send_text(bot_url, chat_id, f"❌ Ошибка генерации CSV: <code>{e}</code>")
        return

    # 5. Отправляем файл
    summary = _build_params_summary(params)
    caption = f"✅ Найдено: <b>{len(rows)} записей</b>\n\n{summary}"

    try:
        await _send_document(bot_url, chat_id, csv_bytes, filename, caption)
    except Exception as e:
        logger.error(f"[marketing_export] sendDocument error: {e}", exc_info=True)
        await _send_text(bot_url, chat_id, f"❌ Ошибка отправки файла: <code>{e}</code>")

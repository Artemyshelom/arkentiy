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
сумма заказа, состав блюд, опоздание (есть/нет, минут), источник заказа, тип оплаты.

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
  "city": "Барнаул" | "Абакан" | "Томск" | "Черногорск" | null,
  "min_orders_in_period": <число> | null,
  "max_orders_in_period": <число> | null,
  "min_total_orders": <число> | null,
  "max_total_orders": <число> | null,
  "exclude_period_from": "YYYY-MM-DD" | null,
  "exclude_period_to": "YYYY-MM-DD" | null,
  "payment_type": "наличные" | "карта" | "онлайн" | null,
  "source": "приложение" | "сайт" | "колл-центр" | null,
  "has_problem": true | false | null,
  "unique_clients_only": true | null
}}

Таблица филиалов — используй ТОЧНОЕ название из колонки «branch_name»:
{branch_table}

Правила:
- "новый клиент" / "новые" → customer_type: "new"; "старый" / "постоянный" / "повторный" → customer_type: "old"
- Дата "14.02" без года → текущий год. "вчера" → вчера. "сегодня" → сегодня.
- "опоздание >15 минут" → only_late: true, min_late_minutes: 15
- "опоздание >5 минут" или просто "опоздание" (без числа) → only_late: true, min_late_minutes: null
  (базовый порог 5 мин применяется автоматически на стороне сервера)
- "без опоздания" / "вовремя" → only_late: false
- Блюда: список если упомянуты конкретные позиции, иначе null
- Если назван конкретный филиал → заполни branch (точное название) и оставь city: null
- Если назван только город → заполни city, оставь branch: null
- null = нет фильтра по этому полю

Правила для новых фильтров:
- "2 и более заказа за период" / "делали 2+ заказа" → min_orders_in_period: 2 (считает заказы клиента внутри date_from/date_to)
- "3 и более заказами за прошлый месяц" → min_orders_in_period: 3 + date_from/date_to = прошлый месяц
- "не сделали повторного заказа" / "больше не возвращались" / "только один заказ за всё время" → max_total_orders: 1
- "заказывали в X, но не заказывали в Y" → date_from/date_to = период X, exclude_period_from/exclude_period_to = период Y
- "не заказывали после [дата]" → exclude_period_from = следующий день после той даты, exclude_period_to оставь null
- "наличные" / "нал" → payment_type: "наличные"; "карта" / "безнал" → payment_type: "карта"; "онлайн" / "сайт оплата" → payment_type: "онлайн"
- "с жалобой" / "была проблема" / "проблемный заказ" → has_problem: true; "без жалоб" → has_problem: false
- "список для обзвона" / "уникальные клиенты" / "без повторов" → unique_clients_only: true

Примеры маппинга:
- "клиенты делали 2 и более заказа в период ноябрь-январь" → min_orders_in_period: 2, date_from: первый день ноября, date_to: последний день января
- "новые клиенты ноября, которые до сих пор не сделали повторного заказа" → customer_type: "new", date_from/date_to: ноябрь, max_total_orders: 1
- "повторные клиенты с 3 и более заказами за прошлый месяц" → customer_type: "old", min_orders_in_period: 3, date_from/date_to: прошлый месяц
- "клиенты которые заказывали август-декабрь, но не заказывали в январе и феврале" → date_from/date_to: авг-дек, exclude_period_from/exclude_period_to: янв-фев

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

    CTE customer_first:   первый заказ клиента в БД (для new/old).
    CTE customer_total:   всего заказов клиента в БД за всё время.
    CTE period_orders:    кол-во заказов клиента внутри date_from/date_to (если задан min/max_orders_in_period).
    CTE excluded_phones:  телефоны с заказами в исключаемом периоде (для "заказывали X, но не Y").
    """
    _cancelled = "('Отменён', 'Отменен', 'Cancelled', 'Отмена')"

    conditions: list[str] = []
    args: list = []

    # Исключаем отменённые заказы
    conditions.append(f"o.status NOT IN {_cancelled}")
    # Только строки с телефоном (иначе клиент неидентифицирован)
    conditions.append("o.client_phone IS NOT NULL AND TRIM(o.client_phone) != ''")

    # --- Дата ---
    date_from = params.get("date_from")
    date_to = params.get("date_to")
    if params.get("date"):
        conditions.append("o.date = ?")
        args.append(params["date"])
    else:
        if date_from:
            conditions.append("o.date >= ?")
            args.append(date_from)
        if date_to:
            conditions.append("o.date <= ?")
            args.append(date_to)

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

    # --- Тип оплаты ---
    if params.get("payment_type"):
        conditions.append("LOWER(COALESCE(o.payment_type, '')) LIKE ?")
        args.append(f"%{params['payment_type'].lower()}%")

    # --- Источник заказа ---
    if params.get("source"):
        conditions.append("LOWER(COALESCE(o.source, '')) LIKE ?")
        args.append(f"%{params['source'].lower()}%")

    # --- Проблемный заказ ---
    has_problem = params.get("has_problem")
    if has_problem is True:
        conditions.append("o.has_problem = 1")
    elif has_problem is False:
        conditions.append("o.has_problem = 0")

    # --- Филиал или город ---
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

    # --- Формируем CTE-блок ---
    cte_parts: list[str] = []
    cte_args: list = []  # аргументы для CTE (prepend перед основными args)

    # CTE customer_first и customer_total — всегда нужны
    cte_parts.append("""customer_first AS (
            SELECT client_phone, MIN(date) AS first_order_date
            FROM orders_raw
            WHERE client_phone IS NOT NULL AND TRIM(client_phone) != ''
            GROUP BY client_phone
        )""")
    cte_parts.append("""customer_total AS (
            SELECT client_phone, COUNT(*) AS total_orders
            FROM orders_raw
            WHERE client_phone IS NOT NULL AND TRIM(client_phone) != ''
            GROUP BY client_phone
        )""")

    # CTE period_orders — только если нужен подсчёт заказов внутри периода
    need_period_orders = (
        params.get("min_orders_in_period") is not None
        or params.get("max_orders_in_period") is not None
    )
    if need_period_orders:
        period_conds = [f"status NOT IN {_cancelled}",
                        "client_phone IS NOT NULL AND TRIM(client_phone) != ''"]
        if date_from:
            period_conds.append("date >= ?")
            cte_args.append(date_from)
        if date_to:
            period_conds.append("date <= ?")
            cte_args.append(date_to)
        if branch:
            period_conds.append("branch_name = ?")
            cte_args.append(branch)
        elif city:
            branch_names = _get_branches_for_city(city)
            if branch_names:
                ph = ",".join("?" * len(branch_names))
                period_conds.append(f"branch_name IN ({ph})")
                cte_args.extend(branch_names)
        period_where = " AND ".join(period_conds)
        cte_parts.append(f"""period_orders AS (
            SELECT client_phone, COUNT(*) AS period_count
            FROM orders_raw
            WHERE {period_where}
            GROUP BY client_phone
        )""")

    # CTE excluded_phones — только если задан исключающий период
    exclude_from = params.get("exclude_period_from")
    exclude_to = params.get("exclude_period_to")
    need_exclude = exclude_from or exclude_to
    if need_exclude:
        excl_conds = [f"status NOT IN {_cancelled}",
                      "client_phone IS NOT NULL AND TRIM(client_phone) != ''"]
        if exclude_from:
            excl_conds.append("date >= ?")
            cte_args.append(exclude_from)
        if exclude_to:
            excl_conds.append("date <= ?")
            cte_args.append(exclude_to)
        excl_where = " AND ".join(excl_conds)
        cte_parts.append(f"""excluded_phones AS (
            SELECT DISTINCT client_phone
            FROM orders_raw
            WHERE {excl_where}
        )""")

    cte_block = "WITH " + ",\n        ".join(cte_parts)

    # --- JOIN-строки ---
    joins = [
        "LEFT JOIN customer_first cf ON o.client_phone = cf.client_phone",
        "LEFT JOIN customer_total ct  ON o.client_phone = ct.client_phone",
    ]
    if need_period_orders:
        joins.append("LEFT JOIN period_orders po ON o.client_phone = po.client_phone")

    # --- WHERE-условия после CTE ---
    post_cte_conds: list[str] = [f"({where_clause})"]
    post_cte_args: list = list(args)

    # Фильтр по всего заказов (total_orders из customer_total)
    if params.get("min_total_orders") is not None:
        post_cte_conds.append("COALESCE(ct.total_orders, 1) >= ?")
        post_cte_args.append(params["min_total_orders"])
    if params.get("max_total_orders") is not None:
        post_cte_conds.append("COALESCE(ct.total_orders, 1) <= ?")
        post_cte_args.append(params["max_total_orders"])

    # Фильтр по заказам внутри периода (period_orders)
    if params.get("min_orders_in_period") is not None:
        post_cte_conds.append("COALESCE(po.period_count, 0) >= ?")
        post_cte_args.append(params["min_orders_in_period"])
    if params.get("max_orders_in_period") is not None:
        post_cte_conds.append("COALESCE(po.period_count, 0) <= ?")
        post_cte_args.append(params["max_orders_in_period"])

    # Исключаем клиентов с заказами в excluded_phones
    if need_exclude:
        post_cte_conds.append("o.client_phone NOT IN (SELECT client_phone FROM excluded_phones)")

    post_where = " AND ".join(post_cte_conds)

    join_block = "\n        ".join(joins)

    inner_sql = f"""
        {cte_block}
        SELECT
            o.branch_name,
            o.delivery_num,
            o.client_name,
            o.client_phone,
            o.delivery_address,
            o.date,
            COALESCE(o.sum, 0)                          AS sum,
            o.is_late,
            COALESCE(o.late_minutes, 0)                 AS late_minutes,
            CASE
                WHEN cf.first_order_date = o.date THEN 'Новый'
                ELSE 'Старый'
            END                                         AS customer_type,
            COALESCE(ct.total_orders, 1)                AS total_orders,
            COALESCE(po.period_count, ct.total_orders, 1) AS orders_in_period,
            COALESCE(o.payment_type, '')                AS payment_type,
            COALESCE(o.source, '')                      AS source
        FROM orders_raw o
        {join_block}
        WHERE {post_where}
    """

    # Итоговые аргументы: сначала CTE-аргументы (они используются в WITH-блоке),
    # потом аргументы WHERE основного запроса и post-CTE фильтров
    final_args = cte_args + post_cte_args

    # Фильтр по типу клиента — применяем снаружи CTE
    customer_type = params.get("customer_type")
    outer_conds = []
    if customer_type == "new":
        outer_conds.append("t.customer_type = 'Новый'")
    elif customer_type == "old":
        outer_conds.append("t.customer_type = 'Старый'")

    outer_where = (" WHERE " + " AND ".join(outer_conds)) if outer_conds else ""

    unique_clients_only = params.get("unique_clients_only")
    if unique_clients_only:
        # Один ряд на клиента — строка с последним заказом
        sql = (
            f"SELECT t.* FROM ({inner_sql}) t"
            f"{outer_where}"
            f" GROUP BY t.client_phone"
            f" HAVING t.date = MAX(t.date)"
            f" ORDER BY t.date DESC, t.branch_name"
        )
    else:
        sql = (
            f"SELECT * FROM ({inner_sql}) t"
            f"{outer_where}"
            f" ORDER BY t.date DESC, t.branch_name"
        )

    return sql, final_args


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
    "Заказов за период",
    "Тип оплаты",
    "Источник заказа",
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
            "Всего заказов в базе": r.get("total_orders") or 0,
            "Заказов за период": r.get("orders_in_period") or 0,
            "Тип оплаты": r.get("payment_type") or "—",
            "Источник заказа": r.get("source") or "—",
        })
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def _build_filename(params: dict) -> str:
    """Читаемое имя файла по параметрам запроса."""
    parts = ["выгрузка"]

    if params.get("unique_clients_only"):
        parts.append("уникальные")

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

    if params.get("min_orders_in_period") is not None:
        parts.append(f"{params['min_orders_in_period']}плюс_заказов")

    if params.get("max_total_orders") is not None:
        parts.append(f"макс{params['max_total_orders']}_всего")

    excl_from = params.get("exclude_period_from")
    excl_to = params.get("exclude_period_to")
    if excl_from or excl_to:
        ef = (excl_from or "")[:7]  # YYYY-MM
        et = (excl_to or "")[:7]
        tag = f"excl_{ef}" if ef == et or not et else f"excl_{ef}_{et}"
        parts.append(tag)

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
        lines.append("👤 Тип: <b>Старые/повторные клиенты</b>")

    d = params.get("date")
    if d:
        lines.append(f"📅 Дата: <b>{d}</b>")
    elif params.get("date_from") or params.get("date_to"):
        d_from = params.get("date_from") or "—"
        d_to = params.get("date_to") or "—"
        lines.append(f"📅 Период: <b>{d_from} — {d_to}</b>")

    if params.get("min_orders_in_period") is not None:
        lines.append(f"🔢 Заказов за период: <b>≥ {params['min_orders_in_period']}</b>")
    if params.get("max_orders_in_period") is not None:
        lines.append(f"🔢 Заказов за период: <b>≤ {params['max_orders_in_period']}</b>")

    if params.get("min_total_orders") is not None:
        lines.append(f"📊 Всего заказов: <b>≥ {params['min_total_orders']}</b>")
    if params.get("max_total_orders") is not None:
        lines.append(f"📊 Всего заказов: <b>≤ {params['max_total_orders']}</b>")

    excl_from = params.get("exclude_period_from")
    excl_to = params.get("exclude_period_to")
    if excl_from or excl_to:
        ef = excl_from or "—"
        et = excl_to or "…"
        lines.append(f"🚫 Исключить активных в: <b>{ef} — {et}</b>")

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

    if params.get("payment_type"):
        lines.append(f"💳 Оплата: <b>{params['payment_type']}</b>")
    if params.get("source"):
        lines.append(f"📲 Источник: <b>{params['source']}</b>")

    has_problem = params.get("has_problem")
    if has_problem is True:
        lines.append("⚠️ Только с жалобами")
    elif has_problem is False:
        lines.append("✅ Только без жалоб")

    if params.get("unique_clients_only"):
        lines.append("👥 Уникальные клиенты (последний заказ)")

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
            "<code>/выгрузка все клиенты с опозданием вчера</code>\n"
            "<code>/выгрузка Барнаул клиенты делали 2 и более заказа ноябрь-январь</code>\n"
            "<code>/выгрузка новые клиенты ноября, которые больше не возвращались</code>\n"
            "<code>/выгрузка Томск повторные клиенты 3+ заказа за прошлый месяц</code>\n"
            "<code>/выгрузка Абакан заказывали август-декабрь, но не заказывали в январе-феврале</code>"
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

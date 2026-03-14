"""
Канонические OLAP v2 запросы — единый источник правды.

4 типа запросов + обёртки покрывают 100% потребностей:

  Query A — «Заказ» (DELIVERIES, per-order)
    Вся метаданных заказа: клиент, адрес, тайминги, оплата, скидка, отмена.
    Заменяет: olap_enrichment, cancel_sync, backfill Phases 1/4/5/6.

  Query B — «Блюда» (SALES, per-order × dish)
    Состав заказа: блюда, количество, курьер (WaiterName).
    Заменяет: backfill Phases 2/3.

  Query C — «Агрегат по точке» (2 sub-запроса: 1 SALES + 1 DELIVERIES)
    Выручка, COGS%, чеки, нал/безнал, самовывоз, скидки, тайминги.
    Поддерживает батчинг по датам (group_by_date=True).
    Заменяет: get_all_branches_stats(), backfill_daily_stats, iiko_bo_olap_v2.

  Query D — «Сторно-аудит» (SALES, специальный)
    Поля Storned/CashierName недоступны в других режимах.
    Используется только в audit.py.

  Обёртки (бывший iiko_bo_olap_v2.py):
    get_branch_olap_stats — для /статус (с таймингами)
    get_all_branches_stats — для iiko_to_sheets (с pickup)
    get_payment_breakdown — для bank_statement
    get_online_orders — для tbank_reconciliation
    get_discount_breakdown — для arkentiy

Правила использования:
  - Все 4 функции возвращают сырые OLAP-строки (list[dict]).
    Исключение: fetch_branch_aggregate возвращает структурированный dict.
  - Агрегацию по заказу делает потребитель.
  - НЕ включать OpenDate.Typed вместе с Delivery.Number — обнуляет DN (документированный баг iiko).
  - DiscountSum корректен только в DELIVERIES (per-order), в SALES считается per-dish.
  - Семафор (OLAP_SEMAPHORE) ограничивает параллельные запросы (settings.olap_max_concurrent).
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import httpx

from app.clients.iiko_auth import get_bo_token
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

OLAP_SEMAPHORE: asyncio.Semaphore | None = None


def _get_olap_semaphore() -> asyncio.Semaphore:
    """Ленивая инициализация семафора (нужен event loop)."""
    global OLAP_SEMAPHORE
    if OLAP_SEMAPHORE is None:
        OLAP_SEMAPHORE = asyncio.Semaphore(settings.olap_max_concurrent)
    return OLAP_SEMAPHORE

# ---------------------------------------------------------------------------
# Query A: поля «Заказ» (DELIVERIES, per-order)
# ---------------------------------------------------------------------------

ORDER_DETAIL_FIELDS: list[str] = [
    "Delivery.Number",
    "Department",
    "Delivery.CustomerPhone",
    "Delivery.CustomerName",
    "Delivery.ActualTime",
    "Delivery.ExpectedTime",
    "Delivery.Address",
    "Delivery.ServiceType",
    "Delivery.CancelCause",
    "Delivery.SourceKey",
    "Delivery.PrintTime",
    "Delivery.CookingFinishTime",
    "Delivery.SendTime",
    "OpenTime",
    "PayTypes",
    "OrderDiscount.Type",
]

# ---------------------------------------------------------------------------
# Query B: поля «Блюда» (SALES, per-order × dish)
# ---------------------------------------------------------------------------

DISH_DETAIL_FIELDS: list[str] = [
    "Delivery.Number",
    "Department",
    "DishName",
    "WaiterName",
]

# ---------------------------------------------------------------------------
# Query D: поля «Сторно-аудит» (SALES, специальный)
# ---------------------------------------------------------------------------

STORNO_AUDIT_FIELDS: list[str] = [
    "Department",
    "OrderNum",
    "Storned",
    "OrderDiscount.Type",
    "OpenTime",
    "CloseTime",
    "PayTypes",
    "CashierName",
]

# ---------------------------------------------------------------------------
# Внутренние хелперы
# ---------------------------------------------------------------------------

CASH_PAY_TYPES = {"Наличные"}
EXCLUDED_PAY_TYPES = {"SailPlay Бонус", "(без оплаты)"}


def _build_olap_body(
    group_fields: list[str],
    agg_fields: list[str],
    date_from: str,
    date_to: str,
    report_type: str = "SALES",
) -> dict:
    """Строит тело OLAP v2 запроса."""
    return {
        "reportType": report_type,
        "buildSummary": "false",
        "groupByRowFields": group_fields,
        "aggregateFields": agg_fields,
        "filters": {
            "OpenDate.Typed": {
                "filterType": "DateRange",
                "periodType": "CUSTOM",
                "from": date_from,
                "to": date_to,
                "includeLow": "true",
                "includeHigh": "false",
            }
        },
    }


async def _execute_olap(
    bo_url: str,
    token: str,
    group_fields: list[str],
    agg_fields: list[str],
    date_from: str,
    date_to: str,
    client: httpx.AsyncClient,
    report_type: str = "SALES",
) -> list[dict]:
    """Выполняет один OLAP v2 запрос, возвращает строки или [] при ошибке."""
    body = _build_olap_body(group_fields, agg_fields, date_from, date_to, report_type)
    sem = _get_olap_semaphore()
    try:
        async with sem:
            resp = await client.post(
                f"{bo_url}/api/v2/reports/olap?key={token}",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
        if resp.status_code != 200:
            logger.warning(f"OLAP {bo_url} [{report_type}] {resp.status_code}: {resp.text[:200]}")
            return []
        return resp.json().get("data", [])
    except Exception as e:
        logger.error(f"OLAP {bo_url} [{report_type}] ошибка: {e}")
        return []


def _group_branches_by_server(branches: list[dict]) -> dict[tuple, dict]:
    """
    Группирует точки по (bo_url, bo_login, bo_password).
    Возвращает {(url, login, pass): {"names": set, "login": str|None, "password": str|None}}.
    """
    by_server: dict[tuple, dict] = {}
    for branch in branches:
        url = branch.get("bo_url", "")
        if not url:
            logger.warning(f"Точка {branch.get('name', '?')} без bo_url — пропущена")
            continue
        login = branch.get("bo_login") or ""
        password = branch.get("bo_password") or ""
        key = (url, login, password)
        if key not in by_server:
            by_server[key] = {"url": url, "names": set(), "login": login or None, "password": password or None}
        by_server[key]["names"].add(branch["name"])
    return by_server


# ---------------------------------------------------------------------------
# Query A: fetch_order_detail
# ---------------------------------------------------------------------------

async def fetch_order_detail(
    date_from: str,
    date_to: str,
    branches: list[dict] | None = None,
) -> list[dict]:
    """
    Query A: данные уровня заказа из DELIVERIES reportType.

    Один запрос на сервер, параллельно по серверам.
    Возвращает сырые OLAP-строки — одна строка на (order × pay_type × discount_type).

    Потребители агрегируют по (Delivery.Number, Department):
      - берут payment_type с макс. суммой
      - discount_type из первой строки
      - тайминги — одинаковы во всех строках заказа

    date_from/date_to: ISO "2026-03-08" / "2026-03-09" (to exclusive).
    branches: список точек. None → settings.branches (tenant_id=1).
    """
    if branches is None:
        branches = settings.branches

    logger.info(f"OLAP Query A (order detail): {date_from} — {date_to}, точек: {len(branches)}")
    by_server = _group_branches_by_server(branches)

    async def _fetch(srv: dict) -> list[dict]:
        try:
            token = await get_bo_token(srv["url"], bo_login=srv["login"], bo_password=srv["password"])
            async with httpx.AsyncClient(verify=False, timeout=60) as client:
                rows = await _execute_olap(
                    srv["url"], token,
                    ORDER_DETAIL_FIELDS,
                    ["DishDiscountSumInt", "DiscountSum"],
                    date_from, date_to, client,
                    report_type="DELIVERIES",
                )
            # Фильтруем только нужные точки
            target = srv["names"]
            return [r for r in rows if (r.get("Department") or "").strip() in target]
        except Exception as e:
            logger.error(f"fetch_order_detail [{srv['url']}]: {e}")
            return []

    results = await asyncio.gather(*[_fetch(srv) for srv in by_server.values()])
    rows: list[dict] = []
    for chunk in results:
        rows.extend(chunk)
    logger.info(f"OLAP Query A: получено {len(rows)} строк")
    return rows


# ---------------------------------------------------------------------------
# Query B: fetch_dish_detail
# ---------------------------------------------------------------------------

async def fetch_dish_detail(
    date_from: str,
    date_to: str,
    branches: list[dict] | None = None,
) -> list[dict]:
    """
    Query B: состав заказа из SALES reportType.

    Одна строка на (order × dish). WaiterName = курьер (для backfill исторических данных;
    текущие заказы получают курьера из Events API).

    Возвращает сырые строки. Потребители группируют по (Delivery.Number, Department)
    и строят items JSON: [{"name": "Блюдо", "qty": 2}].

    Примечание: OrderDiscount.Type НЕ включён в этот запрос — скидки берутся из Query A.
    WaiterName несовместим с DishDiscountSumInt в SALES (задокументированный баг iiko),
    поэтому агрегируем по Amount (количество), а не по сумме.
    """
    if branches is None:
        branches = settings.branches

    logger.info(f"OLAP Query B (dish detail): {date_from} — {date_to}, точек: {len(branches)}")
    by_server = _group_branches_by_server(branches)

    async def _fetch(srv: dict) -> list[dict]:
        try:
            token = await get_bo_token(srv["url"], bo_login=srv["login"], bo_password=srv["password"])
            async with httpx.AsyncClient(verify=False, timeout=60) as client:
                rows = await _execute_olap(
                    srv["url"], token,
                    DISH_DETAIL_FIELDS,
                    ["Amount"],
                    date_from, date_to, client,
                    report_type="SALES",
                )
            target = srv["names"]
            return [r for r in rows if (r.get("Department") or "").strip() in target]
        except Exception as e:
            logger.error(f"fetch_dish_detail [{srv['url']}]: {e}")
            return []

    results = await asyncio.gather(*[_fetch(srv) for srv in by_server.values()])
    rows: list[dict] = []
    for chunk in results:
        rows.extend(chunk)
    logger.info(f"OLAP Query B: получено {len(rows)} строк")
    return rows


# ---------------------------------------------------------------------------
# Хелперы для таймингов и дат
# ---------------------------------------------------------------------------

def _parse_olap_date(val: str | None) -> str | None:
    """Извлекает ISO-дату из OLAP OpenDate.Typed (формат '2026-03-08' или '08.03.2026')."""
    if not val:
        return None
    val = str(val).strip()
    if len(val) >= 10 and val[4] == "-":
        return val[:10]
    try:
        parts = val.split(".")
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    except Exception:
        return None


def _parse_timestamp(val: str | None) -> datetime | None:
    """Парсит OLAP timestamp (ISO 8601) в datetime."""
    if not val:
        return None
    val = str(val).strip()
    if not val or val == "null":
        return None
    try:
        # iiko возвращает "2026-03-08T14:23:45.123" или "2026-03-08T14:23:45.123Z"
        clean = val.rstrip("Z")
        if "T" in clean:
            if "." in clean:
                return datetime.strptime(clean[:23], "%Y-%m-%dT%H:%M:%S.%f")
            return datetime.strptime(clean[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, IndexError):
        pass
    return None


def _collect_timing_mins(
    row: dict,
    acc_key: tuple,
    cooking_mins: dict[tuple, list[float]],
    wait_mins: dict[tuple, list[float]],
    delivery_mins: dict[tuple, list[float]],
) -> None:
    """Собирает timing-дельты из OLAP-строки заказа в аккумуляторы."""
    print_time = _parse_timestamp(row.get("Delivery.PrintTime"))
    cooked_time = _parse_timestamp(row.get("Delivery.CookingFinishTime"))
    send_time = _parse_timestamp(row.get("Delivery.SendTime"))
    actual_time = _parse_timestamp(row.get("Delivery.ActualTime"))
    open_time = _parse_timestamp(row.get("OpenTime"))

    start_time = print_time or open_time

    # avg_cooking = CookingFinishTime - (PrintTime or OpenTime), 1-120 мин
    if cooked_time and start_time:
        delta = (cooked_time - start_time).total_seconds() / 60
        if 1 <= delta <= 120:
            cooking_mins[acc_key].append(delta)

    # avg_wait = SendTime - CookingFinishTime, 0-120 мин
    if send_time and cooked_time:
        delta = (send_time - cooked_time).total_seconds() / 60
        if 0 <= delta <= 120:
            wait_mins[acc_key].append(delta)

    # avg_delivery = ActualTime - SendTime, 1-120 мин (только не PICKUP)
    service_type = (row.get("Delivery.ServiceType") or "").upper()
    if actual_time and send_time and service_type != "PICKUP":
        delta = (actual_time - send_time).total_seconds() / 60
        if 1 <= delta <= 120:
            delivery_mins[acc_key].append(delta)


# ---------------------------------------------------------------------------
# Query C: fetch_branch_aggregate
# ---------------------------------------------------------------------------

async def fetch_branch_aggregate(
    date_from: str,
    date_to: str,
    branches: list[dict] | None = None,
    include_pickup: bool = True,
    skip_discount_query: bool = False,
    include_timings: bool = False,
    group_by_date: bool = False,
    external_discount_types: dict[str, list[dict]] | None = None,
) -> dict[str, dict]:
    """
    Query C: агрегированные метрики по точке.

    2 параллельных sub-запроса на сервер (было 3):
      C_merged (SALES): Department × PayTypes × ServiceType →
          выручка, COGS%, чеки, нал/безнал/sailplay, pickup
      C_discount (DELIVERIES): Department × OrderDiscount.Type →
          типы скидок (корректные) + опционально тайминги per-order

    Параметры:
      skip_discount_query: True → не делать DELIVERIES запрос (скидки берутся из external_discount_types)
      include_timings: True → расширить DELIVERIES запрос до per-order с timing-полями
      group_by_date: True → добавить OpenDate.Typed в GROUP BY (для батчинга по датам)
      external_discount_types: {dept: [{"type": ..., "sum": ...}]} — внешние данные скидок (из Query A)

    Возвращает:
      {dept_name: {revenue_net, cogs_pct, check_count, cash, noncash,
                   sailplay, discount_sum, discount_types, pickup_count,
                   avg_cooking_min, avg_wait_min, avg_delivery_min}}

    При group_by_date=True возвращает:
      {"2026-03-08": {dept_name: {...}}, "2026-03-09": {dept_name: {...}}, ...}
    """
    if branches is None:
        branches = settings.branches

    logger.info(f"OLAP Query C (branch aggregate): {date_from} — {date_to}")
    by_server = _group_branches_by_server(branches)

    async def _fetch(srv: dict) -> dict[str, dict]:
        url = srv["url"]
        target = srv["names"]

        def _new_stats() -> dict:
            return {
                "revenue_net": None,
                "cogs_pct": None,
                "check_count": 0,
                "cash": 0.0,
                "noncash": 0.0,
                "sailplay": 0.0,
                "discount_sum": 0.0,
                "discount_types": [],
                "pickup_count": 0,
                "avg_cooking_min": None,
                "avg_wait_min": None,
                "avg_delivery_min": None,
            }

        # При group_by_date — двухуровневый dict: {date: {dept: stats}}
        if group_by_date:
            stats_by_date: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(_new_stats))
        else:
            stats: dict[str, dict] = defaultdict(_new_stats)

        def _get_stats(date_key: str | None, dept: str) -> dict:
            if group_by_date:
                return stats_by_date[date_key][dept]
            return stats[dept]

        try:
            token = await get_bo_token(url, bo_login=srv["login"], bo_password=srv["password"])
            async with httpx.AsyncClient(verify=False, timeout=60) as client:
                # --- C_merged: один SALES запрос вместо двух (Q1+Q2) ---
                merged_group = ["Department", "PayTypes"]
                if include_pickup:
                    merged_group.append("Delivery.ServiceType")
                if group_by_date:
                    merged_group.append("OpenDate.Typed")

                merged_agg = [
                    "DishDiscountSumInt.withoutVAT",
                    "ProductCostBase.Percent",
                    "DishDiscountSumInt",
                    "UniqOrderId.OrdersCount",
                    "UniqOrderId",
                    "DiscountSum",
                ]

                tasks = [
                    _execute_olap(
                        url, token,
                        merged_group, merged_agg,
                        date_from, date_to, client,
                    ),
                ]

                # --- C_discount: DELIVERIES запрос (скидки + опционально тайминги) ---
                if not skip_discount_query:
                    if include_timings:
                        # Per-order: добавляем Delivery.Number + timing-поля
                        discount_group = [
                            "Department", "OrderDiscount.Type",
                            "Delivery.Number",
                            "Delivery.PrintTime", "Delivery.CookingFinishTime",
                            "Delivery.SendTime", "Delivery.ActualTime",
                            "Delivery.ServiceType",
                            "OpenTime",
                        ]
                    else:
                        discount_group = ["Department", "OrderDiscount.Type"]
                    if group_by_date:
                        discount_group.append("OpenDate.Typed")

                    tasks.append(
                        _execute_olap(
                            url, token,
                            discount_group, ["DiscountSum"],
                            date_from, date_to, client,
                            report_type="DELIVERIES",
                        ),
                    )

                query_results = await asyncio.gather(*tasks)

            q_merged = query_results[0]
            q_discount = query_results[1] if len(query_results) > 1 else []

            # --- Парсинг merged SALES (revenue, COGS, checks, cash/noncash, pickup) ---
            # Накапливаем per-dept (или per-date-dept) для взвешенного COGS%
            revenue_accum: dict[tuple, float] = defaultdict(float)  # (date_key, dept) -> total revenue
            cost_accum: dict[tuple, float] = defaultdict(float)     # (date_key, dept) -> total cost (rev*cogs)
            checks_accum: dict[tuple, int] = defaultdict(int)
            discount_sales_accum: dict[tuple, float] = defaultdict(float)

            for row in q_merged:
                dept = (row.get("Department") or "").strip()
                if dept not in target:
                    continue
                date_key = _parse_olap_date(row.get("OpenDate.Typed")) if group_by_date else None
                st = _get_stats(date_key, dept)
                acc_key = (date_key, dept)

                rev = float(row.get("DishDiscountSumInt.withoutVAT") or 0)
                cogs_pct_raw = row.get("ProductCostBase.Percent")
                checks = int(row.get("UniqOrderId.OrdersCount") or 0)

                revenue_accum[acc_key] += rev
                checks_accum[acc_key] += checks
                discount_sales_accum[acc_key] += float(row.get("DiscountSum") or 0)
                if cogs_pct_raw is not None and rev > 0:
                    cost_accum[acc_key] += rev * float(cogs_pct_raw)

                pay_type = row.get("PayTypes", "")
                amount = float(row.get("DishDiscountSumInt") or 0)
                count = int(row.get("UniqOrderId") or 0)
                service_type = row.get("Delivery.ServiceType", "")

                if pay_type == "SailPlay Бонус":
                    st["sailplay"] += amount
                elif pay_type in CASH_PAY_TYPES:
                    st["cash"] += amount
                elif pay_type not in EXCLUDED_PAY_TYPES:
                    st["noncash"] += amount

                if include_pickup and service_type == "PICKUP":
                    st["pickup_count"] += count

            # Финализируем revenue, COGS%, checks
            for acc_key, total_rev in revenue_accum.items():
                date_key, dept = acc_key
                st = _get_stats(date_key, dept)
                st["revenue_net"] = total_rev if total_rev else None
                st["check_count"] = checks_accum[acc_key]
                st["discount_sum"] = discount_sales_accum[acc_key]
                if total_rev > 0 and cost_accum.get(acc_key):
                    st["cogs_pct"] = round(cost_accum[acc_key] / total_rev * 100, 2)

            # --- Парсинг DELIVERIES (скидки + тайминги) ---
            if q_discount:
                # Для таймингов: собираем уникальные заказы
                seen_orders: dict[tuple, bool] = {}  # (date_key, dept, num) → processed
                cooking_mins: dict[tuple, list[float]] = defaultdict(list)  # (date_key, dept)
                wait_mins: dict[tuple, list[float]] = defaultdict(list)
                delivery_mins: dict[tuple, list[float]] = defaultdict(list)

                for row in q_discount:
                    dept = (row.get("Department") or "").strip()
                    if dept not in target:
                        continue
                    date_key = _parse_olap_date(row.get("OpenDate.Typed")) if group_by_date else None
                    st = _get_stats(date_key, dept)

                    # Скидки
                    disc_type = (row.get("OrderDiscount.Type") or "").strip()
                    disc_sum = float(row.get("DiscountSum") or 0)
                    if disc_type and disc_sum > 0:
                        st["discount_types"].append({"type": disc_type, "sum": disc_sum})

                    # Тайминги (per-order, deduplicate by Delivery.Number)
                    if include_timings:
                        num = row.get("Delivery.Number")
                        if num is not None:
                            order_key = (date_key, dept, str(int(num)))
                            if order_key not in seen_orders:
                                seen_orders[order_key] = True
                                acc_key = (date_key, dept)
                                _collect_timing_mins(
                                    row, acc_key,
                                    cooking_mins, wait_mins, delivery_mins,
                                )

                # Финализируем тайминги
                if include_timings:
                    all_keys = set(cooking_mins) | set(wait_mins) | set(delivery_mins)
                    for acc_key in all_keys:
                        date_key, dept = acc_key
                        st = _get_stats(date_key, dept)
                        if cooking_mins[acc_key]:
                            st["avg_cooking_min"] = round(
                                sum(cooking_mins[acc_key]) / len(cooking_mins[acc_key])
                            )
                        if wait_mins[acc_key]:
                            st["avg_wait_min"] = round(
                                sum(wait_mins[acc_key]) / len(wait_mins[acc_key])
                            )
                        if delivery_mins[acc_key]:
                            st["avg_delivery_min"] = round(
                                sum(delivery_mins[acc_key]) / len(delivery_mins[acc_key])
                            )

            # --- Внешние скидки (из Query A pipeline) ---
            if external_discount_types:
                for dept, dt_list in external_discount_types.items():
                    if dept in target:
                        st = _get_stats(None, dept)
                        st["discount_types"] = dt_list

            # --- Агрегируем discount_types и пересчитываем discount_sum ---
            def _finalize_discounts(st: dict) -> None:
                if st["discount_types"]:
                    # Схлопываем дубликаты типов (при per-order Q3 один тип встречается много раз)
                    agg: dict[str, float] = {}
                    for dt in st["discount_types"]:
                        agg[dt["type"]] = agg.get(dt["type"], 0) + dt["sum"]
                    st["discount_types"] = sorted(
                        [{"type": t, "sum": s} for t, s in agg.items()],
                        key=lambda x: x["sum"], reverse=True,
                    )
                    # Пересчитываем из DELIVERIES (SALES даёт завышенное per-dish значение)
                    st["discount_sum"] = round(
                        sum(dt["sum"] for dt in st["discount_types"]), 2
                    )

            if group_by_date:
                for date_key in stats_by_date:
                    for dept in stats_by_date[date_key]:
                        _finalize_discounts(stats_by_date[date_key][dept])
            else:
                for dept in stats:
                    _finalize_discounts(stats[dept])

        except Exception as e:
            logger.error(f"fetch_branch_aggregate [{url}]: {e}")

        if group_by_date:
            return {dk: dict(depts) for dk, depts in stats_by_date.items()}
        return dict(stats)

    results = await asyncio.gather(*[_fetch(srv) for srv in by_server.values()])

    if group_by_date:
        merged_by_date: dict[str, dict[str, dict]] = defaultdict(dict)
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"OLAP Query C ошибка сервера: {result}")
            elif isinstance(result, dict):
                for date_key, depts in result.items():
                    merged_by_date[date_key].update(depts)
        return dict(merged_by_date)

    merged: dict[str, dict] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"OLAP Query C ошибка сервера: {result}")
        elif isinstance(result, dict):
            merged.update(result)
    return merged


# ---------------------------------------------------------------------------
# Query D: fetch_storno_audit
# ---------------------------------------------------------------------------

async def fetch_storno_audit(
    date_from: str,
    date_to: str,
    branches: list[dict] | None = None,
) -> list[dict]:
    """
    Query D: данные для детектора сторно-скидок из SALES reportType.

    Уникальные поля Storned/CashierName/DishSumInt доступны только в SALES,
    поэтому этот запрос не консолидируется с Query A/B.

    Возвращает сырые строки — агрегация в audit.py.
    """
    if branches is None:
        branches = settings.branches

    logger.info(f"OLAP Query D (storno audit): {date_from} — {date_to}")
    by_server = _group_branches_by_server(branches)

    async def _fetch(srv: dict) -> list[dict]:
        try:
            token = await get_bo_token(srv["url"], bo_login=srv["login"], bo_password=srv["password"])
            async with httpx.AsyncClient(verify=False, timeout=60) as client:
                rows = await _execute_olap(
                    srv["url"], token,
                    STORNO_AUDIT_FIELDS,
                    ["DiscountSum", "DishSumInt"],
                    date_from, date_to, client,
                    report_type="SALES",
                )
            target = srv["names"]
            return [r for r in rows if (r.get("Department") or "").strip() in target]
        except Exception as e:
            logger.error(f"fetch_storno_audit [{srv['url']}]: {e}")
            return []

    results = await asyncio.gather(*[_fetch(srv) for srv in by_server.values()])
    rows: list[dict] = []
    for chunk in results:
        rows.extend(chunk)
    logger.info(f"OLAP Query D: получено {len(rows)} строк")
    return rows


# ---------------------------------------------------------------------------
# Мигрированные функции из iiko_bo_olap_v2.py
# ---------------------------------------------------------------------------

async def get_branch_olap_stats(date: datetime, branches: list[dict] | None = None) -> dict[str, dict]:
    """
    Обёртка для /статус: вызывает fetch_branch_aggregate с include_timings=True.
    Совместимый интерфейс с iiko_bo_olap_v2.get_branch_olap_stats().
    """
    date_iso = date.strftime("%Y-%m-%d")
    next_day = (date + timedelta(days=1)).strftime("%Y-%m-%d")
    return await fetch_branch_aggregate(
        date_iso, next_day, branches,
        include_pickup=False,
        include_timings=True,
    )


async def get_all_branches_stats(
    date: datetime,
    branches: list[dict] | None = None,
) -> dict[str, dict]:
    """
    Обёртка для iiko_to_sheets и daily_report: вызывает fetch_branch_aggregate.
    Совместимый интерфейс с iiko_bo_olap_v2.get_all_branches_stats().
    """
    date_iso = date.strftime("%Y-%m-%d")
    next_day = (date + timedelta(days=1)).strftime("%Y-%m-%d")
    return await fetch_branch_aggregate(date_iso, next_day, branches, include_pickup=True)


async def get_payment_breakdown(
    date_from: str,
    date_to: str,
    branches: list[dict] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Разбивка выручки по типам оплаты для каждой точки за период.
    date_from/date_to в ISO: "2026-02-20" / "2026-02-23" (to exclusive).
    Возвращает: {"Барнаул_1 Ана": {"Картой при получении": 492668.0, ...}}
    """
    if branches is None:
        branches = settings.branches
    logger.info(f"OLAP: payment breakdown {date_from} — {date_to}")
    by_server = _group_branches_by_server(branches)

    result: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    async def _fetch_payments(srv: dict):
        token = await get_bo_token(srv["url"], bo_login=srv["login"], bo_password=srv["password"])
        async with httpx.AsyncClient(verify=False, timeout=60) as client:
            rows = await _execute_olap(
                srv["url"], token,
                ["Department", "PayTypes"],
                ["DishDiscountSumInt"],
                date_from, date_to, client,
            )
            for row in rows:
                dept = (row.get("Department") or "").strip()
                if not dept or dept not in srv["names"]:
                    continue
                pay_type = (row.get("PayTypes") or "").strip()
                amount = float(row.get("DishDiscountSumInt") or 0)
                if pay_type and amount:
                    result[dept][pay_type] += amount

    await asyncio.gather(*[_fetch_payments(srv) for srv in by_server.values()],
                         return_exceptions=True)
    return {k: dict(v) for k, v in result.items()}


async def get_online_orders(
    date_from: str,
    date_to: str,
    branches: list[dict] | None = None,
) -> dict[str, dict[str, dict]]:
    """
    Онлайн-заказы (ТБанк: Оплата на сайте + СБП) по точкам.
    date_from/date_to в ISO: "2026-02-17" / "2026-02-25" (to exclusive).
    Возвращает: {"Барнаул_1 Ана": {"90196": {"amount": 1850.0, "date": "2026-02-24"}, ...}}
    """
    if branches is None:
        branches = settings.branches
    logger.info(f"OLAP: online orders {date_from} — {date_to}")
    ONLINE_PAY_TYPES = {"Оплата на сайте", "СБП"}
    by_server = _group_branches_by_server(branches)

    result: dict[str, dict[str, dict]] = defaultdict(dict)

    async def _fetch_online(srv: dict):
        token = await get_bo_token(srv["url"], bo_login=srv["login"], bo_password=srv["password"])
        async with httpx.AsyncClient(verify=False, timeout=60) as client:
            rows = await _execute_olap(
                srv["url"], token,
                ["Department", "Delivery.Number", "PayTypes", "OpenDate.Typed"],
                ["DishDiscountSumInt"],
                date_from, date_to, client,
            )
            for row in rows:
                dept = (row.get("Department") or "").strip()
                if not dept or dept not in srv["names"]:
                    continue
                pay_type = (row.get("PayTypes") or "").strip()
                order_num = str(row.get("Delivery.Number", "")).strip()
                amount = float(row.get("DishDiscountSumInt") or 0)
                order_date = _parse_olap_date(str(row.get("OpenDate.Typed", ""))) or ""
                if pay_type not in ONLINE_PAY_TYPES:
                    continue
                if order_num and amount:
                    if order_num in result[dept]:
                        result[dept][order_num]["amount"] += amount
                    else:
                        result[dept][order_num] = {"amount": amount, "date": order_date}

    await asyncio.gather(*[_fetch_online(srv) for srv in by_server.values()],
                         return_exceptions=True)
    return dict(result)


async def get_discount_breakdown(
    date_from: str,
    date_to: str,
    branches: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """
    Разбивка скидок по типу для каждой точки за период.
    Использует reportType=DELIVERIES — DiscountSum корректен на уровне заказа (не блюда).
    Возвращает: {"Барнаул_1 Ана": [{"type": "Промокод", "sum": 1200.0}, ...]}
    """
    if branches is None:
        branches = settings.branches
    by_server = _group_branches_by_server(branches)
    result: dict[str, list[dict]] = {}

    async def _fetch(srv: dict):
        try:
            token = await get_bo_token(srv["url"], bo_login=srv["login"], bo_password=srv["password"])
            async with httpx.AsyncClient(verify=False, timeout=60) as client:
                rows = await _execute_olap(
                    srv["url"], token,
                    ["Department", "OrderDiscount.Type"],
                    ["DiscountSum"],
                    date_from, date_to, client,
                    report_type="DELIVERIES",
                )
            by_dept: dict[str, list[dict]] = {}
            for row in rows:
                dept = (row.get("Department") or "").strip()
                if not dept or dept not in srv["names"]:
                    continue
                disc_type = (row.get("OrderDiscount.Type") or "").strip()
                disc_sum = float(row.get("DiscountSum") or 0)
                if disc_type and disc_sum > 0:
                    by_dept.setdefault(dept, []).append({"type": disc_type, "sum": disc_sum})
            for dept, items in by_dept.items():
                result[dept] = sorted(items, key=lambda x: x["sum"], reverse=True)
        except Exception as e:
            logger.error(f"get_discount_breakdown [{srv['url']}]: {e}")

    await asyncio.gather(*[_fetch(srv) for srv in by_server.values()])
    return result

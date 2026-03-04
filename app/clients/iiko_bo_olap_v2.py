"""
iiko BO OLAP v2 — JSON-замена 5 XML-пресетов.

Вместо 5 cookie-auth XML-запросов на сервер использует 2 token-auth JSON-запроса:
  Query 1 (core):   groupBy=[Department] → выручка, COGS%, чеки, скидки
  Query 2 (detail): groupBy=[Department, PayTypes, Delivery.ServiceType] → нал/безнал/sailplay, pickup

Выходной формат идентичен iiko_bo_olap.get_all_branches_stats():
  {dept_name: {revenue_net, cogs_pct, check_count, cash, noncash,
               sailplay, discount_sum, discount_types, pickup_count}}

Используется в:
  - app/jobs/daily_report.py
  - app/jobs/iiko_to_sheets.py
  - app/jobs/iiko_status_report.py
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

CASH_PAY_TYPES = {"Наличные"}
EXCLUDED_PAY_TYPES = {"SailPlay Бонус", "(без оплаты)"}


def _olap_body(
    group_fields: list[str],
    agg_fields: list[str],
    date_from: str,
    date_to: str,
) -> dict:
    return {
        "reportType": "SALES",
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


async def _query_olap_v2(
    bo_url: str,
    token: str,
    group_fields: list[str],
    agg_fields: list[str],
    date_from: str,
    date_to: str,
    client: httpx.AsyncClient,
) -> list[dict]:
    body = _olap_body(group_fields, agg_fields, date_from, date_to)
    resp = await client.post(
        f"{bo_url}/api/v2/reports/olap?key={token}",
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        logger.warning(f"OLAP v2 {resp.status_code} от {bo_url}: {resp.text[:200]}")
        return []
    return resp.json().get("data", [])


async def _fetch_from_server(
    bo_url: str,
    target_names: set[str],
    date_from: str,
    date_to: str,
    include_delivery: bool = True,
    bo_login: str | None = None,
    bo_password: str | None = None,
) -> dict[str, dict]:
    """
    2 OLAP v2 запроса к одному серверу.
    Возвращает метрики только для точек из target_names.
    """
    token = await get_bo_token(bo_url, bo_login=bo_login, bo_password=bo_password)
    stats: dict[str, dict] = defaultdict(lambda: {
        "revenue_net": None,
        "cogs_pct": None,
        "check_count": 0,
        "cash": 0.0,
        "noncash": 0.0,
        "sailplay": 0.0,
        "discount_sum": 0.0,
        "discount_types": [],
        "pickup_count": 0,
    })

    async with httpx.AsyncClient(verify=False) as client:
        # --- Query 1: core metrics ---
        core_agg = [
            "DishDiscountSumInt.withoutVAT",
            "ProductCostBase.Percent",
            "UniqOrderId.OrdersCount",
            "DiscountSum",
        ]

        # --- Query 2: payment + delivery breakdown ---
        detail_group = ["Department", "PayTypes"]
        detail_agg = ["DishDiscountSumInt", "UniqOrderId"]
        if include_delivery:
            detail_group.append("Delivery.ServiceType")

        q1, q2 = await asyncio.gather(
            _query_olap_v2(
                bo_url, token, ["Department"], core_agg,
                date_from, date_to, client,
            ),
            _query_olap_v2(
                bo_url, token, detail_group, detail_agg,
                date_from, date_to, client,
            ),
        )

        for row in q1:
            dept = row.get("Department", "").strip()
            if not dept or dept not in target_names:
                continue
            stats[dept]["revenue_net"] = row.get("DishDiscountSumInt.withoutVAT")
            cogs = row.get("ProductCostBase.Percent")
            if cogs is not None:
                stats[dept]["cogs_pct"] = round(cogs * 100, 2)
            stats[dept]["check_count"] = row.get("UniqOrderId.OrdersCount", 0)
            stats[dept]["discount_sum"] = float(row.get("DiscountSum", 0))

        for row in q2:
            dept = row.get("Department", "").strip()
            if not dept or dept not in target_names:
                continue
            pay_type = row.get("PayTypes", "")
            amount = float(row.get("DishDiscountSumInt", 0))
            service_type = row.get("Delivery.ServiceType", "")
            count = int(row.get("UniqOrderId", 0))

            if pay_type == "SailPlay Бонус":
                stats[dept]["sailplay"] += amount
            elif pay_type in CASH_PAY_TYPES:
                stats[dept]["cash"] += amount
            elif pay_type not in EXCLUDED_PAY_TYPES:
                stats[dept]["noncash"] += amount

            if include_delivery and service_type == "PICKUP":
                stats[dept]["pickup_count"] += count

    return dict(stats)


async def get_all_branches_stats(
    date: datetime,
    branches: list[dict] | None = None,
) -> dict[str, dict]:
    """
    Drop-in замена iiko_bo_olap.get_all_branches_stats().
    2 OLAP v2 запроса на сервер (vs 5 XML), параллельно по серверам.

    branches — список точек для запроса. Если None, берёт settings.branches (tenant_id=1).

    Возвращает {dept_name: {revenue_net, cogs_pct, check_count, cash, noncash,
                             sailplay, discount_sum, discount_types, pickup_count}}.
    """
    if branches is None:
        branches = settings.branches
    date_iso = date.strftime("%Y-%m-%d")
    next_day = (date + timedelta(days=1)).strftime("%Y-%m-%d")
    logger.info(f"OLAP v2: запрашиваю метрики за {date_iso}")

    # Группируем по (bo_url, bo_login, bo_password) — каждый тенант со своим логином
    by_server: dict[tuple, dict] = {}  # (bo_url, bo_login, bo_pass) → {names, login, password}
    for branch in branches:
        url = branch.get("bo_url", "")
        if not url:
            logger.warning(f"Точка {branch['name']} без bo_url — пропущена")
            continue
        login = branch.get("bo_login") or ""
        password = branch.get("bo_password") or ""
        key = (url, login, password)
        if key not in by_server:
            by_server[key] = {"names": set(), "login": login or None, "password": password or None}
        by_server[key]["names"].add(branch["name"])

    tasks = [
        _fetch_from_server(url, srv["names"], date_iso, next_day, include_delivery=True,
                           bo_login=srv["login"], bo_password=srv["password"])
        for (url, _, __), srv in by_server.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: dict[str, dict] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"OLAP v2 ошибка сервера: {result}")
        elif isinstance(result, dict):
            merged.update(result)

    return merged


async def get_payment_breakdown(
    date_from: str,
    date_to: str,
    branches: list[dict] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Разбивка выручки по типам оплаты для каждой точки за период.
    date_from/date_to в ISO: "2026-02-20" / "2026-02-23" (to exclusive).
    branches — список точек. Если None, берёт settings.branches (tenant_id=1).
    Возвращает: {"Барнаул_1 Ана": {"Картой при получении": 492668.0, "Сбербанк": 199273.0, ...}}
    """
    if branches is None:
        branches = settings.branches
    logger.info(f"OLAP v2: payment breakdown {date_from} — {date_to}")

    by_server: dict[tuple, dict] = {}
    for branch in branches:
        url = branch.get("bo_url", "")
        if not url:
            continue
        login = branch.get("bo_login") or ""
        password = branch.get("bo_password") or ""
        key = (url, login, password)
        if key not in by_server:
            by_server[key] = {"names": set(), "login": login or None, "password": password or None}
        by_server[key]["names"].add(branch["name"])

    result: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    async def _fetch_payments(bo_url: str, target_names: set[str], bo_login=None, bo_password=None):
        token = await get_bo_token(bo_url, bo_login=bo_login, bo_password=bo_password)
        async with httpx.AsyncClient(verify=False) as client:
            rows = await _query_olap_v2(
                bo_url, token,
                ["Department", "PayTypes"],
                ["DishDiscountSumInt"],
                date_from, date_to, client,
            )
            for row in rows:
                dept = row.get("Department", "").strip()
                if not dept or dept not in target_names:
                    continue
                pay_type = row.get("PayTypes", "").strip()
                amount = float(row.get("DishDiscountSumInt", 0))
                if pay_type and amount:
                    result[dept][pay_type] += amount

    tasks = [
        _fetch_payments(url, srv["names"], srv["login"], srv["password"])
        for (url, _, __), srv in by_server.items()
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    return {k: dict(v) for k, v in result.items()}


async def get_online_orders(
    date_from: str,
    date_to: str,
    branches: list[dict] | None = None,
) -> dict[str, dict[str, dict]]:
    """
    Онлайн-заказы (ТБанк: Оплата на сайте + СБП) по точкам.
    date_from/date_to в ISO: "2026-02-17" / "2026-02-25" (to exclusive).
    branches — список точек. Если None, берёт settings.branches (tenant_id=1).
    Возвращает: {"Барнаул_1 Ана": {"90196": {"amount": 1850.0, "date": "2026-02-24"}, ...}}
    """
    if branches is None:
        branches = settings.branches
    logger.info(f"OLAP v2: online orders {date_from} — {date_to}")
    ONLINE_PAY_TYPES = {"Оплата на сайте", "СБП"}

    by_server: dict[tuple, dict] = {}
    for branch in branches:
        url = branch.get("bo_url", "")
        if not url:
            continue
        login = branch.get("bo_login") or ""
        password = branch.get("bo_password") or ""
        key = (url, login, password)
        if key not in by_server:
            by_server[key] = {"names": set(), "login": login or None, "password": password or None}
        by_server[key]["names"].add(branch["name"])

    result: dict[str, dict[str, dict]] = defaultdict(dict)

    def _olap_date_to_iso(val: str) -> str:
        if not val:
            return ""
        val = str(val).strip()
        if len(val) == 10 and val[4] == "-":
            return val
        try:
            parts = val.split(".")
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        except Exception:
            return val

    async def _fetch_online(bo_url: str, target_names: set[str], bo_login=None, bo_password=None):
        token = await get_bo_token(bo_url, bo_login=bo_login, bo_password=bo_password)
        async with httpx.AsyncClient(verify=False) as client:
            rows = await _query_olap_v2(
                bo_url, token,
                ["Department", "Delivery.Number", "PayTypes", "OpenDate.Typed"],
                ["DishDiscountSumInt"],
                date_from, date_to, client,
            )
            for row in rows:
                dept = row.get("Department", "").strip()
                if not dept or dept not in target_names:
                    continue
                pay_type = row.get("PayTypes", "").strip()
                order_num = str(row.get("Delivery.Number", "")).strip()
                amount = float(row.get("DishDiscountSumInt", 0))
                order_date = _olap_date_to_iso(str(row.get("OpenDate.Typed", "")))
                if pay_type not in ONLINE_PAY_TYPES:
                    continue
                if order_num and amount:
                    if order_num in result[dept]:
                        result[dept][order_num]["amount"] += amount
                    else:
                        result[dept][order_num] = {"amount": amount, "date": order_date}

    tasks = [
        _fetch_online(url, srv["names"], srv["login"], srv["password"])
        for (url, _, __), srv in by_server.items()
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    return dict(result)


async def get_branch_olap_stats(date: datetime) -> dict[str, dict]:
    """
    Drop-in замена iiko_bo_olap.get_branch_olap_stats() (для /статус).
    Без pickup_count (include_delivery=False).
    """
    date_iso = date.strftime("%Y-%m-%d")
    next_day = (date + timedelta(days=1)).strftime("%Y-%m-%d")

    by_url: dict[str, set[str]] = defaultdict(set)
    for branch in settings.branches:
        url = branch.get("bo_url", "")
        if not url:
            continue
        by_url[url].add(branch["name"])

    tasks = [
        _fetch_from_server(url, names, date_iso, next_day, include_delivery=False)
        for url, names in by_url.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: dict[str, dict] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"OLAP v2 ошибка сервера: {result}")
        elif isinstance(result, dict):
            merged.update(result)

    return merged

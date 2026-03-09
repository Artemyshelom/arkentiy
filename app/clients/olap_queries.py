"""
Канонические OLAP v2 запросы — единый источник правды.

4 типа запросов покрывают 100% потребностей:

  Query A — «Заказ» (DELIVERIES, per-order)
    Вся метаданных заказа: клиент, адрес, тайминги, оплата, скидка, отмена.
    Заменяет: olap_enrichment, cancel_sync, backfill Phases 1/4/5/6.

  Query B — «Блюда» (SALES, per-order × dish)
    Состав заказа: блюда, количество, курьер (WaiterName).
    Заменяет: backfill Phases 2/3.

  Query C — «Агрегат по точке» (SALES, per-branch)
    Выручка, COGS%, чеки, нал/безнал, самовывоз за день.
    Заменяет: get_all_branches_stats(), backfill_daily_stats.

  Query D — «Сторно-аудит» (SALES, специальный)
    Поля Storned/CashierName недоступны в других режимах.
    Используется только в audit.py.

Правила использования:
  - Все 4 функции возвращают сырые OLAP-строки (list[dict]).
    Исключение: fetch_branch_aggregate возвращает структурированный dict (как get_all_branches_stats).
  - Агрегацию по заказу делает потребитель.
  - НЕ включать OpenDate.Typed в groupByRowFields — обнуляет Delivery.Number (документированный баг iiko).
  - DiscountSum корректен только в DELIVERIES (per-order), в SALES считается per-dish.
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
    try:
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
# Query C: fetch_branch_aggregate
# ---------------------------------------------------------------------------

async def fetch_branch_aggregate(
    date_from: str,
    date_to: str,
    branches: list[dict] | None = None,
    include_pickup: bool = True,
) -> dict[str, dict]:
    """
    Query C: агрегированные метрики по точке из SALES reportType.

    3 параллельных sub-запроса на сервер:
      C1 (core):    Department → выручка, COGS%, чеки, скидки
      C2 (detail):  Department × PayTypes × ServiceType → нал/безнал/sailplay, pickup
      C3 (discount): Department × OrderDiscount.Type [DELIVERIES] → типы скидок (корректные)

    Возвращает структурированный dict (аналог get_all_branches_stats()):
      {dept_name: {revenue_net, cogs_pct, check_count, cash, noncash,
                   sailplay, discount_sum, discount_types, pickup_count}}

    date_from/date_to: ISO "2026-03-08" / "2026-03-09" (to exclusive).
    include_pickup: False для /статус (без pickup_count).
    """
    if branches is None:
        branches = settings.branches

    logger.info(f"OLAP Query C (branch aggregate): {date_from} — {date_to}")
    by_server = _group_branches_by_server(branches)

    async def _fetch(srv: dict) -> dict[str, dict]:
        url = srv["url"]
        target = srv["names"]
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
        try:
            token = await get_bo_token(url, bo_login=srv["login"], bo_password=srv["password"])
            async with httpx.AsyncClient(verify=False, timeout=60) as client:
                detail_group = ["Department", "PayTypes"]
                if include_pickup:
                    detail_group.append("Delivery.ServiceType")

                q1, q2, q3 = await asyncio.gather(
                    _execute_olap(
                        url, token,
                        ["Department"],
                        ["DishDiscountSumInt.withoutVAT", "ProductCostBase.Percent",
                         "UniqOrderId.OrdersCount", "DiscountSum"],
                        date_from, date_to, client,
                    ),
                    _execute_olap(
                        url, token,
                        detail_group,
                        ["DishDiscountSumInt", "UniqOrderId"],
                        date_from, date_to, client,
                    ),
                    _execute_olap(
                        url, token,
                        ["Department", "OrderDiscount.Type"],
                        ["DiscountSum"],
                        date_from, date_to, client,
                        report_type="DELIVERIES",
                    ),
                )

            for row in q1:
                dept = (row.get("Department") or "").strip()
                if dept not in target:
                    continue
                stats[dept]["revenue_net"] = row.get("DishDiscountSumInt.withoutVAT")
                cogs = row.get("ProductCostBase.Percent")
                if cogs is not None:
                    stats[dept]["cogs_pct"] = round(cogs * 100, 2)
                stats[dept]["check_count"] = row.get("UniqOrderId.OrdersCount", 0)
                stats[dept]["discount_sum"] = float(row.get("DiscountSum", 0))

            for row in q2:
                dept = (row.get("Department") or "").strip()
                if dept not in target:
                    continue
                pay_type = row.get("PayTypes", "")
                amount = float(row.get("DishDiscountSumInt", 0))
                count = int(row.get("UniqOrderId", 0))
                service_type = row.get("Delivery.ServiceType", "")

                if pay_type == "SailPlay Бонус":
                    stats[dept]["sailplay"] += amount
                elif pay_type in CASH_PAY_TYPES:
                    stats[dept]["cash"] += amount
                elif pay_type not in EXCLUDED_PAY_TYPES:
                    stats[dept]["noncash"] += amount

                if include_pickup and service_type == "PICKUP":
                    stats[dept]["pickup_count"] += count

            for row in q3:
                dept = (row.get("Department") or "").strip()
                if dept not in target:
                    continue
                disc_type = (row.get("OrderDiscount.Type") or "").strip()
                disc_sum = float(row.get("DiscountSum", 0))
                if disc_type and disc_sum > 0:
                    stats[dept]["discount_types"].append({"type": disc_type, "sum": disc_sum})

            for dept in stats:
                if stats[dept]["discount_types"]:
                    stats[dept]["discount_types"].sort(key=lambda x: x["sum"], reverse=True)
                    # Пересчитываем discount_sum из DELIVERIES (SALES даёт завышенное per-dish значение)
                    stats[dept]["discount_sum"] = round(
                        sum(dt["sum"] for dt in stats[dept]["discount_types"]), 2
                    )

        except Exception as e:
            logger.error(f"fetch_branch_aggregate [{url}]: {e}")

        return dict(stats)

    results = await asyncio.gather(*[_fetch(srv) for srv in by_server.values()])
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

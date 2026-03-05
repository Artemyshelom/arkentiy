"""
Ежедневное OLAP-обогащение orders_raw.

Расписание: 09:00 лок (перед утренним отчётом в 09:25).

1 OLAP v2 запрос per server → payment_type, discount_type, source,
send_time, service_print_time, cooked_time (CookingFinishTime),
opened_at (OpenTime), pay_breakdown.

Утренний отчёт затем агрегирует из обогащённого orders_raw → daily_stats.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from app.clients.iiko_auth import get_bo_token
from app.config import get_settings
from app.db import get_branches, log_job_finish, log_job_start

logger = logging.getLogger(__name__)

LOCAL_UTC_OFFSET = 7

ENRICHMENT_FIELDS = [
    "Delivery.Number", "Department", "PayTypes",
    "OrderDiscount.Type", "Delivery.SourceKey",
    "Delivery.PrintTime", "Delivery.CookingFinishTime",
    "Delivery.SendTime", "OpenTime",
]


def _olap_body(date_from: str, date_to: str) -> dict:
    return {
        "reportType": "DELIVERIES",
        "buildSummary": "false",
        "groupByRowFields": ENRICHMENT_FIELDS,
        "aggregateFields": ["DishDiscountSumInt", "DiscountSum"],
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


async def _fetch_enrichment(
    bo_url: str, date_from: str, date_to: str,
    bo_login: str | None = None, bo_password: str | None = None,
) -> list[dict]:
    try:
        token = await get_bo_token(bo_url, bo_login=bo_login, bo_password=bo_password)
    except Exception as e:
        logger.warning(f"olap_enrichment: token error {bo_url}: {e}")
        return []

    try:
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            r = await client.post(
                f"{bo_url}/api/v2/reports/olap?key={token}",
                json=_olap_body(date_from, date_to),
                headers={"Content-Type": "application/json"},
            )
            if r.status_code != 200:
                logger.warning(
                    f"olap_enrichment: {r.status_code} from {bo_url}: "
                    f"{r.text[:200]}"
                )
                return []
            return r.json().get("data", [])
    except Exception as e:
        logger.warning(f"olap_enrichment: fetch error {bo_url}: {e}")
        return []


def _aggregate_by_order(rows: list[dict], target_branches: set[str]) -> dict:
    """
    Группирует OLAP-ответ по (branch, delivery_num).
    Возвращает {(branch, num): {payment_type, pay_breakdown, discount_type,
                                 source, send_time, service_print_time,
                                 cooked_time, opened_at}}.
    """
    by_order: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        dept = row.get("Department", "").strip()
        num = row.get("Delivery.Number")
        if not dept or dept not in target_branches or num is None:
            continue
        by_order[(dept, str(int(num)))].append(row)

    result = {}
    for key, order_rows in by_order.items():
        pay_parts: dict[str, float] = {}
        discount_types: list[str] = []
        source = ""
        send_time = ""
        print_time = ""
        cooked_time = ""
        opened_at = ""

        for r in order_rows:
            pay_type = r.get("PayTypes", "")
            amount = float(r.get("DishDiscountSumInt", 0))
            if pay_type and amount:
                pay_parts[pay_type] = pay_parts.get(pay_type, 0) + amount

            dt = r.get("OrderDiscount.Type", "")
            if dt and dt not in discount_types:
                discount_types.append(dt)

            sk = r.get("Delivery.SourceKey")
            if sk and not source:
                source = sk

            st = r.get("Delivery.SendTime")
            if st and not send_time:
                send_time = str(st)

            pt = r.get("Delivery.PrintTime")
            if pt and not print_time:
                print_time = str(pt)

            ct = r.get("Delivery.CookingFinishTime")
            if ct and not cooked_time:
                cooked_time = str(ct)

            ot = r.get("OpenTime")
            if ot and not opened_at:
                opened_at = str(ot)

        main_pay = max(pay_parts, key=pay_parts.get) if pay_parts else ""

        result[key] = {
            "payment_type": main_pay,
            "pay_breakdown": json.dumps(pay_parts, ensure_ascii=False) if pay_parts else "",
            "discount_type": "; ".join(discount_types),
            "source": source,
            "send_time": send_time,
            "service_print_time": print_time,
            "cooked_time": cooked_time,
            "opened_at": opened_at,
        }
    return result


async def _update_orders_raw(enriched: dict, tenant_id: int) -> int:
    """Обновляет orders_raw OLAP-полями (PostgreSQL). Не перезатирает непустые значения."""
    if not enriched:
        return 0
    from app.database_pg import get_pool

    pool = get_pool()
    updated = 0
    for (branch, num), data in enriched.items():
        sets = []
        vals: list = [tenant_id]  # $1 = tenant_id
        idx = 2

        for col, val in [
            ("payment_type", data["payment_type"]),
            ("pay_breakdown", data["pay_breakdown"]),
            ("discount_type", data["discount_type"]),
            ("source", data["source"]),
            ("send_time", data["send_time"]),
            ("service_print_time", data["service_print_time"]),
            ("cooked_time", data["cooked_time"]),
            ("opened_at", data["opened_at"]),
        ]:
            if val:
                sets.append(
                    f"{col} = CASE WHEN {col} IS NULL OR {col} = '' "
                    f"THEN ${idx} ELSE {col} END"
                )
                vals.append(val)
                idx += 1

        if not sets:
            continue

        vals.append(datetime.now(timezone.utc).isoformat())
        updated_at_idx = idx
        idx += 1
        vals.append(branch)
        branch_idx = idx
        idx += 1
        vals.append(num)
        num_idx = idx

        sql = (
            f"UPDATE orders_raw SET {', '.join(sets)}, "
            f"updated_at = ${updated_at_idx} "
            f"WHERE tenant_id = $1 AND branch_name = ${branch_idx} "
            f"AND delivery_num = ${num_idx}"
        )
        result = await pool.execute(sql, *vals)
        count_str = result.split()[-1]
        if count_str.isdigit():
            updated += int(count_str)

    return updated


async def job_olap_enrichment(tenant_id: int = 1) -> None:
    """Основной job: обогащает orders_raw за вчера данными из OLAP v2."""
    log_id = await log_job_start(f"olap_enrichment_t{tenant_id}")

    branches = get_branches(tenant_id)
    if not branches:
        await log_job_finish(log_id, "ok", f"Нет точек для tenant_id={tenant_id}")
        return

    utc_offset = branches[0].get("utc_offset", LOCAL_UTC_OFFSET)
    now_local = datetime.now(timezone.utc) + timedelta(hours=utc_offset)
    yesterday = now_local - timedelta(days=1)
    yesterday_iso = yesterday.strftime("%Y-%m-%d")
    today_iso = now_local.strftime("%Y-%m-%d")

    is_monday = now_local.weekday() == 0
    if is_monday:
        date_from = (now_local - timedelta(days=7)).strftime("%Y-%m-%d")
    else:
        date_from = yesterday_iso

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

    all_enriched: dict = {}
    for (bo_url, _, __), srv in by_server.items():
        rows = await _fetch_enrichment(bo_url, date_from, today_iso, srv["login"], srv["password"])
        enriched = _aggregate_by_order(rows, srv["names"])
        all_enriched.update(enriched)

    total = len(all_enriched)
    updated = await _update_orders_raw(all_enriched, tenant_id)

    period = f"{date_from}..{yesterday_iso}" if is_monday else yesterday_iso
    detail = f"period={period}, orders={total}, updated={updated}"
    logger.info(f"olap_enrichment: {detail}")
    await log_job_finish(log_id, "ok", detail)

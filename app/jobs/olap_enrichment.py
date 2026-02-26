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

import aiosqlite
import httpx

from app.clients.iiko_auth import get_bo_token
from app.config import get_settings
from app.db import DB_PATH, BACKEND, log_job_finish, log_job_start

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
        "reportType": "SALES",
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
    bo_url: str, date_from: str, date_to: str
) -> list[dict]:
    try:
        token = await get_bo_token(bo_url)
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
                send_time = str(st).replace("T", " ").split(".")[0]

            pt = r.get("Delivery.PrintTime")
            if pt and not print_time:
                print_time = str(pt).replace("T", " ").split(".")[0]

            ct = r.get("Delivery.CookingFinishTime")
            if ct and not cooked_time:
                cooked_time = str(ct).replace("T", " ").split(".")[0]

            ot = r.get("OpenTime")
            if ot and not opened_at:
                opened_at = str(ot).replace("T", " ").split(".")[0]

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


async def _update_orders_raw(enriched: dict) -> int:
    """Обновляет orders_raw OLAP-полями. Не перезатирает непустые значения."""
    if not enriched:
        return 0
    if BACKEND != "sqlite":
        logger.warning("olap_enrichment._update_orders_raw: PG backend не реализован, пропуск")
        return 0
    updated = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for (branch, num), data in enriched.items():
            sets = []
            vals = []
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
                        f"THEN ? ELSE {col} END"
                    )
                    vals.append(val)

            if not sets:
                continue

            sql = (
                f"UPDATE orders_raw SET {', '.join(sets)}, "
                f"updated_at = ? "
                f"WHERE branch_name = ? AND delivery_num = ?"
            )
            vals.extend([
                datetime.now(timezone.utc).isoformat(),
                branch,
                num,
            ])
            cursor = await db.execute(sql, vals)
            updated += cursor.rowcount

        await db.commit()
    return updated


async def job_olap_enrichment() -> None:
    """Основной job: обогащает orders_raw за вчера данными из OLAP v2."""
    log_id = await log_job_start("olap_enrichment")
    settings = get_settings()

    now_local = datetime.now(timezone.utc) + timedelta(hours=LOCAL_UTC_OFFSET)
    yesterday = now_local - timedelta(days=1)
    yesterday_iso = yesterday.strftime("%Y-%m-%d")
    today_iso = now_local.strftime("%Y-%m-%d")

    is_monday = now_local.weekday() == 0
    if is_monday:
        date_from = (now_local - timedelta(days=7)).strftime("%Y-%m-%d")
    else:
        date_from = yesterday_iso

    by_url: dict[str, set[str]] = defaultdict(set)
    for branch in settings.branches:
        url = branch.get("bo_url", "")
        if url:
            by_url[url].add(branch["name"])

    all_enriched: dict = {}
    tasks = []
    for bo_url, names in by_url.items():
        tasks.append((bo_url, names))

    for bo_url, names in tasks:
        rows = await _fetch_enrichment(bo_url, date_from, today_iso)
        enriched = _aggregate_by_order(rows, names)
        all_enriched.update(enriched)

    total = len(all_enriched)
    updated = await _update_orders_raw(all_enriched)

    period = f"{date_from}..{yesterday_iso}" if is_monday else yesterday_iso
    detail = f"period={period}, orders={total}, updated={updated}"
    logger.info(f"olap_enrichment: {detail}")
    await log_job_finish(log_id, "ok", detail)

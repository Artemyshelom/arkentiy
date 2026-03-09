"""
backfill_shaburov.py — бэкфилл OLAP-данных для Никиты Шабурова (tenant_id=3).

Запуск на VPS:
    docker compose exec app python -m app.backfill_shaburov

Что делает:
  1. Читает точки Шабурова из iiko_credentials (tenant_id=3)
  2. Для каждого дня с DATE_FROM до вчера:
     - Запрашивает OLAP v2 (выручка, COGS, чеки, скидки)
     - Пишет в daily_stats с tenant_id=3
"""

import asyncio
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_shaburov")

DATE_FROM = date(2026, 2, 1)
TENANT_ID = 3

# ─── iiko auth ────────────────────────────────────────────────────────────────

async def _get_token(bo_url: str, bo_login: str, bo_password: str, client: httpx.AsyncClient) -> str:
    import hashlib
    pw_hash = hashlib.sha1(bo_password.encode()).hexdigest()
    r = await client.get(
        f"{bo_url}/api/auth?login={bo_login}&pass={pw_hash}",
        timeout=30,
    )
    r.raise_for_status()
    return r.text.strip()


# ─── OLAP ─────────────────────────────────────────────────────────────────────

def _olap_body(date_from: str, date_to: str, group_fields: list, agg_fields: list) -> dict:
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


async def _fetch_olap(bo_url: str, token: str, date_from: str, date_to: str, client: httpx.AsyncClient) -> dict:
    """Два OLAP-запроса к серверу, возвращает {dept_name: stats}."""
    stats: dict[str, dict] = defaultdict(lambda: {
        "revenue_net": None, "cogs_pct": None, "check_count": 0,
        "discount_sum": 0.0, "pickup_count": 0,
    })

    # Query 1: core (те же поля что в iiko_bo_olap_v2.py)
    try:
        r1 = await client.post(
            f"{bo_url}/api/v2/reports/olap?key={token}",
            json=_olap_body(date_from, date_to,
                            ["Department"],
                            ["DishDiscountSumInt.withoutVAT",
                             "ProductCostBase.Percent",
                             "UniqOrderId.OrdersCount",
                             "DiscountSum"]),
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        if r1.status_code == 200:
            for row in r1.json().get("data", []):
                dept = row.get("Department", "").strip()
                if not dept:
                    continue
                rev = row.get("DishDiscountSumInt.withoutVAT", 0) or 0
                cogs_raw = row.get("ProductCostBase.Percent")
                disc = row.get("DiscountSum", 0) or 0
                chk = row.get("UniqOrderId.OrdersCount", 0) or 0
                stats[dept]["revenue_net"] = float(rev)
                stats[dept]["cogs_pct"] = round(float(cogs_raw) * 100, 2) if cogs_raw is not None else None
                stats[dept]["check_count"] = int(chk)
                stats[dept]["discount_sum"] = float(disc)
        else:
            logger.warning(f"OLAP core {r1.status_code}: {r1.text[:200]}")
    except Exception as e:
        logger.error(f"OLAP core error: {e}")

    # Query 2: pickup count
    try:
        r2 = await client.post(
            f"{bo_url}/api/v2/reports/olap?key={token}",
            json=_olap_body(date_from, date_to,
                            ["Department", "Delivery.ServiceType"],
                            ["UniqOrderId"]),
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        if r2.status_code == 200:
            for row in r2.json().get("data", []):
                dept = row.get("Department", "").strip()
                if not dept:
                    continue
                svc = (row.get("Delivery.ServiceType") or "").upper()
                if svc == "PICKUP":
                    stats[dept]["pickup_count"] += int(row.get("UniqOrderId", 0) or 0)
    except Exception as e:
        logger.warning(f"OLAP pickup error: {e}")

    return dict(stats)


# ─── DB helpers ───────────────────────────────────────────────────────────────

async def upsert_daily_stat(pool: asyncpg.Pool, tenant_id: int, branch_name: str, d: str, s: dict):
    from datetime import date as date_type
    d_obj = date_type.fromisoformat(d)
    await pool.execute(
        """INSERT INTO daily_stats
            (tenant_id, branch_name, date, orders_count, revenue, avg_check,
             cogs_pct, discount_sum, pickup_count, updated_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now())
           ON CONFLICT (tenant_id, branch_name, date)
           DO UPDATE SET
             orders_count = EXCLUDED.orders_count,
             revenue      = EXCLUDED.revenue,
             avg_check    = EXCLUDED.avg_check,
             cogs_pct     = EXCLUDED.cogs_pct,
             discount_sum = EXCLUDED.discount_sum,
             pickup_count = EXCLUDED.pickup_count,
             updated_at   = now()
        """,
        tenant_id,
        branch_name,
        d_obj,
        s.get("check_count") or 0,
        s.get("revenue_net") or 0.0,
        round((s["revenue_net"] or 0) / s["check_count"]) if s.get("check_count") else 0,
        s.get("cogs_pct"),
        s.get("discount_sum") or 0.0,
        s.get("pickup_count") or 0,
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    db_url = os.environ["DATABASE_URL"]
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)

    # Получаем точки Шабурова
    branches = await pool.fetch(
        "SELECT branch_name, bo_url, bo_login, bo_password FROM iiko_credentials "
        "WHERE tenant_id = $1 AND is_active = true ORDER BY branch_name",
        TENANT_ID,
    )
    logger.info(f"Точки Шабурова: {[b['branch_name'] for b in branches]}")

    # По серверам группируем точки
    by_server: dict[str, list] = defaultdict(list)
    for b in branches:
        by_server[(b["bo_url"], b["bo_login"], b["bo_password"])].append(b["branch_name"])

    today = date.today()
    yesterday = today - timedelta(days=1)

    current = DATE_FROM
    total_ok = 0
    total_err = 0

    while current <= yesterday:
        date_str = current.strftime("%Y-%m-%d")
        next_str = (current + timedelta(days=1)).strftime("%Y-%m-%d")
        logger.info(f"── {date_str} ──────────────────")

        async with httpx.AsyncClient(verify=False, timeout=60) as client:
            for (bo_url, login, password), branch_names in by_server.items():
                try:
                    token = await _get_token(bo_url, login, password, client)
                    stats = await _fetch_olap(bo_url, token, date_str, next_str, client)

                    for branch_name in branch_names:
                        s = stats.get(branch_name)
                        if not s or not s.get("revenue_net"):
                            logger.warning(f"  {branch_name} {date_str}: нет данных в OLAP")
                            continue
                        await upsert_daily_stat(pool, TENANT_ID, branch_name, date_str, s)
                        rev = s.get("revenue_net", 0)
                        chk = s.get("check_count", 0)
                        logger.info(
                            f"  ✓ {branch_name}: выручка={int(rev):,}, чеков={chk}, "
                            f"COGS={s.get('cogs_pct'):.1f}%" if s.get('cogs_pct') else
                            f"  ✓ {branch_name}: выручка={int(rev):,}, чеков={chk}"
                        )
                        total_ok += 1

                except Exception as e:
                    logger.error(f"  Ошибка {bo_url} {date_str}: {e}")
                    total_err += 1

        current += timedelta(days=1)
        await asyncio.sleep(0.5)  # небольшая пауза между днями

    logger.info(f"\nБэкфилл завершён: {total_ok} записей OK, {total_err} ошибок")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

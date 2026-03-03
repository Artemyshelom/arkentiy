"""
backfill_orders_shaburov.py — бэкфилл orders_raw для Шабурова (tenant_id=3).

Использует OLAP v2 с полями индивидуальных заказов: Delivery.Number, CustomerPhone, etc.
Заполняет: delivery_num, branch_name, client_phone, sum, date, actual_time,
           delivery_address, is_self_service, status, cancel_reason.

Запуск в контейнере:
    docker compose exec app python -m app.onboarding.backfill_orders_shaburov

Возобновляемый: при повторном запуске пропускает уже обработанные дни.
"""

import asyncio
import hashlib
import json
import logging
import os
from collections import defaultdict
from datetime import date, timedelta

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_orders_shaburov")

DATE_FROM = date(2026, 2, 1)
TENANT_ID = 3
PROGRESS_FILE = "/app/data/backfill_orders_shaburov_progress.json"

# Ижевск OLAP таймаутит на исторических данных — пропускаем до выяснения
SKIP_CITIES = {"Ижевск"}

OLAP_GROUP_FIELDS = [
    "Delivery.Number",
    "Department",
    "Delivery.CustomerPhone",
    "Delivery.CancelCause",
    "Delivery.ActualTime",
    "Delivery.Address",
    "Delivery.ServiceType",
]
# OpenDate is intentionally excluded: adding it to groupByRowFields causes
# Delivery.Number to become null on some iiko server versions (e.g. Kansk).
# Date is taken from the filter parameter instead.


def _load_progress() -> set:
    try:
        with open(PROGRESS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_progress(done: set) -> None:
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(sorted(done), f)


async def _get_token(bo_url: str, login: str, password: str, client: httpx.AsyncClient) -> str:
    pw_hash = hashlib.sha1(password.encode()).hexdigest()
    r = await client.get(f"{bo_url}/api/auth?login={login}&pass={pw_hash}", timeout=30)
    r.raise_for_status()
    return r.text.strip()


async def _fetch_orders(
    bo_url: str, token: str, date_from: str, date_to: str, client: httpx.AsyncClient
) -> list[dict]:
    body = {
        "reportType": "SALES",
        "buildSummary": "false",
        "groupByRowFields": OLAP_GROUP_FIELDS,
        "aggregateFields": ["DishDiscountSumInt"],
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
    try:
        r = await client.post(
            f"{bo_url}/api/v2/reports/olap?key={token}",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        if r.status_code == 200:
            return r.json().get("data", [])
        logger.warning(f"OLAP {bo_url} {date_from}: {r.status_code} {r.text[:100]}")
    except Exception as e:
        logger.error(f"OLAP error {bo_url} {date_from}: {e}")
    return []


def _parse_rows(rows: list[dict], branch_names: set, order_date: date) -> list[dict]:
    """Convert OLAP rows to orders_raw dicts. Skip rows without delivery_num."""
    result = []
    for row in rows:
        num = row.get("Delivery.Number")
        dept = (row.get("Department") or "").strip()
        if not num or dept not in branch_names:
            continue

        cancel_cause = row.get("Delivery.CancelCause")
        service_type = row.get("Delivery.ServiceType") or ""

        result.append({
            "delivery_num": str(num),
            "branch_name": dept,
            "client_phone": row.get("Delivery.CustomerPhone") or "",
            "sum": float(row.get("DishDiscountSumInt") or 0),
            "date": order_date,
            "actual_time": row.get("Delivery.ActualTime") or "",
            "delivery_address": row.get("Delivery.Address") or "",
            "is_self_service": service_type.upper() == "PICKUP",
            "status": "Отменена" if cancel_cause else "Доставлена",
            "cancel_reason": cancel_cause or "",
        })
    return result


async def _upsert_orders(pool: asyncpg.Pool, rows: list[dict]) -> int:
    if not rows:
        return 0
    count = 0
    for r in rows:
        await pool.execute(
            """
            INSERT INTO orders_raw
                (tenant_id, branch_name, delivery_num, client_phone, sum, date,
                 actual_time, delivery_address, is_self_service, status, cancel_reason, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now())
            ON CONFLICT (tenant_id, branch_name, delivery_num)
            DO UPDATE SET
                client_phone    = COALESCE(NULLIF(EXCLUDED.client_phone,''), orders_raw.client_phone),
                sum             = EXCLUDED.sum,
                actual_time     = EXCLUDED.actual_time,
                delivery_address= EXCLUDED.delivery_address,
                is_self_service = EXCLUDED.is_self_service,
                status          = EXCLUDED.status,
                cancel_reason   = EXCLUDED.cancel_reason,
                updated_at      = now()
            """,
            TENANT_ID,
            r["branch_name"],
            r["delivery_num"],
            r["client_phone"],
            r["sum"],
            r["date"],
            r["actual_time"],
            r["delivery_address"],
            r["is_self_service"],
            r["status"],
            r["cancel_reason"],
        )
        count += 1
    return count


async def main():
    db_url = os.environ["DATABASE_URL"]
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)

    branches = await pool.fetch(
        "SELECT branch_name, city, bo_url, bo_login, bo_password FROM iiko_credentials "
        "WHERE tenant_id=$1 AND is_active=true ORDER BY branch_name",
        TENANT_ID,
    )
    skipped = [b["branch_name"] for b in branches if b["city"] in SKIP_CITIES]
    branches = [b for b in branches if b["city"] not in SKIP_CITIES]
    logger.info(f"Ветки Шабурова: {[b['branch_name'] for b in branches]}")
    if skipped:
        logger.info(f"Пропущено (SKIP_CITIES): {skipped}")

    by_server: dict[tuple, list] = defaultdict(list)
    for b in branches:
        key = (b["bo_url"], b["bo_login"], b["bo_password"])
        by_server[key].append(b["branch_name"])

    today = date.today()
    yesterday = today - timedelta(days=1)

    done = _load_progress()
    all_dates = []
    current = DATE_FROM
    while current <= yesterday:
        all_dates.append(current)
        current += timedelta(days=1)

    remaining = [d for d in all_dates if str(d) not in done]
    logger.info(f"Дней всего: {len(all_dates)}, уже: {len(done)}, осталось: {len(remaining)}")

    total_ok = 0
    total_err = 0

    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        for (bo_url, login, password), branch_names in by_server.items():
            branch_set = set(branch_names)
            try:
                token = await _get_token(bo_url, login, password, client)
            except Exception as e:
                logger.error(f"Auth failed {bo_url}: {e}")
                continue

            for d in remaining:
                date_str = str(d)
                next_str = str(d + timedelta(days=1))
                try:
                    rows = await _fetch_orders(bo_url, token, date_str, next_str, client)
                    parsed = _parse_rows(rows, branch_set, d)
                    upserted = await _upsert_orders(pool, parsed)

                    by_branch = defaultdict(int)
                    for r in parsed:
                        by_branch[r["branch_name"]] += 1
                    branch_str = " | ".join(f"{k}: {v}" for k, v in sorted(by_branch.items()))
                    logger.info(f"  ✓ {date_str} → {upserted} заказов | {branch_str}")
                    total_ok += upserted
                    done.add(date_str)
                    _save_progress(done)
                except Exception as e:
                    logger.error(f"  Ошибка {date_str} {bo_url}: {e}")
                    total_err += 1

                await asyncio.sleep(0.5)

    # Final stats
    stats = await pool.fetch(
        "SELECT branch_name, COUNT(*) as cnt FROM orders_raw WHERE tenant_id=$1 GROUP BY branch_name ORDER BY branch_name",
        TENANT_ID,
    )
    logger.info(f"\nБэкфилл завершён: {total_ok} записей OK, {total_err} ошибок")
    for s in stats:
        logger.info(f"  {s['branch_name']}: {s['cnt']} заказов")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

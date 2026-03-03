"""
backfill_orders_shaburov.py — бэкфилл orders_raw для Шабурова (tenant_id=3).

Phase 1 — order-level (возобновляемый по дням):
  Заполняет: delivery_num, branch_name, client_phone, sum, date, actual_time,
             delivery_address, is_self_service, status, cancel_reason.

Phase 2 — dish-level enrichment (каждый раз для пустых items):
  Дополняет: items → JSON [{name, qty}] из OLAP DishName per Delivery.Number.
  Идёт по неделям — один запрос = 7 дней.

Phase 3 — courier-level enrichment (каждый раз для пустых courier):
  Дополняет: courier (имя курьера из WaiterName).
  Идёт по неделям.

Phase 4 — planned_time enrichment (каждый раз для пустых planned_time):
  Дополняет: planned_time из Delivery.ExpectedTime.
  Идёт по неделям.

Phase 5 — client_name enrichment (каждый раз для пустых client_name):
  Дополняет: client_name из Delivery.CustomerName.
  Идёт по неделям.

Запуск в контейнере:
    docker compose exec app python -m app.onboarding.backfill_orders_shaburov

Возобновляемый: Phase 1 пропускает уже обработанные дни (progress.json).
Phase 2/3/4/5 всегда запускаются, UPDATE только там где поле IS NULL/empty.
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

# Phase 1: order-level fields (только order-level атрибуты → правильная сумма)
OLAP_ORDER_FIELDS = [
    "Delivery.Number",
    "Department",
    "Delivery.CustomerPhone",
    "Delivery.CancelCause",
    "Delivery.ActualTime",
    "Delivery.Address",
    "Delivery.ServiceType",
]
# ВАЖНО: WaiterName НЕ добавлять в OLAP_ORDER_FIELDS — обнуляет DishDiscountSumInt!
# WaiterName = отдельный Phase 3 запрос.
# Delivery.ExpectedDeliveryTime — 400 Unknown field (нет в этой версии iiko).
# Delivery.ExpectedTime — РАБОТАЕТ, но только как dimension (groupByRowFields), Phase 4.
# OpenDate — null Delivery.Number на Канске, не использовать.

# Phase 2: dish-level fields
OLAP_DISH_FIELDS = [
    "Delivery.Number",
    "Department",
    "DishName",
]

# Phase 3: courier-level (WaiterName отдельно от суммы)
OLAP_COURIER_FIELDS = [
    "Delivery.Number",
    "Department",
    "WaiterName",
]

# Phase 4: planned_time (Delivery.ExpectedTime — работает как dimension-поле)
OLAP_PLANNED_FIELDS = [
    "Delivery.Number",
    "Department",
    "Delivery.ExpectedTime",
]

# Phase 5: client_name (Delivery.CustomerName — dimension поле)
OLAP_CLIENT_NAME_FIELDS = [
    "Delivery.Number",
    "Department",
    "Delivery.CustomerName",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _date_filter(date_from: str, date_to: str) -> dict:
    return {
        "OpenDate.Typed": {
            "filterType": "DateRange",
            "periodType": "CUSTOM",
            "from": date_from,
            "to": date_to,
            "includeLow": "true",
            "includeHigh": "false",
        }
    }


# ---------------------------------------------------------------------------
# Phase 1 — order-level
# ---------------------------------------------------------------------------

async def _fetch_orders(
    bo_url: str, token: str, date_from: str, date_to: str, client: httpx.AsyncClient
) -> list[dict]:
    body = {
        "reportType": "SALES",
        "buildSummary": "false",
        "groupByRowFields": OLAP_ORDER_FIELDS,
        "aggregateFields": ["DishDiscountSumInt"],
        "filters": _date_filter(date_from, date_to),
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
        logger.warning(f"OLAP orders {bo_url} {date_from}: {r.status_code} {r.text[:120]}")
    except Exception as e:
        logger.error(f"OLAP orders error {bo_url} {date_from}: {e}")
    return []


def _parse_orders(rows: list[dict], branch_names: set, order_date: date) -> list[dict]:
    result = []
    for row in rows:
        num = row.get("Delivery.Number")
        dept = (row.get("Department") or "").strip()
        if not num or dept not in branch_names:
            continue

        cancel_cause = row.get("Delivery.CancelCause")
        service_type = row.get("Delivery.ServiceType") or ""

        result.append({
            "delivery_num": str(int(num)),
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
                 actual_time, delivery_address, is_self_service,
                 status, cancel_reason, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now())
            ON CONFLICT (tenant_id, branch_name, delivery_num)
            DO UPDATE SET
                client_phone     = COALESCE(NULLIF(EXCLUDED.client_phone,''), orders_raw.client_phone),
                sum              = CASE WHEN EXCLUDED.sum > 0 THEN EXCLUDED.sum ELSE orders_raw.sum END,
                actual_time      = COALESCE(NULLIF(EXCLUDED.actual_time,''), orders_raw.actual_time),
                delivery_address = COALESCE(NULLIF(EXCLUDED.delivery_address,''), orders_raw.delivery_address),
                is_self_service  = EXCLUDED.is_self_service,
                status           = EXCLUDED.status,
                cancel_reason    = EXCLUDED.cancel_reason,
                updated_at       = now()
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


# ---------------------------------------------------------------------------
# Phase 2 — dish-level items enrichment
# ---------------------------------------------------------------------------

async def _fetch_dishes(
    bo_url: str, token: str, date_from: str, date_to: str, client: httpx.AsyncClient
) -> list[dict]:
    """One row per (Delivery.Number, DishName) — used to build items JSON."""
    body = {
        "reportType": "SALES",
        "buildSummary": "false",
        "groupByRowFields": OLAP_DISH_FIELDS,
        "aggregateFields": ["DishDiscountSumInt"],
        "filters": _date_filter(date_from, date_to),
    }
    try:
        r = await client.post(
            f"{bo_url}/api/v2/reports/olap?key={token}",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=90,
        )
        if r.status_code == 200:
            return r.json().get("data", [])
        logger.warning(f"OLAP dishes {bo_url} {date_from}: {r.status_code} {r.text[:120]}")
    except Exception as e:
        logger.error(f"OLAP dishes error {bo_url} {date_from}: {e}")
    return []


def _build_items_map(rows: list[dict], branch_names: set) -> dict[tuple, list[dict]]:
    """Returns {(branch_name, delivery_num): [{name, qty}]}."""
    by_order: dict[tuple, list] = defaultdict(list)
    for row in rows:
        num = row.get("Delivery.Number")
        dept = (row.get("Department") or "").strip()
        dish = (row.get("DishName") or "").strip()
        if not num or dept not in branch_names or not dish:
            continue
        key = (dept, str(int(num)))
        # qty: OLAP doesn't give exact units per dish in this grouping,
        # but each unique (order, dish) row = at least 1 unit.
        by_order[key].append({"name": dish, "qty": 1})
    return dict(by_order)


async def _update_items(pool: asyncpg.Pool, items_map: dict[tuple, list]) -> int:
    """UPDATE orders_raw.items where currently empty. Returns updated count."""
    if not items_map:
        return 0
    updated = 0
    for (branch, num), dishes in items_map.items():
        items_json = json.dumps(dishes, ensure_ascii=False)
        result = await pool.execute(
            """
            UPDATE orders_raw
            SET items = $1, updated_at = now()
            WHERE tenant_id = $2
              AND branch_name = $3
              AND delivery_num = $4
              AND (items IS NULL OR items = '')
            """,
            items_json, TENANT_ID, branch, num,
        )
        # result is "UPDATE N"
        if result and result.split()[-1] != "0":
            updated += 1
    return updated


async def _phase3_enrich_courier(
    pool: asyncpg.Pool,
    by_server: dict[tuple, list],
    all_dates: list[date],
) -> None:
    """Fetch WaiterName weekly (separately from sum) and update orders_raw.courier."""
    logger.info("\n=== Phase 3: enrichment courier (WaiterName) ===")

    empty_count = await pool.fetchval(
        "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (courier IS NULL OR courier='')",
        TENANT_ID,
    )
    logger.info(f"Заказов без курьера: {empty_count}")
    if empty_count == 0:
        logger.info("Все заказы уже имеют курьера — Phase 3 пропущена")
        return

    total_updated = 0

    async with httpx.AsyncClient(verify=False, timeout=90) as client:
        for (bo_url, login, password), branch_names in by_server.items():
            branch_set = set(branch_names)
            try:
                token = await _get_token(bo_url, login, password, client)
            except Exception as e:
                logger.error(f"Phase 3 auth failed {bo_url}: {e}")
                continue

            week_start = all_dates[0]
            today = date.today()
            while week_start <= all_dates[-1]:
                week_end = min(week_start + timedelta(days=7), today)
                body = {
                    "reportType": "SALES",
                    "buildSummary": "false",
                    "groupByRowFields": OLAP_COURIER_FIELDS,
                    "aggregateFields": ["DishDiscountSumInt"],
                    "filters": _date_filter(str(week_start), str(week_end)),
                }
                try:
                    r = await client.post(
                        f"{bo_url}/api/v2/reports/olap?key={token}",
                        json=body,
                        headers={"Content-Type": "application/json"},
                        timeout=90,
                    )
                    rows = r.json().get("data", []) if r.status_code == 200 else []
                except Exception as e:
                    logger.error(f"Phase 3 OLAP error {bo_url} {week_start}: {e}")
                    rows = []

                # Build {(branch, num): courier_name}
                courier_map: dict[tuple, str] = {}
                for row in rows:
                    num = row.get("Delivery.Number")
                    dept = (row.get("Department") or "").strip()
                    waiter = (row.get("WaiterName") or "").strip()
                    if num and dept in branch_set and waiter:
                        courier_map[(dept, str(int(num)))] = waiter

                updated = 0
                for (branch, num), courier in courier_map.items():
                    result = await pool.execute(
                        """UPDATE orders_raw SET courier=$1, updated_at=now()
                           WHERE tenant_id=$2 AND branch_name=$3 AND delivery_num=$4
                             AND (courier IS NULL OR courier='')""",
                        courier, TENANT_ID, branch, num,
                    )
                    if result and result.split()[-1] != "0":
                        updated += 1

                logger.info(f"  {week_start}..{week_end}: {len(courier_map)} с курьером, обновлено {updated}")
                total_updated += updated
                week_start += timedelta(days=7)
                await asyncio.sleep(0.5)

    remaining = await pool.fetchval(
        "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (courier IS NULL OR courier='')",
        TENANT_ID,
    )
    logger.info(f"Phase 3 завершена: обновлено {total_updated}, без курьера осталось {remaining}")


async def _phase2_enrich_items(
    pool: asyncpg.Pool,
    by_server: dict[tuple, list],
    all_dates: list[date],
) -> None:
    """Fetch dish-level OLAP weekly and update orders_raw.items."""
    logger.info("\n=== Phase 2: enrichment items ===")

    # Check how many orders have no items yet
    empty_count = await pool.fetchval(
        "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (items IS NULL OR items='')",
        TENANT_ID,
    )
    logger.info(f"Заказов без состава: {empty_count}")
    if empty_count == 0:
        logger.info("Все заказы уже имеют состав — Phase 2 пропущена")
        return

    total_updated = 0

    async with httpx.AsyncClient(verify=False, timeout=90) as client:
        for (bo_url, login, password), branch_names in by_server.items():
            branch_set = set(branch_names)
            try:
                token = await _get_token(bo_url, login, password, client)
            except Exception as e:
                logger.error(f"Phase 2 auth failed {bo_url}: {e}")
                continue

            # Process in weekly chunks to reduce OLAP call count
            week_start = all_dates[0]
            today = date.today()
            while week_start <= all_dates[-1]:
                week_end = min(week_start + timedelta(days=7), today)
                date_from = str(week_start)
                date_to = str(week_end)

                rows = await _fetch_dishes(bo_url, token, date_from, date_to, client)
                items_map = _build_items_map(rows, branch_set)
                updated = await _update_items(pool, items_map)

                logger.info(f"  {date_from}..{date_to}: {len(items_map)} заказов с блюдами, обновлено {updated}")
                total_updated += updated
                week_start += timedelta(days=7)
                await asyncio.sleep(0.5)

    remaining = await pool.fetchval(
        "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (items IS NULL OR items='')",
        TENANT_ID,
    )
    logger.info(f"Phase 2 завершена: обновлено {total_updated}, без состава осталось {remaining}")


# ---------------------------------------------------------------------------
# Phase 4 — planned_time enrichment
# ---------------------------------------------------------------------------

async def _phase4_enrich_planned(
    pool: asyncpg.Pool,
    by_server: dict[tuple, list],
    all_dates: list[date],
) -> None:
    """Fetch Delivery.ExpectedTime weekly and update orders_raw.planned_time."""
    logger.info("\n=== Phase 4: enrichment planned_time (Delivery.ExpectedTime) ===")

    empty_count = await pool.fetchval(
        "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (planned_time IS NULL OR planned_time='')",
        TENANT_ID,
    )
    logger.info(f"Заказов без planned_time: {empty_count}")
    if empty_count == 0:
        logger.info("Все заказы уже имеют planned_time — Phase 4 пропущена")
        return

    total_updated = 0

    async with httpx.AsyncClient(verify=False, timeout=90) as client:
        for (bo_url, login, password), branch_names in by_server.items():
            branch_set = set(branch_names)
            try:
                token = await _get_token(bo_url, login, password, client)
            except Exception as e:
                logger.error(f"Phase 4 auth failed {bo_url}: {e}")
                continue

            week_start = all_dates[0]
            today = date.today()
            while week_start <= all_dates[-1]:
                week_end = min(week_start + timedelta(days=7), today)
                body = {
                    "reportType": "DELIVERIES",
                    "buildSummary": "false",
                    "groupByRowFields": OLAP_PLANNED_FIELDS,
                    "aggregateFields": ["DishDiscountSumInt"],
                    "filters": _date_filter(str(week_start), str(week_end)),
                }
                try:
                    r = await client.post(
                        f"{bo_url}/api/v2/reports/olap?key={token}",
                        json=body,
                        headers={"Content-Type": "application/json"},
                        timeout=90,
                    )
                    rows = r.json().get("data", []) if r.status_code == 200 else []
                    if r.status_code != 200:
                        logger.warning(f"Phase 4 OLAP {bo_url} {week_start}: {r.status_code} {r.text[:100]}")
                except Exception as e:
                    logger.error(f"Phase 4 OLAP error {bo_url} {week_start}: {e}")
                    rows = []

                planned_map: dict[tuple, str] = {}
                for row in rows:
                    num = row.get("Delivery.Number")
                    dept = (row.get("Department") or "").strip()
                    expected = (row.get("Delivery.ExpectedTime") or "").strip()
                    if num and dept in branch_set and expected:
                        planned_map[(dept, str(int(num)))] = expected

                updated = 0
                for (branch, num), planned_time in planned_map.items():
                    result = await pool.execute(
                        """UPDATE orders_raw SET planned_time=$1, updated_at=now()
                           WHERE tenant_id=$2 AND branch_name=$3 AND delivery_num=$4
                             AND (planned_time IS NULL OR planned_time='')""",
                        planned_time, TENANT_ID, branch, num,
                    )
                    if result and result.split()[-1] != "0":
                        updated += 1

                logger.info(f"  {week_start}..{week_end}: {len(planned_map)} с плановым, обновлено {updated}")
                total_updated += updated
                week_start += timedelta(days=7)
                await asyncio.sleep(0.5)

    remaining = await pool.fetchval(
        "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (planned_time IS NULL OR planned_time='')",
        TENANT_ID,
    )
    logger.info(f"Phase 4 завершена: обновлено {total_updated}, без planned_time осталось {remaining}")


async def _phase5_enrich_client_name(
    pool: asyncpg.Pool,
    by_server: dict[tuple, list],
    all_dates: list[date],
) -> None:
    """Fetch Delivery.CustomerName weekly and update orders_raw.client_name."""
    logger.info("\n=== Phase 5: enrichment client_name (Delivery.CustomerName) ===")

    empty_count = await pool.fetchval(
        "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (client_name IS NULL OR client_name='')",
        TENANT_ID,
    )
    logger.info(f"Заказов без client_name: {empty_count}")
    if empty_count == 0:
        logger.info("Все заказы уже имеют client_name — Phase 5 пропущена")
        return

    total_updated = 0

    async with httpx.AsyncClient(verify=False, timeout=90) as client:
        for (bo_url, login, password), branch_names in by_server.items():
            branch_set = set(branch_names)
            try:
                token = await _get_token(bo_url, login, password, client)
            except Exception as e:
                logger.error(f"Phase 5 auth failed {bo_url}: {e}")
                continue

            week_start = all_dates[0]
            today = date.today()
            while week_start <= all_dates[-1]:
                week_end = min(week_start + timedelta(days=7), today)
                body = {
                    "reportType": "DELIVERIES",
                    "buildSummary": "false",
                    "groupByRowFields": OLAP_CLIENT_NAME_FIELDS,
                    "aggregateFields": ["DishDiscountSumInt"],
                    "filters": _date_filter(str(week_start), str(week_end)),
                }
                try:
                    r = await client.post(
                        f"{bo_url}/api/v2/reports/olap?key={token}",
                        json=body,
                        headers={"Content-Type": "application/json"},
                        timeout=90,
                    )
                    rows = r.json().get("data", []) if r.status_code == 200 else []
                    if r.status_code != 200:
                        logger.warning(f"Phase 5 OLAP {bo_url} {week_start}: {r.status_code} {r.text[:100]}")
                except Exception as e:
                    logger.error(f"Phase 5 OLAP error {bo_url} {week_start}: {e}")
                    rows = []

                client_name_map: dict[tuple, str] = {}
                for row in rows:
                    num = row.get("Delivery.Number")
                    dept = (row.get("Department") or "").strip()
                    name = (row.get("Delivery.CustomerName") or "").strip()
                    if num and dept in branch_set and name:
                        # Пропускаем анонимные "GUEST" заказы
                        if not name.startswith("GUEST"):
                            client_name_map[(dept, str(int(num)))] = name

                updated = 0
                for (branch, num), client_name in client_name_map.items():
                    result = await pool.execute(
                        """UPDATE orders_raw SET client_name=$1, updated_at=now()
                           WHERE tenant_id=$2 AND branch_name=$3 AND delivery_num=$4
                             AND (client_name IS NULL OR client_name='')""",
                        client_name, TENANT_ID, branch, num,
                    )
                    if result and result.split()[-1] != "0":
                        updated += 1

                logger.info(f"  {week_start}..{week_end}: {len(client_name_map)} с именем, обновлено {updated}")
                total_updated += updated
                week_start += timedelta(days=7)
                await asyncio.sleep(0.5)

    remaining = await pool.fetchval(
        "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (client_name IS NULL OR client_name='')",
        TENANT_ID,
    )
    logger.info(f"Phase 5 завершена: обновлено {total_updated}, без client_name осталось {remaining}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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

    all_dates: list[date] = []
    current = DATE_FROM
    while current <= yesterday:
        all_dates.append(current)
        current += timedelta(days=1)

    # -----------------------------------------------------------------------
    # Phase 1 — order-level upsert
    # -----------------------------------------------------------------------
    done = _load_progress()
    remaining_days = [d for d in all_dates if str(d) not in done]
    logger.info(
        f"\n=== Phase 1: order-level ==="
        f"\nДней всего: {len(all_dates)}, уже: {len(done)}, осталось: {len(remaining_days)}"
    )

    total_ok = 0
    total_err = 0

    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        for (bo_url, login, password), branch_names in by_server.items():
            branch_set = set(branch_names)
            try:
                token = await _get_token(bo_url, login, password, client)
            except Exception as e:
                logger.error(f"Phase 1 auth failed {bo_url}: {e}")
                continue

            for d in remaining_days:
                date_str = str(d)
                next_str = str(d + timedelta(days=1))
                try:
                    rows = await _fetch_orders(bo_url, token, date_str, next_str, client)
                    parsed = _parse_orders(rows, branch_set, d)
                    upserted = await _upsert_orders(pool, parsed)

                    by_branch: dict[str, int] = defaultdict(int)
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

                await asyncio.sleep(0.3)

    logger.info(f"Phase 1 завершена: {total_ok} OK, {total_err} ошибок")

    # -----------------------------------------------------------------------
    # Phase 2 — enrich items (dish composition)
    # -----------------------------------------------------------------------
    await _phase2_enrich_items(pool, by_server, all_dates)

    # -----------------------------------------------------------------------
    # Phase 3 — enrich courier (WaiterName, отдельно от суммы)
    # -----------------------------------------------------------------------
    await _phase3_enrich_courier(pool, by_server, all_dates)

    # -----------------------------------------------------------------------
    # Phase 4 — enrich planned_time (Delivery.ExpectedTime)
    # -----------------------------------------------------------------------
    await _phase4_enrich_planned(pool, by_server, all_dates)

    # -----------------------------------------------------------------------
    # Phase 5 — enrich client_name (Delivery.CustomerName)
    # -----------------------------------------------------------------------
    await _phase5_enrich_client_name(pool, by_server, all_dates)

    # Final stats
    stats = await pool.fetch(
        """SELECT branch_name,
                  COUNT(*) as total,
                  COUNT(*) FILTER (WHERE items IS NOT NULL AND items != '') as with_items,
                  COUNT(*) FILTER (WHERE courier IS NOT NULL AND courier != '') as with_courier,
                  COUNT(*) FILTER (WHERE sum > 0) as with_sum,
                  COUNT(*) FILTER (WHERE planned_time IS NOT NULL AND planned_time != '') as with_planned,
                  COUNT(*) FILTER (WHERE client_name IS NOT NULL AND client_name != '') as with_client_name
           FROM orders_raw WHERE tenant_id=$1
           GROUP BY branch_name ORDER BY branch_name""",
        TENANT_ID,
    )
    logger.info("\n=== Итого в orders_raw ===")
    for s in stats:
        logger.info(
            f"  {s['branch_name']}: {s['total']} заказов, "
            f"{s['with_items']} с составом, {s['with_courier']} с курьером, "
            f"{s['with_sum']} с суммой, {s['with_planned']} с planned_time, {s['with_client_name']} с именем"
        )

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

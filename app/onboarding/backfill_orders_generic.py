"""
backfill_orders_generic.py — универсальный бэкфилл orders_raw из OLAP v2 для любого tenant'а.

Использование:
    python -m app.onboarding.backfill_orders_generic \
        --tenant-id 3 \
        --date-from 2025-01-01 \
        --date-to 2026-03-03 \
        --skip-cities "Город1,Город2"

Phase 1 — DELIVERIES per-order (weekly, resumable):
  Заполняет: все поля заказа (мобильник, тайминги, оплата, скидка, planned_time, client_name).
  Заменяет старые фазы 1+4+5+6.

Phase 2 — SALES per-dish (weekly):
  Заполняет: items (DishName) + courier (WaiterName).
  Заменяет старые фазы 2+3.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_orders_generic")


class OrdersBackfiller:
    def __init__(
        self,
        tenant_id: int,
        date_from: date,
        date_to: date,
        skip_cities: set[str] = None,
    ):
        self.tenant_id = tenant_id
        self.date_from = date_from
        self.date_to = date_to
        self.skip_cities = skip_cities or set()
        self.pool: asyncpg.Pool = None
        self.progress_file = f"/app/data/backfill_orders_{tenant_id}_progress.json"
        self.stats = {"ok": 0, "error": 0, "phases": {}}

    async def init_db(self) -> None:
        db_url = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/ebidoebi")
        self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)

    async def close_db(self) -> None:
        if self.pool:
            await self.pool.close()

    async def get_iiko_credentials(self) -> list[dict]:
        """Get branches for tenant, excluding skip_cities."""
        rows = await self.pool.fetch(
            """SELECT city, bo_url, bo_login, bo_password, branch_name FROM iiko_credentials
               WHERE tenant_id = $1 AND is_active = true
               ORDER BY branch_name""",
            self.tenant_id,
        )
        result = []
        for r in rows:
            if r["city"] not in self.skip_cities:
                result.append(dict(r))
        return result

    def _load_progress(self) -> set:
        try:
            with open(self.progress_file) as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _save_progress(self, done: set) -> None:
        os.makedirs(os.path.dirname(self.progress_file), exist_ok=True)
        with open(self.progress_file, "w") as f:
            json.dump(sorted(done), f)

    async def _get_token(self, bo_url: str, bo_login: str, bo_password: str, client: httpx.AsyncClient) -> str:
        pw_hash = hashlib.sha1(bo_password.encode()).hexdigest()
        r = await client.get(f"{bo_url}/api/auth?login={bo_login}&pass={pw_hash}", timeout=30)
        r.raise_for_status()
        return r.text.strip()

    def _date_filter(self, date_from: str, date_to: str) -> dict:
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

    # ─────────────────────────────────────────────────────────────────────
    # Phase 1 — DELIVERIES per-order
    # ───────────────────────────────────────────────────────────────────

    async def _fetch_deliveries(self, bo_url: str, token: str, date_from: str, date_to: str, client: httpx.AsyncClient) -> list[dict]:
        """Запрос DELIVERIES с 16 полями + 2 агрегатами — новый Query A."""
        body = {
            "reportType": "DELIVERIES",
            "buildSummary": "false",
            "groupByRowFields": [
                "Department", "Delivery.Number",
                "Delivery.ActualTime", "Delivery.ExpectedTime", "Delivery.SendTime",
                "Delivery.PrintTime", "Delivery.CookingFinishTime", "OpenTime",
                "Delivery.CustomerPhone", "Delivery.CustomerName", "Delivery.Address",
                "Delivery.ServiceType", "Delivery.CancelCause", "Delivery.SourceKey",
                "PayTypes", "OrderDiscount.Type",
            ],
            "aggregateFields": ["DiscountSum", "DishDiscountSumInt"],
            "filters": self._date_filter(date_from, date_to),
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
            logger.warning(f"DELIVERIES {bo_url} {date_from}: {r.status_code}")
        except Exception as e:
            logger.error(f"DELIVERIES error {bo_url} {date_from}: {e}")
        return []

    def _aggregate_deliveries(self, rows: list[dict], branch_set: set) -> dict[tuple, dict]:
        """Группирует строки DELIVERIES по (branch, delivery_num)."""
        by_order: dict[tuple, list] = defaultdict(list)
        for row in rows:
            dept = (row.get("Department") or "").strip()
            num = row.get("Delivery.Number")
            if not num or dept not in branch_set:
                continue
            by_order[(dept, str(int(num)))].append(row)

        result: dict[tuple, dict] = {}
        for (branch, num), order_rows in by_order.items():
            pay_parts: dict[str, float] = {}
            disc_types: list[str] = []
            cancel_reason = ""
            source = ""
            send_time = ""
            print_time = ""
            cooked_time = ""
            opened_at = ""
            client_phone = ""
            client_name = ""
            actual_time = ""
            planned_time = ""
            delivery_address = ""
            is_self_service = False
            total_sum = 0.0
            discount_sum = 0.0

            for r in order_rows:
                pay_type = r.get("PayTypes", "")
                amount = float(r.get("DishDiscountSumInt", 0) or 0)
                if pay_type and amount:
                    pay_parts[pay_type] = pay_parts.get(pay_type, 0) + amount
                total_sum = max(total_sum, amount)

                ds = float(r.get("DiscountSum", 0) or 0)
                if ds > discount_sum:
                    discount_sum = ds

                dt = (r.get("OrderDiscount.Type") or "").strip()
                if dt and dt not in disc_types:
                    disc_types.append(dt)

                if not cancel_reason:
                    cr = r.get("Delivery.CancelCause")
                    if cr:
                        cancel_reason = str(cr).strip()
                if not source:
                    sk = r.get("Delivery.SourceKey")
                    if sk:
                        source = str(sk).strip()
                for field, attr in [
                    ("Delivery.SendTime", "send_time"), ("Delivery.PrintTime", "print_time"),
                    ("Delivery.CookingFinishTime", "cooked_time"), ("OpenTime", "opened_at"),
                    ("Delivery.CustomerPhone", "client_phone"),
                    ("Delivery.ActualTime", "actual_time"), ("Delivery.ExpectedTime", "planned_time"),
                    ("Delivery.Address", "delivery_address"),
                ]:
                    if not locals()[attr]:
                        v = r.get(field)
                        if v:
                            locals()[attr]  # can't use locals() assignment
                if not send_time:
                    v = r.get("Delivery.SendTime")
                    if v:
                        send_time = str(v)
                if not print_time:
                    v = r.get("Delivery.PrintTime")
                    if v:
                        print_time = str(v)
                if not cooked_time:
                    v = r.get("Delivery.CookingFinishTime")
                    if v:
                        cooked_time = str(v)
                if not opened_at:
                    v = r.get("OpenTime")
                    if v:
                        opened_at = str(v)
                if not client_phone:
                    v = r.get("Delivery.CustomerPhone")
                    if v:
                        client_phone = str(v).strip()
                if not client_name:
                    v = r.get("Delivery.CustomerName") or ""
                    if v and not v.startswith("GUEST"):
                        client_name = v.strip()
                if not actual_time:
                    v = r.get("Delivery.ActualTime")
                    if v:
                        actual_time = str(v)
                if not planned_time:
                    v = r.get("Delivery.ExpectedTime")
                    if v:
                        planned_time = str(v)
                if not delivery_address:
                    v = r.get("Delivery.Address")
                    if v:
                        delivery_address = str(v).strip()
                if (r.get("Delivery.ServiceType") or "").upper() == "PICKUP":
                    is_self_service = True

            main_pay = max(pay_parts, key=pay_parts.get) if pay_parts else ""
            result[(branch, num)] = {
                "status": "Отменена" if cancel_reason else "Доставлена",
                "cancel_reason": cancel_reason,
                "client_phone": client_phone,
                "client_name": client_name,
                "sum": total_sum,
                "discount_sum": discount_sum,
                "actual_time": actual_time,
                "planned_time": planned_time,
                "send_time": send_time,
                "service_print_time": print_time,
                "cooked_time": cooked_time,
                "opened_at": opened_at,
                "delivery_address": delivery_address,
                "is_self_service": is_self_service,
                "payment_type": main_pay,
                "pay_breakdown": json.dumps(pay_parts, ensure_ascii=False) if pay_parts else "",
                "discount_type": "; ".join(disc_types),
                "source": source,
            }
        return result

    async def _upsert_deliveries(self, orders: dict[tuple, dict], order_date: date) -> int:
        count = 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for (branch, num), data in orders.items():
                    await conn.execute(
                        """
                        INSERT INTO orders_raw (
                            tenant_id, branch_name, delivery_num, date,
                            status, cancel_reason, client_phone, client_name,
                            sum, discount_sum,
                            actual_time, planned_time, send_time, service_print_time,
                            cooked_time, opened_at, delivery_address, is_self_service,
                            payment_type, pay_breakdown, discount_type, source, updated_at
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,now())
                        ON CONFLICT (tenant_id, branch_name, delivery_num) DO UPDATE SET
                            status           = EXCLUDED.status,
                            cancel_reason    = COALESCE(NULLIF(EXCLUDED.cancel_reason,''), orders_raw.cancel_reason),
                            client_phone     = COALESCE(NULLIF(EXCLUDED.client_phone,''), orders_raw.client_phone),
                            client_name      = COALESCE(NULLIF(EXCLUDED.client_name,''), orders_raw.client_name),
                            sum              = CASE WHEN EXCLUDED.sum > 0 THEN EXCLUDED.sum ELSE orders_raw.sum END,
                            discount_sum     = CASE WHEN EXCLUDED.discount_sum > 0 THEN EXCLUDED.discount_sum ELSE orders_raw.discount_sum END,
                            actual_time      = COALESCE(NULLIF(EXCLUDED.actual_time,''), orders_raw.actual_time),
                            planned_time     = COALESCE(NULLIF(EXCLUDED.planned_time,''), orders_raw.planned_time),
                            send_time        = COALESCE(NULLIF(EXCLUDED.send_time,''), orders_raw.send_time),
                            service_print_time = COALESCE(NULLIF(EXCLUDED.service_print_time,''), orders_raw.service_print_time),
                            cooked_time      = COALESCE(NULLIF(EXCLUDED.cooked_time,''), orders_raw.cooked_time),
                            opened_at        = COALESCE(NULLIF(EXCLUDED.opened_at,''), orders_raw.opened_at),
                            delivery_address = COALESCE(NULLIF(EXCLUDED.delivery_address,''), orders_raw.delivery_address),
                            is_self_service  = EXCLUDED.is_self_service,
                            payment_type     = COALESCE(NULLIF(EXCLUDED.payment_type,''), orders_raw.payment_type),
                            pay_breakdown    = COALESCE(NULLIF(EXCLUDED.pay_breakdown,''), orders_raw.pay_breakdown),
                            discount_type    = COALESCE(NULLIF(EXCLUDED.discount_type,''), orders_raw.discount_type),
                            source           = COALESCE(NULLIF(EXCLUDED.source,''), orders_raw.source),
                            updated_at       = now()
                        """,
                        self.tenant_id, branch, num, order_date,
                        data["status"], data["cancel_reason"], data["client_phone"], data["client_name"],
                        data["sum"], data["discount_sum"],
                        data["actual_time"], data["planned_time"], data["send_time"], data["service_print_time"],
                        data["cooked_time"], data["opened_at"], data["delivery_address"], data["is_self_service"],
                        data["payment_type"], data["pay_breakdown"], data["discount_type"], data["source"],
                    )
                    count += 1
        return count

    async def _run_phase1(self) -> None:
        """Фаза 1: DELIVERIES per-order, недельные чанки, резюмируемые."""
        logger.info(f"\n=== Phase 1: DELIVERIES (tenant_id={self.tenant_id}) ===")

        credentials = await self.get_iiko_credentials()
        if not credentials:
            logger.error(f"No active credentials for tenant_id={self.tenant_id}")
            return
        logger.info(f"Branches: {[c['branch_name'] for c in credentials]}")

        by_server: dict[tuple, list[str]] = defaultdict(list)
        for c in credentials:
            key = (c["bo_url"], c["bo_login"], c["bo_password"])
            by_server[key].append(c["branch_name"])

        today = date.today()
        yesterday = today - timedelta(days=1)
        done = self._load_progress()

        total_ok = 0
        async with httpx.AsyncClient(verify=False, timeout=90) as client:
            for (bo_url, login, password), branch_names in by_server.items():
                branch_set = set(branch_names)
                try:
                    token = await self._get_token(bo_url, login, password, client)
                except Exception as e:
                    logger.error(f"Phase 1 auth failed {bo_url}: {e}")
                    continue

                # Недельные чанки
                week_start = self.date_from
                while week_start <= min(self.date_to, yesterday):
                    week_end = min(week_start + timedelta(days=7), today)
                    chunk_key = f"p1:{week_start}"
                    if chunk_key in done:
                        week_start += timedelta(days=7)
                        continue
                    try:
                        rows = await self._fetch_deliveries(bo_url, token, str(week_start), str(week_end), client)
                        orders = self._aggregate_deliveries(rows, branch_set)
                        upserted = await self._upsert_deliveries(orders, week_start)
                        done.add(chunk_key)
                        self._save_progress(done)
                        total_ok += upserted
                        logger.info(f"  ✓ {week_start}..{week_end}: {len(rows)} строк → {upserted} заказов")
                    except Exception as e:
                        logger.error(f"  Error {week_start} {bo_url}: {e}")
                    await asyncio.sleep(0.5)
                    week_start += timedelta(days=7)

        logger.info(f"Phase 1 done: {total_ok}")
        self.stats["phases"]["phase1"] = total_ok

    # ───────────────────────────────────────────────────────────────────
    # Phase 2 — SALES per-dish (блюда + курьер)
    # ───────────────────────────────────────────────────────────────────

    async def _fetch_dishes(self, bo_url: str, token: str, date_from: str, date_to: str, client: httpx.AsyncClient) -> list[dict]:
        """Запрос SALES DishName + WaiterName + Amount — новый Query B."""
        body = {
            "reportType": "SALES",
            "buildSummary": "false",
            "groupByRowFields": ["Department", "Delivery.Number", "DishName", "WaiterName"],
            "aggregateFields": ["Amount"],
            "filters": self._date_filter(date_from, date_to),
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
            logger.warning(f"SALES dishes {bo_url} {date_from}: {r.status_code}")
        except Exception as e:
            logger.error(f"SALES dishes error {bo_url} {date_from}: {e}")
        return []

    async def _run_phase2(self) -> None:
        """Фаза 2: SALES per-dish — items + courier, недельные чанки."""
        logger.info(f"\n=== Phase 2: SALES dishes (tenant_id={self.tenant_id}) ===")

        credentials = await self.get_iiko_credentials()
        if not credentials:
            return

        by_server: dict[tuple, list[str]] = defaultdict(list)
        for c in credentials:
            by_server[(c["bo_url"], c["bo_login"], c["bo_password"])].append(c["branch_name"])

        today = date.today()
        yesterday = today - timedelta(days=1)
        done = self._load_progress()

        total_ok = 0
        async with httpx.AsyncClient(verify=False, timeout=90) as client:
            for (bo_url, login, password), branch_names in by_server.items():
                branch_set = set(branch_names)
                try:
                    token = await self._get_token(bo_url, login, password, client)
                except Exception as e:
                    logger.error(f"Phase 2 auth failed {bo_url}: {e}")
                    continue

                week_start = self.date_from
                while week_start <= min(self.date_to, yesterday):
                    week_end = min(week_start + timedelta(days=7), today)
                    chunk_key = f"p2:{week_start}"
                    if chunk_key in done:
                        week_start += timedelta(days=7)
                        continue

                    try:
                        rows = await self._fetch_dishes(bo_url, token, str(week_start), str(week_end), client)

                        items_map: dict[tuple, list] = defaultdict(list)
                        courier_map: dict[tuple, str] = {}
                        for row in rows:
                            dept = (row.get("Department") or "").strip()
                            num = row.get("Delivery.Number")
                            if not num or dept not in branch_set:
                                continue
                            key = (dept, str(int(num)))
                            dish = (row.get("DishName") or "").strip()
                            if dish:
                                qty = int(float(row.get("Amount", 1) or 1))
                                existing = next((it for it in items_map[key] if it["name"] == dish), None)
                                if existing:
                                    existing["qty"] += qty
                                else:
                                    items_map[key].append({"name": dish, "qty": qty})
                            if key not in courier_map:
                                waiter = (row.get("WaiterName") or "").strip()
                                if waiter:
                                    courier_map[key] = waiter

                        updated = 0
                        async with self.pool.acquire() as conn:
                            async with conn.transaction():
                                for key in set(list(items_map) + list(courier_map)):
                                    branch, num = key
                                    items_json = json.dumps(items_map.get(key, []), ensure_ascii=False) if items_map.get(key) else ""
                                    courier = courier_map.get(key, "")
                                    sets = []
                                    vals = [self.tenant_id]
                                    idx = 2
                                    if items_json:
                                        sets.append(f"items = CASE WHEN items IS NULL OR items = '' OR items = '[]' THEN ${idx} ELSE items END")
                                        vals.append(items_json)
                                        idx += 1
                                    if courier:
                                        sets.append(f"courier = CASE WHEN courier IS NULL OR courier = '' THEN ${idx} ELSE courier END")
                                        vals.append(courier)
                                        idx += 1
                                    if not sets:
                                        continue
                                    vals.extend([branch, num])
                                    sql = f"UPDATE orders_raw SET {', '.join(sets)}, updated_at=now() WHERE tenant_id=$1 AND branch_name=${idx} AND delivery_num=${idx+1}"
                                    res = await conn.execute(sql, *vals)
                                    if res.split()[-1] != "0":
                                        updated += 1

                        done.add(chunk_key)
                        self._save_progress(done)
                        total_ok += updated
                        logger.info(f"  ✓ {week_start}..{week_end}: {len(rows)} строк → {updated} заказов")
                    except Exception as e:
                        logger.error(f"  Error {week_start} {bo_url}: {e}")

                    await asyncio.sleep(0.5)
                    week_start += timedelta(days=7)

        logger.info(f"Phase 2 done: {total_ok}")
        self.stats["phases"]["phase2"] = total_ok

    async def run(self) -> None:
        """Главный запуск бэкфилла."""
        await self.init_db()
        try:
            await self._run_phase1()
            await self._run_phase2()
            self._print_summary()
        finally:
            await self.close_db()

    def _print_summary(self) -> None:
        logger.info("\n" + "=" * 60)
        logger.info(f"BACKFILL COMPLETED (tenant_id={self.tenant_id})")
        logger.info("=" * 60)
        for phase, count in self.stats["phases"].items():
            logger.info(f"  {phase}: {count}")
        self, bo_url: str, token: str, date_from: str, date_to: str, client: httpx.AsyncClient
    ) -> list[dict]:
        olap_fields = [
            "Delivery.Number", "Department", "Delivery.CustomerPhone",
            "Delivery.CancelCause", "Delivery.ActualTime", "Delivery.Address",
            "Delivery.ServiceType",
        ]
        body = {
            "reportType": "SALES",
            "buildSummary": "false",
            "groupByRowFields": olap_fields,
            "aggregateFields": ["DishDiscountSumInt"],
            "filters": self._date_filter(date_from, date_to),
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
            logger.warning(f"OLAP orders {bo_url} {date_from}: {r.status_code}")
        except Exception as e:
            logger.error(f"OLAP orders error {bo_url} {date_from}: {e}")
        return []

    def _parse_orders(self, rows: list[dict], branch_names: set, order_date: date) -> list[dict]:
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

    async def _upsert_orders(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        count = 0
        for r in rows:
            await self.pool.execute(
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
                self.tenant_id,
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

    async def _run_phase1(self) -> None:
        """Phase 1: daily order-level backfill (resumable)."""
        logger.info(f"\n=== Phase 1: order-level (tenant_id={self.tenant_id}) ===")
        
        credentials = await self.get_iiko_credentials()
        if not credentials:
            logger.error(f"No active credentials for tenant_id={self.tenant_id}")
            return

        logger.info(f"Branches: {[c['branch_name'] for c in credentials]}")

        by_server = defaultdict(list)
        for c in credentials:
            key = (c["bo_url"], c["bo_login"], c["bo_password"])
            by_server[key].append(c["branch_name"])

        today = date.today()
        yesterday = today - timedelta(days=1)

        all_dates = []
        current = self.date_from
        while current <= min(self.date_to, yesterday):
            all_dates.append(current)
            current += timedelta(days=1)

        done = self._load_progress()
        remaining = [d for d in all_dates if str(d) not in done]
        logger.info(f"Days: total={len(all_dates)}, done={len(done)}, remaining={len(remaining)}")

        total_ok = 0

        async with httpx.AsyncClient(verify=False, timeout=60) as client:
            for (bo_url, login, password), branch_names in by_server.items():
                branch_set = set(branch_names)
                try:
                    token = await self._get_token(bo_url, login, password, client)
                except Exception as e:
                    logger.error(f"Phase 1 auth failed {bo_url}: {e}")
                    continue

                for d in remaining:
                    date_str = str(d)
                    next_str = str(d + timedelta(days=1))
                    try:
                        rows = await self._fetch_orders(bo_url, token, date_str, next_str, client)
                        parsed = self._parse_orders(rows, branch_set, d)
                        upserted = await self._upsert_orders(parsed)
                        total_ok += upserted
                        done.add(date_str)
                        self._save_progress(done)
                        logger.info(f"  ✓ {date_str}: {upserted} orders")
                    except Exception as e:
                        logger.error(f"  Error {date_str} {bo_url}: {e}")

                    await asyncio.sleep(0.3)

        logger.info(f"Phase 1 done: {total_ok} orders")
        self.stats["phases"]["phase1"] = total_ok

    # ─────────────────────────────────────────────────────────────────────
    # Phase 2-5 — enrichments (dish, courier, planned_time, client_name)
    # ─────────────────────────────────────────────────────────────────────

    async def _phase2_enrich_items(self, by_server, all_dates) -> None:
        """Phase 2: enrich items (DishName)."""
        logger.info("\n=== Phase 2: enrichment items ===")
        
        empty_count = await self.pool.fetchval(
            "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (items IS NULL OR items='')",
            self.tenant_id,
        )
        logger.info(f"Orders without items: {empty_count}")
        if empty_count == 0:
            logger.info("All orders have items — Phase 2 skipped")
            return

        olap_fields = ["Delivery.Number", "Department", "DishName"]
        total_updated = 0

        async with httpx.AsyncClient(verify=False, timeout=90) as client:
            for (bo_url, login, password), branch_names in by_server.items():
                branch_set = set(branch_names)
                try:
                    token = await self._get_token(bo_url, login, password, client)
                except Exception as e:
                    logger.error(f"Phase 2 auth failed {bo_url}: {e}")
                    continue

                week_start = all_dates[0]
                today = date.today()
                while week_start <= all_dates[-1]:
                    week_end = min(week_start + timedelta(days=7), today)
                    body = {
                        "reportType": "SALES",
                        "buildSummary": "false",
                        "groupByRowFields": olap_fields,
                        "aggregateFields": ["DishDiscountSumInt"],
                        "filters": self._date_filter(str(week_start), str(week_end)),
                    }
                    try:
                        r = await client.post(
                            f"{bo_url}/api/v2/reports/olap?key={token}",
                            json=body, timeout=90,
                        )
                        rows = r.json().get("data", []) if r.status_code == 200 else []
                    except Exception as e:
                        logger.error(f"Phase 2 OLAP error {bo_url} {week_start}: {e}")
                        rows = []

                    items_map = defaultdict(list)
                    for row in rows:
                        num = row.get("Delivery.Number")
                        dept = (row.get("Department") or "").strip()
                        dish = (row.get("DishName") or "").strip()
                        if num and dept in branch_set and dish:
                            key = (dept, str(int(num)))
                            items_map[key].append({"name": dish, "qty": 1})

                    updated = 0
                    for (branch, num), dishes in items_map.items():
                        items_json = json.dumps(dishes, ensure_ascii=False)
                        result = await self.pool.execute(
                            """UPDATE orders_raw SET items=$1, updated_at=now()
                               WHERE tenant_id=$2 AND branch_name=$3 AND delivery_num=$4
                               AND (items IS NULL OR items='')""",
                            items_json, self.tenant_id, branch, num,
                        )
                        if result and result.split()[-1] != "0":
                            updated += 1

                    logger.info(f"  {week_start}..{week_end}: {len(items_map)} with items, updated {updated}")
                    total_updated += updated
                    week_start += timedelta(days=7)
                    await asyncio.sleep(0.5)

        logger.info(f"Phase 2 done: {total_updated} updated")
        self.stats["phases"]["phase2"] = total_updated

    async def _phase3_enrich_courier(self, by_server, all_dates) -> None:
        """Phase 3: enrich courier (WaiterName)."""
        logger.info("\n=== Phase 3: enrichment courier ===")
        
        empty_count = await self.pool.fetchval(
            "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (courier IS NULL OR courier='')",
            self.tenant_id,
        )
        logger.info(f"Orders without courier: {empty_count}")
        if empty_count == 0:
            logger.info("All orders have courier — Phase 3 skipped")
            return

        olap_fields = ["Delivery.Number", "Department", "WaiterName"]
        total_updated = 0

        async with httpx.AsyncClient(verify=False, timeout=90) as client:
            for (bo_url, login, password), branch_names in by_server.items():
                branch_set = set(branch_names)
                try:
                    token = await self._get_token(bo_url, login, password, client)
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
                        "groupByRowFields": olap_fields,
                        "aggregateFields": ["DishDiscountSumInt"],
                        "filters": self._date_filter(str(week_start), str(week_end)),
                    }
                    try:
                        r = await client.post(
                            f"{bo_url}/api/v2/reports/olap?key={token}",
                            json=body, timeout=90,
                        )
                        rows = r.json().get("data", []) if r.status_code == 200 else []
                    except Exception as e:
                        logger.error(f"Phase 3 OLAP error {bo_url} {week_start}: {e}")
                        rows = []

                    courier_map = {}
                    for row in rows:
                        num = row.get("Delivery.Number")
                        dept = (row.get("Department") or "").strip()
                        waiter = (row.get("WaiterName") or "").strip()
                        if num and dept in branch_set and waiter:
                            courier_map[(dept, str(int(num)))] = waiter

                    updated = 0
                    for (branch, num), courier in courier_map.items():
                        result = await self.pool.execute(
                            """UPDATE orders_raw SET courier=$1, updated_at=now()
                               WHERE tenant_id=$2 AND branch_name=$3 AND delivery_num=$4
                               AND (courier IS NULL OR courier='')""",
                            courier, self.tenant_id, branch, num,
                        )
                        if result and result.split()[-1] != "0":
                            updated += 1

                    logger.info(f"  {week_start}..{week_end}: {len(courier_map)} with courier, updated {updated}")
                    total_updated += updated
                    week_start += timedelta(days=7)
                    await asyncio.sleep(0.5)

        logger.info(f"Phase 3 done: {total_updated} updated")
        self.stats["phases"]["phase3"] = total_updated

    async def _phase4_enrich_planned(self, by_server, all_dates) -> None:
        """Phase 4: enrich planned_time (Delivery.ExpectedTime)."""
        logger.info("\n=== Phase 4: enrichment planned_time ===")
        
        empty_count = await self.pool.fetchval(
            "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (planned_time IS NULL OR planned_time='')",
            self.tenant_id,
        )
        logger.info(f"Orders without planned_time: {empty_count}")
        if empty_count == 0:
            logger.info("All orders have planned_time — Phase 4 skipped")
            return

        olap_fields = ["Delivery.Number", "Department", "Delivery.ExpectedTime"]
        total_updated = 0

        async with httpx.AsyncClient(verify=False, timeout=90) as client:
            for (bo_url, login, password), branch_names in by_server.items():
                branch_set = set(branch_names)
                try:
                    token = await self._get_token(bo_url, login, password, client)
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
                        "groupByRowFields": olap_fields,
                        "aggregateFields": ["DishDiscountSumInt"],
                        "filters": self._date_filter(str(week_start), str(week_end)),
                    }
                    try:
                        r = await client.post(
                            f"{bo_url}/api/v2/reports/olap?key={token}",
                            json=body, timeout=90,
                        )
                        rows = r.json().get("data", []) if r.status_code == 200 else []
                    except Exception as e:
                        logger.error(f"Phase 4 OLAP error {bo_url} {week_start}: {e}")
                        rows = []

                    planned_map = {}
                    for row in rows:
                        num = row.get("Delivery.Number")
                        dept = (row.get("Department") or "").strip()
                        expected = (row.get("Delivery.ExpectedTime") or "").strip()
                        if num and dept in branch_set and expected:
                            planned_map[(dept, str(int(num)))] = expected

                    updated = 0
                    for (branch, num), planned_time in planned_map.items():
                        result = await self.pool.execute(
                            """UPDATE orders_raw SET planned_time=$1, updated_at=now()
                               WHERE tenant_id=$2 AND branch_name=$3 AND delivery_num=$4
                               AND (planned_time IS NULL OR planned_time='')""",
                            planned_time, self.tenant_id, branch, num,
                        )
                        if result and result.split()[-1] != "0":
                            updated += 1

                    logger.info(f"  {week_start}..{week_end}: {len(planned_map)} with planned, updated {updated}")
                    total_updated += updated
                    week_start += timedelta(days=7)
                    await asyncio.sleep(0.5)

        logger.info(f"Phase 4 done: {total_updated} updated")
        self.stats["phases"]["phase4"] = total_updated

    async def _phase5_enrich_client_name(self, by_server, all_dates) -> None:
        """Phase 5: enrich client_name (Delivery.CustomerName)."""
        logger.info("\n=== Phase 5: enrichment client_name ===")
        
        empty_count = await self.pool.fetchval(
            "SELECT COUNT(*) FROM orders_raw WHERE tenant_id=$1 AND (client_name IS NULL OR client_name='')",
            self.tenant_id,
        )
        logger.info(f"Orders without client_name: {empty_count}")
        if empty_count == 0:
            logger.info("All orders have client_name — Phase 5 skipped")
            return

        olap_fields = ["Delivery.Number", "Department", "Delivery.CustomerName"]
        total_updated = 0

        async with httpx.AsyncClient(verify=False, timeout=90) as client:
            for (bo_url, login, password), branch_names in by_server.items():
                branch_set = set(branch_names)
                try:
                    token = await self._get_token(bo_url, login, password, client)
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
                        "groupByRowFields": olap_fields,
                        "aggregateFields": ["DishDiscountSumInt"],
                        "filters": self._date_filter(str(week_start), str(week_end)),
                    }
                    try:
                        r = await client.post(
                            f"{bo_url}/api/v2/reports/olap?key={token}",
                            json=body, timeout=90,
                        )
                        rows = r.json().get("data", []) if r.status_code == 200 else []
                    except Exception as e:
                        logger.error(f"Phase 5 OLAP error {bo_url} {week_start}: {e}")
                        rows = []

                    client_name_map = {}
                    for row in rows:
                        num = row.get("Delivery.Number")
                        dept = (row.get("Department") or "").strip()
                        name = (row.get("Delivery.CustomerName") or "").strip()
                        if num and dept in branch_set and name and not name.startswith("GUEST"):
                            client_name_map[(dept, str(int(num)))] = name

                    updated = 0
                    for (branch, num), client_name in client_name_map.items():
                        result = await self.pool.execute(
                            """UPDATE orders_raw SET client_name=$1, updated_at=now()
                               WHERE tenant_id=$2 AND branch_name=$3 AND delivery_num=$4
                               AND (client_name IS NULL OR client_name='')""",
                            client_name, self.tenant_id, branch, num,
                        )
                        if result and result.split()[-1] != "0":
                            updated += 1

                    logger.info(f"  {week_start}..{week_end}: {len(client_name_map)} with name, updated {updated}")
                    total_updated += updated
                    week_start += timedelta(days=7)
                    await asyncio.sleep(0.5)

        logger.info(f"Phase 5 done: {total_updated} updated")
        self.stats["phases"]["phase5"] = total_updated

    async def run(self) -> None:
        """Main backfill runner."""
        await self.init_db()
        try:
            await self._run_phase1()

            credentials = await self.get_iiko_credentials()
            by_server = defaultdict(list)
            for c in credentials:
                key = (c["bo_url"], c["bo_login"], c["bo_password"])
                by_server[key].append(c["branch_name"])

            today = date.today()
            yesterday = today - timedelta(days=1)
            all_dates = []
            current = self.date_from
            while current <= min(self.date_to, yesterday):
                all_dates.append(current)
                current += timedelta(days=1)

            await self._phase2_enrich_items(by_server, all_dates)
            await self._phase3_enrich_courier(by_server, all_dates)
            await self._phase4_enrich_planned(by_server, all_dates)
            await self._phase5_enrich_client_name(by_server, all_dates)

            self._print_summary()
        finally:
            await self.close_db()

    def _print_summary(self) -> None:
        logger.info("\n" + "=" * 60)
        logger.info(f"BACKFILL COMPLETED (tenant_id={self.tenant_id})")
        logger.info("=" * 60)
        for phase, count in self.stats["phases"].items():
            logger.info(f"  {phase}: {count}")


async def main():
    parser = argparse.ArgumentParser(description="Generic backfill for orders_raw")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--date-from", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--date-to", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--skip-cities", type=str, help="Cities comma-separated")

    args = parser.parse_args()

    date_from = date.fromisoformat(args.date_from)
    date_to = date.fromisoformat(args.date_to)
    skip_cities = set(c.strip() for c in (args.skip_cities or "").split(",") if c.strip())

    backfiller = OrdersBackfiller(
        tenant_id=args.tenant_id,
        date_from=date_from,
        date_to=date_to,
        skip_cities=skip_cities,
    )

    await backfiller.run()


if __name__ == "__main__":
    asyncio.run(main())

"""
backfill_hourly_stats.py — бэкфилл hourly_stats из orders_raw + shifts_raw.

Агрегирует данные из уже загруженных orders_raw и shifts_raw по всем часам
за указанный период. Не обращается к iiko API — только к БД.

Использование из корня проекта:
    python -m app.onboarding.backfill_hourly_stats \
        --tenant-id 1 \
        --date-from 2025-12-01 \
        --date-to 2026-03-08

Без аргументов — бэкфилл с 2025-12-01 по вчера для всех активных тенантов.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_hourly_stats")

_FINAL_STATUSES = ("Доставлена", "Закрыта")

_PROGRESS_DIR = Path("/tmp/backfill_hourly_progress")


class HourlyStatsBackfiller:
    def __init__(
        self,
        tenant_id: int | None,
        date_from: date,
        date_to: date,
    ):
        self.tenant_id = tenant_id  # None = all tenants
        self.date_from = date_from
        self.date_to = date_to
        self.pool: asyncpg.Pool | None = None
        self.stats = {"ok": 0, "error": 0}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init_db(self) -> None:
        self.pool = await asyncpg.create_pool(
            os.getenv("DATABASE_URL", "postgresql://localhost/arkentiy"),
            min_size=2,
            max_size=5,
        )

    async def close_db(self) -> None:
        if self.pool:
            await self.pool.close()

    # ------------------------------------------------------------------
    # Progress tracking (per-tenant file with processed date strings)
    # ------------------------------------------------------------------

    def _progress_path(self, tenant_id: int) -> Path:
        _PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        return _PROGRESS_DIR / f"tenant_{tenant_id}.json"

    def _load_progress(self, tenant_id: int) -> set[str]:
        p = self._progress_path(tenant_id)
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            return set()

    def _save_progress(self, tenant_id: int, done: set[str]) -> None:
        p = self._progress_path(tenant_id)
        p.write_text(json.dumps(sorted(done)))

    # ------------------------------------------------------------------
    # Core aggregation (mirrors app/jobs/hourly_stats.py:aggregate_hour)
    # ------------------------------------------------------------------

    async def _aggregate_hour(
        self, conn: asyncpg.Connection, tenant_id: int, branch_name: str, hour_start: datetime
    ) -> None:
        """Агрегирует один час для одной точки и делает UPSERT в hourly_stats."""
        hour_end = hour_start + timedelta(hours=1)
        # TEXT::timestamp даёт naive timestamp — параметры должны быть naive.
        hs = hour_start.replace(tzinfo=None)
        he = hour_end.replace(tzinfo=None)

        order_row = await conn.fetchrow(
            """SELECT
                COUNT(*)                                            AS orders_count,
                COALESCE(SUM(sum), 0)                              AS revenue,
                SUM(CASE WHEN is_late = true THEN 1 ELSE 0 END)   AS late_count,

                AVG(CASE
                    WHEN cooked_time IS NOT NULL AND cooked_time != ''
                         AND opened_at  IS NOT NULL AND opened_at  != ''
                         AND EXTRACT(EPOCH FROM (
                                 cooked_time::timestamp
                                 - REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')::timestamp
                             )) / 60 BETWEEN 1 AND 120
                    THEN EXTRACT(EPOCH FROM (
                                 cooked_time::timestamp
                                 - REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')::timestamp
                             )) / 60
                END)                                               AS avg_cook_time,

                AVG(CASE
                    WHEN ready_time  IS NOT NULL AND ready_time  != ''
                         AND cooked_time IS NOT NULL AND cooked_time != ''
                         AND EXTRACT(EPOCH FROM (
                                 REPLACE(SUBSTR(ready_time, 1, 19), 'T', ' ')::timestamp
                                 - cooked_time::timestamp
                             )) / 60 BETWEEN 0 AND 120
                    THEN EXTRACT(EPOCH FROM (
                                 REPLACE(SUBSTR(ready_time, 1, 19), 'T', ' ')::timestamp
                                 - cooked_time::timestamp
                             )) / 60
                END)                                               AS avg_courier_wait,

                AVG(CASE
                    WHEN actual_time IS NOT NULL AND actual_time != ''
                         AND send_time   IS NOT NULL AND send_time   != ''
                         AND EXTRACT(EPOCH FROM (
                                 REPLACE(SUBSTR(actual_time, 1, 19), 'T', ' ')::timestamp
                                 - REPLACE(SUBSTR(send_time,  1, 19), 'T', ' ')::timestamp
                             )) / 60 BETWEEN 1 AND 120
                    THEN EXTRACT(EPOCH FROM (
                                 REPLACE(SUBSTR(actual_time, 1, 19), 'T', ' ')::timestamp
                                 - REPLACE(SUBSTR(send_time,  1, 19), 'T', ' ')::timestamp
                             )) / 60
                END)                                               AS avg_delivery_time

            FROM orders_raw
            WHERE tenant_id = $1
              AND branch_name = $2
              AND actual_time IS NOT NULL AND actual_time != ''
              AND REPLACE(SUBSTR(actual_time, 1, 19), 'T', ' ')::timestamp >= $3
              AND REPLACE(SUBSTR(actual_time, 1, 19), 'T', ' ')::timestamp <  $4
              AND status = ANY($5)
            """,
            tenant_id, branch_name,
            hs, he,
            list(_FINAL_STATUSES),
        )

        shift_rows = await conn.fetch(
            """SELECT role_class
               FROM shifts_raw
               WHERE tenant_id = $1
                 AND branch_name = $2
                 AND clock_in IS NOT NULL AND clock_in != ''
                 AND clock_in::timestamp  < $4
                 AND (clock_out IS NULL OR clock_out = ''
                      OR clock_out::timestamp > $3)
            """,
            tenant_id, branch_name,
            hs, he,
        )

        cooks = sum(1 for r in shift_rows if r["role_class"] == "cook")
        couriers = sum(1 for r in shift_rows if r["role_class"] == "courier")

        in_progress = await conn.fetchval(
            """SELECT COUNT(*)
               FROM orders_raw
               WHERE tenant_id = $1
                 AND branch_name = $2
                 AND opened_at IS NOT NULL AND opened_at != ''
                 AND REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')::timestamp < $3
                 AND (
                     actual_time IS NULL OR actual_time = ''
                     OR REPLACE(SUBSTR(actual_time, 1, 19), 'T', ' ')::timestamp >= $3
                 )
                 AND status NOT IN ('Отменена')
            """,
            tenant_id, branch_name, hs,
        )

        orders_count = int(order_row["orders_count"] or 0)
        revenue = float(order_row["revenue"] or 0)
        late_count = int(order_row["late_count"] or 0)

        # Пропускаем пустые часы (ночь/перерыв в работе)
        if orders_count == 0 and cooks == 0 and couriers == 0:
            return

        def _f(v: object) -> float | None:
            return round(float(v), 1) if v is not None else None

        await conn.execute(
            """INSERT INTO hourly_stats
               (tenant_id, branch_name, hour,
                orders_count, revenue, avg_check,
                avg_cook_time, avg_courier_wait, avg_delivery_time,
                late_count, late_percent,
                cooks_on_shift, couriers_on_shift, orders_in_progress,
                updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,now())
               ON CONFLICT (tenant_id, branch_name, hour) DO UPDATE SET
                 orders_count=EXCLUDED.orders_count,
                 revenue=EXCLUDED.revenue,
                 avg_check=EXCLUDED.avg_check,
                 avg_cook_time=EXCLUDED.avg_cook_time,
                 avg_courier_wait=EXCLUDED.avg_courier_wait,
                 avg_delivery_time=EXCLUDED.avg_delivery_time,
                 late_count=EXCLUDED.late_count,
                 late_percent=EXCLUDED.late_percent,
                 cooks_on_shift=EXCLUDED.cooks_on_shift,
                 couriers_on_shift=EXCLUDED.couriers_on_shift,
                 orders_in_progress=EXCLUDED.orders_in_progress,
                 updated_at=now()""",
            tenant_id, branch_name, hour_start,
            orders_count, revenue,
            round(revenue / orders_count, 2) if orders_count else 0.0,
            _f(order_row["avg_cook_time"]),
            _f(order_row["avg_courier_wait"]),
            _f(order_row["avg_delivery_time"]),
            late_count,
            round(late_count / orders_count * 100, 1) if orders_count else 0.0,
            cooks, couriers,
            int(in_progress or 0),
        )

    # ------------------------------------------------------------------
    # Tenant / branch discovery
    # ------------------------------------------------------------------

    async def _get_tenants(self) -> list[int]:
        if self.tenant_id is not None:
            return [self.tenant_id]
        rows = await self.pool.fetch(
            "SELECT DISTINCT tenant_id FROM iiko_credentials WHERE is_active = true ORDER BY tenant_id"
        )
        return [r["tenant_id"] for r in rows]

    async def _get_branches(self, tenant_id: int) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT DISTINCT branch_name FROM iiko_credentials WHERE tenant_id = $1 AND is_active = true",
            tenant_id,
        )
        return [r["branch_name"] for r in rows]

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    async def run(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        end_date = min(self.date_to, yesterday)

        if self.date_from > end_date:
            logger.warning(f"date_from {self.date_from} > {end_date} — нечего делать")
            return

        tenants = await self._get_tenants()
        logger.info(
            f"Бэкфил hourly_stats: {self.date_from} → {end_date} "
            f"для tenant_id={tenants}"
        )

        for tenant_id in tenants:
            await self._run_tenant(tenant_id, end_date)

        logger.info(
            f"Бэкфил завершён. ok={self.stats['ok']}, error={self.stats['error']}"
        )

    async def _run_tenant(self, tenant_id: int, end_date: date) -> None:
        branches = await self._get_branches(tenant_id)
        if not branches:
            logger.warning(f"[tenant={tenant_id}] Нет активных точек")
            return

        done = self._load_progress(tenant_id)
        current = self.date_from

        while current <= end_date:
            date_str = current.isoformat()
            if date_str in done:
                current += timedelta(days=1)
                continue

            logger.info(f"[tenant={tenant_id}] {date_str} ({len(branches)} точек)")
            day_errors = 0

            async with self.pool.acquire() as conn:
                for branch in branches:
                    for h in range(24):
                        hour_start = datetime(
                            current.year, current.month, current.day, h, 0, 0,
                            tzinfo=timezone.utc,
                        )
                        try:
                            await self._aggregate_hour(conn, tenant_id, branch, hour_start)
                            self.stats["ok"] += 1
                        except Exception as e:
                            logger.error(
                                f"[tenant={tenant_id}] {branch} {hour_start.isoformat()}: {e}"
                            )
                            self.stats["error"] += 1
                            day_errors += 1

            if day_errors == 0:
                done.add(date_str)
                self._save_progress(tenant_id, done)

            current += timedelta(days=1)
            await asyncio.sleep(0.05)  # не давить БД


# --------------------------------------------------------------------------


async def _main(args: argparse.Namespace) -> None:
    date_from = date.fromisoformat(args.date_from)
    date_to = date.fromisoformat(args.date_to)
    tenant_id = args.tenant_id  # None if not provided

    backfiller = HourlyStatsBackfiller(
        tenant_id=tenant_id,
        date_from=date_from,
        date_to=date_to,
    )
    await backfiller.init_db()
    try:
        await backfiller.run()
    finally:
        await backfiller.close_db()


if __name__ == "__main__":
    _default_end = (date.today() - timedelta(days=1)).isoformat()

    parser = argparse.ArgumentParser(description="Бэкфил hourly_stats из orders_raw + shifts_raw")
    parser.add_argument("--tenant-id", type=int, default=None, help="tenant_id (по умолчанию — все)")
    parser.add_argument("--date-from", default="2025-12-01", help="YYYY-MM-DD (включительно)")
    parser.add_argument("--date-to", default=_default_end, help="YYYY-MM-DD (включительно)")
    _args = parser.parse_args()

    asyncio.run(_main(_args))

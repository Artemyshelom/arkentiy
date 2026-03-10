"""
backfill_timing_stats.py — пересчёт timing-полей daily_stats из orders_raw.

Заполняет поля, которые iiko OLAP не отдаёт напрямую, но вычисляются из заказов:
  avg_cooking_min, avg_wait_min, avg_delivery_min,
  late_delivery_count, late_pickup_count, exact_time_count,
  new_customers, new_customers_revenue, repeat_customers, repeat_customers_revenue,
  cooks_count, couriers_count.

Источник: только БД (orders_raw). Не обращается к iiko API.
Применяет UPSERT поверх существующих daily_stats, не затирая OLAP-поля (revenue, cash...).

Использование:
    python -m app.onboarding.backfill_timing_stats \\
        --tenant-id 3 \\
        --date-from 2025-01-01 \\
        --date-to 2026-03-10
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import date, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_timing_stats")


class TimingStatsBackfiller:
    def __init__(self, tenant_id: int, date_from: date, date_to: date):
        self.tenant_id = tenant_id
        self.date_from = date_from
        self.date_to = date_to

    async def run(self) -> None:
        from app.database_pg import (
            init_pool_only,
            aggregate_orders_for_daily_stats,
            upsert_daily_stats_batch,
            get_pool,
        )

        await init_pool_only(os.environ["DATABASE_URL"])
        pool = get_pool()

        branch_rows = await pool.fetch(
            "SELECT DISTINCT branch_name FROM orders_raw "
            "WHERE tenant_id=$1 AND date BETWEEN $2 AND $3",
            self.tenant_id, self.date_from, self.date_to - timedelta(days=1),
        )
        branches = [r["branch_name"] for r in branch_rows]

        if not branches:
            logger.warning("Нет данных в orders_raw за период, пропущено")
            return

        logger.info(f"Точки: {branches}")

        ok = err = 0
        current = self.date_from
        yesterday = date.today() - timedelta(days=1)

        while current <= min(self.date_to - timedelta(days=1), yesterday):
            date_iso = current.isoformat()
            day_rows = []

            for branch_name in branches:
                try:
                    agg = await aggregate_orders_for_daily_stats(branch_name, date_iso)

                    existing = await pool.fetchrow(
                        "SELECT orders_count, revenue, avg_check, cogs_pct, discount_sum, "
                        "sailplay, pickup_count, cash, noncash "
                        "FROM daily_stats "
                        "WHERE tenant_id=$1 AND branch_name=$2 AND date::text = $3",
                        self.tenant_id, branch_name, date_iso,
                    )
                    if existing:
                        late_d = agg.get("late_delivery_count") or 0
                        total_d = agg.get("total_delivery_count") or 0
                        late_pct = round(late_d / total_d * 100, 1) if total_d else 0.0

                        day_rows.append({
                            "branch_name":               branch_name,
                            "date":                      date_iso,
                            "orders_count":              existing["orders_count"] or 0,
                            "revenue":                   existing["revenue"],
                            "avg_check":                 existing["avg_check"],
                            "cogs_pct":                  existing["cogs_pct"],
                            "sailplay":                  existing["sailplay"] or 0.0,
                            "discount_sum":              existing["discount_sum"],
                            "pickup_count":              existing["pickup_count"],
                            "cash":                      existing["cash"],
                            "noncash":                   existing["noncash"],
                            "late_count":                late_d,
                            "total_delivered":           total_d,
                            "late_percent":              late_pct,
                            "avg_late_min":              agg.get("avg_late_min") or 0,
                            "cooks_count":               agg.get("cooks_today") or 0,
                            "couriers_count":            agg.get("couriers_today") or 0,
                            "late_delivery_count":       late_d,
                            "late_pickup_count":         agg.get("late_pickup_count") or 0,
                            "avg_cooking_min":           agg.get("avg_cooking_min"),
                            "avg_wait_min":              agg.get("avg_wait_min"),
                            "avg_delivery_min":          agg.get("avg_delivery_min"),
                            "exact_time_count":          agg.get("exact_time_count") or 0,
                            "new_customers":             agg.get("new_customers") or 0,
                            "new_customers_revenue":     agg.get("new_customers_revenue") or 0.0,
                            "repeat_customers":          agg.get("repeat_customers") or 0,
                            "repeat_customers_revenue":  agg.get("repeat_customers_revenue") or 0.0,
                        })
                except Exception as e:
                    logger.warning(f"  {date_iso} {branch_name}: {e}")
                    err += 1

            if day_rows:
                await upsert_daily_stats_batch(day_rows, tenant_id=self.tenant_id)
                ok += len(day_rows)

            if ok > 0 and ok % 50 == 0:
                logger.info(f"  ... {date_iso}, обработано: {ok}")

            current += timedelta(days=1)

        logger.info(f"Завершено: обновлено {ok}, ошибок {err}")


async def _main(args: argparse.Namespace) -> None:
    backfiller = TimingStatsBackfiller(
        tenant_id=args.tenant_id,
        date_from=date.fromisoformat(args.date_from),
        date_to=date.fromisoformat(args.date_to),
    )
    await backfiller.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Пересчёт timing-полей daily_stats из orders_raw")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--date-from", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--date-to", type=str, required=True, help="YYYY-MM-DD (не включительно)")
    asyncio.run(_main(parser.parse_args()))

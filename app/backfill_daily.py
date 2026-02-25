import asyncio, json, logging
import aiosqlite

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill_daily")

from app.database import DB_PATH, aggregate_orders_for_daily_stats

async def main():
    dates = ["2026-02-21", "2026-02-22", "2026-02-23", "2026-02-24"]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT DISTINCT branch_name FROM daily_stats WHERE date IN (?,?,?,?)",
            dates,
        )).fetchall()
        branches = [r["branch_name"] for r in rows]

    total = 0
    for branch in branches:
        for d in dates:
            agg = await aggregate_orders_for_daily_stats(branch, d)
            has_data = any(agg.get(k) for k in ("late_delivery_count", "late_pickup_count",
                       "avg_cooking_min", "avg_wait_min", "avg_delivery_min", "discount_types_agg"))
            if not has_data:
                continue

            dt_json = json.dumps(agg.get("discount_types_agg") or [], ensure_ascii=False)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """UPDATE daily_stats SET
                        late_delivery_count = ?,
                        late_pickup_count   = ?,
                        avg_cooking_min     = ?,
                        avg_wait_min        = ?,
                        avg_delivery_min    = ?,
                        discount_types      = ?
                    WHERE branch_name = ? AND date = ?""",
                    (
                        agg.get("late_delivery_count") or 0,
                        agg.get("late_pickup_count") or 0,
                        agg.get("avg_cooking_min"),
                        agg.get("avg_wait_min"),
                        agg.get("avg_delivery_min"),
                        dt_json,
                        branch, d,
                    ),
                )
                await db.commit()
            total += 1
            ld = agg.get("late_delivery_count", 0)
            ac = agg.get("avg_cooking_min")
            nt = len(agg.get("discount_types_agg", []))
            logger.info(f"{branch} {d}: late_d={ld}, avg_cook={ac}, disc_types={nt}")

    logger.info(f"daily_stats backfill done: {total} records updated")

asyncio.run(main())

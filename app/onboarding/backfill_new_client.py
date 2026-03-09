"""
backfill_new_client.py — полный бэкфилл при подключении нового клиента.

Выполняет 4 шага последовательно:
  1. orders_raw  — DELIVERIES (тайминги, оплата, скидки) + SALES (блюда, курьер)
  2. daily_stats — агрегат по точке из OLAP Query C (выручка, COGS, нал/безнал)
  3. daily_stats — timing-поля из orders_raw (avg_cooking_min, exact_time_count…)
  4. hourly_stats — почасовая аналитика из orders_raw + shifts_raw (только БД)

Использование:
    python -m app.onboarding.backfill_new_client \\
        --tenant-id 5 \\
        --date-from 2026-01-01 \\
        --date-to 2026-03-09 \\
        --skip-cities "Город1,Город2"

Флаги:
    --tenant-id     ID тенанта в БД (обязательно)
    --date-from     Начало диапазона YYYY-MM-DD (обязательно)
    --date-to       Конец диапазона YYYY-MM-DD (не включительно, обязательно)
    --skip-cities   Через запятую — города, у которых iiko-сервер недоступен
    --steps         Через запятую — запустить только указанные шаги (1,2,3,4)

Примеры:
    # Полный бэкфилл нового клиента
    python -m app.onboarding.backfill_new_client --tenant-id 5 --date-from 2026-01-01 --date-to 2026-03-10

    # Пропустить город с недоступным iiko-сервером
    python -m app.onboarding.backfill_new_client --tenant-id 5 --date-from 2026-01-01 --date-to 2026-03-10 --skip-cities "Город1,Город2"

    # Только orders_raw + daily_stats (без hourly)
    python -m app.onboarding.backfill_new_client --tenant-id 5 --date-from 2026-01-01 --date-to 2026-03-10 --steps 1,2,3

    # Пересчитать только timing-поля в daily_stats (шаг 3)
    python -m app.onboarding.backfill_new_client --tenant-id 1 --date-from 2026-01-01 --date-to 2026-03-10 --steps 3

    # Перезаполнить daily_stats с новыми cash/noncash за период
    python -m app.onboarding.backfill_new_client --tenant-id 1 --date-from 2026-01-01 --date-to 2026-03-10 --steps 2,3

Требования:
    DATABASE_URL — в окружении или .env (asyncpg DSN)
    Для шагов 1–2: доступ к iiko OLAP API через iiko_credentials в БД
    Для шагов 3–4: только БД (читает из orders_raw / shifts_raw)

Недоступные серверы (--skip-cities):
    Используй --skip-cities, если iiko-сервер одного из городов временно недоступен.
    Значение — поле `city` из iiko_credentials. Можно передать несколько через запятую.
    Шаги 3 и 4 не делают OLAP-запросов, --skip-cities на них не влияет.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_new_client")

# ---------------------------------------------------------------------------
# Шаг 1 — orders_raw (OLAP orders + dishes)
# ---------------------------------------------------------------------------

async def step1_orders(tenant_id: int, date_from: date, date_to: date, skip_cities: set[str]):
    """Phase 1+2: DELIVERIES → orders_raw, SALES dishes → orders_raw."""
    logger.info("=== Шаг 1: orders_raw (DELIVERIES + SALES dishes) ===")
    try:
        from app.onboarding.backfill_orders_generic import OrdersBackfiller
        backfiller = OrdersBackfiller(
            tenant_id=tenant_id,
            date_from=date_from,
            date_to=date_to,
            skip_cities=skip_cities,
        )
        await backfiller.run()
    except Exception as e:
        logger.error(f"Шаг 1 ошибка: {e}")
        raise


# ---------------------------------------------------------------------------
# Шаг 2 — daily_stats из OLAP Query C (выручка, COGS, нал/безнал, self-service)
# ---------------------------------------------------------------------------

async def step2_daily_stats_olap(tenant_id: int, date_from: date, date_to: date, skip_cities: set[str]):
    """Query C → daily_stats (revenue, cogs_pct, cash, noncash, pickup_count)."""
    logger.info("=== Шаг 2: daily_stats из OLAP (revenue, cash/noncash) ===")
    try:
        from app.onboarding.backfill_daily_stats_generic import DailyStatsBackfiller
        backfiller = DailyStatsBackfiller(
            tenant_id=tenant_id,
            date_from=date_from,
            date_to=date_to,
            skip_cities=skip_cities,
        )
        await backfiller.run()
    except Exception as e:
        logger.error(f"Шаг 2 ошибка: {e}")
        raise


# ---------------------------------------------------------------------------
# Шаг 3 — daily_stats timing-поля из orders_raw (avg_cooking_min, etc.)
# ---------------------------------------------------------------------------

async def step3_daily_stats_timing(tenant_id: int, date_from: date, date_to: date):
    """
    Пересчитывает timing-поля daily_stats из orders_raw:
      avg_cooking_min, avg_wait_min, avg_delivery_min,
      late_delivery_count, late_pickup_count, exact_time_count,
      new_customers, repeat_customers, cooks_count, couriers_count.

    Использует глобальный пул database_pg (без обращения к iiko API).
    """
    logger.info("=== Шаг 3: daily_stats timing из orders_raw ===")

    from app.database_pg import init_pool_only, aggregate_orders_for_daily_stats, upsert_daily_stats_batch, get_pool

    await init_pool_only(os.environ["DATABASE_URL"])
    pool = get_pool()

    # Получаем список точек тенанта за указанный период
    branch_rows = await pool.fetch(
        "SELECT DISTINCT branch_name FROM orders_raw WHERE tenant_id=$1 AND date BETWEEN $2 AND $3",
        tenant_id, date_from, date_to - timedelta(days=1),
    )
    branches = [r["branch_name"] for r in branch_rows]

    if not branches:
        logger.warning("Нет данных в orders_raw за период, шаг 3 пропущен")
        return

    ok = err = 0
    current = date_from
    yesterday = date.today() - timedelta(days=1)

    while current <= min(date_to - timedelta(days=1), yesterday):
        date_iso = current.isoformat()
        day_rows = []

        for branch_name in branches:
            try:
                agg = await aggregate_orders_for_daily_stats(branch_name, date_iso)

                # Читаем текущий daily_stats чтобы не затереть OLAP-поля (revenue, cash, noncash...)
                existing = await pool.fetchrow(
                    "SELECT revenue, avg_check, cogs_pct, discount_sum, pickup_count, cash, noncash "
                    "FROM daily_stats WHERE tenant_id=$1 AND branch_name=$2 AND date::text = $3",
                    tenant_id, branch_name, date_iso,
                )
                if existing:
                    late_d = agg.get("late_delivery_count") or 0
                    total_d = agg.get("total_delivery_count") or 0
                    late_pct = round(late_d / total_d * 100, 1) if total_d else 0.0

                    day_rows.append({
                        "branch_name": branch_name,
                        "date": date_iso,
                        "orders_count":        existing.get("orders_count") or 0,
                        "revenue":             existing["revenue"],
                        "avg_check":           existing["avg_check"],
                        "cogs_pct":            existing["cogs_pct"],
                        "sailplay":            0.0,
                        "discount_sum":        existing["discount_sum"],
                        "pickup_count":        existing["pickup_count"],
                        "cash":                existing["cash"],
                        "noncash":             existing["noncash"],
                        "late_count":          late_d,
                        "total_delivered":     total_d,
                        "late_percent":        late_pct,
                        "avg_late_min":        agg.get("avg_late_min") or 0,
                        "cooks_count":         agg.get("cooks_today") or 0,
                        "couriers_count":      agg.get("couriers_today") or 0,
                        "late_delivery_count": late_d,
                        "late_pickup_count":   agg.get("late_pickup_count") or 0,
                        "avg_cooking_min":     agg.get("avg_cooking_min"),
                        "avg_wait_min":        agg.get("avg_wait_min"),
                        "avg_delivery_min":    agg.get("avg_delivery_min"),
                        "exact_time_count":    agg.get("exact_time_count") or 0,
                        "new_customers":             agg.get("new_customers") or 0,
                        "new_customers_revenue":     agg.get("new_customers_revenue") or 0.0,
                        "repeat_customers":          agg.get("repeat_customers") or 0,
                        "repeat_customers_revenue":  agg.get("repeat_customers_revenue") or 0.0,
                    })
            except Exception as e:
                logger.warning(f"  {date_iso} {branch_name}: {e}")
                err += 1

        if day_rows:
            await upsert_daily_stats_batch(day_rows, tenant_id=tenant_id)
            ok += len(day_rows)

        if ok > 0 and ok % 50 == 0:
            logger.info(f"  ... {date_iso}, обработано: {ok}")

        current += timedelta(days=1)

    logger.info(f"Шаг 3 завершён: обновлено {ok}, ошибок {err}")


# ---------------------------------------------------------------------------
# Шаг 4 — hourly_stats (только из БД)
# ---------------------------------------------------------------------------

async def step4_hourly_stats(tenant_id: int, date_from: date, date_to: date):
    """Агрегирует hourly_stats из orders_raw + shifts_raw. Только БД."""
    logger.info("=== Шаг 4: hourly_stats (из orders_raw + shifts_raw) ===")
    try:
        from app.onboarding.backfill_hourly_stats import HourlyStatsBackfiller
        backfiller = HourlyStatsBackfiller(
            tenant_id=tenant_id,
            date_from=date_from,
            date_to=date_to,
        )
        await backfiller.run()
    except Exception as e:
        logger.error(f"Шаг 4 ошибка: {e}")
        raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description="Полный бэкфилл при онбординге нового клиента",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--tenant-id", type=int, required=True, help="ID тенанта в БД")
    parser.add_argument("--date-from", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--date-to", type=str, required=True, help="YYYY-MM-DD (не включительно)")
    parser.add_argument("--skip-cities", type=str, default="", help="Города через запятую (недоступные серверы)")
    parser.add_argument("--steps", type=str, default="1,2,3,4", help="Шаги для запуска (по умолчанию: 1,2,3,4)")

    args = parser.parse_args()

    tenant_id = args.tenant_id
    date_from = date.fromisoformat(args.date_from)
    date_to = date.fromisoformat(args.date_to)
    skip_cities = {c.strip() for c in args.skip_cities.split(",") if c.strip()}
    steps = {int(s.strip()) for s in args.steps.split(",") if s.strip()}

    # Проверки
    if date_from >= date_to:
        print("Ошибка: date-from должен быть меньше date-to")
        sys.exit(1)
    if not os.environ.get("DATABASE_URL"):
        print("Ошибка: DATABASE_URL не задан")
        sys.exit(1)

    days = (date_to - date_from).days
    logger.info(f"Бэкфилл tenant_id={tenant_id}, {date_from} — {date_to} ({days} дней)")
    if skip_cities:
        logger.info(f"Пропускаем города: {', '.join(skip_cities)}")
    logger.info(f"Шаги: {sorted(steps)}")

    try:
        if 1 in steps:
            await step1_orders(tenant_id, date_from, date_to, skip_cities)

        if 2 in steps:
            await step2_daily_stats_olap(tenant_id, date_from, date_to, skip_cities)

        if 3 in steps:
            await step3_daily_stats_timing(tenant_id, date_from, date_to)

        if 4 in steps:
            await step4_hourly_stats(tenant_id, date_from, date_to)

    except Exception as e:
        logger.error(f"Бэкфилл прерван: {e}")
        sys.exit(1)

    logger.info("✅ Бэкфилл завершён")


if __name__ == "__main__":
    asyncio.run(main())

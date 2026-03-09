"""
backfill_new_client.py — полный бэкфилл при подключении нового клиента.

Выполняет 5 шагов последовательно:
  1. orders_raw  — DELIVERIES (тайминги, оплата, скидки) + SALES (блюда, курьер)
  2. daily_stats — агрегат по точке из OLAP Query C (выручка, COGS, нал/безнал)
  3. daily_stats — timing-поля из orders_raw (avg_cooking_min, exact_time_count…)
  4. shifts_raw  — расписание сотрудников из iiko schedule API (повара/курьеры)
  5. hourly_stats — почасовая аналитика из orders_raw + shifts_raw (только БД)

Порядок важен: шаг 4 (shifts) должен идти до шага 5 (hourly),
так как hourly_stats читает shifts_raw для подсчёта cooks_on_shift.

Использование:
    python -m app.onboarding.backfill_new_client \\
        --tenant-id 5 \\
        --date-from 2026-01-01 \\
        --date-to 2026-03-09

Флаги:
    --tenant-id     ID тенанта в БД (обязательно)
    --date-from     Начало диапазона YYYY-MM-DD (обязательно)
    --date-to       Конец диапазона YYYY-MM-DD (не включительно, обязательно)
    --skip-cities   Через запятую — города, у которых iiko-сервер недоступен
    --steps         Через запятую — запустить только указанные шаги (1,2,3,4,5)

Примеры:
    # Полный бэкфилл нового клиента (все 5 шагов)
    python -m app.onboarding.backfill_new_client --tenant-id 5 --date-from 2026-01-01 --date-to 2026-03-10

    # Пропустить город с недоступным iiko-сервером
    python -m app.onboarding.backfill_new_client --tenant-id 5 --date-from 2026-01-01 --date-to 2026-03-10 --skip-cities "Город1,Город2"

    # Только orders_raw + daily_stats (без shifts и hourly)
    python -m app.onboarding.backfill_new_client --tenant-id 5 --date-from 2026-01-01 --date-to 2026-03-10 --steps 1,2,3

    # Пересчитать только timing-поля в daily_stats (шаг 3)
    python -m app.onboarding.backfill_new_client --tenant-id 1 --date-from 2026-01-01 --date-to 2026-03-10 --steps 3

    # Только shifts + hourly (если orders_raw уже есть)
    python -m app.onboarding.backfill_new_client --tenant-id 1 --date-from 2026-01-01 --date-to 2026-03-10 --steps 4,5

    # Пересчитать только hourly_stats (shifts уже загружены)
    python -m app.onboarding.backfill_new_client --tenant-id 1 --date-from 2026-01-01 --date-to 2026-03-10 --steps 5

Требования:
    DATABASE_URL — в окружении или .env (asyncpg DSN)
    Для шагов 1–2, 4: доступ к iiko OLAP/schedule API через iiko_credentials в БД
    Для шагов 3, 5: только БД (читает из orders_raw / shifts_raw)

Недоступные серверы (--skip-cities):
    Используй --skip-cities, если iiko-сервер одного из городов временно недоступен.
    Значение — поле `city` из iiko_credentials. Можно передать несколько через запятую.
    Шаги 3 и 5 не делают OLAP-запросов, --skip-cities на них не влияет.
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
    """Пересчитывает timing-поля daily_stats из orders_raw (только БД)."""
    logger.info("=== Шаг 3: daily_stats timing из orders_raw ===")
    try:
        from app.onboarding.backfill_timing_stats import TimingStatsBackfiller
        backfiller = TimingStatsBackfiller(
            tenant_id=tenant_id,
            date_from=date_from,
            date_to=date_to,
        )
        await backfiller.run()
    except Exception as e:
        logger.error(f"Шаг 3 ошибка: {e}")
        raise


# ---------------------------------------------------------------------------
# Шаг 4 — shifts_raw из iiko schedule API
# ---------------------------------------------------------------------------

async def step4_shifts_raw(tenant_id: int, date_from: date, date_to: date):
    """Загружает shifts_raw из /api/v2/employees/schedule. Нужен до hourly_stats."""
    logger.info("=== Шаг 4: shifts_raw из iiko schedule API ===")
    try:
        from app.onboarding.backfill_shifts_generic import ShiftsBackfiller
        backfiller = ShiftsBackfiller(
            tenant_id=tenant_id,
            date_from=date_from,
            date_to=date_to,
        )
        await backfiller.run()
    except Exception as e:
        logger.error(f"Шаг 4 ошибка: {e}")
        raise


# ---------------------------------------------------------------------------
# Шаг 5 — hourly_stats (только из БД)
# ---------------------------------------------------------------------------

async def step5_hourly_stats(tenant_id: int, date_from: date, date_to: date):
    """Агрегирует hourly_stats из orders_raw + shifts_raw. Только БД."""
    logger.info("=== Шаг 5: hourly_stats (из orders_raw + shifts_raw) ===")
    try:
        from app.onboarding.backfill_hourly_stats import HourlyStatsBackfiller
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
    except Exception as e:
        logger.error(f"Шаг 5 ошибка: {e}")
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
    parser.add_argument("--steps", type=str, default="1,2,3,4,5", help="Шаги для запуска (по умолчанию: 1,2,3,4,5)")

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
            await step4_shifts_raw(tenant_id, date_from, date_to)

        if 5 in steps:
            await step5_hourly_stats(tenant_id, date_from, date_to)

    except Exception as e:
        logger.error(f"Бэкфилл прерван: {e}")
        sys.exit(1)

    logger.info("✅ Бэкфилл завершён")


if __name__ == "__main__":
    asyncio.run(main())

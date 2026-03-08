"""
hourly_stats.py — Почасовая агрегация данных для AI-агента Бориса.

Два режима:
  job_hourly_stats()          — каждый час в :05, агрегирует предыдущий час
  job_recalc_yesterday_hourly() — 06:35 МСК, пересчитывает вчерашний день целиком
                                  (нужно т.к. OLAP enrichment с таймингами приходит в 05:26)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.db import get_all_branches, get_pool, upsert_hourly_stats
from app.utils.job_tracker import track_job

logger = logging.getLogger(__name__)

_FINAL_STATUSES = ("Доставлена", "Закрыта")


async def aggregate_hour(tenant_id: int, branch_name: str, hour_start: datetime) -> None:
    """Агрегирует данные за один час для одной точки и сохраняет в hourly_stats."""
    hour_end = hour_start + timedelta(hours=1)
    # TEXT::timestamp в SQL даёт naive timestamp, поэтому параметры должны быть naive.
    # Timezone сохраняется только при записи в hourly_stats.hour (TIMESTAMPTZ).
    hs = hour_start.replace(tzinfo=None)
    he = hour_end.replace(tzinfo=None)
    pool = get_pool()

    async with pool.acquire() as conn:
        # ------------------------------------------------------------------
        # Заказы, завершённые в этот час (actual_time попадает в [hour_start, hour_end))
        # Все финальные статусы кроме Отменена, без лишних фильтров — цель: нагрузка
        # ------------------------------------------------------------------
        order_row = await conn.fetchrow(
            """SELECT
                COUNT(*)                                            AS orders_count,
                COALESCE(SUM(sum), 0)                              AS revenue,
                SUM(CASE WHEN is_late = true THEN 1 ELSE 0 END)   AS late_count,

                -- Тайминги: диапазон 1-120 мин защищает от мусора
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
                         AND opened_at  IS NOT NULL AND opened_at  != ''
                         AND EXTRACT(EPOCH FROM (
                                 REPLACE(SUBSTR(actual_time, 1, 19), 'T', ' ')::timestamp
                                 - REPLACE(SUBSTR(opened_at,  1, 19), 'T', ' ')::timestamp
                             )) / 60 BETWEEN 1 AND 120
                    THEN EXTRACT(EPOCH FROM (
                                 REPLACE(SUBSTR(actual_time, 1, 19), 'T', ' ')::timestamp
                                 - REPLACE(SUBSTR(opened_at,  1, 19), 'T', ' ')::timestamp
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

        # ------------------------------------------------------------------
        # Персонал на смене в этот час (пересечение смены с [hour_start, hour_end))
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # Заказы в работе на начало часа (opened до hour_start, не завершены)
        # ------------------------------------------------------------------
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

    await upsert_hourly_stats(
        {
            "branch_name": branch_name,
            "hour": hour_start,
            "orders_count": orders_count,
            "revenue": revenue,
            "avg_check": round(revenue / orders_count, 2) if orders_count else 0.0,
            "avg_cook_time": (
                round(float(order_row["avg_cook_time"]), 1)
                if order_row["avg_cook_time"] is not None else None
            ),
            "avg_courier_wait": (
                round(float(order_row["avg_courier_wait"]), 1)
                if order_row["avg_courier_wait"] is not None else None
            ),
            "avg_delivery_time": (
                round(float(order_row["avg_delivery_time"]), 1)
                if order_row["avg_delivery_time"] is not None else None
            ),
            "late_count": late_count,
            "late_percent": round(late_count / orders_count * 100, 1) if orders_count else 0.0,
            "cooks_on_shift": cooks,
            "couriers_on_shift": couriers,
            "orders_in_progress": int(in_progress or 0),
        },
        tenant_id=tenant_id,
    )


@track_job("hourly_stats")
async def job_hourly_stats() -> None:
    """Агрегирует предыдущий час по всем точкам всех тенантов. Запускается каждый час в :05."""
    now_utc = datetime.now(timezone.utc)
    # Предыдущий полный час (UTC)
    hour_start = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)

    branches = get_all_branches()
    if not branches:
        logger.warning("[hourly_stats] Нет активных точек")
        return

    processed = 0
    errors = 0
    for b in branches:
        try:
            await aggregate_hour(b["tenant_id"], b["name"], hour_start)
            processed += 1
        except Exception as e:
            logger.error(
                f"[hourly_stats] Ошибка {b['name']} (tenant={b['tenant_id']}) "
                f"за {hour_start.isoformat()}: {e}",
                exc_info=True,
            )
            errors += 1

    logger.info(
        f"[hourly_stats] Час {hour_start.strftime('%Y-%m-%d %H:00')} UTC — "
        f"обработано {processed} точек, ошибок: {errors}"
    )


@track_job("hourly_stats_recalc_yesterday")
async def job_recalc_yesterday_hourly() -> None:
    """Пересчитывает все 24 часа вчерашнего дня (UTC).

    Запускается в 06:35 МСК — после OLAP enrichment (05:26 МСК), который заполняет
    тайминги (cooked_time, opened_at, etc.) за вчера. Без этого avg_cook_time и др.
    будут NULL весь день.
    """
    now_utc = datetime.now(timezone.utc)
    yesterday = (now_utc - timedelta(days=1)).date()

    branches = get_all_branches()
    if not branches:
        logger.warning("[hourly_stats_recalc] Нет активных точек")
        return

    processed = 0
    errors = 0
    for b in branches:
        for h in range(24):
            hour_start = datetime(
                yesterday.year, yesterday.month, yesterday.day, h, 0, 0,
                tzinfo=timezone.utc,
            )
            try:
                await aggregate_hour(b["tenant_id"], b["name"], hour_start)
                processed += 1
            except Exception as e:
                logger.error(
                    f"[hourly_stats_recalc] Ошибка {b['name']} (tenant={b['tenant_id']}) "
                    f"за {hour_start.isoformat()}: {e}",
                    exc_info=True,
                )
                errors += 1

    logger.info(
        f"[hourly_stats_recalc] Пересчёт {yesterday} UTC — "
        f"обработано {processed} точек×часов, ошибок: {errors}"
    )

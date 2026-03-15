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
from app.utils.timezone import DEFAULT_TZ, utc_hour_to_local_bounds

logger = logging.getLogger(__name__)

_FINAL_STATUSES = ("Доставлена", "Закрыта")


async def aggregate_hour(tenant_id: int, branch_name: str, hour_utc: datetime) -> None:
    """Агрегирует данные за один час для одной точки и сохраняет в hourly_stats.

    hour_utc: aware UTC datetime (начало часа). Записывается в TIMESTAMPTZ напрямую.
    WHERE сравнивает с opened_at/clock_in (local naive TEXT) через utc_hour_to_local_bounds.
    """
    assert hour_utc.tzinfo is not None, "hour_utc must be timezone-aware (UTC)"
    # Конвертируем UTC-час в naive local bounds для сравнения с TEXT-timestamps
    hs, he = utc_hour_to_local_bounds(hour_utc)
    pool = get_pool()

    async with pool.acquire() as conn:
        # ------------------------------------------------------------------
        # Заказы, принятые в этот час (opened_at попадает в [hour_start, hour_end))
        # orders_count = все принятые (final status), completed_count = с actual_time
        # ------------------------------------------------------------------
        order_row = await conn.fetchrow(
            """SELECT
                COUNT(*)                                            AS orders_count,
                COALESCE(SUM(sum), 0)                              AS revenue,
                COUNT(*) FILTER (
                    WHERE actual_time IS NOT NULL AND actual_time != ''
                )                                                  AS completed_count,
                SUM(CASE
                    WHEN is_late = true
                         AND actual_time IS NOT NULL AND actual_time != ''
                    THEN 1 ELSE 0
                END)                                               AS late_count,

                -- Тайминги: диапазон 1-120 мин защищает от мусора
                -- avg_cook_time: от печати сервис-чека до готовности (реальный старт готовки).
                -- Фолбэк на opened_at если service_print_time отсутствует.
                AVG(CASE
                    WHEN cooked_time IS NOT NULL AND cooked_time != ''
                         AND COALESCE(
                             NULLIF(service_print_time, ''),
                             REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')
                         ) IS NOT NULL
                         AND EXTRACT(EPOCH FROM (
                                 cooked_time::timestamp
                                 - COALESCE(
                                     NULLIF(service_print_time, ''),
                                     REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')
                                   )::timestamp
                             )) / 60 BETWEEN 1 AND 120
                    THEN EXTRACT(EPOCH FROM (
                                 cooked_time::timestamp
                                 - COALESCE(
                                     NULLIF(service_print_time, ''),
                                     REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')
                                   )::timestamp
                             )) / 60
                END)                                               AS avg_cook_time,

                AVG(CASE
                    WHEN send_time   IS NOT NULL AND send_time   != ''
                         AND cooked_time IS NOT NULL AND cooked_time != ''
                         AND EXTRACT(EPOCH FROM (
                                 REPLACE(SUBSTR(send_time, 1, 19), 'T', ' ')::timestamp
                                 - cooked_time::timestamp
                             )) / 60 BETWEEN 0 AND 120
                    THEN EXTRACT(EPOCH FROM (
                                 REPLACE(SUBSTR(send_time, 1, 19), 'T', ' ')::timestamp
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
              AND opened_at IS NOT NULL AND opened_at != ''
              AND REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')::timestamp >= $3
              AND REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')::timestamp <  $4
              AND status = ANY($5)
            """,
            tenant_id, branch_name,
            hs, he,
            list(_FINAL_STATUSES),
        )

        # ------------------------------------------------------------------
        # Персонал на смене в этот час (пересечение смены с [hour_utc, hour_utc+1))
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
    completed_count = int(order_row["completed_count"] or 0)
    revenue = float(order_row["revenue"] or 0)
    late_count = int(order_row["late_count"] or 0)

    # Пропускаем пустые часы (ночь/перерыв в работе)
    if orders_count == 0 and cooks == 0 and couriers == 0:
        return

    await upsert_hourly_stats(
        {
            "branch_name": branch_name,
            "hour": hour_utc,  # aware UTC → TIMESTAMPTZ
            "orders_count": orders_count,
            "completed_count": completed_count,
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
            "late_percent": round(late_count / completed_count * 100, 1) if completed_count else 0.0,
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
        f"[hourly_stats] Час {hour_start.strftime('%Y-%m-%d %H:00+00')} — "
        f"обработано {processed} точек, ошибок: {errors}"
    )


@track_job("hourly_stats_recalc_yesterday")
async def job_recalc_yesterday_hourly() -> None:
    """Пересчитывает все 24 часа вчерашнего LOCAL дня (Asia/Krasnoyarsk).

    Запускается в 06:35 МСК — после OLAP enrichment (05:26 МСК), который заполняет
    тайминги (cooked_time, opened_at, etc.) за вчера. Без этого avg_cook_time и др.
    будут NULL весь день.

    ВАЖНО: итерация по LOCAL calendar day (не UTC), чтобы покрыть все часы рабочего дня.
    """
    now_local = datetime.now(DEFAULT_TZ)
    yesterday_local = (now_local - timedelta(days=1)).date()

    branches = get_all_branches()
    if not branches:
        logger.warning("[hourly_stats_recalc] Нет активных точек")
        return

    processed = 0
    errors = 0
    for b in branches:
        for h in range(24):
            # Строим aware local datetime → конвертируем в UTC для записи в TIMESTAMPTZ
            local_hour = datetime(
                yesterday_local.year, yesterday_local.month, yesterday_local.day,
                h, 0, 0, tzinfo=DEFAULT_TZ,
            )
            hour_utc = local_hour.astimezone(timezone.utc)
            try:
                await aggregate_hour(b["tenant_id"], b["name"], hour_utc)
                processed += 1
            except Exception as e:
                logger.error(
                    f"[hourly_stats_recalc] Ошибка {b['name']} (tenant={b['tenant_id']}) "
                    f"за {hour_utc.isoformat()}: {e}",
                    exc_info=True,
                )
                errors += 1

    logger.info(
        f"[hourly_stats_recalc] Пересчёт {yesterday_local} (local) — "
        f"обработано {processed} точек×часов, ошибок: {errors}"
    )

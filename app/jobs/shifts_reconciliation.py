"""
shifts_reconciliation.py — пересверка смен из schedule API.

Два джоба:
  job_shifts_reconciliation_daily()   — каждый день в 04:00 МСК
  job_shifts_reconciliation_weekly()  — каждый понедельник в 08:00 МСК

Источник: GET /api/v2/employees/schedule (ShiftsBackfiller)
После перезаписи shifts_raw → пересчитывает fot_daily через run_fot_pipeline.

Порядок запуска утром:
  03:35  hourly_stats_recalc_yesterday
  04:00  shifts_reconciliation_daily  ← перезаписывает shifts_raw за вчера
  04:30  fot_pipeline                 ← считает ФОТ по актуальным сменам
  05:25  morning_report

Порядок запуска в понедельник:
  08:00  shifts_reconciliation_weekly ← перезаписывает shifts_raw за неделю
  08:30  weekly_report                ← считает недельный ФОТ
"""

import logging
from datetime import date, datetime, timedelta, timezone

from app.database_pg import get_active_tenants_with_tokens
from app.jobs.fot_pipeline import run_fot_pipeline
from app.onboarding.backfill_shifts_generic import ShiftsBackfiller
from app.utils.job_tracker import track_job

logger = logging.getLogger(__name__)

_UTC7 = timezone(timedelta(hours=7))


def _yesterday_utc7() -> date:
    """Вчерашняя дата для UTC+7 (все текущие точки)."""
    return (datetime.now(_UTC7) - timedelta(days=1)).date()


@track_job("shifts_reconciliation_daily")
async def job_shifts_reconciliation_daily() -> None:
    """Пересверка смен за вчера для всех тенантов. Запуск: 04:00 МСК.

    Перезаписывает shifts_raw из /api/v2/employees/schedule за вчера,
    затем пересчитывает fot_daily за тот же день.
    """
    yesterday = _yesterday_utc7()
    tenants = await get_active_tenants_with_tokens()
    if not tenants:
        logger.warning("[shifts_reconciliation_daily] Нет активных тенантов")
        return

    logger.info(f"[shifts_reconciliation_daily] Запуск за {yesterday}, тенантов: {len(tenants)}")

    for tenant in tenants:
        tenant_id = tenant["id"]
        try:
            backfiller = ShiftsBackfiller(tenant_id, date_from=yesterday, date_to=yesterday)
            await backfiller.run()
            logger.info(f"  ✓ shifts пересверены tenant={tenant_id} {yesterday}")
        except Exception as e:
            logger.error(f"  ✗ shifts backfill tenant={tenant_id} {yesterday}: {e}", exc_info=True)
            continue

        try:
            result = await run_fot_pipeline(target_date=yesterday, tenant_id=tenant_id, notify=False)
            logger.info(
                f"  ✓ fot_pipeline tenant={tenant_id} {yesterday}: "
                f"branches={result.get('branches', 0)}, rows={result.get('rows_saved', 0)}"
            )
        except Exception as e:
            logger.error(f"  ✗ fot_pipeline tenant={tenant_id} {yesterday}: {e}", exc_info=True)

    logger.info(f"[shifts_reconciliation_daily] Завершено за {yesterday}")


@track_job("shifts_reconciliation_weekly")
async def job_shifts_reconciliation_weekly() -> None:
    """Пересверка смен за прошлую неделю (Пн–Вс). Запуск: каждый Пн в 08:00 МСК.

    Перезаписывает shifts_raw из /api/v2/employees/schedule за 7 дней,
    затем пересчитывает fot_daily за каждый день недели.
    """
    today = (datetime.now(_UTC7)).date()
    # Пн–Вс прошлой недели: today — это понедельник UTC+7
    week_end = today - timedelta(days=1)    # воскресенье
    week_start = today - timedelta(days=7)  # понедельник прошлой недели

    tenants = await get_active_tenants_with_tokens()
    if not tenants:
        logger.warning("[shifts_reconciliation_weekly] Нет активных тенантов")
        return

    logger.info(
        f"[shifts_reconciliation_weekly] Запуск за {week_start}—{week_end}, "
        f"тенантов: {len(tenants)}"
    )

    days = [week_start + timedelta(days=i) for i in range((week_end - week_start).days + 1)]

    for tenant in tenants:
        tenant_id = tenant["id"]
        try:
            backfiller = ShiftsBackfiller(tenant_id, date_from=week_start, date_to=week_end)
            await backfiller.run()
            logger.info(f"  ✓ shifts пересверены tenant={tenant_id} {week_start}—{week_end}")
        except Exception as e:
            logger.error(
                f"  ✗ shifts backfill tenant={tenant_id} {week_start}—{week_end}: {e}",
                exc_info=True,
            )
            continue

        for target_day in days:
            try:
                result = await run_fot_pipeline(
                    target_date=target_day, tenant_id=tenant_id, notify=False
                )
                logger.info(
                    f"  ✓ fot_pipeline tenant={tenant_id} {target_day}: "
                    f"rows={result.get('rows_saved', 0)}"
                )
            except Exception as e:
                logger.error(
                    f"  ✗ fot_pipeline tenant={tenant_id} {target_day}: {e}", exc_info=True
                )

    logger.info(f"[shifts_reconciliation_weekly] Завершено за {week_start}—{week_end}")

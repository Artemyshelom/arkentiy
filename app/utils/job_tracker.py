"""
Трекинг scheduled jobs: декоратор @track_job + dashboard get_jobs_status().

Использование:
    @track_job("daily_report")
    async def job_send_morning_report(...):
        ...

Алерт при падении → telegram monitoring chat (через telegram.error_alert).
Статус хранится в job_logs (таблица уже есть).
"""

import logging
from datetime import datetime, timezone
from functools import wraps

logger = logging.getLogger(__name__)

# Реестр jobs: canonical_id → человеческое название.
# Порядок — порядок вывода в /jobs.
JOB_REGISTRY: dict[str, str] = {
    "daily_report":       "Утренний отчёт",
    "iiko_to_sheets":     "OLAP → Sheets",
    "audit_report":       "Аудит операций",
    "olap_enrichment":    "Обогащение OLAP",
    "competitor_monitor": "Мониторинг конкурентов",
    "late_alerts":        "Алерты опозданий",
    "cancel_sync":        "Синхронизация отмен",
    "recurring_billing":  "Биллинг SaaS",
    "fot_daily":          "ФОТ-пайплайн",
}

# LIKE-паттерны для поиска в job_logs.
# Список: матчим по ANY из паттернов (покрывает старые имена вида morning_report_utc7).
_JOB_PATTERNS: dict[str, list[str]] = {
    "daily_report":       ["daily_report%", "morning_report%"],
    "iiko_to_sheets":     ["iiko_to_sheets%"],
    "audit_report":       ["audit_report%"],
    "olap_enrichment":    ["olap_enrichment%"],
    "competitor_monitor": ["competitor_monitor%"],
    "late_alerts":        ["late_alerts%"],
    "cancel_sync":        ["cancel_sync%"],
    "recurring_billing":  ["recurring_billing%"],
    "fot_daily":          ["fot_daily%"],
}


def track_job(job_id: str):
    """
    Декоратор для отслеживания scheduled job.
    - Создаёт запись в job_logs со status='running' при старте.
    - Обновляет статус на 'ok' при успехе, 'error' при исключении.
    - При исключении отправляет алерт в Telegram и пробрасывает ошибку.
    """
    job_display_name = JOB_REGISTRY.get(job_id, job_id)

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            from app.database_pg import get_pool
            from app.clients.telegram import error_alert

            pool = get_pool()
            row = await pool.fetchrow(
                "INSERT INTO job_logs (tenant_id, job_name, status) VALUES (1, $1, 'running') RETURNING id",
                job_id,
            )
            log_id = row["id"]
            try:
                result = await func(*args, **kwargs)
                await pool.execute(
                    "UPDATE job_logs SET finished_at = now(), status = 'ok' WHERE id = $1",
                    log_id,
                )
                return result
            except Exception as e:
                err_str = str(e)[:500]
                await pool.execute(
                    "UPDATE job_logs SET finished_at = now(), status = 'error', error = $1 WHERE id = $2",
                    err_str, log_id,
                )
                await error_alert(job_display_name, err_str)
                raise

        return wrapper
    return decorator


async def get_jobs_status() -> list[dict]:
    """
    Возвращает последний статус каждого job из реестра.
    Ищет в job_logs по LIKE-паттернам (покрывает старые записи вида morning_report_utc7).
    """
    from app.database_pg import get_pool

    pool = get_pool()

    result = []
    for job_id, job_name in JOB_REGISTRY.items():
        patterns = _JOB_PATTERNS.get(job_id, [f"{job_id}%"])
        row = await pool.fetchrow(
            """SELECT job_name, status, started_at, finished_at, error, details
               FROM job_logs
               WHERE job_name LIKE ANY($1::text[])
               ORDER BY started_at DESC
               LIMIT 1""",
            patterns,
        )
        if row:
            started = row["started_at"]
            finished = row["finished_at"]
            duration_sec = None
            if finished and started:
                duration_sec = round((finished - started).total_seconds())
            result.append({
                "job_id":       job_id,
                "name":         job_name,
                "status":       row["status"],
                "started_at":   started,
                "finished_at":  finished,
                "duration_sec": duration_sec,
                "error":        row["error"],
            })
        else:
            result.append({
                "job_id":       job_id,
                "name":         job_name,
                "status":       "never",
                "started_at":   None,
                "finished_at":  None,
                "duration_sec": None,
                "error":        None,
            })

    return result

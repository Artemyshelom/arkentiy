"""
Health check мониторинг.

GET /health — пингуется UptimeRobot каждые 5 минут.
job_backup_sqlite — ежедневный бэкап SQLite в Google Drive.
"""

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from fastapi import APIRouter

from app.clients import telegram
from app.clients.google_sheets import backup_file_to_drive
from app.config import get_settings
from app.db import DB_PATH, BACKEND, log_job_start, log_job_finish

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(tags=["Health"])

_start_time = datetime.now(timezone.utc)


@router.get("/health")
async def health_check():
    """
    Health check endpoint.
    Пингуется UptimeRobot для мониторинга доступности.
    """
    uptime_seconds = int((datetime.now(timezone.utc) - _start_time).total_seconds())

    db_ok = False
    try:
        if BACKEND == "sqlite":
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("SELECT 1")
                db_ok = True
        else:
            from app.db import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            db_ok = True
    except Exception as e:
        logger.error(f"Health check: БД недоступна: {e}")

    return {
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": uptime_seconds,
        "database": "ok" if db_ok else "error",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/")
async def root():
    return {"service": "Аркентий (Интеграции Ёбидоёби)", "status": "running"}


async def job_backup_sqlite() -> None:
    """
    Ежедневный бэкап SQLite в Google Drive.
    Запускается в main.py через APScheduler в 02:00.
    В PG-режиме бэкап пропускается (PG имеет свою стратегию бэкапа).
    """
    if BACKEND != "sqlite":
        logger.info("job_backup_sqlite: PG backend — SQLite бэкап не нужен, пропуск")
        return
    log_id = await log_job_start("backup_sqlite")
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    backup_path = Path(f"/tmp/ebidoebi_backup_{date_str}.db")

    try:
        shutil.copy2(DB_PATH, backup_path)
        logger.info(f"SQLite скопирован в {backup_path}")

        if settings.google_drive_backup_folder_id:
            file_id = await backup_file_to_drive(
                str(backup_path),
                filename=f"ebidoebi_backup_{date_str}.db",
                folder_id=settings.google_drive_backup_folder_id,
            )
            if file_id:
                logger.info(f"Бэкап загружен в Drive: {file_id}")
                await log_job_finish(log_id, "ok", f"Drive file_id: {file_id}")
            else:
                await log_job_finish(log_id, "error", "Не удалось загрузить в Drive")
        else:
            logger.warning("GOOGLE_DRIVE_BACKUP_FOLDER_ID не настроен, бэкап только локально")
            await log_job_finish(log_id, "ok", "Только локальный бэкап")

    except Exception as e:
        logger.error(f"Ошибка бэкапа SQLite: {e}")
        await telegram.error_alert("backup_sqlite", str(e))
        await log_job_finish(log_id, "error", str(e))
    finally:
        if backup_path.exists():
            backup_path.unlink()

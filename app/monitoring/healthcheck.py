"""
Health check мониторинг.

GET /health — пингуется UptimeRobot каждые 5 минут.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from app.config import get_settings

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

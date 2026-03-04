"""
Утилиты мультитенантности.

run_for_all_tenants(job_fn) — универсальный враппер:
запускает async job для каждого активного тенанта, устанавливая ctx_tenant_id.
"""

import logging
from functools import wraps

from app.ctx import ctx_tenant_id

logger = logging.getLogger(__name__)


def run_for_all_tenants(job_fn):
    """Декоратор: запускает job для каждого активного тенанта по очереди.

    Внутри вызова job_fn доступен ctx_tenant_id.get() для текущего тенанта.
    job_fn должен принимать keyword-аргумент tenant_id.
    Дополнительные kwargs пробрасываются как есть.
    """

    @wraps(job_fn)
    async def wrapper(**kwargs):
        from app.database_pg import get_pool

        try:
            pool = get_pool()
            rows = await pool.fetch(
                "SELECT id, slug FROM tenants WHERE status = 'active' ORDER BY id"
            )
        except Exception as e:
            logger.error(f"[run_for_all_tenants] Не удалось получить тенантов: {e}")
            return

        for row in rows:
            tid = row["id"]
            token = ctx_tenant_id.set(tid)
            try:
                await job_fn(tenant_id=tid, **kwargs)
            except Exception as e:
                logger.error(
                    f"[{job_fn.__name__}] Ошибка для tenant_id={tid} ({row['slug']}): {e}",
                    exc_info=True,
                )
            finally:
                ctx_tenant_id.reset(token)

    return wrapper

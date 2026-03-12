"""
rates_cache_updater.py — ежедневное обновление кеша почасовых ставок.

Заполняет таблицу employee_rates_cache из iiko BO API (salary endpoint).
Используется для real-time расчёта ФОТ поваров в /статус.

Расписание: каждый день в 03:30 МСК (до shifts_reconciliation в 04:00 и fot_pipeline в 04:30).
"""

import logging
from datetime import date

import httpx

from app.clients.iiko_auth import get_bo_token
from app.clients.iiko_schedule import fetch_salary_map
from app.database_pg import (
    get_active_tenants_with_tokens,
    get_branches_from_db,
    upsert_rates_cache,
)
from app.utils.job_tracker import track_job

logger = logging.getLogger(__name__)


@track_job("rates_cache_updater")
async def job_rates_cache_updater() -> None:
    """Обновляет employee_rates_cache для всех тенантов и точек. Запуск: 03:30 МСК."""
    today = date.today()
    tenants = await get_active_tenants_with_tokens()

    if not tenants:
        logger.warning("[rates_cache_updater] Нет активных тенантов")
        return

    logger.info(f"[rates_cache_updater] Запуск за {today}, тенантов: {len(tenants)}")

    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        for tenant in tenants:
            tenant_id = tenant["id"]
            branches = await get_branches_from_db(tenant_id)

            # Группируем точки по BO-серверу (один fetch_salary_map на сервер)
            by_server: dict[str, dict] = {}
            for branch in branches:
                bo_url = branch.get("bo_url", "")
                if not bo_url:
                    continue
                if bo_url not in by_server:
                    by_server[bo_url] = {
                        "bo_url": bo_url,
                        "bo_login": branch.get("bo_login"),
                        "bo_password": branch.get("bo_password"),
                        "branches": [],
                    }
                by_server[bo_url]["branches"].append(branch)

            for bo_url, srv in by_server.items():
                try:
                    token = await get_bo_token(
                        bo_url,
                        client=client,
                        bo_login=srv["bo_login"],
                        bo_password=srv["bo_password"],
                    )
                except Exception as e:
                    logger.error(f"  ✗ Auth {bo_url} tenant={tenant_id}: {e}")
                    continue

                try:
                    salary_map = await fetch_salary_map(bo_url, client, token, today)
                except Exception as e:
                    logger.error(f"  ✗ fetch_salary_map {bo_url} tenant={tenant_id}: {e}")
                    continue

                if not salary_map:
                    logger.warning(f"  ⚠ Нет ставок {bo_url} tenant={tenant_id}")
                    continue

                # Сохраняем ставки для каждой точки этого сервера
                for branch in srv["branches"]:
                    try:
                        await upsert_rates_cache(tenant_id, branch["name"], salary_map)
                        logger.info(
                            f"  ✓ {branch['name']} tenant={tenant_id}: "
                            f"{len(salary_map)} ставок обновлено"
                        )
                    except Exception as e:
                        logger.error(
                            f"  ✗ upsert_rates_cache [{branch['name']}] tenant={tenant_id}: {e}"
                        )

    logger.info("[rates_cache_updater] Завершено")

"""
Синхронизация отменённых заказов из iiko BO OLAP v2.

iiko Events API не отправляет статус "Отменена" — он просто пропускается.
Этот модуль раз в 3 минуты опрашивает OLAP v2 (/api/v2/reports/olap),
получает список отменённых заказов за сегодня и обновляет orders_raw + _states.

Endpoint: POST /api/v2/reports/olap?key=TOKEN (token auth, JSON body)
Возвращает JSON: {"data": [{"Delivery.Number": 292153, "Delivery.CancelCause": "Отказ гостя", ...}]}
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.utils.job_tracker import track_job

import httpx

from app.clients.iiko_auth import get_bo_token
from app.clients.iiko_bo_events import _states
from app.config import get_settings
from app.db import BACKEND, get_pool, get_branches

logger = logging.getLogger(__name__)

LOCAL_UTC_OFFSET = 7


async def _fetch_cancelled_from_server(
    bo_url: str, date_from: str, date_to: str,
    bo_login: str | None = None, bo_password: str | None = None,
) -> list[dict]:
    """
    Запрашивает OLAP v2 для одного BO-сервера.
    Возвращает [{delivery_num, cancel_cause, branch_name}] — только отменённые.
    """
    try:
        token = await get_bo_token(bo_url, bo_login=bo_login, bo_password=bo_password)
    except Exception as e:
        logger.warning(f"cancel_sync: token error for {bo_url}: {e}")
        return []

    body = {
        "reportType": "SALES",
        "buildSummary": "false",
        "groupByRowFields": [
            "Delivery.Number", "Delivery.CancelCause", "Department", "PayTypes",
        ],
        "aggregateFields": ["DishDiscountSumInt"],
        "filters": {
            "OpenDate.Typed": {
                "filterType": "DateRange",
                "periodType": "CUSTOM",
                "from": date_from,
                "to": date_to,
                "includeLow": "true",
                "includeHigh": "false",
            }
        },
    }

    try:
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            r = await client.post(
                f"{bo_url}/api/v2/reports/olap?key={token}",
                json=body,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code != 200:
                logger.warning(f"cancel_sync: OLAP v2 {r.status_code} from {bo_url}")
                return []

            data = r.json().get("data", [])
            seen: dict[tuple[str, str], dict] = {}
            for row in data:
                cause = row.get("Delivery.CancelCause")
                if cause:
                    dnum = str(row.get("Delivery.Number", ""))
                    dept = row.get("Department", "")
                    key = (dnum, dept)
                    if key not in seen:
                        seen[key] = {
                            "delivery_num": dnum,
                            "cancel_cause": cause,
                            "branch_name": dept,
                            "payment_type": row.get("PayTypes", ""),
                        }
            return list(seen.values())

    except Exception as e:
        logger.warning(f"cancel_sync: error fetching OLAP v2 from {bo_url}: {e}")
        return []


@track_job("cancel_sync")
async def job_cancel_sync(tenant_id: int = 1) -> None:
    """
    Основной job: опрашивает все BO-серверы, получает отменённые заказы,
    обновляет orders_raw.status + cancel_reason, убирает из _states.
    """
    branches = get_branches(tenant_id)
    if not branches:
        return

    utc_offset = branches[0].get("utc_offset", LOCAL_UTC_OFFSET)
    now_local = (
        datetime.now(tz=timezone.utc) + timedelta(hours=utc_offset)
    ).replace(tzinfo=None)
    today_iso = now_local.strftime("%Y-%m-%d")
    yesterday_iso = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_iso = (now_local + timedelta(days=1)).strftime("%Y-%m-%d")

    by_server: dict[tuple, dict] = {}
    for branch in branches:
        url = branch.get("bo_url", "")
        if not url:
            continue
        login = branch.get("bo_login") or ""
        password = branch.get("bo_password") or ""
        key = (url, login, password)
        if key not in by_server:
            by_server[key] = {"names": set(), "login": login or None, "password": password or None}
        by_server[key]["names"].add(branch["name"])

    all_cancelled: list[dict] = []
    for (bo_url, _, __), srv in by_server.items():
        branch_names = srv["names"]
        # Покрываем вчера+сегодня, чтобы к утреннему аудиту причины отмен были заполнены
        cancelled = await _fetch_cancelled_from_server(bo_url, yesterday_iso, tomorrow_iso,
                                                       srv["login"], srv["password"])
        for c in cancelled:
            if c["branch_name"] in branch_names:
                all_cancelled.append(c)

    pool = get_pool()
    updated = 0
    if all_cancelled:
        async with pool.acquire() as conn:
            for c in all_cancelled:
                result = await conn.execute(
                    """UPDATE orders_raw
                       SET status = 'Отменена',
                           cancel_reason = $1,
                           payment_type = COALESCE(NULLIF($2, ''), payment_type),
                           updated_at = now()
                       WHERE branch_name = $3 AND delivery_num = $4
                         AND status != 'Отменена'""",
                    c["cancel_cause"],
                    c.get("payment_type", ""),
                    c["branch_name"],
                    c["delivery_num"],
                )
                rows_affected = int(result.split()[-1])
                updated += rows_affected
                if rows_affected == 0:
                    await conn.execute(
                        """UPDATE orders_raw
                           SET cancel_reason = $1,
                               payment_type = COALESCE(NULLIF($2, ''), payment_type),
                               updated_at = now()
                           WHERE branch_name = $3 AND delivery_num = $4
                             AND status = 'Отменена'
                             AND (cancel_reason IS NULL OR cancel_reason = '')""",
                        c["cancel_cause"],
                        c.get("payment_type", ""),
                        c["branch_name"],
                        c["delivery_num"],
                    )

        for c in all_cancelled:
            state = _states.get(c["branch_name"])
            if state and c["delivery_num"] in state.deliveries:
                state.deliveries[c["delivery_num"]]["status"] = "Отменена"

        if updated:
            logger.info(f"cancel_sync: обновлено {updated} отменённых заказов из OLAP v2")

    # --- Фаза 2: зависшие заказы старше 2 дней ---
    stale_cutoff = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        stale_rows = await pool.fetch(
            """SELECT branch_name, delivery_num, date::text AS order_date
               FROM orders_raw
               WHERE date::text < $1
                 AND status NOT IN ('Закрыта', 'Отменена', 'Не подтверждена')""",
            stale_cutoff,
        )

        if stale_rows:
            cancelled_lookup: dict[tuple[str, str], dict] = {
                (c["branch_name"], c["delivery_num"]): c
                for c in all_cancelled
            }

            stale_dates = sorted({r["order_date"] for r in stale_rows})
            if stale_dates:
                extra_from = stale_dates[0]
                extra_to = stale_cutoff
                for (bo_url, _, __), srv in by_server.items():
                    branch_names = srv["names"]
                    rows = await _fetch_cancelled_from_server(bo_url, extra_from, extra_to,
                                                             srv["login"], srv["password"])
                    for c in rows:
                        if c["branch_name"] in branch_names:
                            cancelled_lookup[(c["branch_name"], c["delivery_num"])] = c

                stale_updated = 0
                async with pool.acquire() as conn:
                    for r in stale_rows:
                        branch, dnum = r["branch_name"], r["delivery_num"]
                        key = (branch, dnum)
                        if key in cancelled_lookup:
                            c = cancelled_lookup[key]
                            await conn.execute(
                                """UPDATE orders_raw
                                   SET status='Отменена',
                                       cancel_reason=COALESCE(NULLIF($1, ''), cancel_reason),
                                       payment_type=COALESCE(NULLIF($2, ''), payment_type),
                                       updated_at=now()
                                   WHERE branch_name=$3 AND delivery_num=$4
                                     AND status NOT IN ('Закрыта','Отменена')""",
                                c.get("cancel_cause", ""), c.get("payment_type", ""),
                                branch, dnum,
                            )
                        else:
                            await conn.execute(
                                """UPDATE orders_raw SET status='Закрыта', updated_at=now()
                                   WHERE branch_name=$1 AND delivery_num=$2
                                     AND status NOT IN ('Закрыта','Отменена')""",
                                branch, dnum,
                            )
                        stale_updated += 1

                if stale_updated:
                    logger.info(f"cancel_sync: обновлено {stale_updated} зависших заказов")
    except Exception as e:
        logger.warning(f"cancel_sync: stale orders cleanup error: {e}")

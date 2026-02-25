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

import httpx

from app.clients.iiko_auth import get_bo_token
from app.clients.iiko_bo_events import _states
from app.config import get_settings
from app.database import DB_PATH

logger = logging.getLogger(__name__)

LOCAL_UTC_OFFSET = 7


async def _fetch_cancelled_from_server(
    bo_url: str, date_from: str, date_to: str
) -> list[dict]:
    """
    Запрашивает OLAP v2 для одного BO-сервера.
    Возвращает [{delivery_num, cancel_cause, branch_name}] — только отменённые.
    """
    try:
        token = await get_bo_token(bo_url)
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


async def job_cancel_sync() -> None:
    """
    Основной job: опрашивает все BO-серверы, получает отменённые заказы,
    обновляет orders_raw.status + cancel_reason, убирает из _states.
    """
    settings = get_settings()
    now_local = (
        datetime.now(tz=timezone.utc) + timedelta(hours=LOCAL_UTC_OFFSET)
    ).replace(tzinfo=None)
    today_iso = now_local.strftime("%Y-%m-%d")
    yesterday_iso = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_iso = (now_local + timedelta(days=1)).strftime("%Y-%m-%d")

    by_url: dict[str, set[str]] = defaultdict(set)
    for branch in settings.branches:
        url = branch.get("bo_url", "")
        if url:
            by_url[url].add(branch["name"])

    all_cancelled: list[dict] = []
    for bo_url, branch_names in by_url.items():
        # Покрываем вчера+сегодня, чтобы к утреннему аудиту причины отмен были заполнены
        cancelled = await _fetch_cancelled_from_server(bo_url, yesterday_iso, tomorrow_iso)
        for c in cancelled:
            if c["branch_name"] in branch_names:
                all_cancelled.append(c)

    import aiosqlite

    updated = 0
    if all_cancelled:
        async with aiosqlite.connect(DB_PATH) as db:
            for c in all_cancelled:
                now_utc_str = datetime.now(timezone.utc).isoformat()
                cursor = await db.execute(
                    """UPDATE orders_raw
                       SET status = 'Отменена',
                           cancel_reason = ?,
                           payment_type = COALESCE(NULLIF(?, ''), payment_type),
                           updated_at = ?
                       WHERE branch_name = ? AND delivery_num = ?
                         AND status != 'Отменена'""",
                    (
                        c["cancel_cause"],
                        c.get("payment_type", ""),
                        now_utc_str,
                        c["branch_name"],
                        c["delivery_num"],
                    ),
                )
                updated += cursor.rowcount
                if cursor.rowcount == 0:
                    await db.execute(
                        """UPDATE orders_raw
                           SET cancel_reason = ?,
                               payment_type = COALESCE(NULLIF(?, ''), payment_type),
                               updated_at = ?
                           WHERE branch_name = ? AND delivery_num = ?
                             AND status = 'Отменена'
                             AND (cancel_reason IS NULL OR cancel_reason = '')""",
                        (
                            c["cancel_cause"],
                            c.get("payment_type", ""),
                            now_utc_str,
                            c["branch_name"],
                            c["delivery_num"],
                        ),
                    )
            await db.commit()

        for c in all_cancelled:
            state = _states.get(c["branch_name"])
            if state and c["delivery_num"] in state.deliveries:
                state.deliveries[c["delivery_num"]]["status"] = "Отменена"

        if updated:
            logger.info(f"cancel_sync: обновлено {updated} отменённых заказов из OLAP v2")

    # --- Фаза 2: зависшие заказы старше 2 дней ---
    # Заказы с не-финальным статусом (Новая, В пути, Готовится и т.д.) старше 2 дней
    # точно завершены в iiko. Проверяем через OLAP: если отменены — ставим "Отменена",
    # если нет — значит закрыты (доставлены/выданы).
    stale_cutoff = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT branch_name, delivery_num, date
                   FROM orders_raw
                   WHERE date < ?
                     AND status NOT IN ('Закрыта', 'Отменена', 'Не подтверждена')
                """,
                (stale_cutoff,),
            )
            stale_rows = await cursor.fetchall()

        if stale_rows:
            cancelled_lookup: dict[tuple[str, str], dict] = {
                (c["branch_name"], c["delivery_num"]): c
                for c in all_cancelled
            }

            stale_dates = sorted({r[2] for r in stale_rows})
            if stale_dates:
                extra_from = stale_dates[0]
                extra_to = stale_cutoff
                for bo_url, branch_names in by_url.items():
                    rows = await _fetch_cancelled_from_server(bo_url, extra_from, extra_to)
                    for c in rows:
                        if c["branch_name"] in branch_names:
                            cancelled_lookup[(c["branch_name"], c["delivery_num"])] = c

                now_utc = datetime.now(timezone.utc).isoformat()
                stale_updated = 0
                async with aiosqlite.connect(DB_PATH) as db:
                    for branch, dnum, dt in stale_rows:
                        key = (branch, dnum)
                        if key in cancelled_lookup:
                            c = cancelled_lookup[key]
                            await db.execute(
                                """UPDATE orders_raw
                                   SET status='Отменена',
                                       cancel_reason=COALESCE(NULLIF(?, ''), cancel_reason),
                                       payment_type=COALESCE(NULLIF(?, ''), payment_type),
                                       updated_at=?
                                   WHERE branch_name=? AND delivery_num=?
                                     AND status NOT IN ('Закрыта','Отменена')""",
                                (c.get("cancel_cause", ""), c.get("payment_type", ""),
                                 now_utc, branch, dnum),
                            )
                        else:
                            await db.execute(
                                """UPDATE orders_raw SET status='Закрыта', updated_at=?
                                   WHERE branch_name=? AND delivery_num=?
                                     AND status NOT IN ('Закрыта','Отменена')""",
                                (now_utc, branch, dnum),
                            )
                        stale_updated += 1
                    await db.commit()

                if stale_updated:
                    logger.info(f"cancel_sync: обновлено {stale_updated} зависших заказов")
    except Exception as e:
        logger.warning(f"cancel_sync: stale orders cleanup error: {e}")

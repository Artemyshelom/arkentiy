"""
Синхронизация отменённых заказов из iiko BO OLAP v2.

iiko Events API не отправляет статус "Отменена" — он просто пропускается.
Этот модуль раз в 3 минуты опрашивает OLAP v2 (/api/v2/reports/olap),
получает список отменённых заказов за сегодня и обновляет orders_raw + _states.

Endpoint: POST /api/v2/reports/olap?key=TOKEN (token auth, JSON body)
Возвращает JSON: {"data": [{"Delivery.Number": 292153, "Delivery.CancelCause": "Отказ гостя", ...}]}
"""

import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from app.clients.iiko_bo_events import _states
from app.config import get_settings
from app.database import DB_PATH

logger = logging.getLogger(__name__)

LOCAL_UTC_OFFSET = 7

OLAP_BODY_TEMPLATE = {
    "reportType": "SALES",
    "buildSummary": "false",
    "groupByRowFields": ["Delivery.Number", "Delivery.CancelCause", "Department"],
    "aggregateFields": ["DishDiscountSumInt"],
    "filters": {
        "OpenDate.Typed": {
            "filterType": "DateRange",
            "periodType": "CUSTOM",
            "from": "",   # заполняется динамически
            "to": "",     # заполняется динамически
            "includeLow": "true",
            "includeHigh": "false",
        }
    },
}


async def _get_bo_token(bo_url: str) -> str | None:
    settings = get_settings()
    sha1_pwd = hashlib.sha1(settings.iiko_bo_password.encode()).hexdigest()
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            r = await client.get(
                f"{bo_url}/api/auth?login={settings.iiko_bo_login}&pass={sha1_pwd}"
            )
            if r.status_code == 200 and len(r.text.strip()) == 36:
                return r.text.strip()
    except Exception as e:
        logger.warning(f"cancel_sync: token error for {bo_url}: {e}")
    return None


async def _fetch_cancelled_from_server(
    bo_url: str, date_from: str, date_to: str
) -> list[dict]:
    """
    Запрашивает OLAP v2 для одного BO-сервера.
    Возвращает [{delivery_num, cancel_cause, branch_name}] — только отменённые.
    """
    token = await _get_bo_token(bo_url)
    if not token:
        return []

    body = {
        "reportType": "SALES",
        "buildSummary": "false",
        "groupByRowFields": ["Delivery.Number", "Delivery.CancelCause", "Department"],
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
            cancelled = []
            for row in data:
                cause = row.get("Delivery.CancelCause")
                if cause:
                    cancelled.append({
                        "delivery_num": str(row.get("Delivery.Number", "")),
                        "cancel_cause": cause,
                        "branch_name": row.get("Department", ""),
                    })
            return cancelled

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
    tomorrow_iso = (now_local + timedelta(days=1)).strftime("%Y-%m-%d")

    by_url: dict[str, set[str]] = defaultdict(set)
    for branch in settings.branches:
        url = branch.get("bo_url", "")
        if url:
            by_url[url].add(branch["name"])

    all_cancelled: list[dict] = []
    for bo_url, branch_names in by_url.items():
        cancelled = await _fetch_cancelled_from_server(bo_url, today_iso, tomorrow_iso)
        for c in cancelled:
            if c["branch_name"] in branch_names:
                all_cancelled.append(c)

    if not all_cancelled:
        return

    import aiosqlite

    updated = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for c in all_cancelled:
            cursor = await db.execute(
                """UPDATE orders_raw
                   SET status = 'Отменена',
                       cancel_reason = ?,
                       updated_at = ?
                   WHERE branch_name = ? AND delivery_num = ?
                     AND status != 'Отменена'""",
                (
                    c["cancel_cause"],
                    datetime.now(timezone.utc).isoformat(),
                    c["branch_name"],
                    c["delivery_num"],
                ),
            )
            updated += cursor.rowcount
        await db.commit()

    # Обновляем in-memory _states — убираем отменённые из активных доставок
    for c in all_cancelled:
        state = _states.get(c["branch_name"])
        if state and c["delivery_num"] in state.deliveries:
            state.deliveries[c["delivery_num"]]["status"] = "Отменена"

    if updated:
        logger.info(f"cancel_sync: обновлено {updated} отменённых заказов из OLAP v2")

"""
Отчёт по текущему состоянию точки через iiko Web BO API.

Метрики (выручка, чеки, COGS, скидки): OLAP v2 через app/clients/iiko_bo_olap_v2.py.
Real-time данные (заказы, смены): app/clients/iiko_bo_events.py (event sourcing).

Точки и dept IDs — из /app/secrets/branches.json.
"""

import html
import logging
from datetime import datetime

import httpx

from app.clients.iiko_auth import get_bo_token
from app.clients.iiko_bo_events import get_branch_rt
from app.clients.iiko_bo_olap_v2 import get_branch_olap_stats
from app.config import get_settings
from app.db import aggregate_orders_today
from app.utils.timezone import branch_tz, now_local

logger = logging.getLogger(__name__)
settings = get_settings()


async def get_cash_shift_open(branch: dict, date_iso: str) -> bool | None:
    """Возвращает True если кассовая смена открыта, False если закрыта, None если API недоступен."""
    bo_url = branch.get("bo_url", "")
    dept_id = branch.get("dept_id", "")
    if not bo_url:
        return None
    try:
        token = await get_bo_token(
            bo_url,
            bo_login=branch.get("bo_login") or None,
            bo_password=branch.get("bo_password") or None,
        )
        params = {
            "key": token,
            "openDateFrom": date_iso,
            "openDateTo": date_iso,
            "status": "OPEN",
        }
        if dept_id:
            params["departmentId"] = dept_id
        async with httpx.AsyncClient(verify=False, timeout=10) as client:
            resp = await client.get(
                f"{bo_url.rstrip('/')}/api/v2/cashshifts/list",
                params=params,
            )
        if resp.status_code == 200:
            data = resp.json()
            # Если есть хоть одна открытая смена — возвращаем True
            if isinstance(data, list):
                return len(data) > 0
            return None
        elif resp.status_code in (404, 403):
            return None  # эндпоинт недоступен
        return None
    except Exception as e:
        logger.debug(f"get_cash_shift_open [{branch['name']}]: {e}")
        return None


async def get_branch_status(branch: dict) -> dict:
    """
    Собирает метрики точки за сегодня.
    OLAP v2 (2 JSON-запроса) + orders_raw агрегат (скидки, времена).
    """
    tz = branch_tz(branch)
    today = now_local(tz)
    date_iso = today.strftime("%Y-%m-%d")

    revenue = None
    check_count = None
    avg_check = None
    cogs_pct = None
    discount_sum = None
    sailplay = None
    branch_olap = {}

    try:
        # Передаём все ветки тенанта чтобы OLAP запросился к правильному серверу
        all_branches = get_available_branches()
        olap = await get_branch_olap_stats(today, branches=all_branches)
        branch_olap = olap.get(branch["name"], {})
        revenue = branch_olap.get("revenue_net")
        if revenue is not None:
            revenue = round(revenue)
        check_count = branch_olap.get("check_count")
        cogs_pct = branch_olap.get("cogs_pct")
        discount_sum = branch_olap.get("discount_sum")
        sailplay = branch_olap.get("sailplay")
        if revenue and check_count:
            avg_check = round(revenue / check_count)
    except Exception as e:
        logger.error(f"Ошибка OLAP v2 [{branch['name']}]: {e}")

    rt_data = get_branch_rt(branch["name"])

    orders_agg = {}
    try:
        orders_agg = await aggregate_orders_today(branch["name"], date_iso)
    except Exception as e:
        logger.warning(f"Ошибка aggregate_orders_today [{branch['name']}]: {e}")

    cash_shift_open = await get_cash_shift_open(branch, date_iso)

    return {
        "name": branch["name"],
        "city": branch.get("city", ""),
        "revenue": revenue,
        "check_count": check_count,
        "avg_check": avg_check,
        "cogs_pct": cogs_pct,
        "discount_sum": discount_sum,
        "discount_types_agg": orders_agg.get("discount_types_agg", []),
        "sailplay": sailplay,
        "tz": tz,
        "active_orders": rt_data["active_orders"] if rt_data else None,
        "delivered_today": rt_data["delivered_today"] if rt_data else None,
        "orders_new": rt_data["orders_new"] if rt_data else None,
        "orders_before_dispatch": rt_data["orders_before_dispatch"] if rt_data else None,
        "orders_cooking": rt_data["orders_cooking"] if rt_data else None,
        "orders_ready": rt_data["orders_ready"] if rt_data else None,
        "orders_on_way": rt_data["orders_on_way"] if rt_data else None,
        "couriers_on_shift": rt_data["couriers_on_shift"] if rt_data else None,
        "cooks_on_shift": rt_data["cooks_on_shift"] if rt_data else None,
        "delays": rt_data["delays"] if rt_data else None,
        "avg_cooking_min": rt_data["avg_cooking_min"] if rt_data else None,
        "avg_wait_min": rt_data["avg_wait_min"] if rt_data else None,
        "avg_delivery_min": rt_data["avg_delivery_min"] if rt_data else None,
        "cash_shift_open": cash_shift_open,
    }


def format_branch_status(data: dict) -> str:
    """Форматирует статус точки для Telegram."""
    name = data["name"]
    tz = data.get("tz") or settings.default_tz
    now_str = datetime.now(tz).strftime("%H:%M")

    lines = [f"📍 <b>{html.escape(name)}</b> — {now_str}"]

    if data["revenue"] is not None:
        lines.append(
            f"💰 Выручка: <b>{data['revenue']:,} ₽</b>".replace(",", " ")
        )
    else:
        lines.append("⚠️ Данные недоступны")

    if data.get("check_count") is not None:
        check_str = str(data["check_count"])
        avg_str = (
            f"{data['avg_check']:,} ₽".replace(",", " ")
            if data.get("avg_check")
            else "—"
        )
        lines.append(f"🧾 Чеков: {check_str} | Средний чек: {avg_str}")
    lines.append("")

    disc = data.get("discount_sum")
    disc_types = data.get("discount_types_agg") or []
    sail = data.get("sailplay")
    if disc is not None or sail is not None:
        disc_str = f"{int(disc):,} ₽".replace(",", " ") if disc else "—"
        sail_str = f"{int(sail):,} ₽".replace(",", " ") if sail else "—"
        lines.append(f"💸 Скидки: {disc_str} | SailPlay: {sail_str}")
        for dt in disc_types:
            if isinstance(dt, dict):
                cnt = dt.get("count", "")
                s = dt.get("sum", 0)
                cnt_str = f" x {cnt}" if cnt else ""
                s_str = f"{int(s):,} ₽".replace(",", " ") if s else "—"
                lines.append(f"   └ {dt.get('type', '?')}{cnt_str}: {s_str}")
            else:
                lines.append(f"   └ {dt}")
    cogs = data.get("cogs_pct")
    if cogs is not None:
        lines.append("")
        lines.append(f"📦 Себестоимость: {cogs:.1f}%")

    has_rt = data.get("active_orders") is not None

    cash_shift_open = data.get("cash_shift_open")
    if cash_shift_open is False:
        lines.append("")
        lines.append("🔴 Кассовая смена закрыта")
    elif cash_shift_open is True:
        lines.append("")
        lines.append("✅ Кассовая смена открыта")
    # None — не показываем (API недоступен)

    delays = data.get("delays")
    if has_rt:
        lines.append("")
        if delays and delays.get("total_delivered", 0) > 0:
            late = delays["late_count"]
            total = delays["total_delivered"]
            pct = late / total * 100 if total else 0
            avg_min = delays["avg_delay_min"]
            if late > 0:
                lines.append(f"🔴 Опозданий: {late} из {total} доставок ({pct:.1f}%) | среднее: {avg_min} мин")
            else:
                lines.append(f"✅ Опозданий: 0 из {total} доставок")

        active = data.get("active_orders", 0) or 0
        delivered = data.get("delivered_today", 0) or 0
        n_new = data.get("orders_new", 0) or 0
        n_cook = data.get("orders_cooking", 0) or 0
        n_ready = data.get("orders_ready", 0) or 0
        n_way = data.get("orders_on_way", 0) or 0
        cook = data.get("avg_cooking_min")
        wait = data.get("avg_wait_min")
        delivery = data.get("avg_delivery_min")
        lines.append(f"🚚 Заказы: {active} активных | доставлено: {delivered}")
        stages = [
            ("Новые",     n_new,  None),
            ("Готовятся", n_cook, f"среднее: {cook} мин" if cook else None),
            ("Готовы",    n_ready, f"ждут: {wait} мин" if wait else None),
            ("В пути",    n_way,  f"среднее: {delivery} мин" if delivery else None),
        ]
        for label, cnt, hint in stages:
            if cnt:
                hint_str = f"  ({hint})" if hint else "  (—)"
                lines.append(f"   {label + ':':<12}{cnt}{hint_str}")

        cooks = data.get("cooks_on_shift")
        couriers = data.get("couriers_on_shift")
        if cooks is not None or couriers is not None:
            parts = []
            if cooks is not None:
                parts.append(f"поваров: {cooks}")
            if couriers is not None:
                parts.append(f"курьеров: {couriers}")
            lines.append(f"👥 На смене: {', '.join(parts)}")
    else:
        lines.append("")
        lines.append("⏳ RT-данные загружаются, повтори через 30 сек")

    return "\n".join(lines)


def get_available_branches(query: str | frozenset | None = None) -> list[dict]:
    """
    Возвращает список точек текущего тенанта (из ctx_tenant_id).
    query=None/"" → все точки
    query=str      → фильтрация по подстроке в названии или городе
    query=frozenset → фильтрация по множеству городов (точное совпадение city)
    """
    try:
        from app.ctx import ctx_tenant_id
        from app.db import get_branches
        tenant_id = ctx_tenant_id.get()
        branches = get_branches(tenant_id)
        if not branches:
            branches = settings.branches
    except Exception:
        branches = settings.branches

    if not query:
        return branches
    if isinstance(query, frozenset):
        return [b for b in branches if b.get("city") in query]
    q = query.lower()
    return [
        b for b in branches
        if q in b["name"].lower() or q in b.get("city", "").lower()
    ]

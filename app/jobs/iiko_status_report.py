"""
Отчёт по текущему состоянию точки через iiko Web BO API.

Метрики (выручка, чеки, COGS, скидки, тайминги): OLAP v2 через app/clients/olap_queries.py.
Real-time данные (заказы, смены): app/clients/iiko_bo_events.py (event sourcing).

Точки и dept IDs — из /app/secrets/branches.json.
"""

import asyncio
import html
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from app.clients.iiko_auth import get_bo_token
from app.clients.iiko_bo_events import get_branch_rt
from app.clients.olap_queries import get_branch_olap_stats
from app.config import get_settings
from app.database_pg import get_daily_stats, get_realtime_fot
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


async def get_branch_status(branch: dict, prefetched_olap: dict | None = None) -> dict:
    """
    Собирает метрики точки за сегодня.
    OLAP v2 (2 JSON-запроса) + orders_raw агрегат (скидки, времена).
    prefetched_olap — словарь {branch_name: {...}} уже полученных OLAP-данных;
    если передан, HTTP-запрос к iiko BO не делается (оптимизация для пакетных вызовов).
    """
    tz = branch_tz(branch)
    local_now = now_local(tz)
    # Ночной режим: до 06:00 местного времени показываем данные за вчера.
    # Граница 06:00 согласована с _seed_sessions_from_db (iiko_bo_events.py).
    _NIGHT_GRACE_HOUR = 6
    _is_night_mode = local_now.hour < _NIGHT_GRACE_HOUR
    if _is_night_mode:
        date_iso = (local_now.date() - timedelta(days=1)).isoformat()
    else:
        date_iso = local_now.strftime("%Y-%m-%d")

    revenue = None
    check_count = None
    avg_check = None
    cogs_pct = None
    discount_sum = None
    sailplay = None
    branch_olap = {}

    try:
        if prefetched_olap is not None:
            branch_olap = prefetched_olap.get(branch["name"], {})
        else:
            # Fallback: одиночный вызов (например при refresh одной точки)
            all_branches = get_available_branches()
            olap = await get_branch_olap_stats(local_now, branches=all_branches)
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

    # В ночном режиме OLAP не возвращает данных за вчера — берём выручку и чеки из daily_stats.
    if _is_night_mode:
        try:
            ds = await get_daily_stats(branch["name"], date_iso, branch.get("tenant_id", 1))
            if ds:
                if ds.get("revenue") is not None:
                    revenue = round(ds["revenue"])
                if ds.get("orders_count") is not None:
                    check_count = ds["orders_count"]
                if revenue and check_count:
                    avg_check = round(revenue / check_count)
        except Exception as e:
            logger.warning(f"Ночной режим: ошибка daily_stats [{branch['name']}]: {e}")

    rt_data = get_branch_rt(branch["name"], branch.get("tenant_id", 1))

    # Параллельный сбор DB + cash shift вместо двух последовательных await
    async def _get_orders_agg() -> dict:
        try:
            return await aggregate_orders_today(branch["name"], date_iso, branch.get("tenant_id", 1))
        except Exception as e:
            logger.warning(f"Ошибка aggregate_orders_today [{branch['name']}]: {e}")
            return {}

    async def _get_rt_fot() -> Optional[dict]:
        try:
            return await get_realtime_fot(branch["name"], branch.get("tenant_id", 1))
        except Exception as e:
            logger.debug(f"get_realtime_fot [{branch['name']}]: {e}")
            return None

    orders_agg, cash_shift_open, rt_fot = await asyncio.gather(
        _get_orders_agg(),
        get_cash_shift_open(branch, date_iso),
        _get_rt_fot(),
    )

    # Если Events API ещё не загружен (revision=0 после рестарта) — подставляем
    # счётчики из orders_raw как fallback. Они менее оперативны, но лучше пустого экрана.
    db_fallback = rt_data is None

    # Мёрж разбивки скидок: суммы из OLAP DELIVERIES (корректные) + счётчики из orders_raw
    olap_disc_types = branch_olap.get("discount_types") or []
    if olap_disc_types:
        # Обогащаем OLAP-записи кол-вом заказов из orders_raw (orders_raw.sum — неверный, count — верный)
        orders_count_by_type = {
            d["type"]: d["count"]
            for d in orders_agg.get("discount_types_agg", [])
            if isinstance(d, dict)
        }
        for dt in olap_disc_types:
            dt["count"] = orders_count_by_type.get(dt["type"], "")
        discount_types_final = olap_disc_types
        # Логируем расхождение суммы разбивки с итогом (диагностика)
        if discount_sum:
            breakdown_total = sum(dt["sum"] for dt in olap_disc_types)
            if breakdown_total > 0 and abs(breakdown_total - discount_sum) / max(breakdown_total, discount_sum) > 0.01:
                logger.warning(
                    f"Скидки [{branch['name']}]: итог {discount_sum:.0f} ≠ сумма разбивки {breakdown_total:.0f}"
                )
    else:
        discount_types_final = orders_agg.get("discount_types_agg", [])

    return {
        "name": branch["name"],
        "city": branch.get("city", ""),
        "revenue": revenue,
        "check_count": check_count,
        "avg_check": avg_check,
        "cogs_pct": cogs_pct,
        "discount_sum": discount_sum,
        "discount_types_agg": discount_types_final,
        "sailplay": sailplay,
        "tz": tz,
        "active_orders": rt_data["active_orders"] if rt_data else orders_agg.get("active_count"),
        "delivered_today": rt_data["delivered_today"] if rt_data else orders_agg.get("delivered_count"),
        "orders_new": rt_data["orders_new"] if rt_data else None,
        "orders_before_dispatch": rt_data["orders_before_dispatch"] if rt_data else None,
        "orders_cooking": rt_data["orders_cooking"] if rt_data else None,
        "orders_ready": rt_data["orders_ready"] if rt_data else None,
        "orders_on_way": rt_data["orders_on_way"] if rt_data else None,
        "couriers_on_shift": rt_data["couriers_on_shift"] if rt_data else None,
        "cooks_on_shift": rt_data["cooks_on_shift"] if rt_data else None,
        "delays": rt_data["delays"] if rt_data else None,
        "avg_cooking_min": rt_data["avg_cooking_min"] if rt_data else branch_olap.get("avg_cooking_min"),
        "avg_wait_min": rt_data["avg_wait_min"] if rt_data else branch_olap.get("avg_wait_min"),
        "avg_delivery_min": rt_data["avg_delivery_min"] if rt_data else branch_olap.get("avg_delivery_min"),
        "cash_shift_open": cash_shift_open,
        "db_fallback": db_fallback,
        "rt_fot": rt_fot,
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

    # Кассовая смена — сразу после чеков
    cash_shift_open = data.get("cash_shift_open")
    if cash_shift_open is False:
        lines.append("🔴 Кассовая смена закрыта")
    elif cash_shift_open is True:
        lines.append("🟢 Кассовая смена открыта")
    lines.append("")

    disc = data.get("discount_sum")
    disc_types = data.get("discount_types_agg") or []
    sail = data.get("sailplay")
    if disc is not None or sail is not None:
        disc_str = f"{int(disc):,} ₽".replace(",", " ") if disc else "—"
        sail_str = f"{int(sail):,} ₽".replace(",", " ") if sail else "—"
        lines.append(f"💸 Скидки: {disc_str} | Оплата бонусами: {sail_str}")
        for dt in disc_types:
            if isinstance(dt, dict):
                cnt = dt.get("count", "")
                s = dt.get("sum", 0)
                dtype = dt.get("type", "?")
                if dtype == "SailPlay":
                    dtype = "Промокоды SailPlay"
                s_str = f"{int(s):,} ₽".replace(",", " ") if s else "—"
                cnt_str = f" ({cnt} шт.)" if cnt else ""
                lines.append(f"   └ {dtype}: {s_str}{cnt_str}")
            else:
                lines.append(f"   └ {dt}")
    cogs = data.get("cogs_pct")
    if cogs is not None:
        lines.append("")
        lines.append(f"📦 Себестоимость: {cogs:.1f}%")

    # Среднее время готовки/ожидания/доставки за сегодня (из OLAP или Events API)
    olap_cook = data.get("avg_cooking_min")
    olap_wait = data.get("avg_wait_min")
    olap_deliv = data.get("avg_delivery_min")
    if olap_cook or olap_wait or olap_deliv:
        parts = []
        if olap_cook:
            parts.append(f"готовка {olap_cook} мин")
        if olap_wait:
            parts.append(f"ожидание {olap_wait} мин")
        if olap_deliv:
            parts.append(f"доставка {olap_deliv} мин")
        lines.append(f"📈 Сегодня: {' · '.join(parts)}")

    has_rt = data.get("active_orders") is not None
    db_fallback = data.get("db_fallback", False)

    delays = data.get("delays")
    if has_rt:
        lines.append("")
        if not db_fallback and delays and delays.get("total_delivered", 0) > 0:
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
        if db_fallback:
            lines.append(f"🚚 Заказы: ~{active} активных | доставлено: ~{delivered}")
            lines.append("   ⏳ RT загружается, этапы появятся через ~30 сек")
        else:
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

        if not db_fallback:
            cooks = data.get("cooks_on_shift")
            couriers = data.get("couriers_on_shift")
            if cooks is not None or couriers is not None:
                parts = []
                if cooks is not None:
                    parts.append(f"поваров: {cooks}")
                if couriers is not None:
                    parts.append(f"курьеров: {couriers}")
                lines.append(f"👥 На смене: {', '.join(parts)}")

            # Real-time ФОТ поваров
            rt_fot = data.get("rt_fot")
            revenue = data.get("revenue")
            if rt_fot and rt_fot["fot"] > 0 and revenue and revenue > 0:
                pct = round(rt_fot["fot"] / revenue * 100, 1)
                lines.append(
                    f"💼 ФОТ поваров: ~{pct}% от выручки "
                    f"({rt_fot['cooks']} чел · {rt_fot['hours']}ч)"
                )

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
        else:
            branches = [{**b, "tenant_id": tenant_id} for b in branches]
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

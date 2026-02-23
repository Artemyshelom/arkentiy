"""
Отчёт по текущему состоянию точки через iiko Web BO API.

Простая выручка: GET /api/reports/sales (токен-аутентификация).
Чеки и доп. метрики: OLAP-пресеты через app/clients/iiko_bo_olap.py (cookie-сессия).
Real-time данные (заказы, смены): app/clients/iiko_bo_events.py (event sourcing).

Точки и dept IDs — из /app/secrets/branches.json.
Каждая точка может иметь bo_url — локальный сервер iiko Office.
"""

import hashlib
import html
import logging
import time
from datetime import datetime, timezone, timedelta

import httpx

from app.clients.iiko_bo_olap import IIKO_BO_BASE
from app.clients.iiko_bo_events import get_branch_rt
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

TOKEN_TTL = 3600

# Кеш токенов: {base_url: (token, timestamp)}
_bo_tokens: dict[str, tuple[str, float]] = {}


def branch_tz(branch: dict) -> timezone:
    offset = branch.get("utc_offset", 7)
    return timezone(timedelta(hours=offset))


def now_local(tz: timezone | None = None) -> datetime:
    if tz is None:
        tz = settings.default_tz
    return datetime.now(tz)


async def _get_bo_token(base_url: str = IIKO_BO_BASE) -> str:
    """Получает (или возвращает кешированный) API-токен iiko BO для указанного сервера."""
    cached = _bo_tokens.get(base_url)
    if cached and (time.time() - cached[1]) < TOKEN_TTL:
        return cached[0]

    login = settings.iiko_bo_login
    pwd_hash = hashlib.sha1(settings.iiko_bo_password.encode()).hexdigest()
    url = f"{base_url}/api/auth?login={login}&pass={pwd_hash}"

    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()
        token = r.text.strip()
        _bo_tokens[base_url] = (token, time.time())
        logger.info(f"iiko BO token обновлён: {base_url}")
        return token


async def get_branch_revenue(dept_id: str, tz: timezone | None = None, base_url: str = IIKO_BO_BASE) -> float:
    """Возвращает выручку точки за сегодня (по локальному TZ) через /api/reports/sales."""
    import xml.etree.ElementTree as ET
    token = await _get_bo_token(base_url)
    today = now_local(tz).strftime("%d.%m.%Y")
    url = (
        f"{base_url}/api/reports/sales"
        f"?key={token}&department={dept_id}&dateFrom={today}&dateTo={today}"
    )
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        value_str = root.findtext(".//value", "0") or "0"
        return float(value_str)


async def get_branch_status(branch: dict) -> dict:
    """
    Собирает метрики точки за сегодня.
    Использует локальный сервер точки (bo_url из конфига).
    """
    from app.clients.iiko_bo_olap import get_branch_olap_stats

    tz = branch_tz(branch)
    today = now_local(tz)
    bo_url = branch.get("bo_url") or IIKO_BO_BASE

    revenue = None
    check_count = None
    avg_check = None
    cogs_pct = None
    discount_sum = None
    sailplay = None

    try:
        revenue = await get_branch_revenue(branch["dept_id"], tz, bo_url)
        revenue = round(revenue) if revenue else None
    except Exception as e:
        logger.error(f"Ошибка выручки [{branch['name']}]: {e}")

    branch_olap = {}
    try:
        olap = await get_branch_olap_stats(today)
        branch_olap = olap.get(branch["name"], {})
        check_count = branch_olap.get("check_count")
        cogs_pct = branch_olap.get("cogs_pct")
        discount_sum = branch_olap.get("discount_sum")
        sailplay = branch_olap.get("sailplay")
        if revenue and check_count:
            avg_check = round(revenue / check_count)
    except Exception as e:
        logger.error(f"Ошибка OLAP [{branch['name']}]: {e}")

    rt_data = get_branch_rt(branch["name"])

    return {
        "name": branch["name"],
        "city": branch.get("city", ""),
        "revenue": revenue,
        "check_count": check_count,
        "avg_check": avg_check,
        "cogs_pct": cogs_pct,
        "discount_sum": discount_sum,
        "discount_types": branch_olap.get("discount_types", []) if branch_olap else [],
        "sailplay": sailplay,
        "tz": tz,
        "active_orders": rt_data["active_orders"] if rt_data else None,
        "delivered_today": rt_data["delivered_today"] if rt_data else None,
        "orders_before_dispatch": rt_data["orders_before_dispatch"] if rt_data else None,
        "orders_cooking": rt_data["orders_cooking"] if rt_data else None,
        "orders_ready": rt_data["orders_ready"] if rt_data else None,
        "orders_on_way": rt_data["orders_on_way"] if rt_data else None,
        "couriers_on_shift": rt_data["couriers_on_shift"] if rt_data else None,
        "cooks_on_shift": rt_data["cooks_on_shift"] if rt_data else None,
        "delays": rt_data["delays"] if rt_data else None,
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
    disc_types = data.get("discount_types") or []
    sail = data.get("sailplay")
    if disc is not None or sail is not None:
        disc_str = f"{int(disc):,} ₽".replace(",", " ") if disc else "—"
        sail_str = f"{int(sail):,} ₽".replace(",", " ") if sail else "—"
        lines.append(f"💸 Скидки: {disc_str} | SailPlay: {sail_str}")
        for t in disc_types:
            lines.append(f"   └ {t}")
    cogs = data.get("cogs_pct")
    if cogs is not None:
        lines.append("")
        lines.append(f"📦 Себестоимость: {cogs:.1f}%")

    # Проверяем, есть ли RT-данные
    has_rt = data.get("active_orders") is not None

    delays = data.get("delays")
    if has_rt:
        lines.append("")
        if delays and delays.get("total_delivered", 0) > 0:
            late = delays["late_count"]
            total = delays["total_delivered"]
            pct = late / total * 100 if total else 0
            avg_min = delays["avg_delay_min"]
            if late > 0:
                lines.append(f"🔴 Опозданий: {late} из {total} ({pct:.1f}%) | среднее: {avg_min} мин")
            else:
                lines.append(f"✅ Опозданий: 0 из {total}")

        active = data.get("active_orders", 0) or 0
        delivered = data.get("delivered_today", 0) or 0
        n_dispatch = data.get("orders_before_dispatch", 0) or 0
        n_cook = data.get("orders_cooking", 0) or 0
        n_ready = data.get("orders_ready", 0) or 0
        n_way = data.get("orders_on_way", 0) or 0
        # Заголовок
        lines.append(f"🚚 Заказы: {active} активных | доставлено: {delivered}")
        # Детализация вложенными строками
        if n_dispatch:
            # Показываем kitchen breakdown в скобках если есть данные
            cook_parts = []
            if n_cook:
                cook_parts.append(f"готовится: {n_cook}")
            if n_ready:
                cook_parts.append(f"готовы: {n_ready}")
            cook_hint = f" ({', '.join(cook_parts)})" if cook_parts else ""
            lines.append(f"   └ до отправки: {n_dispatch}{cook_hint}")
        if n_way:
            lines.append(f"   └ в пути: {n_way}")

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
    Возвращает список точек из конфига.
    query=None/"" → все точки
    query=str      → фильтрация по подстроке в названии или городе
    query=frozenset → фильтрация по множеству городов (точное совпадение city)
    """
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

"""
API endpoint /api/stats — операционные данные для внешних AI-агентов (Борис и др.).

GET /api/stats?metric=realtime|daily|period
Authorization: Bearer <token>

Токены хранятся в /app/secrets/api_keys.json:
  {"boris_agent": {"key": "brs_xxx", "tenant_id": 1, "modules": ["stats"]}}

Rate limit: 60 req/min на токен.
"""

import json
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.clients.iiko_bo_events import _states
from app.config import get_settings
from app.ctx import ctx_tenant_id
from app.database_pg import get_daily_stats, get_hourly_stats, get_period_stats, get_shifts_by_date
from app.jobs.iiko_status_report import get_available_branches
from app.jobs.late_alerts import ACTIVE_DELIVERY_STATUSES, LATE_MAX_MIN, LOCAL_UTC_OFFSET
from app.utils.timezone import DEFAULT_TZ

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/stats", tags=["Stats API"])

_bearer = HTTPBearer(auto_error=False)

# Rate limit per token: {token → [monotonic timestamps]}
_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 60  # req/min


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_keys_cache: Optional[dict[str, Any]] = None


def _load_api_keys() -> dict[str, Any]:
    global _keys_cache
    if _keys_cache is None:
        path = Path(settings.stats_api_keys_file)
        if path.exists():
            try:
                _keys_cache = json.loads(path.read_text(encoding="utf-8"))
                assert isinstance(_keys_cache, dict), "API keys должны быть dict"
                logger.info(f"[stats api] Загружено {len(_keys_cache)} API ключей из {path}")
            except Exception as e:
                logger.error(f"[stats api] Ошибка чтения {path}: {e}")
                _keys_cache = {}
        else:
            logger.warning(f"[stats api] {path} не найден — API недоступен")
            _keys_cache = {}
    return _keys_cache if _keys_cache is not None else {}


def _verify_token(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """Проверяет Bearer-токен, возвращает метаданные ключа."""
    if not creds:
        raise HTTPException(status_code=401, detail="Требуется Authorization: Bearer <token>")

    token = creds.credentials
    keys = _load_api_keys()

    for _name, meta in keys.items():
        if meta.get("key") == token:
            # Rate limit check
            now = time.monotonic()
            bucket = _rate_buckets[token]
            cutoff = now - 60.0
            while bucket and bucket[0] < cutoff:
                bucket.pop(0)
            if len(bucket) >= _RATE_LIMIT:
                raise HTTPException(status_code=429, detail="Rate limit exceeded (60 req/min)")
            bucket.append(now)
            return meta

    raise HTTPException(status_code=401, detail="Неверный токен")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today_iso() -> str:
    return date.today().isoformat()


def _parse_date(s: Optional[str], default: str) -> str:
    if not s:
        return default
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Неверный формат даты: {s!r}. Ожидается YYYY-MM-DD")


def _maybe_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _maybe_float(v: Any, ndigits: int = 1) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _set_tenant(tenant_id: int):
    return ctx_tenant_id.set(tenant_id)


# ---------------------------------------------------------------------------
# Realtime
# ---------------------------------------------------------------------------

def _build_realtime(tenant_id: int, branch_filter: Optional[str], city_filter: Optional[str]) -> dict:
    tok = _set_tenant(tenant_id)
    try:
        branches_cfg = get_available_branches()
    finally:
        ctx_tenant_id.reset(tok)

    now_utc = datetime.now(tz=timezone.utc)
    msk_offset = 3
    now_ts = (now_utc + timedelta(hours=msk_offset)).isoformat(timespec="seconds")
    now_local = (now_utc + timedelta(hours=LOCAL_UTC_OFFSET)).replace(tzinfo=None)

    branches_out = []
    for b in branches_cfg:
        name = b["name"]
        city = b.get("city", "")

        if branch_filter and branch_filter.lower() not in name.lower() and branch_filter.lower() not in city.lower():
            continue
        if city_filter and city_filter.lower() not in city.lower():
            continue

        state = _states.get((tenant_id, name))
        now_local_b = now_local  # one timezone for all branches (all UTC+7)

        active_orders = 0
        late_orders = 0

        if state:
            for d in state.deliveries.values():
                if d.get("is_self_service"):
                    continue
                if d.get("status") not in ACTIVE_DELIVERY_STATUSES:
                    continue
                active_orders += 1
                planned_raw = d.get("planned_time")
                if planned_raw:
                    try:
                        clean = planned_raw.replace("T", " ").split(".")[0]
                        planned_dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
                        overdue_min = (now_local_b - planned_dt).total_seconds() / 60
                        if 0 < overdue_min <= LATE_MAX_MIN:
                            late_orders += 1
                    except ValueError:
                        pass

        branches_out.append({
            "name": name,
            "city": city,
            "active_orders": active_orders,
            "late_orders": late_orders,
            "avg_cooking_time": state.avg_cooking_current_min if state else None,
            "avg_wait_time": state.avg_wait_current_min if state else None,
            "avg_delivery_time": state.avg_delivery_current_min if state else None,
        })

    return {"timestamp": now_ts, "branches": branches_out}


# ---------------------------------------------------------------------------
# Daily
# ---------------------------------------------------------------------------

async def _build_daily(
    tenant_id: int,
    date_iso: str,
    branch_filter: Optional[str],
    city_filter: Optional[str],
) -> dict:
    tok = _set_tenant(tenant_id)
    try:
        branches_cfg = get_available_branches(branch_filter or city_filter or None)
    finally:
        ctx_tenant_id.reset(tok)

    branches_out = []
    for b in branches_cfg:
        name = b["name"]
        city = b.get("city", "")
        if city_filter and city_filter.lower() not in city.lower():
            continue

        row = await get_daily_stats(name, date_iso, tenant_id)
        if not row:
            continue

        revenue = float(row.get("revenue") or 0)
        checks = int(row.get("orders_count") or 0)
        late = int(row.get("late_count") or 0)
        dlv = int(row.get("delivery_count") or row.get("total_delivered") or 0)

        branches_out.append({
            "name": name,
            "city": city,
            "revenue": round(revenue),
            "checks": checks,
            "avg_check": round(revenue / checks) if checks else 0,
            "cogs_pct": _maybe_float(row.get("cogs_pct")),
            "discounts": _maybe_int(row.get("discount_sum")),
            "discount_pct": round(
                float(row["discount_sum"]) / revenue * 100, 1
            ) if row.get("discount_sum") and revenue else None,
            "late_count": late,
            "late_pct": round(late / dlv * 100, 1) if dlv else None,
            "avg_cooking_time": _maybe_int(row.get("avg_cooking_min")),
            "avg_waiting_time": _maybe_int(row.get("avg_wait_min")),
            "avg_delivery_time": _maybe_int(row.get("avg_delivery_min")),
            "avg_total_time": (
                (int(row.get("avg_cooking_min") or 0))
                + (int(row.get("avg_wait_min") or 0))
                + (int(row.get("avg_delivery_min") or 0))
            ) or None,
            "new_customers": _maybe_int(row.get("new_customers")),
            "repeat_customers": _maybe_int(row.get("repeat_customers")),
        })

    total_revenue = sum(b["revenue"] for b in branches_out)
    total_checks = sum(b["checks"] for b in branches_out)
    return {
        "date": date_iso,
        "branches": branches_out,
        "totals": {
            "revenue": total_revenue,
            "checks": total_checks,
            "avg_check": round(total_revenue / total_checks) if total_checks else 0,
        },
    }


# ---------------------------------------------------------------------------
# Period
# ---------------------------------------------------------------------------

async def _build_period(
    tenant_id: int,
    date_from: str,
    date_to: str,
    branch_filter: Optional[str],
    city_filter: Optional[str],
) -> dict:
    tok = _set_tenant(tenant_id)
    try:
        branches_cfg = get_available_branches(branch_filter or city_filter or None)
    finally:
        ctx_tenant_id.reset(tok)

    branches_out = []
    for b in branches_cfg:
        name = b["name"]
        city = b.get("city", "")
        if city_filter and city_filter.lower() not in city.lower():
            continue

        row = await get_period_stats(name, date_from, date_to, tenant_id)
        if not row:
            continue

        revenue = float(row.get("revenue") or 0)
        checks = int(row.get("orders_count") or 0)
        late = int(row.get("late_count") or 0)
        dlv = int(row.get("total_delivered") or row.get("delivery_count") or 0)

        branches_out.append({
            "name": name,
            "city": city,
            "revenue": round(revenue),
            "checks": checks,
            "avg_check": round(revenue / checks) if checks else 0,
            "cogs_pct": _maybe_float(row.get("cogs_pct")),
            "discounts": _maybe_int(row.get("discount_sum")),
            "late_count": late,
            "late_pct": round(late / dlv * 100, 1) if dlv else None,
            "avg_cooking_time": _maybe_int(row.get("avg_cooking_min")),
            "avg_waiting_time": _maybe_int(row.get("avg_wait_min")),
            "avg_delivery_time": _maybe_int(row.get("avg_delivery_min")),
            "new_customers": _maybe_int(row.get("new_customers")),
            "repeat_customers": _maybe_int(row.get("repeat_customers")),
        })

    total_revenue = sum(b["revenue"] for b in branches_out)
    total_checks = sum(b["checks"] for b in branches_out)
    return {
        "from": date_from,
        "to": date_to,
        "branches": branches_out,
        "totals": {
            "revenue": total_revenue,
            "checks": total_checks,
            "avg_check": round(total_revenue / total_checks) if total_checks else 0,
        },
    }


# ---------------------------------------------------------------------------
# Hourly
# ---------------------------------------------------------------------------

async def _build_hourly(
    tenant_id: int,
    date_iso: str,
    branch_filter: Optional[str],
    city_filter: Optional[str],
) -> dict:
    tok = _set_tenant(tenant_id)
    try:
        branches_cfg = get_available_branches(branch_filter or city_filter or None)
    finally:
        ctx_tenant_id.reset(tok)

    hour_from = date_iso
    hour_to = (datetime.strptime(date_iso, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    branches_out = []
    for b in branches_cfg:
        name = b["name"]
        city = b.get("city", "")
        if city_filter and city_filter.lower() not in city.lower():
            continue

        rows = await get_hourly_stats(name, hour_from, hour_to, tenant_id)
        if not rows:
            continue

        hours_out = []
        for r in rows:
            h = r["hour"]
            # hour — TIMESTAMPTZ (aware UTC) → конвертируем в local для ответа
            if hasattr(h, "astimezone"):
                h = h.astimezone(DEFAULT_TZ)
            hour_str = h.isoformat() if hasattr(h, "isoformat") else str(h)
            cnt = int(r["orders_count"] or 0)
            rev = float(r["revenue"] or 0)
            late = int(r["late_count"] or 0)
            hours_out.append({
                "hour": hour_str,
                "orders_count": cnt,
                "revenue": round(rev),
                "avg_check": round(rev / cnt) if cnt else 0,
                "avg_cook_time": _maybe_float(r.get("avg_cook_time")),
                "avg_courier_wait": _maybe_float(r.get("avg_courier_wait")),
                "avg_delivery_time": _maybe_float(r.get("avg_delivery_time")),
                "late_count": late,
                "late_percent": _maybe_float(r.get("late_percent")),
                "cooks_on_shift": int(r["cooks_on_shift"] or 0),
                "couriers_on_shift": int(r["couriers_on_shift"] or 0),
                "orders_in_progress": int(r["orders_in_progress"] or 0),
            })

        # Не отдавать ветки где нет ни заказов, ни персонала
        hours_out = [h for h in hours_out if h["orders_count"] > 0 or h["cooks_on_shift"] > 0 or h["couriers_on_shift"] > 0]

        branches_out.append({
            "name": name,
            "city": city,
            "hours": hours_out,
        })

    return {
        "date": date_iso,
        "branches": branches_out,
    }


# ---------------------------------------------------------------------------
# Shifts
# ---------------------------------------------------------------------------

async def _build_shifts(
    tenant_id: int,
    date_iso: str,
    branch_filter: Optional[str],
    city_filter: Optional[str],
) -> dict:
    tok = _set_tenant(tenant_id)
    try:
        branches_cfg = get_available_branches()
    finally:
        ctx_tenant_id.reset(tok)

    branch_city = {b["name"]: b.get("city", "") for b in branches_cfg}
    all_shifts = await get_shifts_by_date(date_iso, tenant_id)

    # Фильтруем по branch/city
    branches_out = {}
    for row in all_shifts:
        name = row["branch_name"]
        city = branch_city.get(name, "")

        if branch_filter and branch_filter.lower() not in name.lower() and branch_filter.lower() not in city.lower():
            continue
        if city_filter and city_filter.lower() not in city.lower():
            continue

        if name not in branches_out:
            branches_out[name] = {
                "name": name,
                "city": city,
                "total": 0,
                "on_shift": 0,  # clock_out is NULL — ещё работают
                "roles": {},
            }

        entry = branches_out[name]
        entry["total"] += 1
        if row["clock_out"] is None:
            entry["on_shift"] += 1

        role = row["role_class"] or "—"
        if role not in entry["roles"]:
            entry["roles"][role] = {"total": 0, "on_shift": 0}
        entry["roles"][role]["total"] += 1
        if row["clock_out"] is None:
            entry["roles"][role]["on_shift"] += 1

    return {
        "date": date_iso,
        "branches": list(branches_out.values()),
        "totals": {
            "total": sum(b["total"] for b in branches_out.values()),
            "on_shift": sum(b["on_shift"] for b in branches_out.values()),
        },
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("", summary="Операционная статистика для AI-агентов")
async def get_stats(
    metric: str = Query(..., description="realtime | daily | period | shifts | hourly"),
    branch: Optional[str] = Query(None, description="Фильтр по названию точки или подстроке"),
    city: Optional[str] = Query(None, description="Фильтр по городу: Барнаул, Томск, Абакан и т.д."),
    date: Optional[str] = Query(None, description="YYYY-MM-DD (для daily/shifts, по умолчанию — вчера)"),
    from_: Optional[str] = Query(None, alias="from", description="YYYY-MM-DD (для period)"),
    to: Optional[str] = Query(None, description="YYYY-MM-DD (для period)"),
    token_meta: dict = Depends(_verify_token),
) -> dict:
    """Операционные метрики для AI-агентов.

    - **realtime** — активные заказы и опоздания из in-memory состояния
    - **daily** — итоги дня из daily_stats (параметр `date`, по умолчанию вчера)
    - **period** — агрегация за период (`from` / `to`)
    - **shifts** — смены сотрудников за дату (по умолчанию сегодня)
    - **hourly** — почасовая аналитика из hourly_stats (параметр `date`, по умолчанию вчера)
    """
    if "stats" not in token_meta.get("modules", []):
        raise HTTPException(status_code=403, detail="Модуль stats не разрешён для этого токена")

    if "tenant_id" not in token_meta:
        raise HTTPException(status_code=403, detail="No tenant_id in token")
    tenant_id: int = token_meta["tenant_id"]

    if metric == "realtime":
        return _build_realtime(tenant_id, branch, city)

    elif metric == "daily":
        yesterday = (datetime.today() - timedelta(days=1)).date().isoformat()
        date_iso = _parse_date(date, yesterday)
        return await _build_daily(tenant_id, date_iso, branch, city)

    elif metric == "shifts":
        today = _today_iso()
        date_iso = _parse_date(date, today)
        return await _build_shifts(tenant_id, date_iso, branch, city)

    elif metric == "period":
        today = _today_iso()
        week_ago = (datetime.today() - timedelta(days=7)).date().isoformat()
        date_from = _parse_date(from_, week_ago)
        date_to = _parse_date(to, today)
        if date_from > date_to:
            raise HTTPException(status_code=400, detail="from должен быть <= to")
        return await _build_period(tenant_id, date_from, date_to, branch, city)

    elif metric == "hourly":
        yesterday = (datetime.today() - timedelta(days=1)).date().isoformat()
        date_iso = _parse_date(date, yesterday)
        return await _build_hourly(tenant_id, date_iso, branch, city)

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный metric: {metric!r}. Допустимые: realtime, daily, period, shifts, hourly",
        )

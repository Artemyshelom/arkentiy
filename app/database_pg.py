"""
PostgreSQL через asyncpg — мультитенантная версия database.py.

Все функции имеют tenant_id=1 по умолчанию (обратная совместимость).
Переключение: DATABASE_URL=postgresql://... в .env.

Экспортирует те же имена что database.py — замена drop-in.
"""

import datetime as _dt
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg


def _to_date(s: str | None) -> _dt.date | None:
    """Конвертирует ISO-строку даты в datetime.date для asyncpg."""
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

MIGRATION_DIR = Path(__file__).parent / "migrations"


async def init_db(database_url: str) -> None:
    """Создаёт пул соединений и применяет миграции."""
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    logger.info("PostgreSQL pool создан")

    # Применяем все миграции по порядку
    for migration_name in sorted(MIGRATION_DIR.glob("*.sql")):
        async with _pool.acquire() as conn:
            sql = migration_name.read_text()
            await conn.execute(sql)
        logger.info(f"Миграция {migration_name.name} применена")

    await seed_default_tenant()

    import os
    branches_json = Path(os.getenv("BRANCHES_CONFIG_FILE", "/app/secrets/branches.json"))
    await seed_branches_from_json(1, branches_json)
    await load_branches_cache()


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("PostgreSQL pool не инициализирован — вызовите init_db()")
    return _pool


async def init_pool_only(database_url: str) -> None:
    """Инициализирует пул соединений без применения миграций.

    Используется в backfill-скриптах, где БД уже содержит актуальную схему.
    """
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=2, max_size=5)
    logger.info("PostgreSQL pool создан (без миграций)")


# =====================================================================
# iiko_tokens
# =====================================================================

async def get_iiko_token(city: str, tenant_id: int = 1) -> str | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT token, expires_at FROM iiko_tokens WHERE tenant_id = $1 AND city = $2",
        tenant_id, city,
    )
    if not row:
        return None
    if datetime.now(timezone.utc) >= row["expires_at"]:
        return None
    return row["token"]


async def set_iiko_token(city: str, token: str, expires_at: datetime, tenant_id: int = 1) -> None:
    pool = get_pool()
    await pool.execute(
        """INSERT INTO iiko_tokens (tenant_id, city, token, expires_at)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (tenant_id, city) DO UPDATE SET token = $3, expires_at = $4""",
        tenant_id, city, token, expires_at,
    )


# =====================================================================
# job_logs
# =====================================================================

async def log_job_start(job_name: str, tenant_id: int = 1) -> int:
    pool = get_pool()
    row = await pool.fetchrow(
        """INSERT INTO job_logs (tenant_id, job_name, status) VALUES ($1, $2, 'running')
           RETURNING id""",
        tenant_id, job_name,
    )
    return row["id"]


async def log_job_finish(log_id: int, status: str, error: str | None = None, details: str | None = None) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE job_logs SET finished_at = now(), status = $1, error = $2, details = $3 WHERE id = $4",
        status, error, details, log_id,
    )


# =====================================================================
# stoplist_state
# =====================================================================

def hash_stoplist(items: list) -> str:
    serialized = json.dumps(items, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(serialized.encode()).hexdigest()


async def get_stoplist_hash(city: str, tenant_id: int = 1) -> str | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT items_hash FROM stoplist_state WHERE tenant_id = $1 AND city = $2",
        tenant_id, city,
    )
    return row["items_hash"] if row else None


async def set_stoplist_hash(city: str, items_hash: str, tenant_id: int = 1) -> None:
    pool = get_pool()
    await pool.execute(
        """INSERT INTO stoplist_state (tenant_id, city, items_hash)
           VALUES ($1, $2, $3)
           ON CONFLICT (tenant_id, city) DO UPDATE SET items_hash = $3, checked_at = now()""",
        tenant_id, city, items_hash,
    )


# =====================================================================
# report_updates
# =====================================================================

async def record_data_update(date: str, branch: str, field: str, old_value, new_value, tenant_id: int = 1) -> None:
    pool = get_pool()
    await pool.execute(
        """INSERT INTO report_updates (tenant_id, date, branch, field, old_value, new_value)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        tenant_id, _to_date(date), branch, field,
        str(old_value) if old_value is not None else None,
        str(new_value) if new_value is not None else None,
    )


async def get_updates_for_date(date: str, tenant_id: int = 1) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT * FROM report_updates WHERE tenant_id = $1 AND date = $2 ORDER BY recorded_at",
        tenant_id, _to_date(date),
    )
    return [dict(r) for r in rows]


async def clear_updates_for_date(date: str, tenant_id: int = 1) -> None:
    pool = get_pool()
    await pool.execute(
        "DELETE FROM report_updates WHERE tenant_id = $1 AND date = $2",
        tenant_id, _to_date(date),
    )


# =====================================================================
# daily_rt_snapshot
# =====================================================================

async def save_rt_snapshot(
    branch: str, date: str,
    delays_late: int, delays_total: int, delays_avg_min: int,
    cooks_today: int, couriers_today: int,
    tenant_id: int = 1,
) -> None:
    pool = get_pool()
    await pool.execute(
        """INSERT INTO daily_rt_snapshot
           (tenant_id, branch, date, delays_late, delays_total, delays_avg_min,
            cooks_today, couriers_today)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (tenant_id, branch, date) DO UPDATE SET
             delays_late = $4, delays_total = $5, delays_avg_min = $6,
             cooks_today = $7, couriers_today = $8, saved_at = now()""",
        tenant_id, branch, _to_date(date),
        delays_late, delays_total, delays_avg_min, cooks_today, couriers_today,
    )


async def get_rt_snapshot(branch: str, date: str, tenant_id: int = 1) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT delays_late, delays_total, delays_avg_min, cooks_today, couriers_today
           FROM daily_rt_snapshot WHERE tenant_id = $1 AND branch = $2 AND date = $3""",
        tenant_id, branch, _to_date(date),
    )
    return dict(row) if row else None


# =====================================================================
# orders_raw
# =====================================================================

async def upsert_orders_batch(rows: list[dict], tenant_id: int = 1) -> None:
    if not rows:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for r in rows:
                await conn.execute(
                    """INSERT INTO orders_raw
                       (tenant_id, branch_name, delivery_num, status, courier, sum,
                        planned_time, actual_time, is_self_service,
                        date, is_late, late_minutes,
                        client_name, client_phone, delivery_address, items,
                        cooked_time, comment, operator, opened_at,
                        has_problem,
                        payment_type, source,
                        cancel_reason, cancellation_details, payment_changed, updated_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                               $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,now())
                       ON CONFLICT (tenant_id, branch_name, delivery_num) DO UPDATE SET
                         status=EXCLUDED.status, courier=EXCLUDED.courier, sum=EXCLUDED.sum,
                         planned_time=EXCLUDED.planned_time, actual_time=EXCLUDED.actual_time,
                         is_self_service=EXCLUDED.is_self_service, date=EXCLUDED.date,
                         is_late=EXCLUDED.is_late, late_minutes=EXCLUDED.late_minutes,
                         client_name=EXCLUDED.client_name, client_phone=EXCLUDED.client_phone,
                         delivery_address=EXCLUDED.delivery_address, items=EXCLUDED.items,
                         cooked_time=COALESCE(EXCLUDED.cooked_time, orders_raw.cooked_time),
                         comment=EXCLUDED.comment, operator=EXCLUDED.operator,
                         opened_at=EXCLUDED.opened_at, has_problem=EXCLUDED.has_problem,
                         payment_type=EXCLUDED.payment_type,
                         source=EXCLUDED.source,
                         cancel_reason=EXCLUDED.cancel_reason, cancellation_details=EXCLUDED.cancellation_details,
                         payment_changed=EXCLUDED.payment_changed,
                         updated_at=now()""",
                    tenant_id,
                    r.get("branch_name"), r.get("delivery_num"), r.get("status"),
                    r.get("courier"), float(r.get("sum") or 0),
                    r.get("planned_time"), r.get("actual_time"),
                    bool(r.get("is_self_service", False)),
                    _to_date(r.get("date")),
                    bool(r.get("is_late", False)),
                    float(r.get("late_minutes") or 0),
                    r.get("client_name"), r.get("client_phone"),
                    r.get("delivery_address"), r.get("items"),
                    r.get("cooked_time") or None, r.get("comment"), r.get("operator"),
                    r.get("opened_at"),
                    bool(r.get("has_problem", False)),
                    r.get("payment_type"),
                    r.get("source"),
                    r.get("cancel_reason"), r.get("cancellation_details") or None,
                    bool(r.get("payment_changed", False)),
                )


async def get_client_order_count(phone: str, tenant_id: int = 1) -> int:
    if not phone:
        return 0
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM orders_raw WHERE tenant_id = $1 AND client_phone = $2",
        tenant_id, phone,
    )
    return row["cnt"] if row else 0


async def get_order_status_from_db(
    branch_name: str,
    delivery_num: str,
    tenant_id: int = 1,
) -> str | None:
    """Возвращает текущий статус заказа из orders_raw. None если заказ не найден."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT status FROM orders_raw
        WHERE branch_name = $1
          AND delivery_num = $2
          AND tenant_id = $3
        LIMIT 1
        """,
        branch_name, delivery_num, tenant_id,
    )
    return row["status"] if row else None


# =====================================================================
# shifts_raw
# =====================================================================

async def upsert_shifts_batch(rows: list[dict], tenant_id: int = 1) -> None:
    if not rows:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for r in rows:
                await conn.execute(
                    """INSERT INTO shifts_raw
                       (tenant_id, branch_name, employee_id, employee_name, role_class,
                        date, clock_in, clock_out, updated_at)
                       VALUES ($1,$2,$3,$4,$5,$6::date,$7,$8,now())
                       ON CONFLICT (tenant_id, branch_name, employee_id, clock_in) DO UPDATE SET
                         employee_name=EXCLUDED.employee_name, role_class=EXCLUDED.role_class,
                         date=EXCLUDED.date, clock_out=EXCLUDED.clock_out, updated_at=now()""",
                    tenant_id,
                    r.get("branch_name"), r.get("employee_id"),
                    r.get("employee_name"), r.get("role_class"),
                    _to_date(r.get("date")), r.get("clock_in"), r.get("clock_out"),
                )


async def get_today_shifts(branch_name: str, date_iso: str, tenant_id: int = 1) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT employee_id, employee_name, role_class, clock_in, clock_out
           FROM shifts_raw WHERE tenant_id = $1 AND branch_name = $2 AND date = $3
           ORDER BY clock_in""",
        tenant_id, branch_name, _to_date(date_iso),
    )
    return [dict(r) for r in rows]


async def get_shifts_by_date(date_iso: str, tenant_id: int = 1) -> list[dict]:
    """Смены всех точек за дату. Для Stats API (Борис)."""
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT branch_name, employee_name, role_class, clock_in, clock_out
           FROM shifts_raw WHERE tenant_id = $1 AND date = $2
           ORDER BY branch_name, clock_in""",
        tenant_id, _to_date(date_iso),
    )
    return [dict(r) for r in rows]


async def get_fot_shifts_by_date(date_iso: str, tenant_id: int = 1) -> list[dict]:
    """Смены с employee_id за дату — для расчёта ФОТ."""
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT branch_name, employee_id, role_class, clock_in, clock_out
           FROM shifts_raw WHERE tenant_id = $1 AND date = $2
             AND clock_out IS NOT NULL AND clock_out != ''
           ORDER BY branch_name, clock_in""",
        tenant_id, _to_date(date_iso),
    )
    return [dict(r) for r in rows]


async def close_stale_shifts(today_iso: str, tenant_id: int = 1) -> int:
    pool = get_pool()
    result = await pool.execute(
        "UPDATE shifts_raw SET clock_out = clock_in WHERE tenant_id = $1 AND date < $2 AND clock_out IS NULL",
        tenant_id, _to_date(today_iso),
    )
    return int(result.split()[-1])


# =====================================================================
# daily_stats
# =====================================================================

async def upsert_daily_stats_batch(rows: list[dict], tenant_id: int = 1) -> None:
    if not rows:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for r in rows:
                await conn.execute(
                    """INSERT INTO daily_stats
                       (tenant_id, branch_name, date, orders_count, revenue, avg_check,
                        cogs_pct, sailplay, discount_sum, discount_types,
                        delivery_count, pickup_count, late_count, total_delivered,
                        late_percent, avg_late_min, cooks_count, couriers_count,
                        late_delivery_count, late_pickup_count,
                        avg_cooking_min, avg_wait_min, avg_delivery_min, exact_time_count,
                        cash, noncash,
                        new_customers, new_customers_revenue,
                        repeat_customers, repeat_customers_revenue)
                       VALUES ($1,$2,$3::date,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30)
                       ON CONFLICT (tenant_id, branch_name, date) DO UPDATE SET
                         orders_count=EXCLUDED.orders_count, revenue=EXCLUDED.revenue,
                         avg_check=EXCLUDED.avg_check, cogs_pct=EXCLUDED.cogs_pct,
                         sailplay=EXCLUDED.sailplay, discount_sum=EXCLUDED.discount_sum,
                         discount_types=EXCLUDED.discount_types,
                         delivery_count=EXCLUDED.delivery_count, pickup_count=EXCLUDED.pickup_count,
                         late_count=EXCLUDED.late_count, total_delivered=EXCLUDED.total_delivered,
                         late_percent=EXCLUDED.late_percent, avg_late_min=EXCLUDED.avg_late_min,
                         cooks_count=EXCLUDED.cooks_count, couriers_count=EXCLUDED.couriers_count,
                         late_delivery_count=EXCLUDED.late_delivery_count,
                         late_pickup_count=EXCLUDED.late_pickup_count,
                         avg_cooking_min=EXCLUDED.avg_cooking_min,
                         avg_wait_min=EXCLUDED.avg_wait_min,
                         avg_delivery_min=EXCLUDED.avg_delivery_min,
                         exact_time_count=EXCLUDED.exact_time_count,
                         cash=EXCLUDED.cash, noncash=EXCLUDED.noncash,
                         new_customers=EXCLUDED.new_customers,
                         new_customers_revenue=EXCLUDED.new_customers_revenue,
                         repeat_customers=EXCLUDED.repeat_customers,
                         repeat_customers_revenue=EXCLUDED.repeat_customers_revenue,
                         updated_at=now()""",
                    tenant_id,
                    r.get("branch_name"), _to_date(r.get("date")),
                    r.get("orders_count", 0), r.get("revenue", 0), r.get("avg_check", 0),
                    r.get("cogs_pct"), r.get("sailplay"), r.get("discount_sum"),
                    r.get("discount_types"),
                    r.get("delivery_count", 0), r.get("pickup_count", 0),
                    r.get("late_count", 0), r.get("total_delivered", 0),
                    r.get("late_percent", 0), r.get("avg_late_min", 0),
                    r.get("cooks_count", 0), r.get("couriers_count", 0),
                    r.get("late_delivery_count", 0), r.get("late_pickup_count", 0),
                    r.get("avg_cooking_min"), r.get("avg_wait_min"), r.get("avg_delivery_min"),
                    r.get("exact_time_count", 0),
                    r.get("cash", 0.0), r.get("noncash", 0.0),
                    r.get("new_customers", 0), r.get("new_customers_revenue", 0.0),
                    r.get("repeat_customers", 0), r.get("repeat_customers_revenue", 0.0),
                )


async def get_daily_stats(branch_name: str, date_iso: str, tenant_id: int = 1) -> dict | None:
    # #region agent log
    try:
        import pathlib as _pl
        import json as _json
        _log_path = _pl.Path(__file__).resolve().parents[3] / ".cursor" / "debug-3e913f.log"
        _p = {"sessionId": "3e913f", "location": "database_pg:get_daily_stats", "message": "get_daily_stats called", "data": {"branch_name": branch_name, "date_iso": date_iso, "tenant_id_used": tenant_id}, "hypothesisId": "H1", "timestamp": __import__("time").time() * 1000}
        with open(_log_path, "a", encoding="utf-8") as _f:
            _f.write(_json.dumps(_p, ensure_ascii=False) + "\n")
    except Exception:
        try:
            with open("/tmp/debug-3e913f.log", "a", encoding="utf-8") as _f:
                _f.write(__import__("json").dumps({"sessionId": "3e913f", "location": "database_pg:get_daily_stats", "data": {"branch_name": branch_name, "tenant_id_used": tenant_id}, "hypothesisId": "H1"}, ensure_ascii=False) + "\n")
        except Exception:
            pass
    # #endregion
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM daily_stats WHERE tenant_id = $1 AND branch_name = $2 AND date = $3",
        tenant_id, branch_name, _to_date(date_iso),
    )
    return dict(row) if row else None


# =====================================================================
# Competitors
# =====================================================================

async def create_competitor_snapshot(
    city: str, competitor_name: str, url: str,
    status: str = "ok", items_count: int = 0, error_msg: str | None = None,
    tenant_id: int = 1,
) -> int:
    pool = get_pool()
    row = await pool.fetchrow(
        """INSERT INTO competitor_snapshots
           (tenant_id, city, competitor_name, url, status, items_count, error_msg)
           VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id""",
        tenant_id, city, competitor_name, url, status, items_count, error_msg,
    )
    return row["id"]


async def save_competitor_items(
    snapshot_id: int, city: str, competitor_name: str, items: list[dict],
    tenant_id: int = 1,
) -> None:
    if not items:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in items:
                await conn.execute(
                    """INSERT INTO competitor_menu_items
                       (snapshot_id, tenant_id, city, competitor_name, category, name, price, price_old, portion)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    snapshot_id, tenant_id, city, competitor_name,
                    item.get("category"), item["name"], item["price"],
                    item.get("price_old"), item.get("portion"),
                )


async def get_second_last_competitor_items(city: str, competitor_name: str, tenant_id: int = 1) -> list[dict]:
    pool = get_pool()
    snapshot_ids = await pool.fetch(
        """SELECT id FROM competitor_snapshots
           WHERE tenant_id = $1 AND city = $2 AND competitor_name = $3 AND status = 'ok'
           ORDER BY scraped_at DESC LIMIT 2""",
        tenant_id, city, competitor_name,
    )
    if len(snapshot_ids) < 2:
        return []
    prev_id = snapshot_ids[1]["id"]
    rows = await pool.fetch(
        "SELECT name, price, price_old, portion, category FROM competitor_menu_items WHERE snapshot_id = $1",
        prev_id,
    )
    return [dict(r) for r in rows]


async def get_competitor_names(tenant_id: int = 1) -> list[tuple[str, str]]:
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT DISTINCT city, competitor_name FROM competitor_snapshots
           WHERE tenant_id = $1 AND status = 'ok' ORDER BY city, competitor_name""",
        tenant_id,
    )
    return [(r["city"], r["competitor_name"]) for r in rows]


async def get_all_competitor_items_by_snapshot(city: str, competitor_name: str, tenant_id: int = 1) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT i.name, i.price, i.price_old, s.scraped_at::date AS snapshot_date, i.category
           FROM competitor_menu_items i
           JOIN competitor_snapshots s ON i.snapshot_id = s.id
           WHERE s.tenant_id = $1 AND s.city = $2 AND s.competitor_name = $3 AND s.status = 'ok'
           ORDER BY s.scraped_at ASC, i.category, i.name""",
        tenant_id, city, competitor_name,
    )
    return [
        {"name": r["name"], "price": r["price"], "price_old": r["price_old"],
         "snapshot_date": str(r["snapshot_date"]), "category": r["category"] or ""}
        for r in rows
    ]


async def get_competitor_last_snapshot(city: str, competitor_name: str, tenant_id: int = 1) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT scraped_at::date AS date, items_count FROM competitor_snapshots
           WHERE tenant_id = $1 AND city = $2 AND competitor_name = $3 AND status = 'ok'
           ORDER BY scraped_at DESC LIMIT 1""",
        tenant_id, city, competitor_name,
    )
    return {"date": str(row["date"]), "items_count": row["items_count"]} if row else None


# =====================================================================
# silence_log
# =====================================================================

async def log_silence(chat_id: int, duration_min: int, user_id: int, tenant_id: int = 1) -> None:
    pool = get_pool()
    await pool.execute(
        "INSERT INTO silence_log (tenant_id, chat_id, duration_min, user_id) VALUES ($1,$2,$3,$4)",
        tenant_id, chat_id, duration_min, user_id,
    )


# =====================================================================
# audit_events
# =====================================================================

async def save_audit_events_batch(events: list[dict], tenant_id: int = 1) -> None:
    if not events:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for e in events:
                await conn.execute(
                    """INSERT INTO audit_events
                       (tenant_id, date, branch_name, city, event_type, severity, description, meta_json, created_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)""",
                    tenant_id,
                    _to_date(e.get("date")), e.get("branch_name"), e.get("city"),
                    e.get("event_type"), e.get("severity", "warning"),
                    e.get("description"), e.get("meta_json"),
                    datetime.fromisoformat((e.get("created_at") or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00")),
                )


async def clear_audit_events(date: str, branch_name: str | None = None, tenant_id: int = 1) -> None:
    pool = get_pool()
    if branch_name:
        await pool.execute(
            "DELETE FROM audit_events WHERE tenant_id = $1 AND date = $2 AND branch_name = $3",
            tenant_id, _to_date(date), branch_name,
        )
    else:
        await pool.execute(
            "DELETE FROM audit_events WHERE tenant_id = $1 AND date = $2",
            tenant_id, _to_date(date),
        )


async def get_audit_events(
    date: str, city: str | None = None, branch_name: str | None = None,
    tenant_id: int = 1,
) -> list[dict]:
    pool = get_pool()
    query = "SELECT * FROM audit_events WHERE tenant_id = $1 AND date = $2"
    params: list = [tenant_id, _to_date(date)]
    idx = 3
    if branch_name:
        query += f" AND branch_name = ${idx}"
        params.append(branch_name)
        idx += 1
    elif city:
        query += f" AND city = ${idx}"
        params.append(city)
        idx += 1
    query += " ORDER BY severity DESC, event_type, branch_name"
    rows = await pool.fetch(query, *params)
    return [dict(r) for r in rows]


# =====================================================================
# SaaS — tenants, modules, users, chats, subscriptions
# =====================================================================

_ALL_MODULES = ["late_alerts", "late_queries", "search", "reports", "marketing", "finance", "admin", "audit"]


async def seed_default_tenant() -> None:
    import os
    pool = get_pool()
    bot_token = os.getenv("TELEGRAM_ANALYTICS_BOT_TOKEN", "")

    row = await pool.fetchrow("SELECT id FROM tenants WHERE id = 1")
    if row:
        # Обновляем bot_token если он не был записан
        if bot_token and not row.get("bot_token"):
            await pool.execute(
                "UPDATE tenants SET bot_token = $1 WHERE id = 1 AND (bot_token IS NULL OR bot_token = '')",
                bot_token,
            )
        return

    now = datetime.now(timezone.utc)
    await pool.execute(
        """INSERT INTO tenants (id, name, slug, bot_token, plan, status, created_at, updated_at)
           VALUES (1, 'Ёбидоёби', 'ebidoebi', $1, 'owner', 'active', $2, $2)""",
        bot_token or None, now,
    )
    for module in _ALL_MODULES:
        await pool.execute(
            """INSERT INTO tenant_modules (tenant_id, module, enabled, updated_at)
               VALUES (1, $1, true, $2)
               ON CONFLICT DO NOTHING""",
            module, now,
        )
    await pool.execute(
        """INSERT INTO subscriptions (tenant_id, status, plan, modules_json, branches_count, started_at, created_at, updated_at)
           VALUES (1, 'active', 'owner', $1, 9, $2, $2, $2)
           ON CONFLICT DO NOTHING""",
        json.dumps(_ALL_MODULES), now,
    )


async def get_tenant(tenant_id: int = 1) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant_id)
    return dict(row) if row else None


async def get_tenant_modules(tenant_id: int = 1) -> list[str]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT module FROM tenant_modules WHERE tenant_id = $1 AND enabled = true ORDER BY module",
        tenant_id,
    )
    return [r["module"] for r in rows]


async def get_subscription(tenant_id: int = 1) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM subscriptions WHERE tenant_id = $1", tenant_id)
    return dict(row) if row else None


async def get_active_tenants_with_tokens() -> list[dict]:
    """Возвращает активных тенантов у которых есть bot_token — для запуска polling loops."""
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT id, name, slug, bot_token
           FROM tenants
           WHERE status = 'active' AND bot_token IS NOT NULL AND bot_token != ''
           ORDER BY id"""
    )
    return [{"id": r["id"], "name": r["name"], "slug": r["slug"], "bot_token": r["bot_token"]} for r in rows]


async def get_all_tenant_users(tenant_id: int = 1) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT user_id, name, role, modules_json, city
           FROM tenant_users WHERE tenant_id = $1 AND is_active = true""",
        tenant_id,
    )
    return [
        {
            "user_id": r["user_id"],
            "name": r["name"] or str(r["user_id"]),
            "role": r["role"],
            "modules": r["modules_json"] if isinstance(r["modules_json"], list) else json.loads(r["modules_json"] or "[]"),
            "city": r["city"],
        }
        for r in rows
    ]


async def get_all_tenant_chats(tenant_id: int = 1) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT chat_id, name, modules_json, city
           FROM tenant_chats WHERE tenant_id = $1 AND is_active = true""",
        tenant_id,
    )
    return [
        {
            "chat_id": r["chat_id"],
            "name": r["name"] or str(r["chat_id"]),
            "modules": r["modules_json"] if isinstance(r["modules_json"], list) else json.loads(r["modules_json"] or "[]"),
            "city": r["city"],
        }
        for r in rows
    ]


async def upsert_tenant_user(
    user_id: int, name: str,
    modules: list[str] | None = None, city: str | None = None,
    role: str = "viewer", tenant_id: int = 1,
) -> None:
    pool = get_pool()
    await pool.execute(
        """INSERT INTO tenant_users
           (tenant_id, user_id, name, role, modules_json, city, is_active)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6, true)
           ON CONFLICT (tenant_id, user_id) DO UPDATE SET
             name = EXCLUDED.name, role = EXCLUDED.role,
             modules_json = EXCLUDED.modules_json, city = EXCLUDED.city, is_active = true""",
        tenant_id, user_id, name, role, json.dumps(modules or []), city,
    )


async def upsert_tenant_chat(
    chat_id: int, name: str,
    modules: list[str] | None = None, city: str | None = None,
    tenant_id: int = 1,
) -> None:
    pool = get_pool()
    await pool.execute(
        """INSERT INTO tenant_chats
           (tenant_id, chat_id, name, modules_json, city, is_active)
           VALUES ($1, $2, $3, $4::jsonb, $5, true)
           ON CONFLICT (tenant_id, chat_id) DO UPDATE SET
             name = EXCLUDED.name, modules_json = EXCLUDED.modules_json,
             city = EXCLUDED.city, is_active = true""",
        tenant_id, chat_id, name, json.dumps(modules or []), city,
    )


async def delete_tenant_user(user_id: int, tenant_id: int = 1) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE tenant_users SET is_active = false WHERE tenant_id = $1 AND user_id = $2",
        tenant_id, user_id,
    )


async def delete_tenant_chat(chat_id: int, tenant_id: int = 1) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE tenant_chats SET is_active = false WHERE tenant_id = $1 AND chat_id = $2",
        tenant_id, chat_id,
    )


async def get_module_chats_for_city(module: str, city: str, tenant_id: int | None = None) -> list[int]:
    """Получить чаты для модуля по городу.
    
    ВАЖНО: tenant_id должен быть передан явно. Ошибка если забыли.
    """
    if tenant_id is None:
        raise ValueError("tenant_id must be specified explicitly in get_module_chats_for_city()")
    
    chats = await get_all_tenant_chats(tenant_id)
    result: list[int] = []
    for chat in chats:
        if module not in chat.get("modules", []):
            continue
        city_raw = chat.get("city")
        if city_raw is None:
            result.append(chat["chat_id"])
            continue
        try:
            cities = frozenset(json.loads(city_raw)) if isinstance(city_raw, str) else frozenset()
        except (ValueError, TypeError):
            cities = frozenset({city_raw}) if city_raw else frozenset()
        if city in cities:
            result.append(chat["chat_id"])
    return result


async def get_alert_chats_for_city(city: str, tenant_id: int = 1) -> list[int]:
    return await get_module_chats_for_city("late_alerts", city, tenant_id)


async def get_tenant_cities(tenant_id: int) -> list[str]:
    """Возвращает список уникальных городов тенанта из iiko_credentials."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT city FROM iiko_credentials "
        "WHERE tenant_id = $1 AND is_active = true AND city IS NOT NULL AND city != '' "
        "ORDER BY city",
        tenant_id,
    )
    return [r["city"] for r in rows]


async def get_tenant_available_modules(tenant_id: int) -> list[str] | None:
    """Возвращает список доступных модулей из подписки, или None если нет ограничений."""
    import json as _json
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT modules_json FROM subscriptions "
        "WHERE tenant_id = $1 AND status = 'active' "
        "ORDER BY created_at DESC LIMIT 1",
        tenant_id,
    )
    if not row or not row["modules_json"]:
        return None
    try:
        return _json.loads(row["modules_json"])
    except Exception:
        return None


async def get_access_config_from_db(tenant_id: int = 1) -> dict:
    users = await get_all_tenant_users(tenant_id)
    chats = await get_all_tenant_chats(tenant_id)
    tenant_cities = await get_tenant_cities(tenant_id)
    available_modules = await get_tenant_available_modules(tenant_id)
    return {
        "chats": {
            str(c["chat_id"]): {"name": c["name"], "modules": c["modules"], "city": c["city"]}
            for c in chats
        },
        "users": {
            str(u["user_id"]): {"name": u["name"], "modules": u["modules"], "city": u["city"]}
            for u in users
        },
        "tenant_cities": tenant_cities or None,
        "available_modules": available_modules,
    }


async def get_tenant_id_by_admin(user_id: int) -> int | None:
    """Возвращает tenant_id пользователя с ролью admin/owner, или None."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT tenant_id FROM tenant_users "
        "WHERE user_id = $1 AND role IN ('admin', 'owner') AND is_active = true LIMIT 1",
        user_id,
    )
    return row["tenant_id"] if row else None


# =====================================================================
# iiko_credentials — точки per tenant
# =====================================================================

_branches_cache: dict[int, list[dict]] = {}


def get_branches(tenant_id: int = 1) -> list[dict]:
    """Sync — из in-memory cache. Заполняется при init_db."""
    return list(_branches_cache.get(tenant_id, []))


def get_all_branches() -> list[dict]:
    """Sync — все точки всех тенантов из кеша, каждая с полем tenant_id."""
    result: list[dict] = []
    for tid, branches in _branches_cache.items():
        for b in branches:
            result.append({**b, "tenant_id": tid})
    return result


async def get_branches_from_db(tenant_id: int = 1) -> list[dict]:
    """Async — прямой запрос к БД (для обновления кеша)."""
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT branch_name, city, bo_url, bo_login, bo_password, dept_id, utc_offset
           FROM iiko_credentials
           WHERE tenant_id = $1 AND is_active = true
           ORDER BY branch_name""",
        tenant_id,
    )
    return [
        {
            "name": r["branch_name"],
            "city": r["city"] or "",
            "bo_url": r["bo_url"] or "",
            "bo_login": r["bo_login"],
            "bo_password": r["bo_password"],
            "dept_id": r["dept_id"] or "",
            "utc_offset": r["utc_offset"],
        }
        for r in rows
    ]


# Маппинг chat_id → tenant_id (sync, заполняется при init_db)
_chat_tenant_map: dict[int, int] = {}


def get_tenant_id_for_chat(chat_id: int) -> int | None:
    """Sync — возвращает tenant_id для chat_id из in-memory кэша."""
    return _chat_tenant_map.get(chat_id)


async def load_chat_tenant_map() -> None:
    """Загружает маппинг chat_id → tenant_id из tenant_chats."""
    global _chat_tenant_map
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT chat_id, tenant_id FROM tenant_chats WHERE is_active = true"
    )
    _chat_tenant_map = {r["chat_id"]: r["tenant_id"] for r in rows}
    logger.info(f"Chat→tenant map загружен: {len(_chat_tenant_map)} чатов")


async def load_branches_cache(tenant_id: int | None = None) -> None:
    """Загружает точки из БД в _branches_cache. Если tenant_id=None — все тенанты."""
    global _branches_cache
    pool = get_pool()
    if tenant_id is not None:
        _branches_cache[tenant_id] = await get_branches_from_db(tenant_id)
        logger.info(f"Кеш точек загружен: tenant_id={tenant_id}, {len(_branches_cache[tenant_id])} записей")
    else:
        rows = await pool.fetch(
            "SELECT DISTINCT tenant_id FROM iiko_credentials WHERE is_active = true"
        )
        for row in rows:
            tid = row["tenant_id"]
            _branches_cache[tid] = await get_branches_from_db(tid)
        logger.info(f"Кеш точек загружен: {len(_branches_cache)} тенантов")


async def seed_branches_from_json(tenant_id: int, json_path: str | Path) -> int:
    """
    Сеедирует iiko_credentials из branches.json для тенанта.
    Пропускает если запись уже есть (ON CONFLICT DO NOTHING).
    """
    pool = get_pool()
    path = Path(json_path)
    if not path.exists():
        logger.warning(f"seed_branches_from_json: файл {path} не найден, пропуск")
        return 0

    try:
        branches: list[dict] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"seed_branches_from_json: ошибка чтения {path}: {e}")
        return 0

    now = datetime.now(timezone.utc)
    inserted = 0
    for b in branches:
        result = await pool.execute(
            """INSERT INTO iiko_credentials
               (tenant_id, branch_name, city, bo_url, bo_login, bo_password, dept_id, utc_offset, is_active, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true, $9)
               ON CONFLICT (tenant_id, branch_name) DO NOTHING""",
            tenant_id,
            b["name"],
            b.get("city", ""),
            b.get("bo_url", ""),
            b.get("bo_login"),
            b.get("bo_password"),
            b.get("dept_id", ""),
            b.get("utc_offset", 7),
            now,
        )
        if result == "INSERT 0 1":
            inserted += 1

    if inserted:
        logger.info(f"seed_branches_from_json: {inserted} точек добавлено → iiko_credentials[tenant_id={tenant_id}]")
    return inserted


async def upsert_branch_credential(
    tenant_id: int,
    branch_name: str,
    city: str = "",
    bo_url: str = "",
    bo_login: str | None = None,
    bo_password: str | None = None,
    dept_id: str = "",
    utc_offset: int = 7,
    is_active: bool = True,
) -> None:
    """CRUD для управления точками тенанта. Инвалидирует кеш."""
    pool = get_pool()
    await pool.execute(
        """INSERT INTO iiko_credentials
           (tenant_id, branch_name, city, bo_url, bo_login, bo_password, dept_id, utc_offset, is_active, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
           ON CONFLICT (tenant_id, branch_name) DO UPDATE SET
               city = EXCLUDED.city,
               bo_url = EXCLUDED.bo_url,
               bo_login = EXCLUDED.bo_login,
               bo_password = EXCLUDED.bo_password,
               dept_id = EXCLUDED.dept_id,
               utc_offset = EXCLUDED.utc_offset,
               is_active = EXCLUDED.is_active""",
        tenant_id, branch_name, city, bo_url, bo_login, bo_password, dept_id, utc_offset, is_active,
    )
    _branches_cache.pop(tenant_id, None)
    await load_branches_cache(tenant_id)


# =====================================================================
# Агрегаты orders_raw (порт из database.py)
# =====================================================================

# Условия заказов «на точное время» — PG-совместимые
_EXACT_TIME_CONDITIONS_PG = """(
        LOWER(COALESCE(comment, '')) LIKE '%точн%'
        OR LOWER(COALESCE(comment, '')) LIKE '%тчн%'
        OR LOWER(COALESCE(comment, '')) LIKE '%предзаказ%'
        OR (planned_time != '' AND planned_time IS NOT NULL
            AND opened_at != '' AND opened_at IS NOT NULL
            AND EXTRACT(EPOCH FROM (planned_time::timestamp
                - REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')::timestamp)) / 60 > 150)
        OR (service_print_time != '' AND service_print_time IS NOT NULL
            AND opened_at != '' AND opened_at IS NOT NULL
            AND EXTRACT(EPOCH FROM (service_print_time::timestamp
                - REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')::timestamp)) / 60 > 90)
        OR (cooked_time != '' AND cooked_time IS NOT NULL
            AND opened_at != '' AND opened_at IS NOT NULL
            AND EXTRACT(EPOCH FROM (cooked_time::timestamp
                - REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')::timestamp)) / 60 > 210)
        OR (send_time != '' AND send_time IS NOT NULL
            AND cooked_time != '' AND cooked_time IS NOT NULL
            AND actual_time != '' AND actual_time IS NOT NULL
            AND EXTRACT(EPOCH FROM (send_time::timestamp - cooked_time::timestamp)) / 60 > 120
            AND EXTRACT(EPOCH FROM (REPLACE(SUBSTR(actual_time, 1, 19), 'T', ' ')::timestamp
                - send_time::timestamp)) / 60 < 5)
    )"""

_EXACT_TIME_FILTER_PG = f"\n    AND NOT {_EXACT_TIME_CONDITIONS_PG}\n"

_PAY_MAP: dict[str, str] = {
    "наличные": "Наличные",
    "безналичный расчет": "Безнал",
    "безналичный расчёт": "Безнал",
    "онлайн": "Онлайн",
    "тинькофф": "Онлайн",
    "т-банк": "Онлайн",
    "системы лояльности": "Бонусы",
}


async def aggregate_orders_today(branch_name: str, date_iso: str, tenant_id: int | None = None) -> dict:
    """Быстрый агрегат из orders_raw за сегодня для /статус (скидки + счётчики).

    Примечание: avg-времена (avg_cooking_min и др.) намеренно не считаются —
    send_time/cooked_time/opened_at для сегодняшних заказов всегда NULL
    (заполняются OLAP enrichment только за вчера). RT-времена берутся из Events API.
    """
    from app.ctx import ctx_tenant_id as _ctx_tenant_id

    if tenant_id is None:
        tenant_id = _ctx_tenant_id.get()

    pool = get_pool()

    # Счётчики активных/доставленных — для fallback когда Events API ещё не загружен
    cnt_row = await pool.fetchrow(
        """SELECT
            COUNT(*) FILTER (WHERE status NOT IN ('Доставлена','Закрыта','Отменена'))
                AS active_count,
            COUNT(*) FILTER (WHERE status IN ('Доставлена','Закрыта'))
                AS delivered_count
        FROM orders_raw
        WHERE tenant_id = $1 AND branch_name = $2 AND date::text = $3""",
        tenant_id, branch_name, date_iso,
    )
    result = {
        "active_count": cnt_row["active_count"] if cnt_row else 0,
        "delivered_count": cnt_row["delivered_count"] if cnt_row else 0,
    }

    dt_rows = await pool.fetch(
        """SELECT discount_type, COUNT(*) as cnt, SUM(COALESCE(discount_sum, 0)) as total
           FROM orders_raw
           WHERE tenant_id = $1 AND branch_name = $2 AND date::text = $3
             AND discount_type IS NOT NULL AND discount_type != ''
             AND status != 'Отменена'
           GROUP BY discount_type
           ORDER BY total DESC""",
        tenant_id, branch_name, date_iso,
    )

    result["discount_types_agg"] = [
        {"type": r["discount_type"], "count": r["cnt"], "sum": round(r["total"] or 0)}
        for r in dt_rows
    ] if dt_rows else []

    return result


async def aggregate_orders_for_daily_stats(branch_name: str, date_iso: str, tenant_id: int) -> dict:
    """Агрегирует данные из orders_raw для daily_stats."""
    pool = get_pool()

    row = await pool.fetchrow(
        """SELECT
            COUNT(*) AS raw_orders_count,
            COALESCE(SUM(sum), 0) AS raw_revenue,
            COALESCE(SUM(CASE
                WHEN pay_breakdown LIKE '%SailPlay%'
                THEN (pay_breakdown::jsonb->>'SailPlay Бонус')::numeric
            END), 0) AS raw_sailplay,
            SUM(CASE WHEN is_self_service = false THEN 1 ELSE 0 END) AS raw_delivery_count,
            SUM(CASE WHEN is_self_service = true  THEN 1 ELSE 0 END) AS raw_pickup_count,
            SUM(CASE WHEN is_late = true AND is_self_service = false 
                     AND COALESCE(payment_changed, false) = false THEN 1 ELSE 0 END)
                AS late_delivery_count,
            SUM(CASE WHEN is_late = true AND is_self_service = true 
                     AND COALESCE(payment_changed, false) = false THEN 1 ELSE 0 END)
                AS late_pickup_count,
            SUM(CASE WHEN COALESCE(payment_changed, false) = true THEN 1 ELSE 0 END)
                AS payment_changed_count,
            SUM(CASE WHEN is_self_service = false
                     AND status IN ('Доставлена','Закрыта') THEN 1 ELSE 0 END)
                AS total_delivery_count,
            AVG(CASE WHEN is_late = true AND is_self_service = false 
                     AND COALESCE(payment_changed, false) = false
                     THEN late_minutes END)
                AS avg_late_min
        FROM orders_raw
        WHERE branch_name = $1 AND date::text = $2 AND tenant_id = $3
          AND status != 'Отменена'""",
        branch_name, date_iso, tenant_id,
    )

    result = dict(row) if row else {}

    time_row = await pool.fetchrow(
        f"""SELECT
            AVG(CASE
                WHEN cooked_time IS NOT NULL AND cooked_time != ''
                  AND sum >= 200
                  AND COALESCE(NULLIF(service_print_time, ''), NULLIF(opened_at, '')) IS NOT NULL
                THEN CASE
                    WHEN EXTRACT(EPOCH FROM (
                        TO_TIMESTAMP(cooked_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                        - TO_TIMESTAMP(
                            COALESCE(NULLIF(service_print_time, ''), opened_at),
                            'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
                          )
                    )) / 60
                         BETWEEN 1 AND 120
                    THEN EXTRACT(EPOCH FROM (
                        TO_TIMESTAMP(cooked_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                        - TO_TIMESTAMP(
                            COALESCE(NULLIF(service_print_time, ''), opened_at),
                            'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
                          )
                    )) / 60
                END
            END) AS avg_cooking_min,
            AVG(CASE
                WHEN send_time IS NOT NULL AND send_time != ''
                  AND cooked_time IS NOT NULL AND cooked_time != ''
                THEN CASE
                    WHEN EXTRACT(EPOCH FROM (
                        TO_TIMESTAMP(send_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                        - TO_TIMESTAMP(cooked_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                    )) / 60
                         BETWEEN 0 AND 120
                    THEN EXTRACT(EPOCH FROM (
                        TO_TIMESTAMP(send_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                        - TO_TIMESTAMP(cooked_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                    )) / 60
                END
            END) AS avg_wait_min,
            AVG(CASE
                WHEN actual_time IS NOT NULL AND actual_time != ''
                  AND send_time IS NOT NULL AND send_time != ''
                  AND is_self_service = false
                THEN CASE
                    WHEN EXTRACT(EPOCH FROM (
                        TO_TIMESTAMP(actual_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                        - TO_TIMESTAMP(send_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                    )) / 60
                         BETWEEN 1 AND 120
                    THEN EXTRACT(EPOCH FROM (
                        TO_TIMESTAMP(actual_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                        - TO_TIMESTAMP(send_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                    )) / 60
                END
            END) AS avg_delivery_min
        FROM orders_raw
        WHERE branch_name = $1 AND date::text = $2 AND tenant_id = $3
          AND status != 'Отменена'
          AND COALESCE(payment_changed, false) = false
          {_EXACT_TIME_FILTER_PG}""",
        branch_name, date_iso, tenant_id,
    )

    if time_row:
        result.update(dict(time_row))

    exact_row = await pool.fetchrow(
        f"""SELECT COUNT(*) AS exact_time_count
        FROM orders_raw
        WHERE branch_name = $1 AND date::text = $2 AND tenant_id = $3
          AND status != 'Отменена'
          AND {_EXACT_TIME_CONDITIONS_PG}""",
        branch_name, date_iso, tenant_id,
    )

    if exact_row:
        result["exact_time_count"] = exact_row["exact_time_count"] or 0

    dt_rows = await pool.fetch(
        """SELECT discount_type, COUNT(*) as cnt, SUM(COALESCE(discount_sum, 0)) as total
           FROM orders_raw
           WHERE branch_name = $1 AND date::text = $2 AND tenant_id = $3
             AND discount_type IS NOT NULL AND discount_type != ''
             AND status != 'Отменена'
           GROUP BY discount_type
           ORDER BY total DESC""",
        branch_name, date_iso, tenant_id,
    )

    result["discount_types_agg"] = [
        {"type": r["discount_type"], "count": r["cnt"], "sum": round(r["total"] or 0)}
        for r in dt_rows
    ] if dt_rows else []

    pt_rows = await pool.fetch(
        """SELECT payment_type, COUNT(*) AS cnt, SUM(COALESCE(sum, 0)) AS total
           FROM orders_raw
           WHERE branch_name = $1 AND date::text = $2 AND tenant_id = $3
             AND status != 'Отменена'
             AND payment_type IS NOT NULL AND payment_type != ''
           GROUP BY payment_type
           ORDER BY total DESC""",
        branch_name, date_iso, tenant_id,
    )
    result["payment_types_agg"] = [
        {"type": _PAY_MAP.get(r["payment_type"].lower(), r["payment_type"]), "sum": round(float(r["total"] or 0))}
        for r in pt_rows
    ] if pt_rows else []

    staff_rows = await pool.fetch(
        """SELECT role_class, COUNT(DISTINCT employee_id) as cnt
           FROM shifts_raw
           WHERE branch_name = $1 AND date::text = $2 AND tenant_id = $3
             AND clock_in != clock_out
           GROUP BY role_class""",
        branch_name, date_iso, tenant_id,
    )
    staff = {r["role_class"]: r["cnt"] for r in staff_rows}
    result["cooks_today"] = staff.get("cook", 0)
    result["couriers_today"] = staff.get("courier", 0)

    # Статистика новых / повторных клиентов
    # «Новый» — клиент, у которого самый первый заказ пришёлся именно на date_iso.
    # Самозаказы (пустой телефон) исключаются.
    customer_rows = await pool.fetch(
        """WITH first_orders AS (
               SELECT client_phone, MIN(date) AS first_date
               FROM orders_raw
               WHERE branch_name = $1 AND tenant_id = $3
                 AND client_phone IS NOT NULL AND client_phone != ''
                 AND status != 'Отменена'
               GROUP BY client_phone
           )
           SELECT
               CASE WHEN fo.first_date::text = $2 THEN 'new' ELSE 'repeat' END AS ctype,
               COUNT(DISTINCT o.client_phone)                                   AS clients,
               COALESCE(SUM(o.sum), 0)                                          AS revenue
           FROM orders_raw o
           JOIN first_orders fo ON o.client_phone = fo.client_phone
           WHERE o.branch_name = $1 AND o.tenant_id = $3
             AND o.date::text = $2
             AND o.status != 'Отменена'
           GROUP BY ctype""",
        branch_name, date_iso, tenant_id,
    )
    result["new_customers"] = 0
    result["new_customers_revenue"] = 0.0
    result["repeat_customers"] = 0
    result["repeat_customers_revenue"] = 0.0
    for cr in customer_rows:
        if cr["ctype"] == "new":
            result["new_customers"] = cr["clients"]
            result["new_customers_revenue"] = float(cr["revenue"])
        else:
            result["repeat_customers"] = cr["clients"]
            result["repeat_customers_revenue"] = float(cr["revenue"])

    for k in ("avg_cooking_min", "avg_wait_min", "avg_delivery_min", "avg_late_min"):
        v = result.get(k)
        if v is not None:
            result[k] = round(float(v), 1)

    return result


async def get_live_today_stats(branch_name: str, date_iso: str, tenant_id: int = 1) -> dict | None:
    """Базовая статистика за сегодня прямо из orders_raw (смена ещё не закрыта)."""
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT
            COUNT(*) FILTER (WHERE status != 'Отменена') AS orders_count,
            SUM(sum) FILTER (WHERE status != 'Отменена') AS revenue,
            COUNT(*) FILTER (WHERE is_self_service = false AND status != 'Отменена') AS delivery_count,
            COUNT(*) FILTER (WHERE is_self_service = true AND status != 'Отменена') AS pickup_count
        FROM orders_raw
        WHERE tenant_id = $1 AND branch_name = $2 AND date::text = $3""",
        tenant_id, branch_name, date_iso,
    )
    if not row or not row["orders_count"]:
        return None
    result = dict(row)
    rev = result.get("revenue") or 0
    chk = result.get("orders_count") or 0
    result["revenue"] = round(rev)
    result["avg_check"] = round(rev / chk) if chk else 0
    result["_is_live"] = True
    return result


async def get_period_stats(branch_name: str, date_from: str, date_to: str, tenant_id: int = 1) -> dict | None:
    """Агрегирует daily_stats за период [date_from, date_to] для одной точки."""
    import json as _json
    pool = get_pool()

    row = await pool.fetchrow(
        """SELECT
            SUM(orders_count) AS orders_count,
            SUM(revenue) AS revenue,
            SUM(discount_sum) AS discount_sum,
            SUM(sailplay) AS sailplay,
            SUM(delivery_count) AS delivery_count,
            SUM(pickup_count) AS pickup_count,
            SUM(late_count) AS late_count,
            SUM(total_delivered) AS total_delivered,
            SUM(COALESCE(late_delivery_count, 0)) AS late_delivery_count,
            SUM(COALESCE(late_pickup_count, 0)) AS late_pickup_count,
            SUM(cooks_count) AS cooks_sum,
            SUM(couriers_count) AS couriers_sum,
            COUNT(*) AS days_count,
            CASE WHEN SUM(revenue) > 0
                 THEN SUM(cogs_pct * revenue) / SUM(revenue)
            END AS cogs_pct,
            SUM(avg_late_min * COALESCE(late_count, 0))
                / NULLIF(SUM(CASE WHEN avg_late_min > 0 THEN COALESCE(late_count, 0) ELSE 0 END), 0)
                AS avg_late_min,
            SUM(avg_cooking_min * COALESCE(total_delivered, 0))
                / NULLIF(SUM(CASE WHEN avg_cooking_min IS NOT NULL THEN COALESCE(total_delivered, 0) ELSE 0 END), 0)
                AS avg_cooking_min,
            SUM(avg_wait_min * COALESCE(total_delivered, 0))
                / NULLIF(SUM(CASE WHEN avg_wait_min IS NOT NULL THEN COALESCE(total_delivered, 0) ELSE 0 END), 0)
                AS avg_wait_min,
            SUM(avg_delivery_min * COALESCE(total_delivered, 0))
                / NULLIF(SUM(CASE WHEN avg_delivery_min IS NOT NULL THEN COALESCE(total_delivered, 0) ELSE 0 END), 0)
                AS avg_delivery_min,
            SUM(COALESCE(exact_time_count, 0)) AS exact_time_count,
            SUM(COALESCE(new_customers, 0)) AS new_customers,
            SUM(COALESCE(new_customers_revenue, 0)) AS new_customers_revenue,
            SUM(COALESCE(repeat_customers, 0)) AS repeat_customers,
            SUM(COALESCE(repeat_customers_revenue, 0)) AS repeat_customers_revenue
        FROM daily_stats
        WHERE tenant_id = $1 AND branch_name = $2 AND date::text BETWEEN $3 AND $4""",
        tenant_id, branch_name, date_from, date_to,
    )

    if not row or not row["revenue"]:
        return None

    result = dict(row)
    rev = result["revenue"] or 0
    chk = result["orders_count"] or 0
    result["avg_check"] = round(rev / chk) if chk else 0
    days = result.pop("days_count", 1) or 1
    result["cooks_count"] = round((result.pop("cooks_sum", 0) or 0) / days)
    result["couriers_count"] = round((result.pop("couriers_sum", 0) or 0) / days)

    for k in ("cogs_pct", "avg_late_min", "avg_cooking_min", "avg_wait_min", "avg_delivery_min"):
        v = result.get(k)
        if v is not None:
            result[k] = round(float(v), 1 if k != "cogs_pct" else 2)

    # Скидки по типам — сначала из daily_stats.discount_types (OLAP, корректные суммы),
    # fallback к orders_raw для данных без OLAP-разбивки.
    daily_dt_rows = await pool.fetch(
        """SELECT discount_types FROM daily_stats
           WHERE tenant_id = $1 AND branch_name = $2 AND date::text BETWEEN $3 AND $4
             AND discount_types IS NOT NULL AND discount_types NOT IN ('', '[]')""",
        tenant_id, branch_name, date_from, date_to,
    )
    type_totals: dict[str, float] = {}
    for _r in daily_dt_rows:
        try:
            for _item in _json.loads(_r["discount_types"]):
                _t = _item.get("type", "?")
                type_totals[_t] = type_totals.get(_t, 0.0) + float(_item.get("sum", 0))
        except (TypeError, _json.JSONDecodeError):
            pass

    if type_totals:
        cnt_rows = await pool.fetch(
            """SELECT discount_type, COUNT(*) as cnt FROM orders_raw
               WHERE branch_name = $1 AND date::text BETWEEN $2 AND $3
                 AND discount_type IS NOT NULL AND discount_type != ''
                 AND status != 'Отменена'
               GROUP BY discount_type""",
            branch_name, date_from, date_to,
        )
        type_counts = {r["discount_type"]: r["cnt"] for r in cnt_rows}
        result["discount_types"] = _json.dumps(
            sorted(
                [{"type": t, "sum": round(s), "count": type_counts.get(t, 0)}
                 for t, s in type_totals.items()],
                key=lambda x: x["sum"], reverse=True,
            ),
            ensure_ascii=False,
        )
    else:
        # Fallback: orders_raw (discount_sum заполнен не для всех заказов)
        dt_rows = await pool.fetch(
            """SELECT discount_type, COUNT(*) as cnt, SUM(COALESCE(discount_sum, 0)) as total
               FROM orders_raw
               WHERE branch_name = $1 AND date::text BETWEEN $2 AND $3
                 AND discount_type IS NOT NULL AND discount_type != ''
                 AND status != 'Отменена'
               GROUP BY discount_type
               ORDER BY total DESC""",
            branch_name, date_from, date_to,
        )
        result["discount_types"] = _json.dumps(
            [{"type": r["discount_type"], "count": r["cnt"], "sum": round(r["total"] or 0)}
             for r in dt_rows],
            ensure_ascii=False,
        ) if dt_rows else "[]"

    pc_row = await pool.fetchrow(
        """SELECT COUNT(*) AS payment_changed_count
           FROM orders_raw
           WHERE branch_name = $1 AND date::text BETWEEN $2 AND $3
             AND COALESCE(payment_changed, false) = true
             AND status != 'Отменена'""",
        branch_name, date_from, date_to,
    )
    result["payment_changed_count"] = pc_row["payment_changed_count"] if pc_row else 0

    pt_rows = await pool.fetch(
        """SELECT payment_type, COUNT(*) AS cnt, SUM(COALESCE(sum, 0)) AS total
           FROM orders_raw
           WHERE branch_name = $1 AND date::text BETWEEN $2 AND $3
             AND tenant_id = $4
             AND status != 'Отменена'
             AND payment_type IS NOT NULL AND payment_type != ''
           GROUP BY payment_type
           ORDER BY total DESC""",
        branch_name, date_from, date_to, tenant_id,
    )
    result["payment_types"] = _json.dumps(
        [{"type": _PAY_MAP.get(r["payment_type"].lower(), r["payment_type"]), "sum": round(float(r["total"] or 0))}
         for r in pt_rows],
        ensure_ascii=False,
    ) if pt_rows else "[]"

    return result


async def get_repeat_conversion(
    branch_names: list[str],
    tenant_id: int,
) -> dict:
    """Конверсия новых клиентов в повторных за прошлый полный календарный месяц.

    Возвращает:
        new_count    — сколько клиентов сделали ПЕРВЫЙ заказ в прошлом месяце
        converted    — из них сколько заказали ещё раз позже
        conversion_pct — процент конверсии (0..100)
        month_label  — читаемое название периода, напр. «февраль 2026»
    """
    from datetime import date as _date, timedelta
    import calendar as _cal

    pool = get_pool()

    today = _date.today()
    first_of_cur = today.replace(day=1)
    last_of_prev = first_of_cur - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)

    _MONTHS_RU = (
        "", "январь", "февраль", "март", "апрель", "май", "июнь",
        "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
    )
    month_label = f"{_MONTHS_RU[first_of_prev.month]} {first_of_prev.year}"

    row = await pool.fetchrow(
        """WITH first_orders AS (
               -- клиенты, чей самый первый заказ был в прошлом месяце
               SELECT client_phone
               FROM orders_raw
               WHERE tenant_id = $1
                 AND branch_name = ANY($2::text[])
                 AND client_phone IS NOT NULL AND client_phone != ''
                 AND status != 'Отменена'
               GROUP BY client_phone
               HAVING MIN(date) BETWEEN $3::date AND $4::date
           ),
           converted AS (
               -- из них кто заказал хотя бы раз после прошлого месяца
               SELECT DISTINCT o.client_phone
               FROM orders_raw o
               JOIN first_orders fo ON o.client_phone = fo.client_phone
               WHERE o.tenant_id = $1
                 AND o.branch_name = ANY($2::text[])
                 AND o.date > $4::date
                 AND o.status != 'Отменена'
           )
           SELECT
               (SELECT COUNT(*) FROM first_orders) AS new_count,
               (SELECT COUNT(*) FROM converted)    AS converted_count""",
        tenant_id,
        branch_names,
        first_of_prev,
        last_of_prev,
    )

    new_count = int(row["new_count"] or 0)
    converted = int(row["converted_count"] or 0)
    conversion_pct = round(converted / new_count * 100) if new_count else 0

    return {
        "new_count": new_count,
        "converted": converted,
        "conversion_pct": conversion_pct,
        "month_label": month_label,
    }


async def get_exact_time_orders(
    branch_name: str | None,
    date_iso: str,
    branch_names: list[str] | None = None,
    tenant_id: int | None = None,
) -> list[dict]:
    """Возвращает заказы, определённые как 'на точное время' для даты."""
    from app.ctx import ctx_tenant_id as _ctx_tenant_id
    
    pool = get_pool()
    
    # Если tenant_id не передан, берём из контекста
    if tenant_id is None:
        tenant_id = _ctx_tenant_id.get()
    
    conditions = [f"tenant_id = $1", f"date::text = $2", "status != 'Отменена'"]
    params: list = [tenant_id, date_iso]

    if branch_name:
        params.append(branch_name)
        conditions.append(f"branch_name = ${len(params)}")
    elif branch_names:
        params.append(branch_names)
        conditions.append(f"branch_name = ANY(${len(params)})")

    conditions.append(_EXACT_TIME_CONDITIONS_PG)
    where = " AND ".join(conditions)

    rows = await pool.fetch(
        f"""SELECT delivery_num, branch_name, sum, comment,
                   opened_at, planned_time, cooked_time, send_time,
                   actual_time, service_print_time, is_self_service
            FROM orders_raw WHERE {where}
            ORDER BY opened_at""",
        *params,
    )
    return [dict(r) for r in rows]


# =====================================================================
# hourly_stats
# =====================================================================

async def upsert_hourly_stats(row: dict, tenant_id: int = 1) -> None:
    """UPSERT одной строки в hourly_stats."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO hourly_stats
               (tenant_id, branch_name, hour,
                orders_count, revenue, avg_check,
                avg_cook_time, avg_courier_wait, avg_delivery_time,
                late_count, late_percent, completed_count,
                cooks_on_shift, couriers_on_shift, orders_in_progress,
                updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,now())
               ON CONFLICT (tenant_id, branch_name, hour) DO UPDATE SET
                 orders_count=EXCLUDED.orders_count,
                 revenue=EXCLUDED.revenue,
                 avg_check=EXCLUDED.avg_check,
                 avg_cook_time=EXCLUDED.avg_cook_time,
                 avg_courier_wait=EXCLUDED.avg_courier_wait,
                 avg_delivery_time=EXCLUDED.avg_delivery_time,
                 late_count=EXCLUDED.late_count,
                 late_percent=EXCLUDED.late_percent,
                 completed_count=EXCLUDED.completed_count,
                 cooks_on_shift=EXCLUDED.cooks_on_shift,
                 couriers_on_shift=EXCLUDED.couriers_on_shift,
                 orders_in_progress=EXCLUDED.orders_in_progress,
                 updated_at=now()""",
            tenant_id,
            row["branch_name"],
            row["hour"],
            row.get("orders_count", 0),
            row.get("revenue", 0.0),
            row.get("avg_check", 0.0),
            row.get("avg_cook_time"),
            row.get("avg_courier_wait"),
            row.get("avg_delivery_time"),
            row.get("late_count", 0),
            row.get("late_percent", 0.0),
            row.get("completed_count", 0),
            row.get("cooks_on_shift", 0),
            row.get("couriers_on_shift", 0),
            row.get("orders_in_progress", 0),
        )


async def get_hourly_stats(
    branch_name: str,
    hour_from: str,
    hour_to: str,
    tenant_id: int = 1,
) -> list[dict]:
    """Возвращает строки hourly_stats за период [hour_from, hour_to) для одной точки.

    hour_from / hour_to — ISO-строки: '2026-03-07' или '2026-03-07T09:00:00'.
    """
    pool = get_pool()
    from datetime import datetime as _dt
    dt_from = _dt.fromisoformat(hour_from) if isinstance(hour_from, str) else hour_from
    dt_to   = _dt.fromisoformat(hour_to)   if isinstance(hour_to,   str) else hour_to
    # Колонка hour — TIMESTAMP (без таймзоны); aware datetime вызовет ошибку
    if dt_from.tzinfo:
        dt_from = dt_from.replace(tzinfo=None)
    if dt_to.tzinfo:
        dt_to = dt_to.replace(tzinfo=None)
    rows = await pool.fetch(
        """SELECT hour, orders_count, revenue, avg_check,
                  avg_cook_time, avg_courier_wait, avg_delivery_time,
                  late_count, late_percent, completed_count,
                  cooks_on_shift, couriers_on_shift, orders_in_progress
           FROM hourly_stats
           WHERE tenant_id = $1
             AND branch_name = $2
             AND hour >= $3
             AND hour <  $4
           ORDER BY hour""",
        tenant_id, branch_name, dt_from, dt_to,
    )
    return [dict(r) for r in rows]


# =====================================================================
# TBank — online_payments + tbank_registry_logs
# =====================================================================

async def save_bank_statement_log(*args, **kwargs) -> None:
    logger.debug("save_bank_statement_log: not implemented in PG mode")


async def save_tbank_registry_log(
    user_id: int,
    chat_id: int,
    filename: str,
    report_date: str,
    total_orders: int,
    confirmed: int,
    mismatched: int,
    new_pending: int,
    missing_in_iiko: int,
    tenant_id: int = 1,
) -> None:
    pool = get_pool()
    await pool.execute(
        """INSERT INTO tbank_registry_logs
           (tenant_id, user_id, chat_id, filename, report_date,
            total_orders, confirmed, mismatched, new_pending, missing_in_iiko)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
        tenant_id, user_id, chat_id, filename, report_date,
        total_orders, confirmed, mismatched, new_pending, missing_in_iiko,
    )


async def upsert_online_payment(
    branch: str,
    order_number: str,
    order_date: str,
    iiko_amount: float,
    tenant_id: int = 1,
) -> None:
    pool = get_pool()
    await pool.execute(
        """INSERT INTO online_payments
           (tenant_id, branch, order_number, order_date, iiko_amount, status)
           VALUES ($1,$2,$3,$4,$5,'pending')
           ON CONFLICT (tenant_id, branch, order_number) DO UPDATE SET
             iiko_amount = EXCLUDED.iiko_amount,
             updated_at  = now()
           WHERE online_payments.status = 'pending'""",
        tenant_id, branch, order_number, order_date, float(iiko_amount),
    )


async def confirm_online_payment(
    branch: str,
    order_number: str,
    tbank_amount: float,
    tbank_commission: float,
    tbank_confirmed_date: str,
    tbank_transaction_id: str,
    iiko_amount: float | None = None,
    tenant_id: int = 1,
) -> str:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, iiko_amount, status FROM online_payments WHERE tenant_id=$1 AND branch=$2 AND order_number=$3",
            tenant_id, branch, order_number,
        )
        if row:
            db_iiko = row["iiko_amount"]
            if iiko_amount is not None:
                status = "confirmed" if abs(iiko_amount - tbank_amount) < 1.0 else "mismatch"
                new_iiko = iiko_amount
            else:
                status = "missing_in_iiko"
                new_iiko = db_iiko
            await conn.execute(
                """UPDATE online_payments SET
                     status=$1, iiko_amount=$2, tbank_amount=$3, tbank_commission=$4,
                     tbank_confirmed_date=$5, tbank_transaction_id=$6, updated_at=now()
                   WHERE tenant_id=$7 AND branch=$8 AND order_number=$9""",
                status, new_iiko, float(tbank_amount), float(tbank_commission),
                tbank_confirmed_date, tbank_transaction_id,
                tenant_id, branch, order_number,
            )
            return status
        else:
            if iiko_amount is not None:
                status = "confirmed" if abs(iiko_amount - tbank_amount) < 1.0 else "mismatch"
                stored_iiko = iiko_amount
            else:
                status = "missing_in_iiko"
                stored_iiko = tbank_amount
            await conn.execute(
                """INSERT INTO online_payments
                   (tenant_id, branch, order_number, order_date, iiko_amount, status,
                    tbank_amount, tbank_commission, tbank_confirmed_date, tbank_transaction_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                tenant_id, branch, order_number, tbank_confirmed_date,
                float(stored_iiko), status, float(tbank_amount), float(tbank_commission),
                tbank_confirmed_date, tbank_transaction_id,
            )
            return f"created_{status}"


async def confirm_payout(
    branch: str,
    order_number: str,
    payout_date: str,
    payout_amount: float,
    tenant_id: int = 1,
) -> str:
    pool = get_pool()
    result = await pool.execute(
        """UPDATE online_payments SET payout_date=$1, payout_amount=$2, updated_at=now()
           WHERE tenant_id=$3 AND branch=$4 AND order_number=$5""",
        payout_date, float(payout_amount), tenant_id, branch, order_number,
    )
    return "confirmed" if result.split()[-1] != "0" else "not_found"


async def record_chargeback(
    branch: str,
    order_number: str,
    chargeback_date: str,
    amount: float,
    tenant_id: int = 1,
) -> None:
    pool = get_pool()
    await pool.execute(
        """UPDATE online_payments SET status='chargeback', payout_date=$1, payout_amount=$2, updated_at=now()
           WHERE tenant_id=$3 AND branch=$4 AND order_number=$5""",
        chargeback_date, -abs(float(amount)), tenant_id, branch, order_number,
    )


async def get_payout_delayed(days: int = 2, since_date: str | None = None, tenant_id: int = 1) -> list[dict]:
    pool = get_pool()
    conditions = [
        "tenant_id = $1",
        "status = 'confirmed'",
        "payout_date IS NULL",
        "tbank_confirmed_date IS NOT NULL",
        f"NOW() - tbank_confirmed_date::timestamp >= interval '{days} days'",
    ]
    params: list = [tenant_id]
    if since_date:
        params.append(since_date)
        conditions.append(f"order_date >= ${len(params)}")
    rows = await pool.fetch(
        f"SELECT * FROM online_payments WHERE {' AND '.join(conditions)} ORDER BY tbank_confirmed_date",
        *params,
    )
    return [dict(r) for r in rows]


async def get_pending_payments(
    max_age_days: int | None = None,
    since_date: str | None = None,
    tenant_id: int = 1,
) -> list[dict]:
    pool = get_pool()
    conditions = ["tenant_id = $1", "status = 'pending'"]
    params: list = [tenant_id]
    if since_date:
        params.append(since_date)
        conditions.append(f"order_date >= ${len(params)}")
    if max_age_days is not None:
        conditions.append(f"NOW() - order_date::timestamp >= interval '{max_age_days} days'")
    rows = await pool.fetch(
        f"SELECT * FROM online_payments WHERE {' AND '.join(conditions)} ORDER BY order_date",
        *params,
    )
    return [dict(r) for r in rows]


async def get_overdue_payments(days: int = 4, since_date: str | None = None, tenant_id: int = 1) -> list[dict]:
    return await get_pending_payments(max_age_days=days, since_date=since_date, tenant_id=tenant_id)


async def get_tracking_summary(since_date: str | None = None, tenant_id: int = 1) -> dict[str, dict[str, dict]]:
    pool = get_pool()
    params: list = [tenant_id]
    where = "tenant_id = $1"
    if since_date:
        params.append(since_date)
        where += f" AND order_date >= ${len(params)}"
    rows = await pool.fetch(
        f"""SELECT branch, order_date, status, COUNT(*) cnt, COALESCE(SUM(iiko_amount), 0) total_amt
            FROM online_payments WHERE {where}
            GROUP BY branch, order_date, status ORDER BY branch, order_date DESC""",
        *params,
    )
    result: dict[str, dict[str, dict]] = {}
    for r in rows:
        b, d, s = r["branch"], r["order_date"], r["status"]
        if b not in result:
            result[b] = {}
        if d not in result[b]:
            result[b][d] = {"confirmed": 0, "pending": 0, "missing": 0, "amount_pending": 0.0}
        if s == "confirmed":
            result[b][d]["confirmed"] += r["cnt"]
        elif s == "pending":
            result[b][d]["pending"] += r["cnt"]
            result[b][d]["amount_pending"] += float(r["total_amt"])
        elif s == "missing_in_iiko":
            result[b][d]["missing"] += r["cnt"]
    return result


async def get_payment_changed_orders(branch_names: list[str], date_iso: str, tenant_id: int) -> list[dict]:
    """Заказы со сменой оплаты за дату по указанным точкам."""
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT branch_name, delivery_num, planned_time, sum, comment
           FROM orders_raw
           WHERE date::text = $1
             AND COALESCE(payment_changed, false) = true
             AND branch_name = ANY($2)
             AND tenant_id = $3
           ORDER BY branch_name, planned_time""",
        date_iso, branch_names, tenant_id,
    )
    return [dict(r) for r in rows]


# =====================================================================
# fot_daily
# =====================================================================

async def upsert_fot_daily_batch(rows: list[dict], tenant_id: int = 1) -> None:
    """UPSERT записей ФОТ по точке+дате+категории."""
    if not rows:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for r in rows:
                await conn.execute(
                    """INSERT INTO fot_daily
                       (tenant_id, branch_name, date, category,
                        fot_sum, hours_sum, employees_count, employees_no_rate)
                       VALUES ($1,$2,$3::date,$4,$5,$6,$7,$8)
                       ON CONFLICT (tenant_id, branch_name, date, category) DO UPDATE SET
                         fot_sum=EXCLUDED.fot_sum,
                         hours_sum=EXCLUDED.hours_sum,
                         employees_count=EXCLUDED.employees_count,
                         employees_no_rate=EXCLUDED.employees_no_rate,
                         updated_at=now()""",
                    tenant_id,
                    r["branch_name"], _to_date(r["date"]), r["category"],
                    r["fot_sum"], r["hours_sum"], r["employees_count"], r["employees_no_rate"],
                )


async def get_fot_daily(
    branch_name: str, date_iso: str, tenant_id: int = 1
) -> dict | None:
    """Возвращает {category: fot_sum} для точки за день. None если нет данных."""
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT category, fot_sum, hours_sum, employees_count, employees_no_rate
           FROM fot_daily
           WHERE tenant_id = $1 AND branch_name = $2 AND date = $3""",
        tenant_id, branch_name, _to_date(date_iso),
    )
    if not rows:
        return None
    return {r["category"]: float(r["fot_sum"]) for r in rows}


async def get_fot_period(
    branch_names: list[str], date_from: str, date_to: str, tenant_id: int = 1
) -> dict | None:
    """Суммарный ФОТ по категориям для списка точек за период. None если нет данных."""
    if not branch_names:
        return None
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT category, SUM(fot_sum) AS fot_sum
           FROM fot_daily
           WHERE tenant_id = $1 AND branch_name = ANY($2)
             AND date BETWEEN $3::date AND $4::date
           GROUP BY category""",
        tenant_id, branch_names, _to_date(date_from), _to_date(date_to),
    )
    if not rows:
        return None
    return {r["category"]: float(r["fot_sum"]) for r in rows}


# =====================================================================
# Real-time ФОТ — кеш ставок + расчёт по открытым сменам
# =====================================================================

async def upsert_rates_cache(
    tenant_id: int, branch_name: str, rates: dict
) -> None:
    """Сохраняет почасовые ставки сотрудников в employee_rates_cache.

    rates: {employee_id: Decimal(rate)} — из fetch_salary_map().
    """
    if not rates:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for emp_id, rate in rates.items():
                await conn.execute(
                    """INSERT INTO employee_rates_cache
                           (tenant_id, branch_name, employee_id, rate_per_hour, cached_at)
                       VALUES ($1, $2, $3, $4, now())
                       ON CONFLICT (tenant_id, branch_name, employee_id) DO UPDATE SET
                           rate_per_hour = EXCLUDED.rate_per_hour,
                           cached_at     = now()""",
                    tenant_id, branch_name, emp_id, float(rate),
                )


async def get_realtime_fot(
    branch_name: str, tenant_id: int = 1
) -> Optional[dict]:
    """Считает накопленный ФОТ поваров за сегодня — все смены (включая закрытые).

    Для активных смен конец = NOW(), для закрытых = clock_out.
    Смены с аномальной длительностью (>= 24ч) пропускаются.
    Ставка берётся из employee_rates_cache.
    Возвращает {'fot': int, 'hours': float, 'cooks': int} или None.
    """
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT
               s.employee_id,
               EXTRACT(EPOCH FROM (COALESCE(s.clock_out::timestamptz, NOW()) - s.clock_in::timestamptz)) / 3600.0 AS hours_worked,
               COALESCE(r.rate_per_hour, 0) AS rate
           FROM shifts_raw s
           LEFT JOIN employee_rates_cache r
               ON r.tenant_id = s.tenant_id
              AND r.branch_name = s.branch_name
              AND r.employee_id = s.employee_id
           WHERE s.tenant_id = $1
             AND s.branch_name = $2
             AND s.role_class = 'cook'
             AND s.clock_in IS NOT NULL
             AND DATE(s.clock_in::timestamptz AT TIME ZONE 'Asia/Krasnoyarsk') = CURRENT_DATE AT TIME ZONE 'Asia/Krasnoyarsk'""",
        tenant_id, branch_name,
    )

    if not rows:
        return None

    valid_rows = [r for r in rows if 0 < float(r["hours_worked"]) < 24]
    if not valid_rows:
        return None

    total_fot = sum(
        float(r["hours_worked"]) * float(r["rate"])
        for r in valid_rows
        if float(r["rate"]) > 0
    )
    total_hours = sum(float(r["hours_worked"]) for r in valid_rows)
    cooks = len(valid_rows)
    avg_hours = round(total_hours / cooks, 1) if cooks else 0

    return {
        "fot": round(total_fot),
        "hours": avg_hours,
        "cooks": cooks,
    }


# =====================================================================
# Совместимость: DB_PATH и BACKEND для файлов с BACKEND-guard
# =====================================================================

DB_PATH = None  # В PG режиме нет SQLite-файла; используется как sentinel
BACKEND = "postgresql"

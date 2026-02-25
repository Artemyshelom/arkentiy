"""
SQLite через aiosqlite.
Таблицы:
  - iiko_tokens           — кэш токенов iiko (TTL ~15 мин)
  - job_logs              — история запусков задач
  - stoplist_state        — хэши стоп-листов для дедупликации алертов
  - report_updates        — флаги изменения данных (для утреннего отчёта)
  - daily_rt_snapshot     — RT-итоги дня (delays + staff)
  - orders_raw            — заказы из Events API
  - shifts_raw            — смены сотрудников
  - daily_stats           — OLAP-итоги дня по точкам
  - competitor_snapshots  — история запусков мониторинга конкурентов
  - competitor_menu_items — позиции меню конкурентов (нормализованно)
  - silence_log           — история активации режима тишины по чатам
  - audit_events          — подозрительные операции (аудитор)
  --- SaaS / Фаза 0 ---
  - tenants               — реестр клиентов (тенантов)
  - tenant_modules        — включённые модули per тенант
  - tenant_users          — TG-пользователи per тенант с ролями
  - tenant_chats          — TG-чаты per тенант
  - subscriptions         — биллинговое состояние подписки
  - iiko_credentials      — данные подключения iiko per тенант
"""

import aiosqlite
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path("/app/data/app.db")


async def init_db() -> None:
    """Создаёт таблицы если не существуют."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS iiko_tokens (
                city        TEXT PRIMARY KEY,
                token       TEXT NOT NULL,
                expires_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name    TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                status      TEXT NOT NULL DEFAULT 'running',
                error       TEXT,
                details     TEXT
            );

            CREATE TABLE IF NOT EXISTS stoplist_state (
                city        TEXT PRIMARY KEY,
                items_hash  TEXT NOT NULL,
                checked_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS report_updates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                branch      TEXT NOT NULL,
                field       TEXT NOT NULL,
                old_value   TEXT,
                new_value   TEXT,
                recorded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_rt_snapshot (
                branch              TEXT NOT NULL,
                date                TEXT NOT NULL,
                delays_late         INTEGER DEFAULT 0,
                delays_total        INTEGER DEFAULT 0,
                delays_avg_min      INTEGER DEFAULT 0,
                cooks_today         INTEGER DEFAULT 0,
                couriers_today      INTEGER DEFAULT 0,
                saved_at            TEXT NOT NULL,
                PRIMARY KEY (branch, date)
            );

            CREATE TABLE IF NOT EXISTS silence_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                activated_at TEXT NOT NULL,
                duration_min INTEGER NOT NULL,
                user_id      INTEGER
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT NOT NULL,
                branch_name  TEXT NOT NULL,
                city         TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                severity     TEXT NOT NULL DEFAULT 'warning',
                description  TEXT NOT NULL,
                meta_json    TEXT,
                created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_date
                ON audit_events(date);
            CREATE INDEX IF NOT EXISTS idx_audit_branch_date
                ON audit_events(branch_name, date);
        """)
        await db.commit()
    await init_analytics_tables()
    await init_competitor_tables()
    await init_bank_statement_tables()
    await init_saas_tables()
    await seed_default_tenant()


async def init_bank_statement_tables() -> None:
    """Создаёт таблицу логов обработки банковских выписок."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS bank_statement_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                processed_at TEXT NOT NULL,
                user_id      INTEGER,
                chat_id      INTEGER,
                filename     TEXT,
                date_from    TEXT,
                date_to      TEXT,
                total_docs   INTEGER,
                total_files  INTEGER
            );
        """)
        await db.commit()


async def save_bank_statement_log(
    user_id: int,
    chat_id: int,
    filename: str,
    date_from: str,
    date_to: str,
    total_docs: int,
    total_files: int,
) -> None:
    """Записывает факт обработки банковской выписки."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO bank_statement_logs
               (processed_at, user_id, chat_id, filename, date_from, date_to, total_docs, total_files)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                user_id, chat_id, filename, date_from, date_to, total_docs, total_files,
            ),
        )
        await db.commit()


async def init_competitor_tables() -> None:
    """Создаёт таблицы мониторинга конкурентов."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS competitor_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                city            TEXT NOT NULL,
                competitor_name TEXT NOT NULL,
                url             TEXT NOT NULL,
                scraped_at      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'ok',
                items_count     INTEGER DEFAULT 0,
                error_msg       TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_competitor
                ON competitor_snapshots(city, competitor_name, scraped_at);

            CREATE TABLE IF NOT EXISTS competitor_menu_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id     INTEGER NOT NULL REFERENCES competitor_snapshots(id),
                city            TEXT NOT NULL,
                competitor_name TEXT NOT NULL,
                category        TEXT,
                name            TEXT NOT NULL,
                price           REAL NOT NULL,
                price_old       REAL,
                portion         TEXT,
                scraped_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_items_competitor_date
                ON competitor_menu_items(competitor_name, scraped_at);
            CREATE INDEX IF NOT EXISTS idx_items_name
                ON competitor_menu_items(name);
        """)
        await db.commit()



# --- Токены iiko ---

async def get_iiko_token(city: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT token, expires_at FROM iiko_tokens WHERE city = ?", (city,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            token, expires_at_str = row
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now(timezone.utc) >= expires_at:
                return None
            return token


async def set_iiko_token(city: str, token: str, expires_at: datetime) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO iiko_tokens (city, token, expires_at)
               VALUES (?, ?, ?)""",
            (city, token, expires_at.isoformat()),
        )
        await db.commit()


# --- Логи задач ---

async def log_job_start(job_name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO job_logs (job_name, started_at, status) VALUES (?, ?, 'running')",
            (job_name, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def log_job_finish(log_id: int, status: str, error: str | None = None, details: str | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE job_logs SET finished_at=?, status=?, error=?, details=?
               WHERE id=?""",
            (datetime.now(timezone.utc).isoformat(), status, error, details, log_id),
        )
        await db.commit()


# --- Стоп-лист дедупликация ---

def hash_stoplist(items: list) -> str:
    serialized = json.dumps(items, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(serialized.encode()).hexdigest()


async def get_stoplist_hash(city: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT items_hash FROM stoplist_state WHERE city = ?", (city,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_stoplist_hash(city: str, items_hash: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO stoplist_state (city, items_hash, checked_at)
               VALUES (?, ?, ?)""",
            (city, items_hash, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()



# --- Флаги обновления данных (для утреннего отчёта) ---

async def record_data_update(date: str, branch: str, field: str, old_value, new_value) -> None:
    """Записывает факт изменения данных — для пометки в утреннем отчёте."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO report_updates (date, branch, field, old_value, new_value, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                date,
                branch,
                field,
                str(old_value) if old_value is not None else None,
                str(new_value) if new_value is not None else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()


async def get_updates_for_date(date: str) -> list[dict]:
    """Возвращает все записанные изменения за дату."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM report_updates WHERE date = ? ORDER BY recorded_at",
            (date,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def clear_updates_for_date(date: str) -> None:
    """Удаляет флаги изменений за дату (вызывается после отправки утреннего отчёта)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM report_updates WHERE date = ?", (date,))
        await db.commit()


# --- RT Daily Snapshot (delays + staff для утреннего отчёта) ---

async def save_rt_snapshot(
    branch: str,
    date: str,
    delays_late: int,
    delays_total: int,
    delays_avg_min: int,
    cooks_today: int,
    couriers_today: int,
) -> None:
    """Сохраняет RT-итоги дня (опоздания, штат) для утреннего отчёта."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO daily_rt_snapshot
               (branch, date, delays_late, delays_total, delays_avg_min,
                cooks_today, couriers_today, saved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                branch, date,
                delays_late, delays_total, delays_avg_min,
                cooks_today, couriers_today,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()


async def get_rt_snapshot(branch: str, date: str) -> dict | None:
    """Возвращает сохранённый RT-снапшот или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT delays_late, delays_total, delays_avg_min,
                      cooks_today, couriers_today
               FROM daily_rt_snapshot WHERE branch = ? AND date = ?""",
            (branch, date),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return dict(row)


# =============================================================================
# Analytics: orders_raw, shifts_raw, daily_stats
# =============================================================================

async def init_analytics_tables() -> None:
    """Создаёт аналитические таблицы (вызывается из init_db или отдельно)."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Миграция: добавляем колонки если их нет (для существующих БД)
        new_columns = [
            ("ready_time",       "TEXT"),
            ("comment",          "TEXT"),
            ("operator",         "TEXT"),
            ("opened_at",        "TEXT"),
            ("has_problem",      "INTEGER DEFAULT 0"),
            ("problem_comment",  "TEXT"),
            ("payment_type",     "TEXT"),
            ("bonus_accrued",    "REAL"),
            ("source",           "TEXT"),
            ("return_sum",       "REAL"),
            ("service_charge",   "REAL"),
            ("cancel_reason",    "TEXT"),
            ("cancel_comment",   "TEXT"),
            ("send_time",          "TEXT"),
            ("service_print_time", "TEXT"),
            ("cooking_to_send_duration", "INTEGER"),
            ("pay_breakdown",      "TEXT"),
            ("cooked_time",        "TEXT"),
            ("discount_type",      "TEXT"),
        ]
        for col_name, col_type in new_columns:
            try:
                await db.execute(f"ALTER TABLE orders_raw ADD COLUMN {col_name} {col_type}")
                await db.commit()
            except Exception:
                pass  # Колонка уже существует

        daily_stats_new_cols = [
            ("cogs_pct",             "REAL"),
            ("sailplay",             "REAL"),
            ("discount_sum",         "REAL"),
            ("discount_types",       "TEXT"),
            ("late_delivery_count",  "INTEGER DEFAULT 0"),
            ("late_pickup_count",    "INTEGER DEFAULT 0"),
            ("avg_cooking_min",      "REAL"),
            ("avg_wait_min",         "REAL"),
            ("avg_delivery_min",     "REAL"),
            ("exact_time_count",     "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in daily_stats_new_cols:
            try:
                await db.execute(f"ALTER TABLE daily_stats ADD COLUMN {col_name} {col_type}")
                await db.commit()
            except Exception:
                pass

        await db.executescript("""
            CREATE TABLE IF NOT EXISTS orders_raw (
                branch_name     TEXT NOT NULL,
                delivery_num    TEXT NOT NULL,
                status          TEXT,
                courier         TEXT,
                sum             REAL,
                planned_time    TEXT,
                actual_time     TEXT,
                is_self_service  INTEGER DEFAULT 0,
                date             TEXT,
                is_late          INTEGER DEFAULT 0,
                late_minutes     REAL DEFAULT 0,
                client_name      TEXT,
                client_phone     TEXT,
                delivery_address TEXT,
                items            TEXT,
                ready_time       TEXT,
                -- Новые поля (Events API)
                comment          TEXT,
                operator         TEXT,
                opened_at        TEXT,
                has_problem      INTEGER DEFAULT 0,
                problem_comment  TEXT,
                -- Новые поля (OLAP, заполняются при бэкфилле)
                payment_type     TEXT,
                bonus_accrued    REAL,
                source           TEXT,
                return_sum       REAL,
                service_charge   REAL,
                cancel_reason    TEXT,
                cancel_comment   TEXT,
                updated_at       TEXT NOT NULL,
                PRIMARY KEY (branch_name, delivery_num)
            );

            CREATE INDEX IF NOT EXISTS idx_orders_date
                ON orders_raw(date);
            CREATE INDEX IF NOT EXISTS idx_orders_branch_date
                ON orders_raw(branch_name, date);

            CREATE TABLE IF NOT EXISTS shifts_raw (
                branch_name     TEXT NOT NULL,
                employee_id     TEXT NOT NULL,
                employee_name   TEXT,
                role_class      TEXT,
                date            TEXT,
                clock_in        TEXT,
                clock_out       TEXT,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (branch_name, employee_id, clock_in)
            );

            CREATE INDEX IF NOT EXISTS idx_shifts_branch_date
                ON shifts_raw(branch_name, date);

            CREATE TABLE IF NOT EXISTS daily_stats (
                branch_name         TEXT NOT NULL,
                date                TEXT NOT NULL,
                orders_count        INTEGER DEFAULT 0,
                revenue             REAL DEFAULT 0,
                avg_check           REAL DEFAULT 0,
                cogs_pct            REAL,
                sailplay            REAL,
                discount_sum        REAL,
                discount_types      TEXT,
                delivery_count      INTEGER DEFAULT 0,
                pickup_count        INTEGER DEFAULT 0,
                late_count          INTEGER DEFAULT 0,
                total_delivered     INTEGER DEFAULT 0,
                late_percent        REAL DEFAULT 0,
                avg_late_min        REAL DEFAULT 0,
                cooks_count         INTEGER DEFAULT 0,
                couriers_count      INTEGER DEFAULT 0,
                updated_at          TEXT NOT NULL,
                PRIMARY KEY (branch_name, date)
            );
        """)
        await db.commit()


async def upsert_orders_batch(rows: list[dict]) -> None:
    """Batch UPSERT доставок в orders_raw. Ключ: (branch_name, delivery_num)."""
    if not rows:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO orders_raw
               (branch_name, delivery_num, status, courier, sum,
                planned_time, actual_time, is_self_service,
                date, is_late, late_minutes,
                client_name, client_phone, delivery_address, items,
                ready_time, cooked_time,
                comment, operator, opened_at, has_problem, problem_comment,
                payment_type, bonus_accrued, source, return_sum, service_charge,
                cancel_reason, cancel_comment,
                updated_at)
               VALUES (:branch_name, :delivery_num, :status, :courier, :sum,
                       :planned_time, :actual_time, :is_self_service,
                       :date, :is_late, :late_minutes,
                       :client_name, :client_phone, :delivery_address, :items,
                       :ready_time, :cooked_time,
                       :comment, :operator, :opened_at, :has_problem, :problem_comment,
                       :payment_type, :bonus_accrued, :source, :return_sum, :service_charge,
                       :cancel_reason, :cancel_comment,
                       :updated_at)
               ON CONFLICT(branch_name, delivery_num) DO UPDATE SET
                 status=excluded.status, courier=excluded.courier, sum=excluded.sum,
                 planned_time=excluded.planned_time, actual_time=excluded.actual_time,
                 is_self_service=excluded.is_self_service, date=excluded.date,
                 is_late=excluded.is_late, late_minutes=excluded.late_minutes,
                 client_name=excluded.client_name, client_phone=excluded.client_phone,
                 delivery_address=excluded.delivery_address, items=excluded.items,
                 ready_time=COALESCE(excluded.ready_time, orders_raw.ready_time),
                 cooked_time=COALESCE(excluded.cooked_time, orders_raw.cooked_time),
                 comment=excluded.comment, operator=excluded.operator,
                 opened_at=excluded.opened_at, has_problem=excluded.has_problem,
                 problem_comment=excluded.problem_comment,
                 payment_type=COALESCE(NULLIF(excluded.payment_type, ''), orders_raw.payment_type),
                 bonus_accrued=COALESCE(excluded.bonus_accrued, orders_raw.bonus_accrued),
                 source=COALESCE(NULLIF(excluded.source, ''), orders_raw.source),
                 return_sum=COALESCE(excluded.return_sum, orders_raw.return_sum),
                 service_charge=COALESCE(excluded.service_charge, orders_raw.service_charge),
                 cancel_reason=COALESCE(NULLIF(excluded.cancel_reason, ''), orders_raw.cancel_reason),
                 cancel_comment=COALESCE(NULLIF(excluded.cancel_comment, ''), orders_raw.cancel_comment),
                 updated_at=excluded.updated_at""",
            rows,
        )
        await db.commit()


async def upsert_shifts_batch(rows: list[dict]) -> None:
    """Batch UPSERT смен в shifts_raw. Ключ: (branch_name, employee_id, clock_in)."""
    if not rows:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT OR REPLACE INTO shifts_raw
               (branch_name, employee_id, employee_name, role_class,
                date, clock_in, clock_out, updated_at)
               VALUES (:branch_name, :employee_id, :employee_name, :role_class,
                       :date, :clock_in, :clock_out, :updated_at)""",
            rows,
        )
        await db.commit()


async def upsert_daily_stats_batch(rows: list[dict]) -> None:
    """Batch UPSERT в daily_stats. Ключ: (branch_name, date)."""
    if not rows:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT OR REPLACE INTO daily_stats
               (branch_name, date, orders_count, revenue, avg_check,
                cogs_pct, sailplay, discount_sum, discount_types,
                delivery_count, pickup_count, late_count, total_delivered,
                late_percent, avg_late_min, cooks_count, couriers_count,
                late_delivery_count, late_pickup_count,
                avg_cooking_min, avg_wait_min, avg_delivery_min,
                exact_time_count, updated_at)
               VALUES (:branch_name, :date, :orders_count, :revenue, :avg_check,
                       :cogs_pct, :sailplay, :discount_sum, :discount_types,
                       :delivery_count, :pickup_count, :late_count, :total_delivered,
                       :late_percent, :avg_late_min, :cooks_count, :couriers_count,
                       :late_delivery_count, :late_pickup_count,
                       :avg_cooking_min, :avg_wait_min, :avg_delivery_min,
                       :exact_time_count, datetime('now'))""",
            rows,
        )
        await db.commit()


async def backfill_daily_stats_from_orders_raw() -> int:
    """One-time backfill: recalculate aggregate fields in daily_stats from orders_raw + shifts_raw."""
    import json as _json

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        pairs = await (await db.execute(
            "SELECT DISTINCT branch_name, date FROM orders_raw ORDER BY date"
        )).fetchall()

    updated = 0
    for pair in pairs:
        bn, dt = pair["branch_name"], pair["date"]
        agg = await aggregate_orders_for_daily_stats(bn, dt)

        dt_json = _json.dumps(agg.get("discount_types_agg") or [], ensure_ascii=False)
        total_d = agg.get("total_delivery_count") or 0
        late_d = agg.get("late_delivery_count") or 0
        late_pct = round(late_d / total_d * 100, 1) if total_d else 0

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE daily_stats SET
                       total_delivered = ?,
                       late_count = ?,
                       late_delivery_count = ?,
                       late_pickup_count = ?,
                       late_percent = ?,
                       avg_late_min = ?,
                       avg_cooking_min = ?,
                       avg_wait_min = ?,
                       avg_delivery_min = ?,
                       cooks_count = ?,
                       couriers_count = ?,
                       discount_types = ?,
                       exact_time_count = ?,
                       updated_at = datetime('now')
                   WHERE branch_name = ? AND date = ?""",
                (
                    total_d, late_d, late_d,
                    agg.get("late_pickup_count") or 0,
                    late_pct,
                    agg.get("avg_late_min") or 0,
                    agg.get("avg_cooking_min"),
                    agg.get("avg_wait_min"),
                    agg.get("avg_delivery_min"),
                    agg.get("cooks_today") or 0,
                    agg.get("couriers_today") or 0,
                    dt_json,
                    agg.get("exact_time_count") or 0,
                    bn, dt,
                ),
            )
            await db.commit()
        updated += 1
    return updated


async def backfill_daily_stats_olap() -> int:
    """One-time backfill: fill cogs_pct, sailplay, discount_sum from OLAP v2 for dates that lack them."""
    import asyncio
    from app.clients.iiko_bo_olap_v2 import get_all_branches_stats

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT DISTINCT date FROM daily_stats
               WHERE (cogs_pct IS NULL OR cogs_pct = 0)
               ORDER BY date DESC"""
        )).fetchall()

    dates = [r["date"] for r in rows]
    if not dates:
        return 0

    updated = 0
    for date_iso in dates:
        try:
            dt = datetime.fromisoformat(date_iso)
            all_stats = await get_all_branches_stats(dt)
        except Exception:
            continue

        async with aiosqlite.connect(DB_PATH) as db:
            for name, stats in all_stats.items():
                cogs = stats.get("cogs_pct")
                sail = stats.get("sailplay")
                disc = stats.get("discount_sum")
                if cogs or sail or disc:
                    await db.execute(
                        """UPDATE daily_stats SET
                               cogs_pct = COALESCE(?, cogs_pct),
                               sailplay = COALESCE(?, sailplay),
                               discount_sum = COALESCE(?, discount_sum),
                               updated_at = datetime('now')
                           WHERE branch_name = ? AND date = ?""",
                        (cogs, sail, disc, name, date_iso),
                    )
            await db.commit()
        updated += 1
        await asyncio.sleep(0.3)

    return updated


_EXACT_TIME_CONDITIONS = """(
        LOWER(COALESCE(comment, '')) LIKE '%точн%'
        OR LOWER(COALESCE(comment, '')) LIKE '%тчн%'
        OR LOWER(COALESCE(comment, '')) LIKE '%предзаказ%'
        OR (planned_time != '' AND opened_at != ''
            AND (julianday(planned_time)
                 - julianday(replace(substr(opened_at, 1, 19), 'T', ' '))) * 1440 > 150)
        OR (service_print_time != '' AND opened_at != ''
            AND (julianday(service_print_time)
                 - julianday(replace(substr(opened_at, 1, 19), 'T', ' '))) * 1440 > 90)
        OR (cooked_time != '' AND opened_at != ''
            AND (julianday(cooked_time)
                 - julianday(replace(substr(opened_at, 1, 19), 'T', ' '))) * 1440 > 210)
        OR (send_time != '' AND cooked_time != '' AND actual_time != ''
            AND (julianday(send_time) - julianday(cooked_time)) * 1440 > 120
            AND (julianday(replace(substr(actual_time, 1, 19), 'T', ' '))
                 - julianday(send_time)) * 1440 < 5)
    )"""

_EXACT_TIME_FILTER = f"\n    AND NOT {_EXACT_TIME_CONDITIONS}\n"


async def aggregate_orders_for_daily_stats(
    branch_name: str, date_iso: str
) -> dict:
    """
    Агрегирует данные из orders_raw для daily_stats:
    задержки, средние времена (без заказов на точное время), типы скидок, штат.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        row = await (await db.execute(
            f"""SELECT
                SUM(CASE WHEN is_late = 1 AND is_self_service = 0 THEN 1 ELSE 0 END)
                    AS late_delivery_count,
                SUM(CASE WHEN is_late = 1 AND is_self_service = 1 THEN 1 ELSE 0 END)
                    AS late_pickup_count,
                SUM(CASE WHEN is_self_service = 0
                         AND status IN ('Доставлена','Закрыта') THEN 1 ELSE 0 END)
                    AS total_delivery_count,
                AVG(CASE WHEN is_late = 1 AND is_self_service = 0
                         THEN late_minutes END)
                    AS avg_late_min
            FROM orders_raw
            WHERE branch_name = ? AND date = ?
              AND status != 'Отменена'""",
            (branch_name, date_iso),
        )).fetchone()

        result = dict(row) if row else {}

        time_row = await (await db.execute(
            f"""SELECT
                AVG(CASE
                    WHEN cooked_time != '' AND opened_at != '' AND sum >= 200
                    THEN CASE
                        WHEN (julianday(cooked_time)
                              - julianday(replace(substr(opened_at, 1, 19), 'T', ' '))) * 1440
                             BETWEEN 1 AND 120
                        THEN (julianday(cooked_time)
                              - julianday(replace(substr(opened_at, 1, 19), 'T', ' '))) * 1440
                    END
                END) AS avg_cooking_min,
                AVG(CASE
                    WHEN send_time != '' AND cooked_time != ''
                    THEN CASE
                        WHEN (julianday(send_time)
                              - julianday(cooked_time)) * 1440
                             BETWEEN 0 AND 120
                        THEN (julianday(send_time)
                              - julianday(cooked_time)) * 1440
                    END
                END) AS avg_wait_min,
                AVG(CASE
                    WHEN actual_time != '' AND send_time != ''
                        AND is_self_service = 0
                    THEN CASE
                        WHEN (julianday(replace(substr(actual_time, 1, 19), 'T', ' '))
                              - julianday(send_time)) * 1440
                             BETWEEN 1 AND 120
                        THEN (julianday(replace(substr(actual_time, 1, 19), 'T', ' '))
                              - julianday(send_time)) * 1440
                    END
                END) AS avg_delivery_min
            FROM orders_raw
            WHERE branch_name = ? AND date = ?
              AND status != 'Отменена'
              {_EXACT_TIME_FILTER}""",
            (branch_name, date_iso),
        )).fetchone()

        if time_row:
            result.update(dict(time_row))

        exact_row = await (await db.execute(
            f"""SELECT COUNT(*) AS exact_time_count
            FROM orders_raw
            WHERE branch_name = ? AND date = ?
              AND status != 'Отменена'
              AND {_EXACT_TIME_CONDITIONS}""",
            (branch_name, date_iso),
        )).fetchone()

        if exact_row:
            result["exact_time_count"] = exact_row["exact_time_count"] or 0

        dt_rows = await (await db.execute(
            """SELECT discount_type, COUNT(*) as cnt, SUM(sum) as total
               FROM orders_raw
               WHERE branch_name = ? AND date = ?
                 AND discount_type IS NOT NULL AND discount_type != ''
                 AND status != 'Отменена'
               GROUP BY discount_type
               ORDER BY total DESC""",
            (branch_name, date_iso),
        )).fetchall()

        if dt_rows:
            result["discount_types_agg"] = [
                {"type": r["discount_type"], "count": r["cnt"], "sum": round(r["total"] or 0)}
                for r in dt_rows
            ]
        else:
            result["discount_types_agg"] = []

        for k in ("avg_cooking_min", "avg_wait_min", "avg_delivery_min", "avg_late_min"):
            v = result.get(k)
            if v is not None:
                result[k] = round(v, 1)

        # Штат из shifts_raw
        staff_rows = await (await db.execute(
            """SELECT role_class, COUNT(DISTINCT employee_id) as cnt
               FROM shifts_raw
               WHERE branch_name = ? AND date = ?
               GROUP BY role_class""",
            (branch_name, date_iso),
        )).fetchall()
        staff = {r["role_class"]: r["cnt"] for r in staff_rows}
        result["cooks_today"] = staff.get("cook", 0)
        result["couriers_today"] = staff.get("courier", 0)

        return result


async def aggregate_orders_today(branch_name: str, date_iso: str) -> dict:
    """Быстрый агрегат из orders_raw за сегодня для /статус (скидки + времена)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        time_row = await (await db.execute(
            f"""SELECT
                AVG(CASE
                    WHEN cooked_time != '' AND opened_at != '' AND sum >= 200
                    THEN CASE
                        WHEN (julianday(cooked_time)
                              - julianday(replace(substr(opened_at, 1, 19), 'T', ' '))) * 1440
                             BETWEEN 1 AND 120
                        THEN (julianday(cooked_time)
                              - julianday(replace(substr(opened_at, 1, 19), 'T', ' '))) * 1440
                    END
                END) AS avg_cooking_min,
                AVG(CASE
                    WHEN send_time != '' AND cooked_time != ''
                    THEN CASE
                        WHEN (julianday(send_time)
                              - julianday(cooked_time)) * 1440
                             BETWEEN 0 AND 120
                        THEN (julianday(send_time)
                              - julianday(cooked_time)) * 1440
                    END
                END) AS avg_wait_min,
                AVG(CASE
                    WHEN actual_time != '' AND send_time != ''
                        AND is_self_service = 0
                    THEN CASE
                        WHEN (julianday(replace(substr(actual_time, 1, 19), 'T', ' '))
                              - julianday(send_time)) * 1440
                             BETWEEN 1 AND 120
                        THEN (julianday(replace(substr(actual_time, 1, 19), 'T', ' '))
                              - julianday(send_time)) * 1440
                    END
                END) AS avg_delivery_min
            FROM orders_raw
            WHERE branch_name = ? AND date = ?
              AND status != 'Отменена'
              {_EXACT_TIME_FILTER}""",
            (branch_name, date_iso),
        )).fetchone()

        result = dict(time_row) if time_row else {}

        dt_rows = await (await db.execute(
            """SELECT discount_type, COUNT(*) as cnt, SUM(sum) as total
               FROM orders_raw
               WHERE branch_name = ? AND date = ?
                 AND discount_type IS NOT NULL AND discount_type != ''
                 AND status != 'Отменена'
               GROUP BY discount_type
               ORDER BY total DESC""",
            (branch_name, date_iso),
        )).fetchall()

        result["discount_types_agg"] = [
            {"type": r["discount_type"], "count": r["cnt"], "sum": round(r["total"] or 0)}
            for r in dt_rows
        ] if dt_rows else []

        for k in ("avg_cooking_min", "avg_wait_min", "avg_delivery_min"):
            v = result.get(k)
            if v is not None:
                result[k] = round(v, 1)

        return result


async def get_daily_stats(branch_name: str, date_iso: str) -> dict | None:
    """Читает строку daily_stats для точки и даты. Возвращает dict или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM daily_stats WHERE branch_name = ? AND date = ?",
            (branch_name, date_iso),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_period_stats(branch_name: str, date_from: str, date_to: str) -> dict | None:
    """Агрегирует daily_stats за период [date_from, date_to] для одной точки."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        row = await (await db.execute(
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
                -- Средневзвешенный COGS% (вес = выручка)
                CASE WHEN SUM(revenue) > 0
                     THEN SUM(cogs_pct * revenue) / SUM(revenue)
                END AS cogs_pct,
                AVG(avg_late_min) AS avg_late_min,
                AVG(avg_cooking_min) AS avg_cooking_min,
                AVG(avg_wait_min) AS avg_wait_min,
                AVG(avg_delivery_min) AS avg_delivery_min,
                SUM(COALESCE(exact_time_count, 0)) AS exact_time_count
            FROM daily_stats
            WHERE branch_name = ? AND date BETWEEN ? AND ?""",
            (branch_name, date_from, date_to),
        )).fetchone()

        if not row or not row["revenue"]:
            return None

        result = dict(row)
        rev = result["revenue"] or 0
        chk = result["orders_count"] or 0
        result["avg_check"] = round(rev / chk) if chk else 0
        days = result.pop("days_count", 1) or 1
        result["cooks_count"] = round(result.pop("cooks_sum", 0) / days) if days else 0
        result["couriers_count"] = round(result.pop("couriers_sum", 0) / days) if days else 0

        for k in ("cogs_pct", "avg_late_min", "avg_cooking_min", "avg_wait_min", "avg_delivery_min"):
            v = result.get(k)
            if v is not None:
                result[k] = round(v, 1 if k != "cogs_pct" else 2)

        # Скидки по типам — из orders_raw напрямую
        dt_rows = await (await db.execute(
            """SELECT discount_type, COUNT(*) as cnt, SUM(sum) as total
               FROM orders_raw
               WHERE branch_name = ? AND date BETWEEN ? AND ?
                 AND discount_type IS NOT NULL AND discount_type != ''
                 AND status != 'Отменена'
               GROUP BY discount_type
               ORDER BY total DESC""",
            (branch_name, date_from, date_to),
        )).fetchall()

        result["discount_types"] = json.dumps(
            [{"type": r["discount_type"], "count": r["cnt"], "sum": round(r["total"] or 0)}
             for r in dt_rows],
            ensure_ascii=False,
        ) if dt_rows else "[]"

        return result


async def get_exact_time_orders(
    branch_name: str | None,
    date_iso: str,
    branch_names: list[str] | None = None,
) -> list[dict]:
    """Возвращает заказы, определённые как 'на точное время' для даты."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        where = "WHERE date = ? AND status != 'Отменена'"
        params: list = [date_iso]

        if branch_name:
            where += " AND branch_name = ?"
            params.append(branch_name)
        elif branch_names:
            placeholders = ",".join("?" * len(branch_names))
            where += f" AND branch_name IN ({placeholders})"
            params.extend(branch_names)

        where += f" AND {_EXACT_TIME_CONDITIONS}"

        rows = await (await db.execute(
            f"""SELECT delivery_num, branch_name, sum, comment,
                       opened_at, planned_time, cooked_time, send_time,
                       actual_time, service_print_time, is_self_service
                FROM orders_raw {where}
                ORDER BY opened_at""",
            params,
        )).fetchall()
        return [dict(r) for r in rows]


# =============================================================================
# Competitor monitoring
# =============================================================================

async def create_competitor_snapshot(
    city: str,
    competitor_name: str,
    url: str,
    status: str = "ok",
    items_count: int = 0,
    error_msg: str | None = None,
) -> int:
    """Создаёт запись снапшота, возвращает id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO competitor_snapshots
               (city, competitor_name, url, scraped_at, status, items_count, error_msg)
               VALUES (?, ?, ?, datetime('now'), ?, ?, ?)""",
            (city, competitor_name, url, status, items_count, error_msg),
        )
        await db.commit()
        return cursor.lastrowid


async def save_competitor_items(snapshot_id: int, city: str, competitor_name: str, items: list[dict]) -> None:
    """Сохраняет позиции меню для снапшота."""
    if not items:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO competitor_menu_items
               (snapshot_id, city, competitor_name, category, name, price, price_old, portion, scraped_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            [
                (snapshot_id, city, competitor_name,
                 item.get("category"), item["name"], item["price"],
                 item.get("price_old"), item.get("portion"))
                for item in items
            ],
        )
        await db.commit()



async def get_second_last_competitor_items(city: str, competitor_name: str) -> list[dict]:
    """Возвращает позиции из предпоследнего успешного снапшота (для диффа)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Найти два последних успешных снапшота
        async with db.execute(
            """SELECT id FROM competitor_snapshots
               WHERE city = ? AND competitor_name = ? AND status = 'ok'
               ORDER BY scraped_at DESC LIMIT 2""",
            (city, competitor_name),
        ) as cursor:
            snapshot_ids = [row[0] for row in await cursor.fetchall()]

        if len(snapshot_ids) < 2:
            return []

        prev_snapshot_id = snapshot_ids[1]
        async with db.execute(
            """SELECT name, price, price_old, portion, category
               FROM competitor_menu_items
               WHERE snapshot_id = ?""",
            (prev_snapshot_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(zip(["name", "price", "price_old", "portion", "category"], row)) for row in rows]



async def close_stale_shifts(today_iso: str) -> int:
    """
    Закрывает зависшие смены предыдущих дней (clock_out IS NULL, date < today).
    Используется при full load чтобы исключить их из fallback-сида.
    Возвращает количество закрытых записей.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE shifts_raw SET clock_out = clock_in WHERE date < ? AND clock_out IS NULL",
            (today_iso,),
        )
        await db.commit()
        return cursor.rowcount


async def get_today_shifts(branch_name: str, date_iso: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT employee_id, employee_name, role_class, clock_in, clock_out FROM shifts_raw WHERE branch_name = ? AND date = ? ORDER BY clock_in",
            (branch_name, date_iso),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_client_order_count(phone: str) -> int:
    """Количество заказов клиента по номеру телефона в orders_raw."""
    if not phone:
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM orders_raw WHERE client_phone = ?", (phone,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ---------------------------------------------------------------------------
# Конкуренты — функции для Sheets-экспорта
# ---------------------------------------------------------------------------

async def get_competitor_names() -> list[tuple[str, str]]:
    """Возвращает уникальные (city, competitor_name) из успешных снапшотов."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT DISTINCT city, competitor_name
               FROM competitor_snapshots
               WHERE status = 'ok'
               ORDER BY city, competitor_name"""
        ) as cursor:
            return [(row[0], row[1]) for row in await cursor.fetchall()]


async def get_all_competitor_items_by_snapshot(
    city: str, competitor_name: str
) -> list[dict]:
    """
    Все позиции конкурента по всем снапшотам.
    Возвращает [{name, price, snapshot_date}], отсортировано по дате ASC.
    Только успешные снапшоты.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT i.name, i.price, i.price_old,
                      date(s.scraped_at) AS snapshot_date,
                      i.category
               FROM competitor_menu_items i
               JOIN competitor_snapshots s ON i.snapshot_id = s.id
               WHERE s.city = ? AND s.competitor_name = ? AND s.status = 'ok'
               ORDER BY s.scraped_at ASC, i.category, i.name""",
            (city, competitor_name),
        ) as cursor:
            return [
                {
                    "name": r[0], "price": r[1], "price_old": r[2],
                    "snapshot_date": r[3], "category": r[4] or "",
                }
                for r in await cursor.fetchall()
            ]


async def get_competitor_last_snapshot(city: str, competitor_name: str) -> dict | None:
    """Последний успешный снапшот конкурента: {date, items_count}."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT date(scraped_at), items_count
               FROM competitor_snapshots
               WHERE city = ? AND competitor_name = ? AND status = 'ok'
               ORDER BY scraped_at DESC LIMIT 1""",
            (city, competitor_name),
        ) as cursor:
            row = await cursor.fetchone()
            return {"date": row[0], "items_count": row[1]} if row else None


# =============================================================================
# SaaS / Фаза 0 — мультитенантная архитектура
# =============================================================================

_ALL_MODULES = ["late_alerts", "late_queries", "search", "reports", "marketing", "finance", "admin"]


async def init_saas_tables() -> None:
    """Создаёт таблицы мультитенантной архитектуры. Вызывается из init_db()."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS tenants (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                slug        TEXT NOT NULL UNIQUE,
                plan        TEXT NOT NULL DEFAULT 'trial',
                status      TEXT NOT NULL DEFAULT 'active',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tenant_modules (
                tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
                module      TEXT NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                config_json TEXT,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (tenant_id, module)
            );

            CREATE TABLE IF NOT EXISTS tenant_users (
                tenant_id    INTEGER NOT NULL REFERENCES tenants(id),
                user_id      INTEGER NOT NULL,
                name         TEXT,
                role         TEXT NOT NULL DEFAULT 'viewer',
                modules_json TEXT,
                city         TEXT,
                is_active    INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS tenant_chats (
                tenant_id    INTEGER NOT NULL REFERENCES tenants(id),
                chat_id      INTEGER NOT NULL,
                name         TEXT,
                modules_json TEXT,
                city         TEXT,
                is_active    INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (tenant_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       INTEGER NOT NULL UNIQUE REFERENCES tenants(id),
                status          TEXT NOT NULL DEFAULT 'active',
                plan            TEXT NOT NULL DEFAULT 'owner',
                modules_json    TEXT NOT NULL DEFAULT '[]',
                branches_count  INTEGER NOT NULL DEFAULT 9,
                amount_monthly  INTEGER,
                started_at      TEXT,
                next_billing_at TEXT,
                grace_until     TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS iiko_credentials (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
                branch_name TEXT NOT NULL,
                city        TEXT,
                bo_url      TEXT NOT NULL,
                dept_id     TEXT,
                utc_offset  INTEGER NOT NULL DEFAULT 7,
                is_active   INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL,
                UNIQUE (tenant_id, branch_name)
            );
        """)
        await db.commit()

        # Идемпотентные миграции для уже существующих таблиц (добавляем колонку name)
        for migration in [
            "ALTER TABLE tenant_users ADD COLUMN name TEXT",
            "ALTER TABLE tenant_chats ADD COLUMN name TEXT",
        ]:
            try:
                await db.execute(migration)
                await db.commit()
            except Exception:
                pass  # колонка уже существует — всё ок


async def seed_default_tenant() -> None:
    """
    Создаёт дефолтный тенант id=1 (Ёбидоёби) если не существует.
    Идемпотентно — повторный вызов безопасен.
    """
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM tenants WHERE id = 1") as cursor:
            if await cursor.fetchone():
                return  # уже засеяно

        await db.execute(
            """INSERT INTO tenants (id, name, slug, plan, status, created_at, updated_at)
               VALUES (1, 'Ёбидоёби', 'ebidoebi', 'owner', 'active', ?, ?)""",
            (now, now),
        )

        for module in _ALL_MODULES:
            await db.execute(
                """INSERT OR IGNORE INTO tenant_modules (tenant_id, module, enabled, updated_at)
                   VALUES (1, ?, 1, ?)""",
                (module, now),
            )

        await db.execute(
            """INSERT OR IGNORE INTO subscriptions
               (tenant_id, status, plan, modules_json, branches_count, started_at, created_at, updated_at)
               VALUES (1, 'active', 'owner', ?, 9, ?, ?, ?)""",
            (json.dumps(_ALL_MODULES), now, now, now),
        )

        await db.commit()


# --- Геттеры ---

async def get_tenant(tenant_id: int = 1) -> dict | None:
    """Данные тенанта по ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_tenant_modules(tenant_id: int = 1) -> list[str]:
    """Список включённых модулей тенанта."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT module FROM tenant_modules WHERE tenant_id = ? AND enabled = 1 ORDER BY module",
            (tenant_id,),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_subscription(tenant_id: int = 1) -> dict | None:
    """Подписка тенанта."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM subscriptions WHERE tenant_id = ?", (tenant_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# --- Управление доступом через БД (Фаза 0.1) ---

async def get_all_tenant_users(tenant_id: int = 1) -> list[dict]:
    """Все активные пользователи тенанта."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT user_id, name, role, modules_json, city
               FROM tenant_users WHERE tenant_id = ? AND is_active = 1""",
            (tenant_id,),
        ) as cur:
            return [
                {
                    "user_id": r["user_id"],
                    "name": r["name"] or str(r["user_id"]),
                    "role": r["role"],
                    "modules": json.loads(r["modules_json"] or "[]"),
                    "city": r["city"],
                }
                for r in await cur.fetchall()
            ]


async def get_all_tenant_chats(tenant_id: int = 1) -> list[dict]:
    """Все активные чаты тенанта."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT chat_id, name, modules_json, city
               FROM tenant_chats WHERE tenant_id = ? AND is_active = 1""",
            (tenant_id,),
        ) as cur:
            return [
                {
                    "chat_id": r["chat_id"],
                    "name": r["name"] or str(r["chat_id"]),
                    "modules": json.loads(r["modules_json"] or "[]"),
                    "city": r["city"],
                }
                for r in await cur.fetchall()
            ]


async def upsert_tenant_user(
    user_id: int,
    name: str,
    modules: list[str] | None = None,
    city: str | None = None,
    role: str = "viewer",
    tenant_id: int = 1,
) -> None:
    """UPSERT пользователя тенанта."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO tenant_users
               (tenant_id, user_id, name, role, modules_json, city, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT (tenant_id, user_id) DO UPDATE SET
                 name         = excluded.name,
                 role         = excluded.role,
                 modules_json = excluded.modules_json,
                 city         = excluded.city,
                 is_active    = 1""",
            (tenant_id, user_id, name, role, json.dumps(modules or []), city, now),
        )
        await db.commit()


async def upsert_tenant_chat(
    chat_id: int,
    name: str,
    modules: list[str] | None = None,
    city: str | None = None,
    tenant_id: int = 1,
) -> None:
    """UPSERT чата тенанта."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO tenant_chats
               (tenant_id, chat_id, name, modules_json, city, is_active)
               VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT (tenant_id, chat_id) DO UPDATE SET
                 name         = excluded.name,
                 modules_json = excluded.modules_json,
                 city         = excluded.city,
                 is_active    = 1""",
            (tenant_id, chat_id, name, json.dumps(modules or []), city),
        )
        await db.commit()


async def delete_tenant_user(user_id: int, tenant_id: int = 1) -> None:
    """Деактивирует пользователя (soft delete)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tenant_users SET is_active = 0 WHERE tenant_id = ? AND user_id = ?",
            (tenant_id, user_id),
        )
        await db.commit()


async def delete_tenant_chat(chat_id: int, tenant_id: int = 1) -> None:
    """Деактивирует чат (soft delete)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tenant_chats SET is_active = 0 WHERE tenant_id = ? AND chat_id = ?",
            (tenant_id, chat_id),
        )
        await db.commit()


async def log_silence(chat_id: int, duration_min: int, user_id: int) -> None:
    """Логируем активацию режима тишины в чате."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO silence_log (chat_id, activated_at, duration_min, user_id) VALUES (?,?,?,?)",
            (chat_id, now, duration_min, user_id),
        )
        await db.commit()


async def save_audit_events_batch(events: list[dict]) -> None:
    """Вставляет список audit_events в БД. Дубликаты за дату/точку не удаляет — вызывай clear_audit_events перед повторным запуском."""
    if not events:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO audit_events
               (date, branch_name, city, event_type, severity, description, meta_json, created_at)
               VALUES (:date, :branch_name, :city, :event_type, :severity, :description, :meta_json, :created_at)""",
            events,
        )
        await db.commit()


async def clear_audit_events(date: str, branch_name: str | None = None) -> None:
    """Удаляет audit_events за дату (и точку, если задана) — для re-run аудита."""
    async with aiosqlite.connect(DB_PATH) as db:
        if branch_name:
            await db.execute(
                "DELETE FROM audit_events WHERE date = ? AND branch_name = ?",
                (date, branch_name),
            )
        else:
            await db.execute("DELETE FROM audit_events WHERE date = ?", (date,))
        await db.commit()


async def get_audit_events(
    date: str,
    city: str | None = None,
    branch_name: str | None = None,
) -> list[dict]:
    """Возвращает audit_events за дату, опционально фильтруя по городу или точке."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM audit_events WHERE date = ?"
        params: list = [date]
        if branch_name:
            query += " AND branch_name = ?"
            params.append(branch_name)
        elif city:
            query += " AND city = ?"
            params.append(city)
        query += " ORDER BY severity DESC, event_type, branch_name"
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_module_chats_for_city(module: str, city: str, tenant_id: int = 1) -> list[int]:
    """
    Возвращает список chat_id, у которых включён указанный модуль
    и город совпадает с city (или у чата нет ограничения по городу).
    """
    import json as _json
    chats = await get_all_tenant_chats(tenant_id)
    result: list[int] = []
    for chat in chats:
        if module not in chat.get("modules", []):
            continue
        city_raw: str | None = chat.get("city")
        if city_raw is None:
            result.append(chat["chat_id"])
            continue
        try:
            cities = frozenset(_json.loads(city_raw))
        except (ValueError, TypeError):
            cities = frozenset({city_raw}) if city_raw else frozenset()
        if city in cities:
            result.append(chat["chat_id"])
    return result


async def get_alert_chats_for_city(city: str, tenant_id: int = 1) -> list[int]:
    """
    Возвращает список chat_id, у которых включён модуль late_alerts
    и город совпадает с city (или у чата нет ограничения по городу).
    """
    import json as _json
    chats = await get_all_tenant_chats(tenant_id)
    result: list[int] = []
    for chat in chats:
        if "late_alerts" not in chat.get("modules", []):
            continue
        city_raw: str | None = chat.get("city")
        if city_raw is None:
            result.append(chat["chat_id"])
            continue
        try:
            cities = frozenset(_json.loads(city_raw))
        except (ValueError, TypeError):
            cities = frozenset({city_raw}) if city_raw else frozenset()
        if city in cities:
            result.append(chat["chat_id"])
    return result


async def get_access_config_from_db(tenant_id: int = 1) -> dict:
    """
    Возвращает конфиг доступа из БД в формате access_config.json.
    {"chats": {str(chat_id): {...}}, "users": {str(user_id): {...}}}
    """
    users = await get_all_tenant_users(tenant_id)
    chats = await get_all_tenant_chats(tenant_id)
    return {
        "chats": {
            str(c["chat_id"]): {
                "name": c["name"],
                "modules": c["modules"],
                "city": c["city"],
            }
            for c in chats
        },
        "users": {
            str(u["user_id"]): {
                "name": u["name"],
                "modules": u["modules"],
                "city": u["city"],
            }
            for u in users
        },
    }

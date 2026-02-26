-- 001_initial.sql — Полная схема PostgreSQL для Аркентия (мультитенант)
-- Все операционные таблицы содержат tenant_id DEFAULT 1.

-- =====================================================================
-- SaaS-ядро (без изменений, PG-типы)
-- =====================================================================

CREATE TABLE IF NOT EXISTS tenants (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    bot_token   TEXT,
    plan        TEXT NOT NULL DEFAULT 'trial',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_modules (
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    module      TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    config_json JSONB,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, module)
);

CREATE TABLE IF NOT EXISTS tenant_users (
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL,
    name         TEXT,
    role         TEXT NOT NULL DEFAULT 'viewer',
    modules_json JSONB DEFAULT '[]',
    city         TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS tenant_chats (
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    chat_id      BIGINT NOT NULL,
    name         TEXT,
    modules_json JSONB DEFAULT '[]',
    city         TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, chat_id)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'active',
    plan            TEXT NOT NULL DEFAULT 'owner',
    modules_json    JSONB NOT NULL DEFAULT '[]',
    branches_count  INTEGER NOT NULL DEFAULT 9,
    amount_monthly  INTEGER,
    started_at      TIMESTAMPTZ,
    next_billing_at TIMESTAMPTZ,
    grace_until     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS iiko_credentials (
    id          SERIAL PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    branch_name TEXT NOT NULL,
    city        TEXT,
    bo_url      TEXT NOT NULL,
    bo_login    TEXT,
    bo_password TEXT,
    dept_id     TEXT,
    utc_offset  INTEGER NOT NULL DEFAULT 7,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, branch_name)
);

-- =====================================================================
-- Операционные таблицы (все с tenant_id)
-- =====================================================================

CREATE TABLE IF NOT EXISTS iiko_tokens (
    tenant_id   INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    city        TEXT NOT NULL,
    token       TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, city)
);

CREATE TABLE IF NOT EXISTS job_logs (
    id          SERIAL PRIMARY KEY,
    tenant_id   INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    job_name    TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',
    error       TEXT,
    details     TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_logs_tenant ON job_logs(tenant_id);

CREATE TABLE IF NOT EXISTS stoplist_state (
    tenant_id   INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    city        TEXT NOT NULL,
    items_hash  TEXT NOT NULL,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, city)
);

CREATE TABLE IF NOT EXISTS report_updates (
    id          SERIAL PRIMARY KEY,
    tenant_id   INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    date        DATE NOT NULL,
    branch      TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_report_updates_tenant_date ON report_updates(tenant_id, date);

CREATE TABLE IF NOT EXISTS daily_rt_snapshot (
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    branch          TEXT NOT NULL,
    date            DATE NOT NULL,
    delays_late     INTEGER DEFAULT 0,
    delays_total    INTEGER DEFAULT 0,
    delays_avg_min  INTEGER DEFAULT 0,
    cooks_today     INTEGER DEFAULT 0,
    couriers_today  INTEGER DEFAULT 0,
    saved_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, branch, date)
);

CREATE TABLE IF NOT EXISTS silence_log (
    id           SERIAL PRIMARY KEY,
    tenant_id    INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    chat_id      BIGINT NOT NULL,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_min INTEGER NOT NULL,
    user_id      BIGINT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id           SERIAL PRIMARY KEY,
    tenant_id    INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    date         DATE NOT NULL,
    branch_name  TEXT NOT NULL,
    city         TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'warning',
    description  TEXT NOT NULL,
    meta_json    JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_date ON audit_events(tenant_id, date);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_branch_date ON audit_events(tenant_id, branch_name, date);

-- =====================================================================
-- Аналитика (orders, shifts, daily_stats)
-- =====================================================================

CREATE TABLE IF NOT EXISTS orders_raw (
    tenant_id        INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    branch_name      TEXT NOT NULL,
    delivery_num     TEXT NOT NULL,
    status           TEXT,
    courier          TEXT,
    sum              DOUBLE PRECISION,
    planned_time     TEXT,
    actual_time      TEXT,
    is_self_service  BOOLEAN DEFAULT false,
    date             DATE,
    is_late          BOOLEAN DEFAULT false,
    late_minutes     DOUBLE PRECISION DEFAULT 0,
    client_name      TEXT,
    client_phone     TEXT,
    delivery_address TEXT,
    items            TEXT,
    ready_time       TEXT,
    comment          TEXT,
    operator         TEXT,
    opened_at        TEXT,
    payment_type     TEXT,
    source           TEXT,
    cancel_reason    TEXT,
    send_time        TEXT,
    service_print_time TEXT,
    cooking_to_send_duration INTEGER,
    pay_breakdown    TEXT,
    cooked_time      TEXT,
    discount_type    TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, branch_name, delivery_num)
);
CREATE INDEX IF NOT EXISTS idx_orders_tenant_date ON orders_raw(tenant_id, date);
CREATE INDEX IF NOT EXISTS idx_orders_tenant_branch_date ON orders_raw(tenant_id, branch_name, date);

CREATE TABLE IF NOT EXISTS shifts_raw (
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    branch_name     TEXT NOT NULL,
    employee_id     TEXT NOT NULL,
    employee_name   TEXT,
    role_class      TEXT,
    date            DATE,
    clock_in        TEXT,
    clock_out       TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, branch_name, employee_id, clock_in)
);
CREATE INDEX IF NOT EXISTS idx_shifts_tenant_branch_date ON shifts_raw(tenant_id, branch_name, date);

CREATE TABLE IF NOT EXISTS daily_stats (
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    branch_name     TEXT NOT NULL,
    date            DATE NOT NULL,
    orders_count    INTEGER DEFAULT 0,
    revenue         DOUBLE PRECISION DEFAULT 0,
    avg_check       DOUBLE PRECISION DEFAULT 0,
    cogs_pct        DOUBLE PRECISION,
    sailplay        DOUBLE PRECISION,
    discount_sum    DOUBLE PRECISION,
    discount_types  TEXT,
    delivery_count  INTEGER DEFAULT 0,
    pickup_count    INTEGER DEFAULT 0,
    late_count      INTEGER DEFAULT 0,
    total_delivered INTEGER DEFAULT 0,
    late_percent    DOUBLE PRECISION DEFAULT 0,
    avg_late_min    DOUBLE PRECISION DEFAULT 0,
    cooks_count          INTEGER DEFAULT 0,
    couriers_count       INTEGER DEFAULT 0,
    late_delivery_count  INTEGER DEFAULT 0,
    late_pickup_count    INTEGER DEFAULT 0,
    avg_cooking_min      DOUBLE PRECISION,
    avg_wait_min         DOUBLE PRECISION,
    avg_delivery_min     DOUBLE PRECISION,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, branch_name, date)
);

-- =====================================================================
-- Конкуренты
-- =====================================================================

CREATE TABLE IF NOT EXISTS competitor_snapshots (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    city            TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    url             TEXT NOT NULL,
    scraped_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'ok',
    items_count     INTEGER DEFAULT 0,
    error_msg       TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_tenant_competitor
    ON competitor_snapshots(tenant_id, city, competitor_name, scraped_at);

CREATE TABLE IF NOT EXISTS competitor_menu_items (
    id              SERIAL PRIMARY KEY,
    snapshot_id     INTEGER NOT NULL REFERENCES competitor_snapshots(id) ON DELETE CASCADE,
    tenant_id       INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE,
    city            TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    category        TEXT,
    name            TEXT NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    price_old       DOUBLE PRECISION,
    portion         TEXT,
    scraped_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_items_tenant_competitor_date
    ON competitor_menu_items(tenant_id, competitor_name, scraped_at);
CREATE INDEX IF NOT EXISTS idx_items_tenant_name
    ON competitor_menu_items(tenant_id, name);

-- =====================================================================
-- Дефолтный тенант
-- =====================================================================

INSERT INTO tenants (id, name, slug, plan, status)
VALUES (1, 'Ёбидоёби', 'ebidoebi', 'owner', 'active')
ON CONFLICT (id) DO NOTHING;

SELECT setval('tenants_id_seq', (SELECT MAX(id) FROM tenants));

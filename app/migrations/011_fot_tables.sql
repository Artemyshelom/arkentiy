-- ФОТ (фонд оплаты труда) по категориям персонала на точке
-- Источник данных: shifts_raw (Events API) + /api/v2/employees/salary

CREATE TABLE IF NOT EXISTS fot_daily (
    id                SERIAL PRIMARY KEY,
    tenant_id         INTEGER NOT NULL DEFAULT 1,
    branch_name       TEXT NOT NULL,
    date              DATE NOT NULL,
    category          TEXT NOT NULL,   -- cook / courier / admin / other
    fot_sum           NUMERIC(12,2) NOT NULL DEFAULT 0,
    hours_sum         NUMERIC(8,2) NOT NULL DEFAULT 0,
    employees_count   INTEGER NOT NULL DEFAULT 0,
    employees_no_rate INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMP DEFAULT NOW(),
    updated_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE (tenant_id, branch_name, date, category)
);

CREATE INDEX IF NOT EXISTS idx_fot_daily_date   ON fot_daily (tenant_id, date);
CREATE INDEX IF NOT EXISTS idx_fot_daily_branch ON fot_daily (tenant_id, branch_name, date);

-- Таблица дефолтных ставок (fallback при отсутствии ставки в iiko).
-- В v1 не используется логикой — заполняется вручную при необходимости.
CREATE TABLE IF NOT EXISTS fot_default_rates (
    tenant_id     INTEGER NOT NULL DEFAULT 1,
    category      TEXT NOT NULL,   -- cook / courier / admin / other
    rate_per_hour NUMERIC(10,2) NOT NULL,
    updated_at    TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (tenant_id, category)
);

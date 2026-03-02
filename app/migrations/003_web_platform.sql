-- 003_web_platform.sql — Таблицы для веб-платформы: онбординг, оплата, кабинет
-- Зависит от: 001_initial.sql, 002_payment_changed.sql

-- =====================================================================
-- Расширение tenants: поля для веб-регистрации
-- =====================================================================

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS email        TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS contact      TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS phone        TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS inn          TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS legal_name   TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_email ON tenants(email) WHERE email IS NOT NULL;

-- =====================================================================
-- Расширение subscriptions: period и способ оплаты
-- =====================================================================

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS period              TEXT NOT NULL DEFAULT 'monthly';
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS connection_fee_paid BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS yukassa_payment_method_id TEXT;

-- =====================================================================
-- Платежи (ЮKassa и ручные)
-- =====================================================================

CREATE TABLE IF NOT EXISTS payments (
    id              TEXT PRIMARY KEY,                -- UUID, наш внутренний ID
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    yukassa_id      TEXT,                            -- ID платежа в ЮKassa
    amount          INTEGER NOT NULL,                -- сумма в рублях
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending, succeeded, canceled, refunded
    payment_method  TEXT,                            -- card / invoice
    card_last4      TEXT,
    card_brand      TEXT,
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_payments_tenant ON payments(tenant_id);
CREATE INDEX IF NOT EXISTS idx_payments_yukassa ON payments(yukassa_id) WHERE yukassa_id IS NOT NULL;

-- =====================================================================
-- Счета для юрлиц
-- =====================================================================

CREATE TABLE IF NOT EXISTS invoices (
    id              TEXT PRIMARY KEY,                -- UUID
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    invoice_number  TEXT NOT NULL UNIQUE,            -- АРК-2026-001
    amount          INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending, pending_verification, paid, cancelled
    inn             TEXT,
    legal_name      TEXT,
    items_json      JSONB NOT NULL DEFAULT '[]',     -- [{name, amount}]
    pdf_path        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_invoices_tenant ON invoices(tenant_id);

-- =====================================================================
-- Промокоды
-- =====================================================================

CREATE TABLE IF NOT EXISTS promo_codes (
    id              SERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    bonuses_json    JSONB NOT NULL DEFAULT '[]',     -- [{type, amount?, months?, description}]
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ,
    usage_limit     INTEGER,                         -- NULL = безлимит
    used_count      INTEGER NOT NULL DEFAULT 0,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS promo_usage (
    id              SERIAL PRIMARY KEY,
    promo_id        INTEGER NOT NULL REFERENCES promo_codes(id),
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (promo_id, tenant_id)
);

-- =====================================================================
-- Лента событий кабинета
-- =====================================================================

CREATE TABLE IF NOT EXISTS tenant_events (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,                   -- payment, report, connection, subscription, system
    text            TEXT NOT NULL,
    icon            TEXT NOT NULL DEFAULT 'info',     -- success, warning, error, info
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tenant_events_tenant_date ON tenant_events(tenant_id, created_at DESC);

-- =====================================================================
-- Seed: стартовые промокоды
-- =====================================================================

INSERT INTO promo_codes (code, bonuses_json) VALUES
    ('EARLY', '[{"type": "free_connection", "description": "Бесплатное подключение"}, {"type": "fixed_discount", "amount": 2000, "months": 3, "description": "Скидка 2 000 ₽/мес на 3 мес"}]'),
    ('FRIEND', '[{"type": "free_connection", "description": "Бесплатное подключение"}]')
ON CONFLICT (code) DO NOTHING;

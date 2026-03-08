-- 008: Управление подписками — отмена, смена плана, история

-- Поля отмены подписки
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS cancel_scheduled BOOLEAN DEFAULT false;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS cancel_at TIMESTAMPTZ;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS cancel_reason TEXT;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS cancel_feedback TEXT;

-- Отложенная смена плана (downgrade)
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS pending_plan TEXT;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS pending_plan_from TIMESTAMPTZ;

-- Платёжный метод для смены карты (last4 для отображения)
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS payment_method_id TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS payment_method_last4 TEXT;

-- История изменений подписки
CREATE TABLE IF NOT EXISTS subscription_changes (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    from_plan TEXT,
    to_plan TEXT,
    from_status TEXT,
    to_status TEXT,
    action TEXT NOT NULL,  -- upgrade/downgrade/cancel/reactivate/created
    prorata_amount DECIMAL(10,2),
    payment_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_subscription_changes_tenant ON subscription_changes(tenant_id);

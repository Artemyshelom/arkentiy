-- Кеш почасовых ставок сотрудников для real-time расчёта ФОТ в /статус.
-- Обновляется ежедневно в 03:30 МСК джобом rates_cache_updater.
-- Источник: GET {bo_url}/api/v2/employees/salary (iiko BO).

CREATE TABLE IF NOT EXISTS employee_rates_cache (
    tenant_id     INTEGER NOT NULL DEFAULT 1,
    branch_name   TEXT    NOT NULL,
    employee_id   TEXT    NOT NULL,
    employee_name TEXT    NOT NULL DEFAULT '',
    rate_per_hour NUMERIC(10,2) NOT NULL DEFAULT 0,
    cached_at     TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (tenant_id, branch_name, employee_id)
);

CREATE INDEX IF NOT EXISTS idx_rates_cache_branch
    ON employee_rates_cache (tenant_id, branch_name);

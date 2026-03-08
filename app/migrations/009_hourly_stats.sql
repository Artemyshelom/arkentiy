-- 009: Почасовая аналитика для AI-агента Бориса

CREATE TABLE IF NOT EXISTS hourly_stats (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL,
    branch_name         TEXT NOT NULL,
    hour                TIMESTAMPTZ NOT NULL,       -- начало часа: 2026-03-08 14:00:00+00

    -- Заказы (все кроме Отменена)
    orders_count        INTEGER NOT NULL DEFAULT 0, -- заказов завершено за час
    revenue             DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_check           DOUBLE PRECISION NOT NULL DEFAULT 0,

    -- Тайминги (только при наличии данных; NULL = OLAP ещё не пришёл)
    avg_cook_time       DOUBLE PRECISION,           -- cooked_time - opened_at, мин (1-120)
    avg_courier_wait    DOUBLE PRECISION,           -- ready_time - cooked_time, мин (0-120)
    avg_delivery_time   DOUBLE PRECISION,           -- actual_time - opened_at, мин (1-120)

    -- Опоздания
    late_count          INTEGER NOT NULL DEFAULT 0,
    late_percent        DOUBLE PRECISION NOT NULL DEFAULT 0,

    -- Персонал на смене В ЭТОТ ЧАС (пересечение смены с [hour, hour+1))
    cooks_on_shift      INTEGER NOT NULL DEFAULT 0,
    couriers_on_shift   INTEGER NOT NULL DEFAULT 0,

    -- Очередь — заказы в работе на начало часа (opened_at < hour, actual_time >= hour или NULL)
    orders_in_progress  INTEGER NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (tenant_id, branch_name, hour)
);

CREATE INDEX IF NOT EXISTS hourly_stats_lookup
    ON hourly_stats (tenant_id, branch_name, hour);

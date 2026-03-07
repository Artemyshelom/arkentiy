-- Миграция 006: статистика новых/повторных клиентов в daily_stats
-- Применять: psql $DATABASE_URL -f app/migrations/006_customer_stats.sql

ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS new_customers        INTEGER          DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS new_customers_revenue DOUBLE PRECISION DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS repeat_customers     INTEGER          DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS repeat_customers_revenue DOUBLE PRECISION DEFAULT 0;

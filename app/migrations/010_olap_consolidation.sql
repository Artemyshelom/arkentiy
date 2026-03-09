-- Консолидация OLAP-пайплайна
-- Добавляет поля, необходимые для единого ежедневного пайплайна (olap_pipeline.py).

-- per-order сумма скидки (из DELIVERIES reportType)
ALTER TABLE orders_raw ADD COLUMN IF NOT EXISTS discount_sum DOUBLE PRECISION;

-- кол-во заказов, доставленных точно в срок (вычисляется из orders_raw)
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS exact_time_count INTEGER DEFAULT 0;

-- наличная/безналичная выручка (для Google Sheets); заменяет подзапрос в iiko_to_sheets
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS cash    DOUBLE PRECISION DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS noncash DOUBLE PRECISION DEFAULT 0;

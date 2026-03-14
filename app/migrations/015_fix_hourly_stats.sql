-- 015: Исправление hourly_stats
-- 1. Смена типа hour с TIMESTAMPTZ → TIMESTAMP (данные хранились в местном
--    времени с ложной пометкой +00; AT TIME ZONE 'UTC' убирает пометку,
--    числа не меняются — 13 715 строк, почти мгновенно).
-- 2. Добавление колонки completed_count — заказов с actual_time IS NOT NULL
--    (доставлено). Нужно чтобы Борис видел: принято / доставлено / опоздало.

ALTER TABLE hourly_stats
    ALTER COLUMN hour TYPE TIMESTAMP USING hour AT TIME ZONE 'UTC';

ALTER TABLE hourly_stats
    ADD COLUMN IF NOT EXISTS completed_count INTEGER NOT NULL DEFAULT 0;

-- Обновить комментарий в описании: orders_count теперь = заказов принято
-- (grouped by opened_at), completed_count = из них доставлено.
COMMENT ON COLUMN hourly_stats.orders_count    IS 'заказов принято за час (grouped by opened_at)';
COMMENT ON COLUMN hourly_stats.completed_count IS 'из них доставлено (actual_time IS NOT NULL)';

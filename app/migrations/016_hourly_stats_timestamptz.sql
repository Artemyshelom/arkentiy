-- 016: hourly_stats.hour → TIMESTAMPTZ (aware UTC)
--
-- Текущее состояние: hour TIMESTAMP (naive) хранит LOCAL время (UTC+7).
-- Пример:  hour = 2026-03-08 15:00:00  ←→  заказы local 15:00–16:00
--
-- AT TIME ZONE 'Asia/Krasnoyarsk' на TIMESTAMP интерпретирует значение как KSK-время
-- и конвертирует в UTC:
--   15:00:00 (KSK) → 08:00:00+00  (UTC)
--
-- PRECHECK (запустить ДО ALTER):
-- Убедиться что конверсия 1:1 — дублей быть не должно (фиксированный offset):
--   SELECT tenant_id, branch_name, hour AT TIME ZONE 'Asia/Krasnoyarsk', COUNT(*)
--   FROM hourly_stats GROUP BY 1, 2, 3 HAVING COUNT(*) > 1;
--
-- ОТКАТ (если что-то пошло не так):
--   ALTER TABLE hourly_stats
--       ALTER COLUMN hour TYPE TIMESTAMP
--       USING hour AT TIME ZONE 'Asia/Krasnoyarsk';

ALTER TABLE hourly_stats
    ALTER COLUMN hour TYPE TIMESTAMPTZ
    USING hour AT TIME ZONE 'Asia/Krasnoyarsk';

COMMENT ON COLUMN hourly_stats.hour IS 'начало часа в UTC (TIMESTAMPTZ). AT TIME ZONE branch_tz → местное время.';

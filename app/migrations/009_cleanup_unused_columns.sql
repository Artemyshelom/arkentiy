-- Удаляем неиспользуемые колонки из orders_raw
-- ready_time             = Delivery.BillTime ≈ cooked_time, не используется в расчётах
-- bonus_accrued          = Events API не присылает, всегда NULL
-- return_sum             = Events API не присылает, всегда NULL
-- service_charge         = Events API не присылает, всегда NULL
-- cooking_duration       = pre-computed INTERVAL из onboarding, в продакшне не читается
-- idle_time              = pre-computed INTERVAL из onboarding, в продакшне не читается
-- delivery_duration      = pre-computed INTERVAL из onboarding, в продакшне не читается
-- total_duration         = pre-computed INTERVAL из onboarding, в продакшне не читается
-- cooking_to_send_duration = нигде не используется

ALTER TABLE orders_raw
    DROP COLUMN IF EXISTS ready_time,
    DROP COLUMN IF EXISTS bonus_accrued,
    DROP COLUMN IF EXISTS return_sum,
    DROP COLUMN IF EXISTS service_charge,
    DROP COLUMN IF EXISTS cooking_duration,
    DROP COLUMN IF EXISTS idle_time,
    DROP COLUMN IF EXISTS delivery_duration,
    DROP COLUMN IF EXISTS total_duration,
    DROP COLUMN IF EXISTS cooking_to_send_duration;

-- ====================================================================
-- 005_refactor_comments.sql
-- Рефакторинг комментариев в orders_raw
-- - Переименование cancel_comment → cancellation_details
-- - Удаление problem_comment (данные в comment)
-- ====================================================================

-- 1. Добавляем новую колонку cancellation_details
ALTER TABLE orders_raw
ADD COLUMN cancellation_details TEXT;

-- 2. Переносим данные из cancel_comment в cancellation_details
UPDATE orders_raw
SET cancellation_details = cancel_comment
WHERE cancel_comment IS NOT NULL;

-- 3. Удаляем старую колонку cancel_comment
ALTER TABLE orders_raw
DROP COLUMN cancel_comment;

-- 4. Удаляем колонку problem_comment (данные в comment + has_problem)
ALTER TABLE orders_raw
DROP COLUMN IF EXISTS problem_comment;

-- Готово!
-- Новая структура:
-- - cancellation_details (TEXT) — комментарий оператора при отмене (заменяет cancel_comment)
-- - comment (TEXT) — общий комментарий к заказу (включает проблемы)
-- - has_problem (BOOLEAN) — флаг наличия проблемы

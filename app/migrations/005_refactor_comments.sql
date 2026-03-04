-- ====================================================================
-- 005_refactor_comments.sql
-- Рефакторинг комментариев в orders_raw
-- - Переименование cancel_comment → cancellation_details
-- - Удаление problem_comment (данные в comment)
-- ====================================================================

-- 1. Добавляем новую колонку cancellation_details (если не существует)
DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM information_schema.columns 
                  WHERE table_name='orders_raw' AND column_name='cancellation_details') THEN
        ALTER TABLE orders_raw ADD COLUMN cancellation_details TEXT;
    END IF;
END $$;

-- 2. Переносим данные из cancel_comment в cancellation_details (если cancel_comment ещё существует)
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM information_schema.columns 
              WHERE table_name='orders_raw' AND column_name='cancel_comment') THEN
        UPDATE orders_raw 
        SET cancellation_details = cancel_comment 
        WHERE cancel_comment IS NOT NULL AND cancellation_details IS NULL;
    END IF;
END $$;

-- 3. Удаляем старую колонку cancel_comment (если существует)
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM information_schema.columns 
              WHERE table_name='orders_raw' AND column_name='cancel_comment') THEN
        ALTER TABLE orders_raw DROP COLUMN cancel_comment;
    END IF;
END $$;

-- 4. Удаляем колонку problem_comment (если существует)
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM information_schema.columns 
              WHERE table_name='orders_raw' AND column_name='problem_comment') THEN
        ALTER TABLE orders_raw DROP COLUMN problem_comment;
    END IF;
END $$;

-- Готово!
-- Новая структура:
-- - cancellation_details (TEXT) — комментарий оператора при отмене (заменяет cancel_comment)
-- - comment (TEXT) — общий комментарий к заказу (включает проблемы)
-- - has_problem (BOOLEAN) — флаг наличия проблемы

-- Миграция: добавление поля payment_changed для отслеживания смен оплаты
-- Дата: 2026-02-28

-- Добавляем поле в orders_raw
ALTER TABLE orders_raw ADD COLUMN IF NOT EXISTS payment_changed BOOLEAN DEFAULT false;

-- Индекс для быстрой выборки
CREATE INDEX IF NOT EXISTS idx_orders_payment_changed ON orders_raw(payment_changed) WHERE payment_changed = true;

-- Обновляем существующие записи по признаку в комментарии
UPDATE orders_raw SET payment_changed = true WHERE LOWER(comment) LIKE '%смен%' AND payment_changed = false;


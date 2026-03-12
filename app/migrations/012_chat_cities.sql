-- Миграция: добавить cities_json в tenant_chats
-- Мотивация: чаты могут покрывать несколько городов (финансы, аудит, маркетинг)
-- или только один. Старое поле city остаётся для обратной совместимости.

ALTER TABLE tenant_chats
    ADD COLUMN IF NOT EXISTS cities_json jsonb DEFAULT '[]'::jsonb;

-- Бэкфилл: переносим старое скалярное значение city в массив
UPDATE tenant_chats
SET cities_json = jsonb_build_array(city)
WHERE city IS NOT NULL AND city != '' AND cities_json = '[]'::jsonb;

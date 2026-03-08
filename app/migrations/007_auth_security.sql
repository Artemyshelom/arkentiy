-- 007_auth_security.sql — Email-верификация, восстановление пароля, token versioning
-- Зависит от: 003_web_platform.sql

-- =====================================================================
-- Email верификация
-- =====================================================================
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS email_verified       BOOLEAN     NOT NULL DEFAULT false;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS email_token          TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS email_token_expires  TIMESTAMPTZ;

-- =====================================================================
-- Восстановление пароля
-- =====================================================================
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS reset_token         TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS reset_token_expires TIMESTAMPTZ;

-- =====================================================================
-- Token versioning — инвалидация JWT при смене пароля
-- =====================================================================
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 1;

-- Отмечаем существующих тенантов как email_verified (они уже в системе)
UPDATE tenants SET email_verified = true WHERE email_verified = false AND password_hash IS NOT NULL;

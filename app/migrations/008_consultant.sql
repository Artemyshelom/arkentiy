-- Миграция 008: Станислав — консультант-агент
-- Таблица для регистрации чатов под онбординг

CREATE TABLE IF NOT EXISTS consultant_chats (
    id          SERIAL PRIMARY KEY,
    chat_id     BIGINT UNIQUE NOT NULL,
    tenant_id   VARCHAR(100)  NOT NULL,
    note        TEXT,
    activated_at TIMESTAMPTZ  DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_consultant_chats_tenant ON consultant_chats (tenant_id);

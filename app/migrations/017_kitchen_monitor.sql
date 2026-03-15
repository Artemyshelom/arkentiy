-- 017_kitchen_monitor.sql
-- Модуль «Директор по производству»

-- Дедупликация алертов ухода поваров (persistent, выживает после рестарта).
-- employee_id + clock_out — уникальный ключ события.
CREATE TABLE IF NOT EXISTS kitchen_alerts_sent (
    tenant_id    INT  NOT NULL,
    branch_name  TEXT NOT NULL,
    employee_id  TEXT NOT NULL,
    clock_out    TEXT NOT NULL,
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, branch_name, employee_id, clock_out)
);

-- Чат директора по производству (tenant_id=1, city=NULL = все города)
INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
VALUES (1, -5205259142, 'Директор по производству', '["kitchen_monitor"]'::jsonb, NULL, true)
ON CONFLICT (tenant_id, chat_id) DO UPDATE
    SET modules_json = EXCLUDED.modules_json,
        name         = EXCLUDED.name,
        is_active    = true;

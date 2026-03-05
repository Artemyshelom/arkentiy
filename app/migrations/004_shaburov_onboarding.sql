-- ====================================================================
-- 004_shaburov_onboarding.sql
-- Онбординг клиента Никита Шабуров (tenant_id=3)
-- Города: Канск, Зеленогорск, Ижевск
-- Идемпотентно: все INSERT ... ON CONFLICT DO NOTHING/UPDATE
-- ====================================================================

-- 1. Tenant (unique = slug)
INSERT INTO tenants (name, slug, email, contact, password_hash, plan, status, created_at, updated_at)
VALUES (
    'Шабуров',
    'shaburov',
    'shaburovn1991@gmail.com',
    'Никита Шабуров',
    '$2b$12$K4A6Bzw1JxCwNJIuGzhxru1.mWoh8Jmct6nt/4Sxai2W4X4bfduZO',
    'base',
    'active',
    now(), now()
)
ON CONFLICT (slug) DO UPDATE SET status = 'active', updated_at = now();

-- 2. Subscription
INSERT INTO subscriptions (tenant_id, status, plan, modules_json, branches_count, amount_monthly, started_at, created_at, updated_at)
SELECT id, 'active', 'base',
    '["audit","search","reports","late_alerts","late_queries","iiko_to_sheets"]'::jsonb,
    3, 15000, now(), now(), now()
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id) DO NOTHING;

-- 3. iiko credentials (3 точки)
INSERT INTO iiko_credentials (tenant_id, branch_name, city, bo_url, bo_login, bo_password, dept_id, utc_offset, is_active, created_at)
SELECT id, 'Канск_1 Сов', 'Канск',
    'https://yobidoyobi-kansk.iiko.it/resto',
    'lazarevich', '19121984',
    '02c44079-96ab-49be-8164-a13fc172f20d', 7, true, now()
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, branch_name) DO NOTHING;

INSERT INTO iiko_credentials (tenant_id, branch_name, city, bo_url, bo_login, bo_password, dept_id, utc_offset, is_active, created_at)
SELECT id, 'Зеленогорск_1 Изы', 'Зеленогорск',
    'https://ebidoebi-zelenogorsk-shaburov.iiko.it/resto',
    'lazarevich', '19121984',
    '91e53a53-abf4-4ee7-ae68-58648c681fad', 7, true, now()
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, branch_name) DO NOTHING;

INSERT INTO iiko_credentials (tenant_id, branch_name, city, bo_url, bo_login, bo_password, dept_id, utc_offset, is_active, created_at)
SELECT id, 'Ижевск_1 Авт', 'Ижевск',
    'https://yobidoyobi-izhevsk.iiko.it/resto',
    'lazarevich', '19121984',
    '5093557c-7089-42c7-9405-98ac641521eb', 4, true, now()
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, branch_name) DO NOTHING;

-- 4. Telegram chats
INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
SELECT id, -5128713915, 'Отчёты', '["reports","late_queries"]'::jsonb, NULL, true
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, chat_id) DO UPDATE SET is_active = true, modules_json = '["reports","late_queries"]'::jsonb;

INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
SELECT id, -5114358382, 'Аудит', '["audit"]'::jsonb, NULL, true
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, chat_id) DO UPDATE SET is_active = true, modules_json = '["audit"]'::jsonb;

INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
SELECT id, -5169819257, 'Поиск заказов', '["search"]'::jsonb, NULL, true
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, chat_id) DO UPDATE SET is_active = true, modules_json = '["search"]'::jsonb;

INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
SELECT id, -4860116340, 'Опоздания Ижевск', '["late_alerts","late_queries"]'::jsonb, '["Ижевск"]', true
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, chat_id) DO UPDATE SET is_active = true, city = '["Ижевск"]', modules_json = '["late_alerts","late_queries"]'::jsonb;

INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
SELECT id, -5168619845, 'Опоздания Зеленогорск', '["late_alerts","late_queries"]'::jsonb, '["Зеленогорск"]', true
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, chat_id) DO UPDATE SET is_active = true, city = '["Зеленогорск"]', modules_json = '["late_alerts","late_queries"]'::jsonb;

INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
SELECT id, -5179980907, 'Опоздания Канск', '["late_alerts","late_queries"]'::jsonb, '["Канск"]', true
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, chat_id) DO UPDATE SET is_active = true, city = '["Канск"]', modules_json = '["late_alerts","late_queries"]'::jsonb;

INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
SELECT id, -5117954628, 'Маркетинг', '[]'::jsonb, NULL, false
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, chat_id) DO NOTHING;

INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, city, is_active)
SELECT id, -5268363335, 'Финансы', '[]'::jsonb, NULL, false
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, chat_id) DO NOTHING;

-- 5. Tenant user — Никита, admin
INSERT INTO tenant_users (tenant_id, user_id, name, role, is_active)
SELECT id, 400872656, 'Никита Шабуров', 'admin', true
FROM tenants WHERE slug = 'shaburov'
ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = 'admin', is_active = true;

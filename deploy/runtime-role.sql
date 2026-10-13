-- Run as a trusted database administrator after migrations, not as the API login.
-- This group has NOLOGIN; create a separate managed login and grant it membership.
-- Never place its password here. Migrations must use a different owner/DDL account.
CREATE ROLE vpnstore_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT USAGE ON SCHEMA public TO vpnstore_runtime;
GRANT SELECT ON alembic_version TO vpnstore_runtime;
GRANT SELECT, INSERT ON purchase_quotes TO vpnstore_runtime;
GRANT SELECT, INSERT ON audit_logs TO vpnstore_runtime;
GRANT SELECT, UPDATE ON application_settings TO vpnstore_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON users, admin_users, roles, permissions,
    role_permissions, admin_role_assignments, user_status_history,
    admin_sessions, admin_tokens, product_categories, products, product_plans TO vpnstore_runtime;
-- No ownership, DDL, schema CREATE, audit UPDATE/DELETE/TRUNCATE grants.
-- Also revoke CREATE ON SCHEMA public FROM PUBLIC if legacy cluster permissions allow it.

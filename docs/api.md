# HTTP API

Health: GET /health/live; GET /health/ready checks PostgreSQL, expected Alembic revision and Redis.
Authentication: POST /auth/login, POST /auth/refresh, GET /auth/me, POST /auth/logout, POST /auth/password.
Protected read APIs: GET /admin/users, /admin/products, /admin/admins, /admin/roles, /admin/audit, /admin/settings/login.
Protected mutations: PATCH /admin/users/{id}/status, POST /admin/admins, PUT /admin/admins/{id}/roles, POST /admin/roles, PUT /admin/roles/{id}/permissions, POST /admin/sessions/{id}/revoke, PUT /admin/settings/login.

Bearer access tokens are required for protected operations; live permission checks are enforced by SecurityService, independent of UI. Sensitive mutations require current_password and an approved reason. Read limits are bounded to 100; audit responses contain metadata, never passwords/tokens. See authentication.md and audit.md for guarantees and limitations. Development OpenAPI is at /docs; disabled in hosted environments. Validation is strict and errors never echo input.

No purchase, wallet, gateway, VPN provisioning or Telegram endpoints are implemented yet.

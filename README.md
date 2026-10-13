# فروشگاه VPN تلگرام / Telegram VPN Store

A Persian-first VPN store under active implementation. **Not production-ready.**

The repository started empty. The implementation includes a runnable FastAPI foundation plus versioned PostgreSQL identity/RBAC/catalog schema and strict IRT currency utilities, not a completed sales bot. Administrator authentication, protected management APIs, RBAC and append-only auditing are implemented. Catalog management and persisted idempotent IRT quotes are also implemented. Purchases, payment integrations, VPN provisioning and user interfaces remain unimplemented.

## Local development

Requires Python 3.13, Docker Compose, and uv 0.9.2.

```bash
cp .env.example .env
# .env is local-only and must never be committed.
docker compose --env-file .env up --build
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

For Python-only development with PostgreSQL and Redis already running:

```bash
cd backend
uv sync --frozen
export VPNSTORE_ENVIRONMENT=development
export VPNSTORE_DATABASE_URL='postgresql+asyncpg://vpnstore:local-development-only@127.0.0.1/vpnstore'
export VPNSTORE_REDIS_URL='redis://127.0.0.1:6379/0'
uv run --frozen alembic upgrade head
# Optional: disabled illustrative catalog, no accounts or live configurations:
uv run --frozen python -m scripts.seed_dev
uv run --frozen uvicorn app.main:create_app --factory --no-access-log --no-proxy-headers
```

Settings use `VPNSTORE_`-prefixed environment variables; `.env` is not silently loaded by Python. Docker Compose loads the explicitly selected local file. Do not use development credentials or this localhost Compose setup in production.

## Verification

```bash
cd backend
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy app scripts migrations
uv run --frozen pytest -m 'not integration'
# Use a dedicated disposable *_test database and environment=test.
# Configure VPNSTORE_DATABASE_URL and VPNSTORE_REDIS_URL for those services.
uv run --frozen alembic upgrade head
uv run --frozen alembic check
uv run --frozen pytest --cov=app --cov-report=term-missing --cov-report=xml
uv run --frozen python scripts/check_secrets.py
uv run --frozen pip-audit
```

Bootstrap/recover using masked terminal prompts; see `docs/authentication.md`. Never pass administrator passwords as CLI arguments. Hosted runtime roles and audit guarantees are described in `deploy/runtime-role.sql` and `docs/audit.md`.

See `docs/architecture.md`, `docs/testing.md`, `docs/security.md`, and `docs/implementation-progress.md`. The full accepted project brief is in `docs/project-requirements.md`.

**Capacity is unmeasured. No claim of 100,000 daily active users or real payment/provider verification is made.**

## Catalog and pricing

Authenticated catalog create/read/update/disable and stable quotes are described in
`docs/catalog-pricing.md`. Quotes are immutable price snapshots, not purchases or inventory
reservations. Configure `VPNSTORE_QUOTE_SECONDS` to control their lifetime (default 900 seconds).
No public customer quote endpoint or provider credentials in catalog attributes are supported.

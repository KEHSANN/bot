# فروشگاه VPN تلگرام / Telegram VPN Store

A Persian-first VPN store under active implementation. **Not production-ready.**

The repository started empty. The first implementation is a runnable FastAPI infrastructure foundation, not a completed sales bot. There are no working purchases, payment integrations, VPN provisioning, administrator accounts, or user interfaces yet.

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
uv run --frozen uvicorn app.main:create_app --factory --no-access-log
```

Settings use `VPNSTORE_`-prefixed environment variables; `.env` is not silently loaded by Python. Docker Compose loads the explicitly selected local file. Do not use development credentials or this localhost Compose setup in production.

## Verification

```bash
cd backend
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy app
uv run --frozen pytest -m 'not integration' --cov=app
# Requires real PostgreSQL and Redis configured via the environment:
uv run --frozen pytest -m integration
uv run --frozen pip-audit
```

See `docs/architecture.md`, `docs/testing.md`, `docs/security.md`, and `docs/implementation-progress.md`. The full accepted project brief is in `docs/project-requirements.md`.

**Capacity is unmeasured. No claim of 100,000 daily active users or real payment/provider verification is made.**

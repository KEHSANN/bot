# Deployment status

The provided Docker Compose topology is **local development only**. PostgreSQL and Redis bind to loopback and use persistent volumes. API image uses UID 10001. Hosted deployment, migrations, bot/worker processes, admin bootstrap, TLS reverse proxy, backups, upgrades and rollback are not implemented yet.

Hosted startup validates explicit allowed hosts and authenticated TLS database/cache URLs. Use `VPNSTORE_ENVIRONMENT=staging` or `production`. PostgreSQL URL query `ssl=verify-full` is recommended; use `rediss://` for Redis. Use separate, least-privilege service credentials. Do not reuse the example development password.

API entrypoint: `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log`. Environment must contain both dependency URLs. Health readiness fails if either service is unreachable. `/health/live` is not evidence of financial or provider health.

Docker image build and full Compose startup require a Docker daemon; execution status is recorded in the progress document. Do not infer an executed deployment from this file.

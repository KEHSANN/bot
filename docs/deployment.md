# Deployment status

The provided Docker Compose topology is **local development only**. PostgreSQL and Redis bind to loopback and use persistent volumes. API image uses UID 10001. A one-shot migration service completes before the local API starts. Hosted deployment, bot/worker processes, admin bootstrap, TLS reverse proxy, backups and operational upgrade/rollback procedures are not implemented yet.

Hosted startup validates explicit allowed hosts and authenticated TLS database/cache URLs. Use `VPNSTORE_ENVIRONMENT=staging` or `production`. PostgreSQL URL query `ssl=verify-full` is recommended; use `rediss://` for Redis. Use separate, least-privilege service credentials. Do not reuse the example development password.

API entrypoint: `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log`. Environment must contain both dependency URLs. Health readiness fails if either service is unreachable. `/health/live` is not evidence of financial or provider health.

Docker image build and full Compose startup require a Docker daemon; execution status is recorded in the progress document. Do not infer an executed deployment from this file.

Migrations: run `/opt/venv/bin/alembic upgrade head` as a separate controlled migration job with appropriate DDL credentials. Runtime database credentials should have only the least privilege required by implemented operations. Do not race migrations across API replicas. The image contains Alembic configuration and revision files. Development Compose waits for its one-shot migration service; readiness validates the application schema revision.

The foundation Docker build passed in the observed phase-1 GitHub Actions job. No full Compose startup or hosted deployment is claimed. Phase-2 GitHub Actions jobs also passed, including its migration-containing image build and real Compose configuration validation. Full Compose startup remains unexecuted.

For administrative API hosting, terminate HTTPS, isolate direct API/database/cache ingress, keep proxy headers disabled unless trust is explicitly configured, migrate with an owner account, and apply the non-owner group grants in deploy/runtime-role.sql to a separately managed runtime login. Bootstrap via the masked prompt documented in authentication.md; do not commit or pass passwords on the command line. No production rollout, MFA assurance or full Compose startup is claimed.

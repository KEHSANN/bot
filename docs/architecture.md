# Architecture and decisions

## Baseline inspection

The remote repository `KEHSANN/bot` was empty on 2026-10-13: no files, branches, or commit history. Repository access reports push permission. No existing application or conventions needed preservation. A feature branch will hold implementation; main receives only the initialization README. No shared history is rewritten.

## Implemented foundation

- Python 3.13 (supported version available in the execution environment), FastAPI, Pydantic v2 settings.
- SQLAlchemy asyncpg PostgreSQL connection pool with pre-ping and bounded size; Redis async client.
- `/health/live` checks process responsiveness. `/health/ready` performs concurrent, bounded real dependency probes and returns 503 when unavailable. It does not yet check migration version.
- Lifespan-managed resources; exception-safe pool cleanup.
- UUID request IDs, explicit host validation, response safety headers, generic Persian failure response, allowlisted JSON logging without request payloads or credentials.
- uv lockfile, quality CI, local-development Compose, unprivileged API container.

## Planned domain architecture (not yet implemented)

A modular monolith will share business services between HTTP and Telegram adapters. Separate API, bot, and background worker processes scale independently. PostgreSQL is authoritative; Redis is for ephemeral coordination and jobs, never authoritative balances.

Business domains follow the accepted project brief: users/auth/RBAC, catalog/pricing, inventory, immutable ledger/wallet, orders/payments, providers/provisioning/delivery, referrals, membership/workflows, campaigns/coupons/support, audit/reports.

Financial amounts will use integer minor units, explicit currency, transaction-scoped locks, unique idempotency keys, and database constraints. External provisioning/payment calls will not be made inside open financial database transactions. Durable outbox/state machines and reconciliation are required before enabling purchases.

Next.js/TypeScript Persian RTL admin and aiogram 3 bot remain planned. No blank frontend or fake production adapters are shipped.

## Security boundaries

No sales/admin APIs exist yet. Credentials come from environment; hosted settings fail closed without authenticated TLS dependencies and explicit hosts. Health endpoints reveal only component status. Production docs are disabled. This foundation alone is not a security certification or production deployment.

## Scale

The 100,000-DAU goal remains an unverified target. No capacity/load benchmark has been run. Pool budgets, Redis queues, callback idempotency, rate limits, multi-process behavior, and recovery must be evaluated in later phases.

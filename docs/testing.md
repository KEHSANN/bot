# Testing

Run formatting, lint, type checks, unit coverage, integration tests, and dependency audit using README commands. The required backend coverage threshold is 85%; financial/referral coverage is not applicable until those modules exist.

Unit tests replace external clients explicitly using `unittest.mock`; these mocks are not application adapters. Integration tests use actual PostgreSQL/Redis and fail rather than fabricate success if services are absent. Unit invocation deselects integration tests; that is not an integration pass. Real-service tests require environment=test and an explicitly dedicated *_test database.

GitHub Actions is configured for actual PostgreSQL and Redis, migration upgrade/drift/downgrade/re-upgrade on a disposable database, all tests, baseline-aware secret scanning, a dependency audit, Compose configuration validation, and a Docker build. Workflow completion must be observed before reporting a CI pass. Current executed results and unavailable checks are recorded in `implementation-progress.md`.

No financial concurrency, Telegram, frontend, end-to-end payment/provider, or load tests are claimed yet.

## Database verification

The 20-way concurrent duplicate Telegram-ID insertion test proves the uniqueness boundary permits only one insert. It does not implement onboarding behavior or prove wallet correctness. Plan constraints, foreign-key deletion protection, composite grant uniqueness, rollback, disabled/idempotent seeds and exact monetary conversion/overflow/rounding rejection have real tests. Coverage of declarative schema lines does not prove every business invariant.

Local secret scan compares against `.secrets.baseline`, whose ten findings were explicitly reviewed as development/example/test placeholders. New findings fail CI without printing their values. Never automatically approve a new baseline; inspect each candidate first. A synthetic invalid token-shaped canary was rejected by the actual detector and removed before publication.

Security tests include role/endpoint allow/deny matrices, every current protected read and write rejecting unauthenticated access, malformed inputs/tokens, digest-only storage, token/session expiry, refresh rotation and concurrent replay, logout/password/session revocation, distributed rate counters, live grant changes, protected-role/subset checks, inactive accounts, last-super blocking protection, audit data redaction, SQL/ORM UPDATE/DELETE/TRUNCATE rejection, non-owner DDL denial, audit-insert rollback and replay-outage revocation. These are real PostgreSQL/Redis integrations. Coverage traces greenlet and thread concurrency because SQLAlchemy async and Argon2 thread offloading otherwise lose executed lines; the 85% threshold was not weakened. One upstream Starlette/httpx warning remains.

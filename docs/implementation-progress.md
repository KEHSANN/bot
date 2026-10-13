# Implementation progress

## Baseline

2026-10-13: `KEHSANN/bot` is empty (no branches/commits/files). Push access is available. No existing behavior was replaced. Main will receive an initialization README; implementation is developed on `feature/platform-foundation`.

## Phase checklist

- [x] 1. Runtime, configuration, health, logging, development setup, quality CI foundation (local checks and quality CI verified)
- [x] 2. Database foundation and migrations (identity/RBAC/catalog; remaining domain schema is assigned to later phases)
- [x] 3. Authenticated administrator APIs, enforced RBAC and append-only audit
- [x] 4. Catalog management, pricing engine and persistent stable quotes
- [ ] 5. Inventory and atomic import
- [ ] 6. Wallet, immutable ledger, purchases and orders
- [ ] 7. Manual deposits and verified gateway adapters
- [ ] 8. Providers, provisioning, delivery recovery
- [ ] 9. Customer Telegram bot and workflows
- [ ] 10. Referral rewards, approval and abuse controls
- [ ] 11. Persian RTL web administration
- [ ] 12. Telegram administration with parity
- [ ] 13. Campaigns, coupons, support and reports
- [ ] 14. Comprehensive concurrency, security and E2E tests
- [ ] 15. Load, monitoring and recovery validation
- [ ] 16. Production-readiness review and deployment verification

## Executed verification

Phase 1 local verification on 2026-10-13:

- Python 3.13.14; actual PostgreSQL 15.18 and Redis 6.2.20, bound to localhost test ports. CI specifies PostgreSQL 17.6/Redis 7.4.5. Phase-1 quality jobs later completed successfully using that configured pair.
- `uv run --frozen ruff format --check .`: passed.
- `uv run --frozen ruff check .`: passed.
- `uv run --frozen mypy app`: passed, 6 source files.
- `uv run --frozen pytest --cov=app --cov-report=term-missing --cov-report=xml`: 34 passed, 0 failed, 0 skipped; 100% coverage of this small foundation only. One upstream Starlette/httpx deprecation warning remains.
- Initial integration failure was a wrong local test database username; corrected the dedicated test role and reran all tests successfully. Initial type-check and audit-command errors were also corrected before publication.
- Real uvicorn HTTP smoke: `/health/live` and `/health/ready` returned 200 with both dependencies healthy.
- `pip-audit` of the locked installed environment: no known vulnerabilities found.
- GitHub content secret scan unavailable: GitHub Advanced Security is not enabled. Local detect-secrets 1.5.0 scanned tracked files with all default plugins, no online verification; dependency checksum lockfile excluded. Ten findings reviewed, all explicit development/example/test credentials or the placeholder rejection constant; no live production secret was introduced. This is not a full security audit.
- `git diff --cached --check`: passed; staged files reviewed, no environment files/database data included.
- Docker build/Compose runtime: not executed (Docker daemon/tooling absent).
- GitHub Actions phase 1: observed completed/success for both quality jobs on the publication-log head, including required tests, dependency audit and container build. Jobs: https://github.com/KEHSANN/bot/actions/runs/38350772102/job/115142456015 and https://github.com/KEHSANN/bot/actions/runs/38350775723/job/115142442726. Full Compose startup remains unverified.

Publication verified with GitHub commit lookup and branch listing: phase-1 implementation commit `4750e8f3c6496929dee42119e6cc895747277c3c`; remote branch pointed to that exact SHA. Initialization commit: `b4f4b9094d3b758712912b9199c0b8d8d7a6ff77`. Branch: `feature/platform-foundation`.

## Phase 2: database and currency foundation

Implemented ten normalized identity/RBAC/catalog tables and initial reversible Alembic migration, named constraints/indexes/FKs, strict integer IRT currency and exact toman/rial conversion/display, explicit transaction scope, migration-aware readiness, disabled/idempotent development seed, one-shot local migration service, integration isolation guards, and baseline-aware CI secret detection.

Actual local checks on 2026-10-13:

- Formatting/lint passed; strict mypy checked app, seed/scanner scripts and migrations.
- Full test suite: 68 passed, 0 failed, 0 skipped; 100% coverage of implemented foundation/helpers/model mappings only; one upstream Starlette/httpx deprecation warning.
- Real PostgreSQL 15.18 and Redis 6.2.20. Includes a 20-way uniqueness race, plan amount/range/currency/JSON checks, grant uniqueness, foreign keys/deletion restrictions, transaction rollback and two-run disabled seed idempotency.
- Migration upgrade, drift check, downgrade, re-upgrade and second drift check passed on disposable `vpnstore_test`. Final full tests reran after lifecycle checks. No SQLite substitution.
- Initial constraint-edge failures exposed unsafe subtraction/modulo evaluation on malformed data. Replaced with NUMERIC addition and guarded CASE before publication; all tests passed after correction.
- A mocked context manager initially suppressed the schema-mismatch test exception; fixed its __aexit__ return to False and reran rather than weakening the assertion.
- Final real uvicorn HTTP smoke passed after migrations: liveness and migration-aware readiness both 200, PostgreSQL and Redis healthy.
- Locked environment dependency audit: no known advisories.
- Local default-detector secret scan: ten reviewed fixture/example findings, zero new findings. An invalid synthetic token-format canary produced a genuine GitHub Token detection and failing scan; the canary was removed before publication. An initial canary harness PATH error was corrected and not counted as detector verification.
- Phase-2 CI observed completed/success for both quality jobs on implementation commit `34287e8a19f63eed76e46293a4e5ba57fc301d08`, including real PostgreSQL/Redis tests, migration lifecycle, formatting/lint/types, secret scan, dependency audit, Compose configuration validation and container build. Jobs: https://github.com/KEHSANN/bot/actions/runs/38351828319/job/115145721745 and https://github.com/KEHSANN/bot/actions/runs/38351823500/job/115145706903. No full Compose startup, production deployment, load tests or external gateway/provider verification claimed.

Phase-2 implementation commit: `34287e8a19f63eed76e46293a4e5ba57fc301d08`. Verified by remote commit lookup and branch listing pointing to that exact SHA. Branch remains `feature/platform-foundation`. Draft review: https://github.com/KEHSANN/bot/pull/1.

Next required phase: authentication, RBAC enforcement, safe administrator bootstrap and immutable audit. Tables alone do not constitute those features.

## Known limitations

Not a functioning VPN sales system yet. Authenticated administration, RBAC, audit and catalog/quotes are implemented; wallet/ledger, sales, provider, gateway, bot, frontend and production deployment are not. Three schema revisions are implemented. Capacity is unknown. Credentials and sandbox documentation will be required for real provider/payment validation in later phases.


## Phase 3: authenticated administration, enforced RBAC, immutable audit

Implemented app/security/{crypto,schemas,policy,metadata,service,api}.py, app/models/security.py, admin credential/system-role columns, revision 0002_admin_security, core/body_limit.py, operator bootstrap/recovery CLI and deploy/runtime-role.sql. Thin protected HTTP routes invoke live centralized service authorization. No default administrator/password or public recovery bypass was created.

Actual final local verification:

- Full existing + security suite: **128 passed, 0 failed, 0 skipped**, one upstream Starlette/httpx deprecation warning.
- Coverage: **98.14% overall**, security/service.py 96% as displayed by the branch report; 85% threshold retained. SQLAlchemy greenlet and Argon2 thread tracing configured correctly. Early under-reporting was corrected rather than excluding code/lowering the threshold.
- Formatting, lint, strict mypy (29 files): passed. Early generated-code type/lint issues and a test import were fixed before publication.
- Real PostgreSQL 15.18/Redis 6.2.20: access/refresh/session expiry; digest storage; generic login errors; account/IP rate limits; refresh race/reuse; logout/password/session revocation; live grants; allowed/denied role matrices; privilege escalation/self/system-role/last-super protection; account states; current-password step-up; operator recovery; validation/body limits; audit redaction, DML/ORM/TRUNCATE rejection and non-owner DDL denial; audit failure rollback and restrictive replay fallback.
- Migration check, disposable downgrade/base/re-upgrade/check and offline SQL generation passed. Destructive downgrade is not a production rollback recommendation.
- Locked pip-audit: no known advisories. Default-detector secret scan: zero new findings. Exactly five reviewed test-only findings (random fixture/public name expressions and intentionally wrong passwords) added to the existing ten-placeholder baseline; no detector disabled and no live secret introduced.
- git diff whitespace check and focused security review completed. Positive mutations/token issuance require committed audit; database-owner/DBA, fabricated-insert, audit-outage and operator attribution limitations are explicit in docs/audit.md.

Publication verified: `1b8d0262d4933c1320f3c225bb7f2635fd6c93cd`; remote branch matched. Both quality jobs completed successfully, including real PostgreSQL/Redis integration, migration lifecycle, scans, types, Compose configuration and Docker build: https://github.com/KEHSANN/bot/actions/runs/38398623320/job/115300168996 and https://github.com/KEHSANN/bot/actions/runs/38398615427/job/115300142294. Real network smoke confirmed readiness 200 and unauthenticated administrator access 401. Branch remains feature/platform-foundation. Next phase: catalog management, pricing engine and persistent idempotent quotes. No wallet/payment/provider/bot/UI/deployment/load-completion claim.


## Phase 4: catalog management, integer pricing, persistent stable quotes

Implemented app/catalog/{schemas,service,api}.py, app/models/quote.py, revision
0003_catalog_quotes and current-password protected catalog create/read/update/disable.
Quote creation/read use live orders/users permissions; prices cannot be supplied by clients.
Per-user request keys, unique constraints and transactional locks persist one stable snapshot.
Pricing edits bump the plan version without changing existing quotes. Seven-role allow/deny
matrix, different-session concurrency and application-restart persistence are exercised.
Runtime grants and PostgreSQL triggers keep quote updates/deletes/truncation unavailable.
Detailed algorithm, guarantees, scope and DBA limitations are in docs/catalog-pricing.md.

Actual final local checks:

- Full suite: **148 passed, 0 failed, 0 skipped**, one upstream Starlette/httpx deprecation warning.
- Coverage: **97.59% overall** with branch/greenlet/thread tracing; original 85% threshold retained.
  All 128 existing tests retained; 20 catalog/pricing tests added.
- Formatting, lint, strict mypy (35 files) passed. Real PostgreSQL 15.18/Redis 6.2.20.
- Migration drift check, downgrade/base/re-upgrade/check and offline SQL generation passed.
  Fresh-database tests verify the final migration, not just modified existing metadata.
- Initial tests found a catalog-update server-timestamp refresh regression and an incorrect
  step-up response expectation. Fixed application refresh/expected 403 denial and reran all tests.
  An early unpublished migration constraint-naming omission was fixed with explicit op.f naming;
  the final clean lifecycle and database method constraint test passed.
- Secret scan: zero new findings. One reviewed public rejection-test literal added to the baseline;
  no detectors disabled or live credentials introduced. Locked pip-audit: no known advisories.
- Real uvicorn HTTP smoke: liveness/readiness 200 and unauthenticated catalog access 401.
- Focused security review: no client amount override or mass assignment; permission/step-up and
  audit rollback enforced; metadata excludes credentials/bodies; database/non-owner tamper tests
  retained. Conservative shared write lock is deliberate, not a load-tested scalability claim.
- Docker/Compose local execution remains unavailable; hosted CI will verify build/configuration.

Publication and hosted CI verification are recorded only after observing the remote commit
and completed checks. The platform is still not production-ready. Next incomplete phase:
**5 — inventory management and atomic import**. Quotes do not reserve inventory, charge a wallet,
consume a checkout attempt, provision VPNs or enforce order-history purchase limits.

# Database foundation

## Implemented revision

`0001_identity_catalog` creates ten normalized PostgreSQL tables:

- Identity: `users`, `admin_users`, `roles`, `permissions`, `role_permissions`, `admin_role_assignments`, `user_status_history`.
- Catalog: `product_categories`, `products`, `product_plans`.

UUID internal IDs are independent of Telegram usernames. Optional Telegram IDs are positive BIGINTs with a uniqueness constraint. User state is constrained to active/blocked/suspended/deleted. Role grants use composite primary keys; referenced identities and catalog entities are protected from deletion with RESTRICT foreign keys. Authentication and server-side authorization were added in revision 0002; see docs/authentication.md. Administrators and catalog records default to disabled.

Products support service type, manual/provisioned mode, category, Persian descriptions, economic flag, display ordering and JSONB object attributes. Plans store duration, quota, device limits, IRT integer price and bounded/aligned randomized increment settings. Revision 0003 adds the persisted quote engine; catalog APIs now enforce approved attributes and bounded category-cycle checks. Provider configuration is a separate future domain.

Server-side created timestamps and ORM-managed updated timestamps use timezone-aware columns. User status history is distinct from the append-only security audit added in revision 0002.

## Money convention

**One stored IRT unit = one Iranian toman = ten Iranian rials.** Values are nonnegative signed-64-bit integers. Currency must be exactly `IRT`; booleans, floats, strings, negatives and overflow are rejected by `app/core/money.py`.

`to_rials` performs explicit multiplication by ten with overflow checking. `from_rials` requires exact divisibility by ten; fractional-toman amounts are rejected rather than silently rounded. Persian display uses Persian digits and thousands separators. Provider integrations must explicitly use these boundaries and preserve original provider amounts; no gateway implementation is claimed.

Plan constraints independently reject invalid currency, negative/overflowing prices, zero quota/device/duration, invalid random ranges/steps, unaligned increments and non-object JSON. Constraint expressions use guarded CASE and NUMERIC addition so malformed inputs fail constraints instead of causing division-by-zero or overflow during constraint evaluation.

## Migrations and transactions

With both connection environment variables configured:

```bash
cd backend
uv run --frozen alembic upgrade head
uv run --frozen alembic check
uv run --frozen python -m scripts.seed_dev
```

Seed is idempotent and allowed only in development/test. It creates one disabled illustrative catalog, no VPN inventory, wallet, user/admin account or credentials. Do not enable or sell these example records without implementing real inventory/provisioning and purchases.

`app/db/session.py` provides explicit commit/rollback/close transaction scope. There is no shared global session. Readiness requires the expected Alembic revision; missing or mismatched schema returns 503. Migrations are separate one-shot operations, not automatically run by every API replica.

Migration downgrade/upgrade was tested on a disposable PostgreSQL database. **Downgrading this initial revision drops its tables and data. Never use the CI lifecycle commands on a production database.** Integration tests require `VPNSTORE_ENVIRONMENT=test` and a database name ending in `_test`.

## Remaining database work

Wallet/immutable ledger, financial transactions, inventory, orders, payments, provider/provisioning/outbox, referrals, workflows, campaigns/coupons/support and reporting schemas are not implemented. No mutable placeholder wallet or SQLite substitute is shipped. Financial concurrency, inventory reservation, quote consumption and provider/payment integration remain blockers before enabling sales.

## Revision 0002_admin_security

Adds optional unique validated username and Argon2id hash to existing disabled admin rows, an enabled-requires-credentials constraint, system-role designation, admin_sessions, digest-only admin_tokens, audit_logs and a typed admin_login_enabled setting. Seeds seven explicit roles/27 granular permissions, creates append-only audit triggers, records policy initialization, and does not create/enable a default admin. Existing enabled rows without credentials require trusted backfill/disable before upgrading; no insecure credential is invented. The downgrade removes security tables/credential columns and destroys audit history; only tested on disposable databases. Deploy a separate non-owner runtime group after migrating.

## Revision 0003_catalog_quotes

Adds the supported randomization_method column/check on product_plans and purchase_quotes
with UUID identity, owner/plan RESTRICT references, per-user request-key uniqueness,
integer IRT amount, pricing version, timestamps and a JSONB snapshot. Adds an owner/created
index and a statement-level UPDATE/DELETE/TRUNCATE rejection trigger.
Runtime grants allow only SELECT/INSERT on quotes. No default quote or sellable product is seeded.
Readiness expects this revision. Downgrading removes quotes and the method column, destroying
quote history; lifecycle verification is only for disposable databases, not a production rollback.

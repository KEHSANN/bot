# Database foundation

## Implemented revision

`0001_identity_catalog` creates ten normalized PostgreSQL tables:

- Identity: `users`, `admin_users`, `roles`, `permissions`, `role_permissions`, `admin_role_assignments`, `user_status_history`.
- Catalog: `product_categories`, `products`, `product_plans`.

UUID internal IDs are independent of Telegram usernames. Optional Telegram IDs are positive BIGINTs with a uniqueness constraint. User state is constrained to active/blocked/suspended/deleted. Role grants use composite primary keys; referenced identities and catalog entities are protected from deletion with RESTRICT foreign keys. These are **schema foundations**, not implemented authentication or authorization services. Administrators and catalog records default to disabled.

Products support service type, manual/provisioned mode, category, Persian descriptions, economic flag, display ordering and JSONB object attributes. Plans store duration, quota, device limits, IRT integer price and bounded/aligned randomized increment settings. The quote/pricing engine is not yet implemented. Provider-specific attribute validation and deeper category-cycle validation remain required before exposing mutation endpoints.

Server-side created timestamps and ORM-managed updated timestamps use timezone-aware columns. User status history is not yet an immutable audit subsystem; that is a later security phase.

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

Wallet/immutable ledger, financial transactions, inventory, orders/quotes, payments, provider/provisioning/outbox, referrals, sessions/audit, workflows, campaigns/coupons/support and reporting schemas are not implemented. No mutable placeholder wallet or SQLite substitute is shipped. Financial concurrency and audit immutability remain blockers before enabling sales.

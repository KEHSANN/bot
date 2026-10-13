# Catalog and stable purchase quotes

## Implemented API

All routes require a revocable administrator access token and live backend permissions.

| Route | Permission | Additional protection |
| --- | --- | --- |
| GET /admin/catalog/{categories,products,plans} | products.read | limit 1–100; offset 0–10000 |
| GET /admin/catalog/{kind}/{id} | products.read | typed UUID target |
| POST /admin/catalog/{kind} | products.manage | current password and approved reason |
| PUT /admin/catalog/{kind}/{id} | products.manage | current password and approved reason |
| POST /admin/quotes | orders.manage + users.read | server-priced; user/plan/request_key UUIDs only |
| GET /admin/quotes/{id} | orders.read + users.read | no mutation or repricing |

PUT replaces the editable catalog fields. Set enabled=false to retire records;
there is deliberately no destructive deletion endpoint. Foreign keys preserve quoted plans.
Creation defaults to disabled. Categories support bounded/cycle-checked parents and position;
products support Persian descriptions, economic flag, supported service types,
manual/provisioned mode, position and an allowlisted region/transport/TLS attribute object.
Plans support duration, traffic quota, device limits and integer IRT pricing.
Unknown fields, provider secrets, client price/version overrides and unapproved methods fail validation.

## Exact pricing algorithm

One IRT unit is one toman (ten rials). No floating-point monetary calculations.
Supported method: uniform_discrete. For configured aligned inclusive bounds min/max and step:

```text
n = (max_increment - min_increment) // increment_step + 1
increment = min_increment + secrets.randbelow(n) * increment_step
amount_minor = price_minor + increment
```

All n outcomes are equally likely. Equal bounds permit fixed/zero increments.
API and database reject negative amounts, zero steps, unaligned ranges,
invalid currency, unsupported methods and signed-64-bit overflow.
Each plan edit increments pricing_version; existing quotes retain their original snapshot.
Quote lifetime is configurable via VPNSTORE_QUOTE_SECONDS (default 900, range 60–3600).

## Retry, expiry and concurrency guarantees

The caller generates one UUID request_key per purchase attempt and preserves it across retries.
A PostgreSQL unique constraint on (user_id, request_key) and transactional advisory lock ensure
one persisted quote. The key cannot be reused for another plan or silently recycled after expiry:
those requests return 409. An explicit new attempt uses a new key.
The amount, currency, product/plan snapshot, version and expiry are stored before success is returned.
Retries from another administrator session return the same ID, amount and timestamps, even after
price edits. The application never updates/deletes a quote. Reads identify expired quotes.
Eligibility is checked on creation/retry: active user, enabled plan/product and enabled category ancestors.
A newly disabled product does not get a new/random replacement price; retries are denied.
An existing quote is not a promise of inventory or a completed order.

Catalog/security writes and quote creation share a database advisory lock acquired before
live authorization/eligibility reads. This intentionally conservative serialization prevents
role, account-state or availability edits racing past quote authorization. The per-attempt lock
and plan row lock additionally protect pricing. This is not a load-tested throughput claim.
Direct database writers bypass application-level category-cycle/version and eligibility checks;
production writes must go through the API and use separate trusted migrations/operators.

Quote creation and its audit event commit atomically. Audit insertion failure rolls the quote back.
Metadata is limited to entity IDs and pricing version; no request body/current password is audited.
PostgreSQL statement triggers reject quote UPDATE, DELETE and TRUNCATE, including ORM writes.
The documented non-owner runtime group receives only SELECT/INSERT on quotes and no DDL.
Owners/DBAs can disable triggers or alter data; this is append-only operational protection,
not cryptographic proof, external WORM storage or absolute immutability.

## Scope boundaries

This is an authenticated administration/on-behalf quote API, not a customer identity endpoint.
Customer Telegram callbacks must later derive their own verified user ID and preserve the key.
No wallet charge, checkout, inventory reservation, payment or provisioning occurs here.
Stock/limit checks and single-use quote consumption must be implemented atomically with orders
in phases 5–6; gateway/provider checks follow in phases 7–8. Promotions/coupons belong to phase 13.
Expired quotes currently remain append-only; a privileged retention/archive policy is not provided.
No provider connection credentials may be stored in catalog attributes.

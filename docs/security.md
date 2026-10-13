# Security engineering status

## Threat model and trust boundaries

Untrusted HTTP clients may guess credentials, steal/replay tokens, spoof forwarding headers, submit malformed/oversized bodies, seek unauthorized data/role escalation, or try to hide administrative changes. PostgreSQL/Redis, migration/host operators and application code are trusted infrastructure; compromises of those boundaries are not solved by API RBAC.

## Implemented controls

Argon2id passwords, short opaque access tokens, digest-only rotating refresh tokens, absolute expiry, revocation, fail-closed shared Redis counters, bounded body reads, safe validation, live server-side RBAC, current-password mutation confirmation, subset delegation and self/system-role/last-super guards. Operator bootstrap/recovery uses masked prompts, no defaults or public reset route. Successful state changes and audit commit together. PostgreSQL triggers reject audit UPDATE/DELETE/TRUNCATE; non-owner runtime grants exclude audit mutation/DDL. Exact guarantees and outage/DBA limitations: authentication.md and audit.md.

Read serializers/audit details contain no password hashes, token values/digests or arbitrary request payloads. Logging serializes only allowlisted metadata. Hosted connection settings fail closed without authenticated TLS dependencies/explicit hosts; production API docs are disabled. HTTPS ingress and strict proxy trust are deployment responsibilities. No auth cookies/CORS credentials/browser storage exists yet; future browser cookies need CSRF protection.

## Security review and scans

Verified central policy enforcement on all implemented admin operations; no client/token role claims; UUID targeting/strict schemas; constant dummy hash path for unknown users; row-locked session rotation/revocation; audit failure rollback; raw forwarding headers ignored; non-owner/owner audit tampering and restrictive replay-outage fallback tested. pip-audit found no known advisories in the locked environment. Locked detect-secrets runs in CI against reviewed non-secret fixture/example fingerprints; no default detector disabled. GitHub Advanced Security remains unavailable. See implementation-progress.md for actual results.

## Remaining risks

Not a security certification or production-ready sales system. MFA, browser/BFF session/CSRF design, Telegram identity binding, external WORM/cryptographic audit, retention/archival/monitoring, penetration testing, hosted deployment/TLS verification, financial invariants, payment/provider verification, upload handling, backup/restore and load/capacity testing remain incomplete. Fixed windows and per-process hash bounds are not a complete DDoS defense. Owner/DBA privileges can defeat triggers; compromised DB credentials can fabricate events/direct changes. Keep migration ownership separate and protect operator credentials. Never use the development Compose credentials/topology in production.

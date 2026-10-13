# Security status

Implemented: secret-typed settings (redacted repr), fail-closed hosted dependency configuration, TLS requirements for hosted PostgreSQL/Redis, explicit host allowlist, unprivileged container process, generic error responses, UUID-only request correlation, no query/body/header logging, disabled hosted API docs, no default administrator.

The dependency audit is a real advisory scan, not a complete security assessment. No credentials from Telegram, gateways, or providers are required for this phase; none are present in committed configuration. The example credentials are local-development placeholders only. Production must use unique secrets managed outside Git.

Remaining blockers before exposure: authentication/session lifecycle, RBAC, CSRF/CORS policy, rate limiting, encryption for provider/configuration secrets, financial invariants, payment verification, Telegram webhook validation, upload protection, audit immutability, penetration tests, proxy policy, backup/recovery tests. Public health readiness may disclose availability; restrict it at the deployment ingress if needed.

Do not disable protections or use the development Compose topology in production. Hosted TLS termination and certificate validation must be configured and verified against the chosen services.

## Foundation scan results

Locked environment dependency audit found no known advisories at execution time. GitHub Advanced Security content scan could not run because it is not enabled. A local detect-secrets 1.5.0 default-detector scan reviewed ten placeholder/test findings; no live production credentials were introduced. Dependency lockfile hashes were excluded; no potential secrets were sent for online validation. Detailed verification status is in the progress log.

A locked detect-secrets scanner now runs in CI against a reviewed placeholder baseline; new findings fail closed. It never sends potential credentials for online verification and never prints their values. This is independent of unavailable GitHub Advanced Security. The baseline's ten explicitly non-secret fixture/example findings must not be expanded without reviewing their source lines. Database integration tests refuse non-test environments and database names without a _test suffix.

Authentication, RBAC enforcement, safe admin bootstrap and immutable audit are not implemented merely because their foundation tables exist. No admin endpoint, enabled default administrator or password is exposed.

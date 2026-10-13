# Administrator authentication and authorization

## Implemented API

`POST /auth/login` accepts a lowercase ASCII username (3–64 characters) and password (1–128 at login). Bootstrap/new/reset passwords require 16–128 characters and at least eight distinct characters; no plaintext credential is persisted. Hashing uses Argon2id, 64 MiB, time cost 3, parallelism 2, random 16-byte salt. Four hashing/verification operations per API process may run concurrently; unknown logins verify a dummy hash. Error responses are generic Persian and validation never echoes supplied inputs.

Successful login creates a new database-backed session and two independent 256-bit opaque tokens. Only SHA-256 token digests are stored. Default access lifetime: 5 minutes; refresh lifetime: 24 hours; absolute session lifetime: 7 days. Access cannot exceed 15 minutes and session cannot exceed 7 days. Expiration uses the database clock, not browser input. Tokens are returned only after session state and success audit commit.

`POST /auth/refresh` rotates the refresh token, marks the old one used and revokes previous access tokens. Session row locks serialize rotation across workers. Reusing any retired refresh token revokes the whole session, including newly issued tokens. Clients must serialize refresh; retrying a lost refresh response with the old token intentionally logs the client out rather than accepting replay.

`GET /auth/me`, `POST /auth/logout`, and `POST /auth/password` require a current Bearer access token. Password changes require the current password and revoke all sessions. Administrators with `sessions.manage` can revoke another session through `POST /admin/sessions/{id}/revoke` with current-password confirmation and an enumerated reason.

## Brute-force and transport protection

Atomic Redis Lua counters apply a 5-attempt/account and 20-attempt/peer-IP fixed 5-minute login window by default, counting successes and failures. Protected API calls/refresh also have a 120-request/peer/minute limit. Keys contain fingerprints, not raw identifiers. Counters are shared across API processes. Redis failure returns 503; no bypass/fake limiter exists. No X-Forwarded-For header is trusted by application code; the image disables Uvicorn proxy headers. If deploying behind a proxy, plan explicit trusted proxy configuration and edge limits; otherwise the proxy's address shares a bucket.

POST/PUT/PATCH bodies are limited to 64 KiB while reading, including chunked requests, with a 10-second read timeout. Validation and transport rejections generate security audit events without input values. HTTP responses are no-store. HTTPS termination and network isolation remain operator requirements.

This is an API token flow: no authentication cookies, browser token storage or credentialed CORS is implemented, so cookie-CSRF handling is not applicable to current endpoints. A future browser admin must use a properly secured BFF/HttpOnly-cookie design and CSRF protection if cookies carry authority; do not place refresh tokens in localStorage or URLs.

## RBAC

Every administrative operation authorizes a live enabled administrator, active user, unrevoked/unexpired session/token, and current database grants. Token payloads contain no trusted roles; grants are never cached in a token. Roles: super_admin, operations, finance, inventory, support, marketing, auditor. The permission registry and defaults are in `app/security/policy.py`; empty role assignments grant no administration privileges.

Read endpoints require their explicit read permissions. Status changes require users.manage; modifying an administrator's status additionally requires admins.manage. Role management requires roles.manage; admin creation/assignment requires admins.manage; settings require settings.manage; audit reads require audit.read. Role, administrator, account-state, session-revocation, settings and catalog mutations require current-password step-up and an enumerated reason. Quote creation instead requires live orders.manage plus users.read and performs no wallet debit, payment or provisioning; see docs/catalog-pricing.md. Custom roles can contain only known permissions the actor already holds; existing greater privileges cannot be modified by a lower-privileged actor. System roles and an actor's own roles are protected from editing; self-assignment is denied. Only existing super administrators can delegate the reserved super_admin role. Self-blocking and blocking the last active super administrator are denied. Security writes serialize under a transaction-scoped advisory lock; session locks prevent rotation/revocation races.

## Operator bootstrap and recovery

After migrations, run locally with trusted database/operator credentials:

```bash
cd backend
uv run --frozen python -m scripts.bootstrap_admin --username YOUR_LOWERCASE_USERNAME
# Offline reset of an existing account, revoking all sessions:
uv run --frozen python -m scripts.bootstrap_admin --username YOUR_LOWERCASE_USERNAME --reset-existing
# Explicitly restore global login when recovering maintenance lockout:
uv run --frozen python -m scripts.bootstrap_admin --username YOUR_LOWERCASE_USERNAME --reset-existing --restore-login
```

Passwords use masked terminal prompts and confirmation; they are never command-line arguments or output. Bootstrap refuses once an enabled super administrator exists. Recovery is a trusted host operation, never a public HTTP reset route. It does not silently unblock users, enable disabled admins or change roles. Operators must protect database credentials and filesystem access. Host operations have null administrator actor and explicit operator actions; OS/DB operator identity attribution requires external host/database auditing.

## Remaining limitations

MFA, self-service reset, browser admin/BFF, Telegram administrator authentication, internet-facing penetration testing and production deployment are not implemented. Fixed windows permit boundary bursts and distributed attacks need edge protection; the hashing semaphore is not a global DDoS solution. Session/token/audit retention and archival require an operational policy. No capacity or full platform readiness claim is made.

# Testing

Run formatting, lint, type checks, unit coverage, integration tests, and dependency audit using README commands. The required backend coverage threshold is 85%; financial/referral coverage is not applicable until those modules exist.

Unit tests replace external clients explicitly using `unittest.mock`; these mocks are not application adapters. Integration tests use actual PostgreSQL/Redis and fail rather than fabricate success if services are absent. Unit invocation deselects integration tests; that is not an integration pass.

GitHub Actions is configured for actual PostgreSQL and Redis, all tests, a dependency audit, and a Docker build. Workflow completion must be observed before reporting a CI pass. Current executed results and unavailable checks are recorded in `implementation-progress.md`.

No financial concurrency, Telegram, frontend, end-to-end payment/provider, or load tests are claimed yet.

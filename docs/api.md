# Implemented HTTP API

- `GET /health/live`: 200 with `{"status":"ok"}`. No external dependencies.
- `GET /health/ready`: 200 only if PostgreSQL connection and expected Alembic revision and Redis PING succeed within the configured timeout; otherwise 503. Response includes component status only.
- Development/test OpenAPI at `/openapi.json` and docs at `/docs`; disabled in staging/production.

No authenticated admin, wallet, payment, order, or Telegram webhook endpoints exist yet. All responses carry a UUID X-Request-ID. Unhandled errors return generic Persian detail and the correlation ID without internal exceptions.

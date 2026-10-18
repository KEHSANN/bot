"""Internal HTTP surface: liveness and readiness only.

Nothing here is public and nothing here serves the store — the platform is
fully Telegram-driven. The container orchestrator and an uptime probe are the
only consumers, so there are exactly three routes: ``/healthz`` answers
without touching dependencies, ``/readyz`` checks Postgres and (when enabled)
Redis, and ``/version`` reports the build identity.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vpnbot.config import Settings

log = logging.getLogger(__name__)

API_VERSION = "1.0.0"


def create_app(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("health endpoint up on %s:%s", settings.health_host, settings.health_port)
        yield

    app = FastAPI(
        title="vpnbot internal",
        version=API_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, object]:
        checks: dict[str, str] = {}

        try:
            async with session_factory() as session:
                await session.execute(text("select 1"))
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001 - readiness reports, never raises
            checks["database"] = f"error: {type(exc).__name__}"
            log.warning("readiness: database check failed", exc_info=True)

        if settings.redis_enabled:
            try:
                from redis.asyncio import Redis

                client = Redis.from_url(settings.redis_url)
                try:
                    await client.ping()
                    checks["redis"] = "ok"
                finally:
                    await client.aclose()
            except Exception as exc:  # noqa: BLE001
                checks["redis"] = f"error: {type(exc).__name__}"
                log.warning("readiness: redis check failed", exc_info=True)

        healthy = all(value == "ok" for value in checks.values())
        if not healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "ok" if healthy else "degraded", "checks": checks}

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"name": "vpnbot", "version": API_VERSION, "environment": settings.environment}

    return app


__all__ = ["API_VERSION", "create_app"]

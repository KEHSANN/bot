import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import Settings
from app.core.dependencies import Dependencies
from app.core.logging import configure_logging

logger = logging.getLogger("vpnstore.http")


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        app.state.dependencies = Dependencies(config)
        try:
            yield
        finally:
            await app.state.dependencies.close()

    development = config.environment in {"development", "test"}
    app = FastAPI(
        title="VPN Store",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development else None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts)

    @app.middleware("http")
    async def request_metadata(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            request_id = str(UUID(request.headers.get("X-Request-ID", "")))
        except ValueError:
            request_id = str(uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                "request_failed",
                extra={
                    "event": "request_failed",
                    "request_id": request_id,
                    "exception_type": type(exc).__name__,
                },
            )
            response = JSONResponse(
                {"detail": "خطای داخلی سامانه. لطفاً دوباره تلاش کنید.", "request_id": request_id},
                status_code=500,
            )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        if not development:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        logger.info(
            "request_completed",
            extra={
                "event": "request_completed",
                "request_id": request_id,
                "method": request.method,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
        return response

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        services = await request.app.state.dependencies.readiness()
        healthy = all(status == "ok" for status in services.values())
        return JSONResponse(
            {"status": "ok" if healthy else "unavailable", "dependencies": services},
            status_code=200 if healthy else 503,
        )

    return app

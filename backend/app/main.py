import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Scope

from app.core.body_limit import BodyLimitMiddleware
from app.core.config import Settings
from app.core.dependencies import Dependencies
from app.core.logging import configure_logging
from app.db.session import session_factory
from app.security.api import admin, auth
from app.security.metadata import request_metadata as security_metadata
from app.security.service import SecurityError, SecurityService

logger = logging.getLogger("vpnstore.http")


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        app.state.dependencies = Dependencies(config)
        app.state.security = SecurityService(
            session_factory(app.state.dependencies.engine), app.state.dependencies.redis, config
        )
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
    app.include_router(auth)
    app.include_router(admin)

    async def rejected_transport(scope: Scope, status: int) -> int:
        request = Request(scope)
        audit_status = await app.state.security.record_invalid(
            security_metadata(request), "security.transport_rejected"
        )
        return audit_status if audit_status in {429, 503} else status

    app.add_middleware(BodyLimitMiddleware, on_rejected=rejected_transport)
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

    @app.exception_handler(SecurityError)
    async def security_error(request: Request, exc: SecurityError) -> JSONResponse:
        messages = {
            401: "اطلاعات ورود یا نشست معتبر نیست.",
            403: "دسترسی مجاز نیست.",
            404: "مورد یافت نشد.",
            409: "عملیات با وضعیت فعلی سازگار نیست.",
            422: "اطلاعات درخواست معتبر نیست.",
            429: "تعداد درخواست‌ها زیاد است؛ بعداً تلاش کنید.",
            503: "سرویس موقتاً در دسترس نیست.",
        }
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
        if exc.status == 429:
            headers["Retry-After"] = str(config.login_window_seconds)
        return JSONResponse(
            {"detail": messages[exc.status]}, status_code=exc.status, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Default FastAPI validation responses can echo plaintext secret field inputs.
        status = await request.app.state.security.record_invalid(
            security_metadata(request), "security.invalid_request"
        )
        return JSONResponse({"detail": "اطلاعات درخواست معتبر نیست."}, status_code=status)

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

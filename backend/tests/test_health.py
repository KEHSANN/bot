from unittest.mock import AsyncMock
from uuid import UUID

from fastapi import Request
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_liveness_and_headers(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "untrusted\r\nvalue"})
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        UUID(response.headers["X-Request-ID"])
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "Strict-Transport-Security" not in response.headers


def test_request_id_preserved(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        request_id = "858b9952-6e19-4db4-96d3-39760e6ed647"
        response = client.get("/health/live", headers={"X-Request-ID": request_id})
        assert response.headers["X-Request-ID"] == request_id


def test_untrusted_host(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live", headers={"host": "evil.example"}).status_code == 400


def test_readiness(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.dependencies.readiness = AsyncMock(return_value={"database": "ok", "redis": "ok"})
        assert client.get("/health/ready").status_code == 200
        app.state.dependencies.readiness.return_value = {"database": "unavailable", "redis": "ok"}
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"


def test_errors_do_not_leak(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/test-error")
    async def error(request: Request) -> None:
        assert request.state.request_id
        raise RuntimeError("sensitive-provider-secret")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-error")
        assert response.status_code == 500
        assert "sensitive-provider-secret" not in response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"]
        assert "خطای داخلی" in response.json()["detail"]


def test_production_docs_disabled() -> None:
    settings = Settings(
        environment="production",
        database_url="postgresql+asyncpg://store:long-nondefault-secret@db/store?ssl=require",  # type: ignore[arg-type]
        redis_url="rediss://:long-nondefault-secret@cache:6379/0",  # type: ignore[arg-type]
        allowed_hosts=["testserver"],
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert "Strict-Transport-Security" in client.get("/health/live").headers


def test_config_loaded_from_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("VPNSTORE_DATABASE_URL", "postgresql+asyncpg://user:test@localhost/store")
    monkeypatch.setenv("VPNSTORE_REDIS_URL", "redis://localhost/0")
    with TestClient(create_app()) as client:
        assert client.get("/health/live").status_code == 200

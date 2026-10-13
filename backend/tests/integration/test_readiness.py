import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.dependencies import Dependencies
from app.main import create_app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_database_and_redis() -> None:
    # No mocks or fallback: this fails if either required service is absent.
    settings = Settings()  # type: ignore[call-arg]
    dependencies = Dependencies(settings)
    try:
        assert await dependencies.readiness() == {"database": "ok", "redis": "ok"}
        app = create_app(settings)
        app.state.dependencies = dependencies
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.get("/health/ready")
            assert response.status_code == 200
    finally:
        await dependencies.close()

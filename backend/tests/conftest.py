import pytest
from sqlalchemy.engine import make_url

from app.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        database_url="postgresql+asyncpg://tester:local-test@127.0.0.1/vpnstore",  # type: ignore[arg-type]
        redis_url="redis://127.0.0.1:6379/15",  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def isolate_integration_database(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("integration") is None:
        return
    config = Settings()
    database = make_url(config.database_url.get_secret_value()).database or ""
    if config.environment != "test" or not database.endswith("_test"):
        pytest.fail("Integration tests require environment=test and a dedicated *_test database")

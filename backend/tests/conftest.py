import pytest

from app.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        database_url="postgresql+asyncpg://tester:local-test@127.0.0.1/vpnstore",  # type: ignore[arg-type]
        redis_url="redis://127.0.0.1:6379/15",  # type: ignore[arg-type]
    )

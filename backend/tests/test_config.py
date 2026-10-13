import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE = {
    "database_url": "postgresql+asyncpg://user:local-dev@localhost/store",
    "redis_url": "redis://localhost:6379/0",
}


@pytest.mark.parametrize(
    "values",
    [
        {"database_url": "sqlite:///test.db"},
        {"database_url": "postgresql+asyncpg:///store"},
        {"database_url": "postgresql+asyncpg://localhost"},
        {"redis_url": "https://localhost"},
        {"redis_url": "redis:///0"},
        {"allowed_hosts": []},
        {"allowed_hosts": ["*"]},
        {"allowed_hosts": ["store.example/path"]},
        {"dependency_timeout_seconds": 0},
        {"database_pool_size": 0},
        {"database_max_overflow": -1},
        {"environment": "unknown"},
    ],
)
def test_invalid_config(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**(BASE | values))  # type: ignore[arg-type]


PRODUCTION = {
    "environment": "production",
    "database_url": "postgresql+asyncpg://store:long-nondefault-secret@db/store?ssl=verify-full",
    "redis_url": "rediss://:long-nondefault-secret@cache:6379/0",
    "allowed_hosts": ["store.example"],
}


@pytest.mark.parametrize(
    "values",
    [
        {"database_url": BASE["database_url"]},
        {"database_url": "postgresql+asyncpg://user:long-nondefault-secret@db/store"},
        {"redis_url": "redis://:long-nondefault-secret@cache/0"},
        {"redis_url": "rediss://:short@cache/0"},
        {"redis_url": "rediss://:local-development-only@cache/0"},
        {"database_url": "postgresql+asyncpg://u:local-development-only@db/store?ssl=require"},
        {"allowed_hosts": ["localhost", "127.0.0.1", "testserver"]},
    ],
)
def test_production_fails_closed(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**(PRODUCTION | values))  # type: ignore[arg-type]


def test_production_valid() -> None:
    settings = Settings(**PRODUCTION)  # type: ignore[arg-type]
    assert settings.environment == "production"
    assert "long-nondefault-secret" not in repr(settings)


def test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VPNSTORE_DATABASE_URL", str(BASE["database_url"]))
    monkeypatch.setenv("VPNSTORE_REDIS_URL", str(BASE["redis_url"]))
    monkeypatch.setenv("VPNSTORE_ALLOWED_HOSTS", '["localhost"]')
    assert Settings().allowed_hosts == ["localhost"]  # type: ignore[call-arg]


def test_validation_errors_hide_values() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(
            database_url="postgresql+asyncpg://u:private-password@localhost/store",
            redis_url="invalid://private-cache-secret",
        )
    assert "private-password" not in str(error.value)
    assert "private-cache-secret" not in str(error.value)

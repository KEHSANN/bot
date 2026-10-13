import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.core.dependencies import Dependencies
from app.db.schema import EXPECTED_SCHEMA_REVISION


@pytest.mark.asyncio
async def test_probes_and_cleanup(settings: Settings) -> None:
    with (
        patch("app.core.dependencies.create_async_engine") as engine_factory,
        patch("app.core.dependencies.Redis.from_url") as redis_factory,
    ):
        engine = engine_factory.return_value
        connection = AsyncMock()
        connection.scalar.return_value = EXPECTED_SCHEMA_REVISION
        engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
        engine.connect.return_value.__aexit__ = AsyncMock(return_value=False)
        engine.dispose = AsyncMock()
        redis = redis_factory.return_value
        redis.ping = AsyncMock()
        redis.aclose = AsyncMock()
        dependencies = Dependencies(settings)
        assert await dependencies.readiness() == {"database": "ok", "redis": "ok"}
        connection.scalar.assert_awaited_once()
        redis.ping.assert_awaited_once()
        await dependencies.close()
        engine.dispose.assert_awaited_once()
        redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_unavailable_and_timeout(settings: Settings) -> None:
    dependencies = Dependencies(settings)
    dependencies.database_ping = AsyncMock(side_effect=RuntimeError("private-dsn"))  # type: ignore[method-assign]

    async def slow() -> None:
        await asyncio.sleep(10)

    dependencies.redis_ping = slow  # type: ignore[method-assign]
    dependencies.timeout = 0.01
    assert await dependencies.readiness() == {"database": "unavailable", "redis": "unavailable"}
    await dependencies.close()


@pytest.mark.asyncio
async def test_engine_disposed_when_redis_close_fails(settings: Settings) -> None:
    dependencies = Dependencies(settings)
    dependencies.redis.aclose = AsyncMock(side_effect=RuntimeError("close"))  # type: ignore[method-assign]
    dependencies.engine = MagicMock(dispose=AsyncMock())
    with pytest.raises(RuntimeError):
        await dependencies.close()
    dependencies.engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_schema_mismatch_fails_readiness(settings: Settings) -> None:
    with patch("app.core.dependencies.create_async_engine") as engine_factory:
        connection = AsyncMock()
        connection.scalar.return_value = "outdated_revision"
        engine_factory.return_value.connect.return_value.__aenter__ = AsyncMock(
            return_value=connection
        )
        engine_factory.return_value.connect.return_value.__aexit__ = AsyncMock(return_value=False)
        engine_factory.return_value.dispose = AsyncMock()
        dependencies = Dependencies(settings)
        dependencies.redis_ping = AsyncMock()
        assert (await dependencies.readiness())["database"] == "unavailable"
        await dependencies.close()

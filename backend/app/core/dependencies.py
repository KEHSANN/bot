import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.db.schema import EXPECTED_SCHEMA_REVISION


class Dependencies:
    def __init__(self, settings: Settings) -> None:
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            connect_args={"timeout": settings.dependency_timeout_seconds},
        )
        self.redis: Redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_connect_timeout=settings.dependency_timeout_seconds,
            socket_timeout=settings.dependency_timeout_seconds,
            decode_responses=True,
        )
        self.timeout = settings.dependency_timeout_seconds

    async def database_ping(self) -> None:
        async with self.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != EXPECTED_SCHEMA_REVISION:
                raise RuntimeError("Database migration revision mismatch")

    async def redis_ping(self) -> None:
        await cast(Awaitable[bool], self.redis.ping())

    async def readiness(self) -> dict[str, str]:
        async def check(probe: Callable[[], Awaitable[None]]) -> str:
            try:
                async with asyncio.timeout(self.timeout):
                    await probe()
                return "ok"
            except Exception:
                # Deliberately do not expose or log DSNs, provider details, or exception text.
                return "unavailable"

        database, redis = await asyncio.gather(check(self.database_ping), check(self.redis_ping))
        return {"database": database, "redis": redis}

    async def close(self) -> None:
        try:
            await self.redis.aclose()
        finally:
            await self.engine.dispose()

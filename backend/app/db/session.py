from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def transaction(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Exactly one transaction; rollback on error, commit on normal exit, always close."""
    async with factory() as session, session.begin():
        yield session

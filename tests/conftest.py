"""Test harness.

Every test runs against a real PostgreSQL database. The fixture below creates
``vpnbot_tests`` from scratch, applies the Alembic migrations (so the
append-only audit trigger and the single-use inventory trigger really exist),
and drops the database afterwards. Rows are truncated between tests; nothing
is mocked away.

The database URL is configurable with ``TEST_DATABASE_URL``. The default points
at the local test cluster on port 55432.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Iterator

import pytest

DEFAULT_TEST_URL = "postgresql+asyncpg://postgres@127.0.0.1:55432/vpnbot_tests"
TEST_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL)
ADMIN_URL = TEST_URL.rsplit("/", 1)[0] + "/postgres"
DB_NAME = TEST_URL.rsplit("/", 1)[1]

# Settings are read once per process; set them before vpnbot.config is imported.
os.environ.setdefault("BOT_TOKEN", "123456:TESTONLYAAAAAAAAAAAAAAAAAAAAAAAAAA")
os.environ.setdefault("DATABASE_URL", TEST_URL)
os.environ.setdefault("FINGERPRINT_SECRET", secrets.token_hex(32))
os.environ.setdefault("OWNER_TELEGRAM_IDS", "900000001")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("REDIS_ENABLED", "false")

from cryptography.fernet import Fernet  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

os.environ.setdefault("INVENTORY_ENCRYPTION_KEY", Fernet.generate_key().decode())

from vpnbot.config import get_settings, reset_settings_cache  # noqa: E402
from vpnbot.security.crypto import CredentialCipher  # noqa: E402

TABLES = (
    "payment_reviews",
    "delivery_attempts",
    "inventory_assignments",
    "inventory_items",
    "payments",
    "orders",
    "referral_rewards",
    "ledger_entries",
    "wallet_accounts",
    "broadcast_recipients",
    "broadcast_campaigns",
    "ticket_messages",
    "tickets",
    "plans",
    "products",
    "categories",
    "screen_versions",
    "screens",
    "admins",
    "roles",
    "users",
    "jobs",
    "message_dispatch_log",
    "app_settings",
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    """Create a fresh database, migrate it, and drop it when the run ends."""
    from sqlalchemy.engine import make_url

    reset_settings_cache()
    admin_url = make_url(ADMIN_URL).set(drivername="postgresql+asyncpg")

    async def recreate() -> None:
        # A fresh engine per event loop: asyncpg connections cannot cross loops,
        # and Windows closes the selector/proactor loop between asyncio.run calls.
        en = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with en.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'))
                await conn.execute(text(f'CREATE DATABASE "{DB_NAME}"'))
        finally:
            await en.dispose()

    async def drop() -> None:
        en = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with en.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'))
        finally:
            await en.dispose()

    _run(recreate())

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    # env.py reads DATABASE_URL; point it at the test database for this run.
    os.environ["DATABASE_URL"] = TEST_URL
    reset_settings_cache()
    command.upgrade(cfg, "head")

    yield

    _run(drop())


@pytest.fixture
async def engine(database: None) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_URL, poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session_factory(
    engine: AsyncEngine, clean_tables: None
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def clean_tables(engine: AsyncEngine, database: None) -> None:
    """Empty every table before each test (TRUNCATE ignores row triggers).

    Deliberately not autouse: pure unit tests stay synchronous and never open
    a database connection, which also keeps them independent of the event loop.
    """
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as db:
        yield db


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def cipher(settings) -> CredentialCipher:
    return CredentialCipher(settings.inventory_encryption_key)


@pytest.fixture
def fingerprint_secret(settings) -> str:
    return settings.fingerprint_secret

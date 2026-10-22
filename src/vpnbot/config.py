"""Application settings loaded from environment variables.

The application refuses to start (raises ``SettingsError``) when required
secrets are missing or malformed. Add ``production`` deployments always load
configuration from the environment or a mounted env file, never from code.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsError(RuntimeError):
    """Raised when the environment configuration is invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram
    bot_token: str = Field(min_length=10)
    owner_telegram_ids: str = ""
    bot_username: str = ""

    # Database
    database_url: str = Field(min_length=10)
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_enabled: bool = True

    # Security
    inventory_encryption_key: str
    fingerprint_secret: str

    # App
    environment: Literal["development", "test", "production"] = "production"
    log_level: str = "INFO"
    log_json: bool = True
    api_host: str = "127.0.0.1"  # loopback by default; Docker Compose sets 0.0.0.0 inside the container
    api_port: int = 8080
    health_host: str = "127.0.0.1"  # loopback by default; Docker Compose sets 0.0.0.0 inside the container
    health_port: int = 8081

    # Limits / feature flags
    max_import_file_bytes: int = 5 * 1024 * 1024
    max_import_records: int = 20_000
    broadcast_rate_per_second: int = 20
    payment_review_ttl_hours: int = 6

    @field_validator("bot_token")
    @classmethod
    def _bot_token_shape(cls, value: str) -> str:
        if ":" not in value or not value.split(":", 1)[0].isdecimal():
            raise ValueError("BOT_TOKEN must look like '<digits>:<secret>'")
        return value

    @field_validator("inventory_encryption_key")
    @classmethod
    def _fernet_key(cls, value: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(value.encode())
        except Exception as exc:  # noqa: BLE001 - we re-raise as config error
            raise ValueError("INVENTORY_ENCRYPTION_KEY must be a Fernet key") from exc
        if len(raw) != 32:
            raise ValueError("INVENTORY_ENCRYPTION_KEY must decode to 32 bytes (Fernet key)")
        return value

    @field_validator("fingerprint_secret")
    @classmethod
    def _fingerprint_secret(cls, value: str) -> str:
        if len(value) < 32 or value.startswith("REPLACE"):
            raise ValueError("FINGERPRINT_SECRET must be a long random secret (>=32 chars)")
        return value

    @property
    def owner_ids(self) -> tuple[int, ...]:
        return tuple(int(part.strip()) for part in self.owner_telegram_ids.split(",") if part.strip())

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.is_production and not self.owner_ids:
            raise ValueError("OWNER_TELEGRAM_IDS is required in production")
        if not self.database_url.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError("DATABASE_URL must point to PostgreSQL")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE001
        raise SettingsError(f"Invalid application configuration: {exc}") from exc


def reset_settings_cache() -> None:
    """Testing hook: forget the cached settings."""
    get_settings.cache_clear()

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VPNSTORE_", extra="forbid", frozen=True, hide_input_in_errors=True
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    database_url: SecretStr
    redis_url: SecretStr
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "testserver"]
    )
    dependency_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)

    @model_validator(mode="after")
    def validate_connections(self) -> "Settings":
        from urllib.parse import urlsplit

        database = make_url(self.database_url.get_secret_value())
        redis = urlsplit(self.redis_url.get_secret_value())
        if (
            database.drivername != "postgresql+asyncpg"
            or not database.host
            or not database.database
        ):
            raise ValueError("A PostgreSQL asyncpg URL with a host and database is required")
        if redis.scheme not in {"redis", "rediss"} or not redis.hostname:
            raise ValueError("A Redis URL with a host is required")
        if not self.allowed_hosts or any("*" in host or "/" in host for host in self.allowed_hosts):
            raise ValueError("Explicit non-wildcard allowed hosts are required")
        if self.environment in {"staging", "production"}:
            if (
                not database.password
                or len(database.password) < 16
                or database.password == "local-development-only"
            ):
                raise ValueError("Non-development PostgreSQL credentials are required")
            if database.query.get("ssl") not in {"require", "verify-ca", "verify-full"}:
                raise ValueError("PostgreSQL TLS is required outside development/test")
            if (
                redis.scheme != "rediss"
                or not redis.password
                or len(redis.password) < 16
                or redis.password == "local-development-only"
            ):
                raise ValueError("Authenticated Redis TLS is required outside development/test")
            if self.allowed_hosts == ["localhost", "127.0.0.1", "testserver"]:
                raise ValueError("Public allowed hosts must be configured")
        return self

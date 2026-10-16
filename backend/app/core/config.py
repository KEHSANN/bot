"""
Application Configuration, Environment Validation, and IRT Currency Utilities.
All monetary amounts are represented in IRT (1 IRT = 1 Iranian Toman = 10 IRR) as exact integers.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Optional

PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

class CurrencyError(ValueError):
    """Raised when a monetary value violates IRT currency invariants."""


def validate_irt_amount(
    amount: int,
    *,
    allow_zero: bool = False,
    min_amount: int = 1,
    max_amount: int = 500_000_000,
) -> int:
    """Validate that an amount is a strict integer in IRT within safe bounds."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise CurrencyError(f"IRT amount must be an exact integer, got {type(amount).__name__}")
    if allow_zero and amount == 0:
        return 0
    if amount < min_amount:
        raise CurrencyError(f"IRT amount {amount} is below minimum {min_amount}")
    if amount > max_amount:
        raise CurrencyError(f"IRT amount {amount} exceeds maximum {max_amount}")
    return amount


def irr_to_irt(irr_amount: int) -> int:
    """Convert Iranian Rials (IRR) from external gateways to internal IRT (Tomans).
    Rejects amounts not cleanly divisible by 10 to prevent silent rounding loss.
    """
    if isinstance(irr_amount, bool) or not isinstance(irr_amount, int):
        raise CurrencyError("IRR amount must be an exact integer")
    if irr_amount < 0 or irr_amount % 10 != 0:
        raise CurrencyError(f"IRR amount {irr_amount} is negative or not divisible by 10")
    return irr_amount // 10


def irt_to_irr(irt_amount: int) -> int:
    """Convert internal IRT (Tomans) to Iranian Rials (IRR) for gateway requests."""
    validate_irt_amount(irt_amount, allow_zero=True, min_amount=0, max_amount=5_000_000_000)
    return irt_amount * 10


def format_persian_irt(irt_amount: int) -> str:
    """Format an integer IRT amount into human-readable Persian text (e.g., '۱۵۰٬۰۰۰ تومان')."""
    if isinstance(irt_amount, bool) or not isinstance(irt_amount, int):
        raise CurrencyError("IRT amount must be an integer")
    sign = "-" if irt_amount < 0 else ""
    formatted = f"{abs(irt_amount):,}".replace(",", "٬").translate(PERSIAN_DIGITS)
    return f"{sign}{formatted} تومان"


@dataclass(frozen=True)
class AppSettings:
    env: str = field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    app_name: str = "فروشگاه اختصاصی VPN تلگرام"
    currency: str = "IRT"
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql+asyncpg://vpnstore:vpnstore@localhost:5432/vpnstore"
        )
    )
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "dev-only-secret-key-change-in-prod-32b"))
    encryption_master_key: str = field(
        default_factory=lambda: os.getenv("ENCRYPTION_MASTER_KEY", "dev-only-master-key-change-in-prod-32b")
    )
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "000000:DEV_TOKEN"))
    telegram_webhook_secret: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_WEBHOOK_SECRET", "dev-webhook-secret")
    )
    allow_fake_providers: bool = field(
        default_factory=lambda: os.getenv("ALLOW_FAKE_PROVIDERS", "true").lower() == "true"
    )
    membership_cache_ttl_seconds: int = 86400
    quote_ttl_seconds: int = 900

    def validate_startup(self) -> None:
        """Fail safely if production configuration is missing or insecure."""
        if self.env not in {"development", "test", "staging", "production"}:
            raise ValueError(f"Unsupported APP_ENV: {self.env}")
        if self.currency != "IRT":
            raise ValueError("Application currency must be strictly 'IRT'")
        if self.env == "production":
            if "dev-only" in self.secret_key or len(self.secret_key) < 32:
                raise RuntimeError("Production requires a strong SECRET_KEY (>= 32 chars)")
            if "dev-only" in self.encryption_master_key or len(self.encryption_master_key) < 32:
                raise RuntimeError("Production requires a strong ENCRYPTION_MASTER_KEY (>= 32 chars)")
            if self.allow_fake_providers:
                raise RuntimeError("Fake VPN or payment providers are strictly forbidden in production")
            if self.telegram_bot_token.endswith("DEV_TOKEN"):
                raise RuntimeError("Valid TELEGRAM_BOT_TOKEN is required in production")

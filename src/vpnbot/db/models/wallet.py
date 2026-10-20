"""Wallet accounts, append-only financial ledger and top-up requests."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vpnbot.db.base import Base, TimestampMixin, enum_type, utcnow
from vpnbot.db.enums import LedgerKind, PaymentMethod, PaymentStatus


class WalletAccount(Base):
    __tablename__ = "wallet_accounts"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id"), unique=True, nullable=False
    )
    balance_irt: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (sa.CheckConstraint("balance_irt >= 0", name="ck_wallet_nonneg"),)

    entries: Mapped[list[LedgerEntry]] = relationship(lazy="noload")


class LedgerEntry(Base):
    """Append-only. Updates and deletes are blocked by a DB rule."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("wallet_accounts.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(sa.BigInteger, sa.ForeignKey("users.id"), nullable=False)
    kind: Mapped[LedgerKind] = mapped_column(enum_type(LedgerKind), nullable=False)
    amount_irt: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)  # signed
    balance_after_irt: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.String(500))
    ref_type: Mapped[str | None] = mapped_column(sa.String(32))
    ref_id: Mapped[str | None] = mapped_column(sa.String(64))
    actor_type: Mapped[str] = mapped_column(sa.String(16), default="SYSTEM", nullable=False)
    actor_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    idempotency_key: Mapped[str] = mapped_column(sa.String(120), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint("amount_irt <> 0", name="ck_ledger_nonzero"),
        sa.Index("ix_ledger_user_created", "user_id", "created_at"),
    )


class WalletTopUp(Base, TimestampMixin):
    """A customer's request to load their wallet, reviewed like a payment.

    Mirrors the order ``payments`` shape (method, evidence, snapshot,
    review fields) but credits the wallet ledger on approval instead of
    releasing an order. The ledger idempotency key ``topup:<id>`` makes a
    double approval credit exactly once.
    """

    __tablename__ = "wallet_topups"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id"), nullable=False, index=True
    )
    method: Mapped[PaymentMethod] = mapped_column(enum_type(PaymentMethod), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        enum_type(PaymentStatus), default=PaymentStatus.AWAITING_PAYMENT_DETAILS, nullable=False
    )
    amount_irt: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    txid: Mapped[str | None] = mapped_column(sa.String(200), unique=True)
    receipt_file_id: Mapped[str | None] = mapped_column(sa.String(300))
    receipt_file_type: Mapped[str | None] = mapped_column(sa.String(24))
    instructions_snapshot: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    reviewed_by_admin_id: Mapped[int | None] = mapped_column(
        sa.Integer, sa.ForeignKey("admins.id", ondelete="SET NULL")
    )
    rejection_reason: Mapped[str | None] = mapped_column(sa.String(500))
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

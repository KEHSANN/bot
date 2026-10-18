"""Orders, payments and payment review history."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vpnbot.db.base import Base, TimestampMixin, enum_type
from vpnbot.db.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(sa.String(24), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, sa.ForeignKey("users.id"), nullable=False)

    product_id: Mapped[int] = mapped_column(sa.Integer, sa.ForeignKey("products.id"), nullable=False)
    plan_id: Mapped[int | None] = mapped_column(sa.Integer, sa.ForeignKey("plans.id"))

    # Immutable purchase snapshot: history must survive catalog edits.
    product_title_snapshot: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    plan_title_snapshot: Mapped[str | None] = mapped_column(sa.String(200))
    specs_snapshot: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    unit_price_irt: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    total_irt: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)

    status: Mapped[OrderStatus] = mapped_column(
        enum_type(OrderStatus), default=OrderStatus.PENDING_PAYMENT, nullable=False, index=True
    )
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        enum_type(DeliveryStatus), default=DeliveryStatus.NOT_STARTED, nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    fulfilled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(sa.Text)

    payment: Mapped[Payment | None] = relationship(
        back_populates="order", lazy="selectin", uselist=False
    )

    __table_args__ = (sa.Index("ix_orders_user_created", "user_id", "created_at"),)

    @property
    def reference(self) -> str:
        return self.public_id


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    method: Mapped[PaymentMethod] = mapped_column(enum_type(PaymentMethod), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        enum_type(PaymentStatus),
        default=PaymentStatus.AWAITING_PAYMENT_DETAILS,
        nullable=False,
    )
    amount_irt: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)

    # Crypto evidence
    txid: Mapped[str | None] = mapped_column(sa.String(200), unique=True)
    crypto_asset: Mapped[str | None] = mapped_column(sa.String(24))
    crypto_network: Mapped[str | None] = mapped_column(sa.String(48))

    # Card evidence
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
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(80), unique=True)

    order: Mapped[Order] = relationship(back_populates="payment", lazy="noload")

    __table_args__ = (
        sa.CheckConstraint("amount_irt >= 0", name="ck_payment_amount_nonneg"),
        sa.CheckConstraint(
            "method <> 'CRYPTO' OR txid IS NULL OR length(txid) >= 8",
            name="ck_payment_txid_len",
        ),
        sa.Index("ix_payments_status_submitted", "status", "submitted_at"),
    )


class PaymentReview(Base):
    __tablename__ = "payment_reviews"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admin_id: Mapped[int] = mapped_column(sa.Integer, sa.ForeignKey("admins.id"), nullable=False)
    decision: Mapped[str] = mapped_column(sa.String(24), nullable=False)  # APPROVED / REJECTED
    reason: Mapped[str | None] = mapped_column(sa.String(500))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

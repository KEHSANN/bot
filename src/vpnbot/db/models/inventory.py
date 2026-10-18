"""VPN inventory: the single-use configuration/subscription pair.

Invariants enforced here and in migrations/triggers:
  * ``config_fingerprint`` is globally unique -> a configuration can never be
    imported twice as sellable stock.
  * ``status`` only ever moves AVAILABLE -> RESERVED -> DELIVERED (or -> DISABLED
    before assignment). A DB trigger forbids leaving DELIVERED.
  * ``assigned_order_id`` is unique among non-null rows: an order owns at most
    one pair, and a pair belongs to at most one order.
  * DELIVERED rows keep their assignment forever; refunds never return them.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from vpnbot.db.base import Base, TimestampMixin, enum_type
from vpnbot.db.enums import InventoryStatus


class InventoryItem(Base, TimestampMixin):
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("products.id"), nullable=False, index=True
    )
    plan_id: Mapped[int | None] = mapped_column(sa.Integer, sa.ForeignKey("plans.id"), index=True)

    # Encrypted at rest; fingerprints are keyed HMACs (no raw value stored).
    config_cipher: Mapped[str] = mapped_column(sa.Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    sub_url_cipher: Mapped[str | None] = mapped_column(sa.Text)
    sub_url_fingerprint: Mapped[str | None] = mapped_column(sa.String(64), unique=True)

    status: Mapped[InventoryStatus] = mapped_column(
        enum_type(InventoryStatus), default=InventoryStatus.AVAILABLE, nullable=False
    )
    assigned_order_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("orders.id", ondelete="RESTRICT")
    )
    reserved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(sa.String(500))
    source_batch_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)

    __table_args__ = (
        sa.CheckConstraint(
            "(sub_url_cipher IS NULL) = (sub_url_fingerprint IS NULL)",
            name="ck_inventory_suburl_pair",
        ),
        sa.CheckConstraint(
            "status <> 'AVAILABLE' OR (assigned_order_id IS NULL AND delivered_at IS NULL)",
            name="ck_inventory_available_unassigned",
        ),
        sa.CheckConstraint(
            "status <> 'DELIVERED' OR (assigned_order_id IS NOT NULL AND delivered_at IS NOT NULL)",
            name="ck_inventory_delivered_assigned",
        ),
        sa.Index("ix_inventory_status_pool", "product_id", "plan_id", "status", "id"),
        sa.Index("ix_inventory_order", "assigned_order_id"),
    )

    @property
    def has_subscription(self) -> bool:
        return self.sub_url_cipher is not None


class InventoryAssignment(Base, TimestampMixin):
    """Permanent, one-per-order binding of a pair to a customer purchase."""

    __tablename__ = "inventory_assignments"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    inventory_item_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("inventory_items.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(sa.BigInteger, sa.ForeignKey("users.id"), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint("inventory_item_id", name="uq_assignment_inventory"),
        sa.UniqueConstraint("order_id", name="uq_assignment_order"),
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inventory_item_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("inventory_items.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(32), nullable=False)  # SENT / FAILED / UNCERTAIN
    error_code: Mapped[str | None] = mapped_column(sa.String(120))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (sa.UniqueConstraint("order_id", "attempt_no", name="uq_attempt_no"),)

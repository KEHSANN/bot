from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PurchaseQuote(Base):
    __tablename__ = "purchase_quotes"
    __table_args__ = (
        UniqueConstraint("user_id", "request_key"),
        CheckConstraint("amount_minor >= 0 AND currency = 'IRT'", name="valid_money"),
        CheckConstraint("expires_at > created_at", name="positive_lifetime"),
        CheckConstraint("pricing_version > 0", name="positive_version"),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="object_snapshot"),
        Index("ix_purchase_quotes_user_created", "user_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("product_plans.id", ondelete="RESTRICT"))
    request_key: Mapped[UUID] = mapped_column()
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), server_default="IRT")
    pricing_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)

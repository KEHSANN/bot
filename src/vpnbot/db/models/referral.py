"""Referral rewards."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from vpnbot.db.base import Base, enum_type
from vpnbot.db.enums import RewardStatus


class ReferralReward(Base):
    __tablename__ = "referral_rewards"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id"), nullable=False, index=True
    )
    referred_user_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id"), nullable=False
    )
    qualifying_order_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("orders.id"), nullable=False
    )
    tier: Mapped[int] = mapped_column(sa.Integer, nullable=False)  # 1 or 2
    amount_irt: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    status: Mapped[RewardStatus] = mapped_column(
        enum_type(RewardStatus), default=RewardStatus.ISSUED, nullable=False
    )
    ledger_entry_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("ledger_entries.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        # A referred customer can generate at most one reward per tier, ever.
        sa.UniqueConstraint("referred_user_id", "tier", name="uq_reward_referred_tier"),
        # Tier 2 is once per INVITER (migration f6d94b3a8c21): under
        # concurrent fulfilment, the count-based check alone raced and
        # paid it several times.
        sa.Index(
            "uq_reward_inviter_tier2",
            "inviter_id",
            unique=True,
            postgresql_where=sa.text("tier = 2"),
        ),
    )

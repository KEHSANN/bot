"""Broadcast campaigns and per-recipient delivery state."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from vpnbot.db.base import Base, TimestampMixin, enum_type
from vpnbot.db.enums import CampaignStatus, RecipientStatus


class BroadcastCampaign(Base, TimestampMixin):
    __tablename__ = "broadcast_campaigns"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    message_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    media_file_id: Mapped[str | None] = mapped_column(sa.String(300))
    media_type: Mapped[str | None] = mapped_column(sa.String(24))
    buttons: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    audience: Mapped[str] = mapped_column(sa.String(32), default="ALL", nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        enum_type(CampaignStatus), default=CampaignStatus.DRAFT, nullable=False, index=True
    )
    created_by_admin_id: Mapped[int | None] = mapped_column(sa.Integer)
    confirmed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    total_recipients: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)


class BroadcastRecipient(Base):
    __tablename__ = "broadcast_recipients"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(sa.BigInteger, sa.ForeignKey("users.id"), nullable=False)
    telegram_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    status: Mapped[RecipientStatus] = mapped_column(
        enum_type(RecipientStatus), default=RecipientStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.String(200))
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint("campaign_id", "user_id", name="uq_campaign_recipient"),
        sa.Index("ix_recipient_status", "campaign_id", "status", "id"),
    )

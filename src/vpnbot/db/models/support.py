"""Support tickets."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from vpnbot.db.base import Base, TimestampMixin, enum_type
from vpnbot.db.enums import TicketSender, TicketStatus


class Ticket(Base, TimestampMixin):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id"), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    status: Mapped[TicketStatus] = mapped_column(
        enum_type(TicketStatus), default=TicketStatus.OPEN, nullable=False, index=True
    )
    assigned_admin_id: Mapped[int | None] = mapped_column(sa.Integer, sa.ForeignKey("admins.id"))
    last_message_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_type: Mapped[TicketSender] = mapped_column(enum_type(TicketSender), nullable=False)
    sender_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

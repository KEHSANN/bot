"""Configurable customer messages and inline keyboards (versioned).

Every editable surface has a stable ``stage`` key. A stage points at a *draft*
definition; versions are immutable snapshots that can be published and rolled
back. Handlers render the published version, falling back to code defaults when
no published version exists.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from vpnbot.db.base import Base, TimestampMixin, enum_type, utcnow
from vpnbot.db.enums import ButtonStyle, MessageType


class ScreenDefinition(Base, TimestampMixin):
    """Editable definition (draft) for one screen."""

    __tablename__ = "screens"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    stage: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    # "messages" is a list of {type,text,file_id,caption,delay_ms,enabled}
    draft_messages: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    draft_buttons: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    published_version: Mapped[int | None] = mapped_column(sa.Integer)
    is_locked: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)


class ScreenVersion(Base):
    __tablename__ = "screen_versions"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    screen_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("screens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    messages: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    buttons: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    published_by_admin_id: Mapped[int | None] = mapped_column(sa.Integer)
    published_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, nullable=False
    )
    note: Mapped[str | None] = mapped_column(sa.String(300))

    __table_args__ = (sa.UniqueConstraint("screen_id", "version", name="uq_screen_version"),)


class MessageDispatchLog(Base):
    """Outcome of rendering one screen per customer (no sensitive payloads)."""

    __tablename__ = "message_dispatch_log"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    stage: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    user_telegram_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    version: Mapped[int | None] = mapped_column(sa.Integer)
    outcome: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    error_code: Mapped[str | None] = mapped_column(sa.String(120))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


__all__ = [
    "ButtonStyle",
    "MessageDispatchLog",
    "MessageType",
    "ScreenDefinition",
    "ScreenVersion",
    "enum_type",
]

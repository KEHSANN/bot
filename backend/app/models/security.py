from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        Index("ix_admin_sessions_admin_expires", "admin_user_id", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    admin_user_id: Mapped[UUID] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminToken(Base):
    __tablename__ = "admin_tokens"
    __table_args__ = (
        CheckConstraint("kind IN ('access','refresh')", name="valid_kind"),
        CheckConstraint("token_digest ~ '^[0-9a-f]{64}$'", name="sha256_digest"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        Index("ix_admin_tokens_session_kind", "session_id", "kind"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("admin_sessions.id", ondelete="RESTRICT"))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("outcome IN ('success','denied','failure')", name="valid_outcome"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="object_details"),
        Index("ix_audit_logs_actor_time", "actor_id", "occurred_at"),
        Index("ix_audit_logs_action_time", "action", "occurred_at"),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    outcome: Mapped[str] = mapped_column(String(8))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[UUID] = mapped_column()
    client_hash: Mapped[str] = mapped_column(String(64))
    user_agent_hash: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class ApplicationSetting(Base):
    __tablename__ = "application_settings"
    __table_args__ = (CheckConstraint("key = 'admin_login_enabled'", name="approved_key"),)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)

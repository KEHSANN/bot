"""Audit trail, persistent jobs and settings."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from vpnbot.db.base import Base, enum_type, utcnow
from vpnbot.db.enums import AuditActor, JobKind, JobStatus


class AuditEvent(Base):
    """Append-only audit log. A DB rule blocks UPDATE/DELETE."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    actor_type: Mapped[AuditActor] = mapped_column(enum_type(AuditActor), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    action: Mapped[str] = mapped_column(sa.String(80), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(sa.String(48))
    entity_id: Mapped[str | None] = mapped_column(sa.String(64))
    data: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True
    )


class Job(Base):
    """Persistent job queue (outbox). Workers claim with SKIP LOCKED."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    kind: Mapped[JobKind] = mapped_column(enum_type(JobKind), nullable=False, index=True)
    status: Mapped[JobStatus] = mapped_column(
        enum_type(JobStatus), default=JobStatus.QUEUED, nullable=False, index=True
    )
    payload: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(160), unique=True, nullable=False)
    attempt: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, default=8, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (sa.Index("ix_jobs_claim", "status", "scheduled_at", "id"),)


class AppSetting(Base):
    """Runtime-editable settings (payment details, toggles, texts)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(sa.String(96), primary_key=True)
    value: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    updated_by_admin_id: Mapped[int | None] = mapped_column(sa.Integer)

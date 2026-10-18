"""Declarative base, shared mixins and enum column helper."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: sa.JSON,
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )


def enum_type(enum_cls: type[enum.Enum], name: str | None = None) -> sa.Enum:
    """Stored as VARCHAR + CHECK constraint (no native PG enum headaches)."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=48,
        validate_strings=True,
        values_callable=lambda members: [m.value for m in members],
    )


def jsonb_column(**kwargs: Any) -> Any:
    from sqlalchemy.dialects.postgresql import JSONB

    return mapped_column(JSONB, **kwargs)


__all__ = ["Base", "TimestampMixin", "enum_type", "utcnow", "jsonb_column"]

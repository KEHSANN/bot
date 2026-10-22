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


def sane_id(value: object) -> bool:
    """True when *value* can be a row id in this schema.

    All id columns are PostgreSQL INTEGER (int32). Ids arrive from
    callback data a user can forge: an out-of-range value makes
    asyncpg raise an unhandled DataError instead of a clean
    "not found", so service getters check this first.
    """
    return isinstance(value, int) and 0 < value < 2**31


def enum_type(enum_cls: type[enum.Enum], name: str | None = None) -> sa.Enum:
    """Stored as VARCHAR + CHECK constraint (no native PG enum headaches)."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=48,
        validate_strings=True,
        create_constraint=True,
        values_callable=lambda members: [m.value for m in members],
    )


def jsonb_column(**kwargs: Any) -> Any:
    from sqlalchemy.dialects.postgresql import JSONB

    return mapped_column(JSONB, **kwargs)


__all__ = ["Base", "TimestampMixin", "enum_type", "utcnow", "jsonb_column"]

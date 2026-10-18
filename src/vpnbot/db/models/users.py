"""Users, administrators and roles."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vpnbot.db.base import Base, TimestampMixin, utcnow
from vpnbot.db.enums import UserStatus


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(sa.String(64))
    first_name: Mapped[str | None] = mapped_column(sa.String(128))
    last_name: Mapped[str | None] = mapped_column(sa.String(128))
    phone: Mapped[str | None] = mapped_column(sa.String(32))
    status: Mapped[UserStatus] = mapped_column(
        sa.Enum(UserStatus, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e]),
        default=UserStatus.ACTIVE,
        nullable=False,
    )
    referral_code: Mapped[str] = mapped_column(sa.String(32), unique=True, nullable=False, index=True)
    referred_by_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    referral_attributed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    is_test_recipient: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)

    referred_by: Mapped[User | None] = relationship(remote_side="User.id", lazy="noload")

    @property
    def display_name(self) -> str:
        return self.first_name or self.username or f"کاربر {self.telegram_id}"


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    key: Mapped[str] = mapped_column(sa.String(48), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(sa.JSON, default=list, nullable=False)
    is_system: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow, nullable=False)


class Admin(Base, TimestampMixin):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    role_id: Mapped[int] = mapped_column(sa.Integer, sa.ForeignKey("roles.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    granted_by_admin_id: Mapped[int | None] = mapped_column(sa.Integer, sa.ForeignKey("admins.id"))

    user: Mapped[User] = relationship(lazy="joined")
    role: Mapped[Role] = relationship(lazy="joined")

    def has(self, permission: str) -> bool:
        return self.is_active and permission in (self.role.permissions or [])

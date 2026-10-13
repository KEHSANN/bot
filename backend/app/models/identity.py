from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdentityMixin


class User(IdentityMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("telegram_id IS NULL OR telegram_id > 0", name="telegram_id_positive"),
        CheckConstraint(
            "status IN ('active','blocked','suspended','deleted')", name="valid_status"
        ),
        Index("ix_users_status_created_at", "status", "created_at"),
    )
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(256))
    language: Mapped[str] = mapped_column(String(10), server_default="fa")
    status: Mapped[str] = mapped_column(String(16), server_default="active")


class AdminUser(IdentityMixin, Base):
    __tablename__ = "admin_users"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # No password or auth/session bypass: credentials are added by the authentication phase.


class Role(IdentityMixin, Base):
    __tablename__ = "roles"
    __table_args__ = (CheckConstraint("length(code) > 0", name="nonempty_code"),)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(128))


class Permission(IdentityMixin, Base):
    __tablename__ = "permissions"
    __table_args__ = (CheckConstraint("length(code) > 0", name="nonempty_code"),)
    code: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(String(512))


class RolePermission(Base):
    __tablename__ = "role_permissions"
    role_id: Mapped[UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True
    )
    permission_id: Mapped[UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="RESTRICT"), primary_key=True
    )


class AdminRoleAssignment(Base):
    __tablename__ = "admin_role_assignments"
    admin_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"), primary_key=True
    )
    role_id: Mapped[UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserStatusHistory(IdentityMixin, Base):
    __tablename__ = "user_status_history"
    __table_args__ = (
        CheckConstraint(
            "old_status IN ('active','blocked','suspended','deleted')", name="valid_old_status"
        ),
        CheckConstraint(
            "new_status IN ('active','blocked','suspended','deleted')", name="valid_new_status"
        ),
        CheckConstraint("old_status <> new_status", name="changed_status"),
        CheckConstraint("length(reason) > 0", name="nonempty_reason"),
        Index("ix_user_status_history_user_created", "user_id", "created_at"),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    changed_by_admin_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT")
    )
    old_status: Mapped[str] = mapped_column(String(16))
    new_status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(512))

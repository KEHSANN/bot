"""Users, admin bootstrap and permission checks."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from vpnbot.db.base import utcnow
from vpnbot.db.enums import ROLE_PRESETS, Permission
from vpnbot.db.models.users import Admin, Role, User
from vpnbot.security.crypto import random_token


class NotAuthorized(Exception):
    pass


def make_referral_code(telegram_id: int) -> str:
    return f"u{telegram_id}{random_token(4).replace('-', '').replace('_', '')[:4]}"


async def upsert_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> User:
    user = (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()
    now = utcnow()
    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            referral_code=make_referral_code(telegram_id),
            last_seen_at=now,
        )
        session.add(user)
        await session.flush()
    else:
        user.username = username
        user.first_name = first_name or user.first_name
        user.last_name = last_name or user.last_name
        user.last_seen_at = now
    return user


async def bind_referral(session: AsyncSession, user: User, code: str) -> bool:
    """Attribute a new user to an inviter exactly once.

    Rules: unknown code -> False; self referral -> False; users who already have
    a referrer -> False; users with any order -> False.
    """
    if user.referred_by_id is not None:
        return False
    inviter = (
        await session.execute(select(User).where(User.referral_code == code))
    ).scalar_one_or_none()
    if inviter is None or inviter.id == user.id:
        return False
    from vpnbot.db.models.orders import Order

    has_orders = (
        await session.execute(select(func.count(Order.id)).where(Order.user_id == user.id))
    ).scalar_one()
    if has_orders:
        return False
    user.referred_by_id = inviter.id
    user.referral_attributed_at = utcnow()
    await session.flush()
    return True


async def get_admin(session: AsyncSession, telegram_id: int) -> Admin | None:
    result = await session.execute(
        select(Admin)
        .join(User, User.id == Admin.user_id)
        .options(joinedload(Admin.user), joinedload(Admin.role))
        .where(User.telegram_id == telegram_id, Admin.is_active.is_(True))
    )
    return result.scalar_one_or_none()


def admin_has(admin: Admin | None, permission: Permission | str) -> bool:
    if admin is None or not admin.is_active:
        return False
    perm = permission.value if isinstance(permission, Permission) else permission
    return perm in (admin.role.permissions or [])


def require(admin: Admin | None, permission: Permission | str) -> Admin:
    if not admin_has(admin, permission):
        raise NotAuthorized(f"permission denied: {permission}")
    return admin  # type: ignore[return-value]


async def ensure_roles(session: AsyncSession) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for key, preset in ROLE_PRESETS.items():
        role = (await session.execute(select(Role).where(Role.key == key))).scalar_one_or_none()
        if role is None:
            role = Role(
                key=key,
                title=str(preset["title"]),
                permissions=list(preset["permissions"]),  # type: ignore[arg-type]
                is_system=True,
            )
            session.add(role)
        roles[key] = role
    await session.flush()
    return roles


async def bootstrap_owner(session: AsyncSession, telegram_id: int) -> Admin:
    roles = await ensure_roles(session)
    user = await upsert_user(session, telegram_id=telegram_id)
    admin = (
        await session.execute(select(Admin).where(Admin.user_id == user.id))
    ).scalar_one_or_none()
    if admin is None:
        admin = Admin(user_id=user.id, role_id=roles["owner"].id, is_active=True)
        session.add(admin)
    else:
        admin.role_id = roles["owner"].id
        admin.is_active = True
    await session.flush()
    return admin


async def grant_admin(
    session: AsyncSession, *, actor_admin_id: int, telegram_id: int, role_key: str
) -> Admin:
    roles = await ensure_roles(session)
    if role_key not in roles:
        raise ValueError("نقش نامعتبر است")
    user = await upsert_user(session, telegram_id=telegram_id)
    admin = (
        await session.execute(select(Admin).where(Admin.user_id == user.id))
    ).scalar_one_or_none()
    if admin is None:
        admin = Admin(user_id=user.id, role_id=roles[role_key].id, granted_by_admin_id=actor_admin_id)
        session.add(admin)
    else:
        admin.role_id = roles[role_key].id
        admin.is_active = True
        admin.granted_by_admin_id = actor_admin_id
    await session.flush()
    return admin


async def revoke_admin(session: AsyncSession, admin_id: int) -> None:
    admin = await session.get(Admin, admin_id)
    if admin is not None:
        admin.is_active = False
        await session.flush()


async def inactive_since_cutoff(session: AsyncSession, hours: int) -> list[User]:
    cutoff = utcnow() - timedelta(hours=hours)
    result = await session.execute(select(User).where(User.last_seen_at >= cutoff))
    return list(result.scalars())

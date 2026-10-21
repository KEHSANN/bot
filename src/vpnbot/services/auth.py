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
    # Opaque random code. The old format embedded the telegram_id
    # itself (f"u{telegram_id}..."), so anyone who saw a user's
    # invite link learned their numeric Telegram id.
    _ = telegram_id  # kept for signature compatibility; unused
    return "u" + random_token(12).replace("-", "").replace("_", "")[:12]


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
        # Insert atomically: two first-ever updates from the same new
        # user processed concurrently both found no row and both
        # inserted — one of them crashed on the telegram_id unique
        # constraint and the update was lost.
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await session.execute(
            pg_insert(User.__table__)
            .values(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                referral_code=make_referral_code(telegram_id),
                last_seen_at=now,
            )
            .on_conflict_do_nothing(index_elements=["telegram_id"])
        )
        user = (
            await session.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one()
    else:
        # Write sparingly: this runs on EVERY update, and an
        # unconditional last_seen_at bump issues an UPDATE that holds
        # the users row lock for the whole handler transaction
        # (including Telegram HTTP calls). Only write when the
        # profile actually changed or the seen-stamp is stale (>60s).
        new_first = first_name or user.first_name
        new_last = last_name or user.last_name
        stale_seen = (
            user.last_seen_at is None
            or (now - user.last_seen_at).total_seconds() >= 60
        )
        if (
            user.username != username
            or user.first_name != new_first
            or user.last_name != new_last
            or stale_seen
        ):
            user.username = username
            user.first_name = new_first
            user.last_name = new_last
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
    # Lock both user rows (in id order, so concurrent binds can't
    # deadlock) and re-read under the lock: two users sending each
    # other /start ref links simultaneously could otherwise each pass
    # the mutual/cycle checks against the other's uncommitted state
    # and close a loop anyway.
    locked = (
        await session.execute(
            select(User)
            .where(User.id.in_([user.id, inviter.id]))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    by_id = {u.id: u for u in locked}
    user = by_id.get(user.id, user)
    inviter = by_id.get(inviter.id, inviter)
    if user.referred_by_id is not None:
        return False
    if inviter.referred_by_id == user.id:
        # Mutual invites: A invited B, now B tries to invite A back
        # before either has bought — each would earn a reward from the
        # other's first purchase. A two-cycle is not a referral.
        return False
    # Longer cycles (A -> B -> C -> A) need a chain walk: follow the
    # inviter's own referrer chain upwards; if this user appears in it,
    # binding would close a loop.
    seen: set[int] = set()
    cursor = inviter
    for _ in range(10):
        nxt = cursor.referred_by_id
        if nxt is None or nxt in seen:
            break
        if nxt == user.id:
            return False
        seen.add(nxt)
        parent = await session.get(User, nxt)
        if parent is None:
            break
        cursor = parent
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
    result = await session.execute(select(User).where(User.last_seen_at <= cutoff))
    return list(result.scalars())

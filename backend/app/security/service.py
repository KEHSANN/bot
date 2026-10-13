import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog.schemas import CategoryBody, PlanBody, ProductBody, QuoteBody
from app.core.config import Settings
from app.models import AdminRoleAssignment, AdminUser, Permission, Role, RolePermission, User
from app.models.identity import UserStatusHistory
from app.models.security import AdminSession, AdminToken, ApplicationSetting, AuditLog
from app.security.crypto import (
    DUMMY_HASH,
    digest_token,
    hash_password,
    issue_token,
    verify_password,
)
from app.security.metadata import RequestMetadata, fingerprint
from app.security.policy import PERMISSIONS
from app.security.schemas import (
    CreateAdminBody,
    GrantBody,
    PasswordBody,
    RoleBody,
    SettingBody,
    StatusBody,
    StepUpBody,
)

# Atomic fixed-window counters, shared across processes. No client-supplied Redis keys.
RATE_SCRIPT = """
local allowed = 1
for i, key in ipairs(KEYS) do
  local attempts = redis.call('INCR', key)
  if attempts == 1 then redis.call('EXPIRE', key, ARGV[1]) end
  if attempts > tonumber(ARGV[i + 1]) then allowed = 0 end
end
return allowed
"""
SECURITY_WRITE_LOCK = 71420931
ACTION_PERMISSIONS = {
    "catalog.read": "products.read",
    "catalog.create": "products.manage",
    "catalog.update": "products.manage",
    "quotes.create": "orders.manage",
    "quotes.read": "orders.read",
    "users.read": "users.read",
    "users.status": "users.manage",
    "products.read": "products.read",
    "roles.read": "roles.read",
    "roles.create": "roles.manage",
    "roles.permissions": "roles.manage",
    "admins.read": "admins.read",
    "admins.create": "admins.manage",
    "admins.roles": "admins.manage",
    "audit.read": "audit.read",
    "settings.read": "settings.read",
    "settings.change": "settings.manage",
    "sessions.revoke": "sessions.manage",
}
WRITE_ACTIONS = frozenset(
    {
        "catalog.create",
        "catalog.update",
        "users.status",
        "roles.create",
        "roles.permissions",
        "admins.create",
        "admins.roles",
        "settings.change",
        "sessions.revoke",
    }
)


class SecurityError(Exception):
    def __init__(self, status: int, reason: str, revoke_session: UUID | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.revoke_session = revoke_session


@dataclass
class Context:
    actor_id: UUID | None = None
    target_type: str = "security"
    target_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True)
class Principal:
    admin: AdminUser
    session: AdminSession
    permissions: frozenset[str]
    roles: frozenset[UUID]
    role_codes: frozenset[str]


Operation = Callable[[AsyncSession, Context], Awaitable[dict[str, Any]]]
Body = StepUpBody | QuoteBody | None


class SecurityService:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings
    ) -> None:
        self.factory, self.redis, self.settings = factory, redis, settings
        self.hash_slots = asyncio.Semaphore(4)

    async def _verify(self, encoded: str, value: str) -> bool:
        if not 1 <= len(value) <= 128:
            return False
        async with self.hash_slots:
            return await asyncio.to_thread(verify_password, encoded, value)

    async def _hash(self, value: str) -> str:
        async with self.hash_slots:
            return await asyncio.to_thread(hash_password, value)

    async def _now(self, session: AsyncSession) -> datetime:
        return cast(datetime, await session.scalar(select(func.clock_timestamp())))

    async def _rate(
        self, meta: RequestMetadata, username: str | None = None, login: bool = False
    ) -> None:
        prefix = "{vpnstore-admin-auth}:login" if login else "{vpnstore-admin-auth}:api"
        keys = [f"{prefix}:ip:{meta.client_hash}"]
        limits = [self.settings.login_ip_limit if login else self.settings.admin_ip_limit]
        window = self.settings.login_window_seconds if login else 60
        if username is not None:
            keys.append(f"{prefix}:account:{fingerprint(username)}")
            limits.append(self.settings.login_account_limit)
        allowed = await cast(
            Awaitable[Any], self.redis.eval(RATE_SCRIPT, len(keys), *keys, window, *limits)
        )
        if type(allowed) is not int or allowed != 1:
            raise SecurityError(429, "rate_limited")

    def _audit(
        self,
        session: AsyncSession,
        action: str,
        meta: RequestMetadata,
        ctx: Context,
        outcome: str,
        reason: str | None = None,
    ) -> None:
        session.add(
            AuditLog(
                actor_id=ctx.actor_id,
                action=action,
                target_type=ctx.target_type,
                target_id=ctx.target_id,
                outcome=outcome,
                reason_code=reason or ctx.reason,
                request_id=meta.request_id,
                client_hash=meta.client_hash,
                user_agent_hash=meta.user_agent_hash,
                details={**ctx.details, "request_method": meta.method, "route": meta.route},
            )
        )

    async def _revoke(self, session: AsyncSession, session_id: UUID) -> None:
        row = await session.scalar(
            select(AdminSession).where(AdminSession.id == session_id).with_for_update()
        )
        if row is not None:
            now = await self._now(session)
            row.revoked_at = now
            await session.execute(
                update(AdminToken).where(AdminToken.session_id == session_id).values(revoked_at=now)
            )

    async def _execute(
        self,
        action: str,
        meta: RequestMetadata,
        operation: Operation,
        *,
        locked: bool = False,
        target: UUID | None = None,
    ) -> dict[str, Any]:
        ctx = Context(target_id=str(target) if target else None)
        try:
            async with self.factory() as session, session.begin():
                if locked:
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": SECURITY_WRITE_LOCK}
                    )
                result = await operation(session, ctx)
                self._audit(session, action, meta, ctx, "success")
            # Never issue credentials before the success audit and state commit together.
            return result
        except SecurityError as exc:
            try:
                async with self.factory() as session, session.begin():
                    if exc.revoke_session:
                        await self._revoke(session, exc.revoke_session)
                    self._audit(
                        session,
                        action,
                        meta,
                        ctx,
                        "failure" if exc.reason == "credentials_invalid" else "denied",
                        exc.reason,
                    )
            except SQLAlchemyError:
                # Restrictive replay revocation must survive an audit-insert outage when DB is up.
                if exc.revoke_session:
                    try:
                        async with self.factory() as session, session.begin():
                            await self._revoke(session, exc.revoke_session)
                    except SQLAlchemyError:
                        pass
                raise SecurityError(503, "audit_unavailable") from None
            raise
        except (SQLAlchemyError, RedisError):
            try:
                async with self.factory() as session, session.begin():
                    self._audit(session, action, meta, ctx, "failure", "dependency_unavailable")
            except SQLAlchemyError:
                pass  # Impossible to durably audit when PostgreSQL itself is unavailable.
            raise SecurityError(503, "dependency_unavailable") from None

    async def _tokens(self, db: AsyncSession, row: AdminSession) -> dict[str, Any]:
        now = await self._now(db)
        access, access_digest = issue_token("access")
        refresh, refresh_digest = issue_token("refresh")
        access_expiry = min(
            row.expires_at, now + timedelta(seconds=self.settings.access_token_seconds)
        )
        refresh_expiry = min(
            row.expires_at, now + timedelta(seconds=self.settings.refresh_token_seconds)
        )
        db.add_all(
            [
                AdminToken(
                    session_id=row.id,
                    kind="access",
                    token_digest=access_digest,
                    expires_at=access_expiry,
                ),
                AdminToken(
                    session_id=row.id,
                    kind="refresh",
                    token_digest=refresh_digest,
                    expires_at=refresh_expiry,
                ),
            ]
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": max(0, int((access_expiry - now).total_seconds())),
            "session_id": str(row.id),
        }

    async def login(self, username: str, password: str, meta: RequestMetadata) -> dict[str, Any]:
        async def operation(db: AsyncSession, ctx: Context) -> dict[str, Any]:
            ctx.target_type, ctx.target_id = "account", fingerprint(username)
            await self._rate(meta, username, login=True)
            enabled = await db.scalar(
                select(ApplicationSetting.enabled).where(
                    ApplicationSetting.key == "admin_login_enabled"
                )
            )
            admin = await db.scalar(
                select(AdminUser).where(AdminUser.username == username).with_for_update()
            )
            if admin:
                ctx.target_id = str(admin.id)
            encoded = admin.password_hash if admin and admin.password_hash else DUMMY_HASH
            valid = await self._verify(encoded, password)
            user = await db.get(User, admin.user_id) if admin else None
            if (
                not valid
                or not admin
                or not admin.enabled
                or not user
                or user.status != "active"
                or enabled is not True
            ):
                raise SecurityError(401, "credentials_invalid")
            ctx.actor_id = admin.id
            now = await self._now(db)
            row = AdminSession(
                admin_user_id=admin.id,
                expires_at=now + timedelta(seconds=self.settings.session_seconds),
            )
            db.add(row)
            await db.flush()
            ctx.target_type, ctx.target_id = "session", str(row.id)
            return await self._tokens(db, row)

        return await self._execute("auth.login", meta, operation)

    async def _principal(self, db: AsyncSession, raw: str, ctx: Context) -> Principal:
        try:
            digest = digest_token(raw, "access")
        except ValueError:
            raise SecurityError(401, "invalid_session") from None
        token = await db.scalar(
            select(AdminToken).where(AdminToken.token_digest == digest, AdminToken.kind == "access")
        )
        if token is None:
            raise SecurityError(401, "invalid_session")
        row = await db.scalar(
            select(AdminSession).where(AdminSession.id == token.session_id).with_for_update()
        )
        await db.refresh(
            token
        )  # A concurrent rotation may have revoked it while acquiring the lock.
        now = await self._now(db)
        if (
            not row
            or row.revoked_at
            or token.revoked_at
            or token.expires_at <= now
            or row.expires_at <= now
        ):
            raise SecurityError(401, "invalid_session")
        admin = await db.get(AdminUser, row.admin_user_id)
        user = await db.get(User, admin.user_id) if admin else None
        ctx.actor_id = admin.id if admin else None
        if not admin or not admin.enabled or not user or user.status != "active":
            raise SecurityError(401, "invalid_session")
        roles = (
            await db.scalars(
                select(Role)
                .join(AdminRoleAssignment, AdminRoleAssignment.role_id == Role.id)
                .where(AdminRoleAssignment.admin_user_id == admin.id)
            )
        ).all()
        permissions = frozenset(
            (
                await db.scalars(
                    select(Permission.code)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .join(
                        AdminRoleAssignment, AdminRoleAssignment.role_id == RolePermission.role_id
                    )
                    .where(AdminRoleAssignment.admin_user_id == admin.id)
                )
            ).all()
        )
        return Principal(
            admin,
            row,
            permissions,
            frozenset(r.id for r in roles),
            frozenset(r.code for r in roles),
        )

    def _require(self, principal: Principal, permission: str) -> None:
        if permission not in principal.permissions:
            raise SecurityError(403, "permission_denied")

    async def _step_up(self, principal: Principal, body: StepUpBody, ctx: Context) -> None:
        if not await self._verify(
            principal.admin.password_hash or DUMMY_HASH, body.current_password.get_secret_value()
        ):
            raise SecurityError(403, "step_up_failed")
        ctx.reason = body.reason

    async def refresh(self, raw: str, meta: RequestMetadata) -> dict[str, Any]:
        async def operation(db: AsyncSession, ctx: Context) -> dict[str, Any]:
            await self._rate(meta)
            try:
                digest = digest_token(raw, "refresh")
            except ValueError:
                raise SecurityError(401, "invalid_session") from None
            token = await db.scalar(
                select(AdminToken).where(
                    AdminToken.token_digest == digest, AdminToken.kind == "refresh"
                )
            )
            if not token:
                raise SecurityError(401, "invalid_session")
            row = await db.scalar(
                select(AdminSession).where(AdminSession.id == token.session_id).with_for_update()
            )
            await db.refresh(token)
            now = await self._now(db)
            if not row or row.revoked_at:
                raise SecurityError(401, "invalid_session")
            ctx.actor_id, ctx.target_type, ctx.target_id = row.admin_user_id, "session", str(row.id)
            if token.used_at:
                raise SecurityError(401, "refresh_reuse", revoke_session=row.id)
            admin = await db.get(AdminUser, row.admin_user_id)
            user = await db.get(User, admin.user_id) if admin else None
            if (
                token.revoked_at
                or token.expires_at <= now
                or row.expires_at <= now
                or not admin
                or not admin.enabled
                or not user
                or user.status != "active"
            ):
                raise SecurityError(401, "invalid_session")
            token.used_at = now
            token.revoked_at = now
            await db.execute(
                update(AdminToken)
                .where(AdminToken.session_id == row.id, AdminToken.kind == "access")
                .values(revoked_at=now)
            )
            return await self._tokens(db, row)

        return await self._execute("auth.refresh", meta, operation)

    async def identity(self, raw: str, meta: RequestMetadata) -> dict[str, Any]:
        async def operation(db: AsyncSession, ctx: Context) -> dict[str, Any]:
            await self._rate(meta)
            principal = await self._principal(db, raw, ctx)
            return {
                "id": str(principal.admin.id),
                "username": principal.admin.username,
                "permissions": sorted(principal.permissions),
                "roles": sorted(principal.role_codes),
            }

        return await self._execute("auth.identity", meta, operation)

    async def logout(self, raw: str, meta: RequestMetadata) -> dict[str, Any]:
        async def operation(db: AsyncSession, ctx: Context) -> dict[str, Any]:
            await self._rate(meta)
            principal = await self._principal(db, raw, ctx)
            ctx.target_type, ctx.target_id = "session", str(principal.session.id)
            await self._revoke(db, principal.session.id)
            return {"revoked": True}

        return await self._execute("auth.logout", meta, operation)

    async def change_password(
        self, raw: str, body: PasswordBody, meta: RequestMetadata
    ) -> dict[str, Any]:
        async def operation(db: AsyncSession, ctx: Context) -> dict[str, Any]:
            await self._rate(meta)
            principal = await self._principal(db, raw, ctx)
            await self._step_up(principal, body, ctx)
            # Serialize credential changes with concurrent logins before revoking sessions.
            admin = await db.scalar(
                select(AdminUser).where(AdminUser.id == principal.admin.id).with_for_update()
            )
            assert admin
            admin.password_hash = await self._hash(body.new_password.get_secret_value())
            for sid in (
                await db.scalars(
                    select(AdminSession.id)
                    .where(AdminSession.admin_user_id == admin.id)
                    .order_by(AdminSession.id)
                )
            ).all():
                await self._revoke(db, sid)
            ctx.target_type, ctx.target_id = "administrator", str(admin.id)
            return {"changed": True, "sessions_revoked": True}

        return await self._execute("auth.password_change", meta, operation, locked=True)

    async def record_invalid(self, meta: RequestMetadata, action: str) -> int:
        async def operation(db: AsyncSession, ctx: Context) -> dict[str, Any]:
            await self._rate(meta)
            raise SecurityError(422, "invalid_input")

        try:
            await self._execute(action, meta, operation)
        except SecurityError as exc:
            return exc.status
        return 422

    async def _delegable_roles(
        self, db: AsyncSession, ids: list[UUID], principal: Principal
    ) -> list[Role]:
        roles = list((await db.scalars(select(Role).where(Role.id.in_(ids)))).all())
        if len(roles) != len(set(ids)):
            raise SecurityError(404, "role_not_found")
        if (
            any(r.code == "super_admin" for r in roles)
            and "super_admin" not in principal.role_codes
        ):
            raise SecurityError(403, "privilege_escalation_denied")
        permissions = set(
            (
                await db.scalars(
                    select(Permission.code)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .where(RolePermission.role_id.in_(ids))
                )
            ).all()
        )
        if not permissions <= principal.permissions:
            raise SecurityError(403, "privilege_escalation_denied")
        return roles

    async def _last_super(self, db: AsyncSession, target: AdminUser) -> None:
        is_super = await db.scalar(
            select(Role.id)
            .join(AdminRoleAssignment, AdminRoleAssignment.role_id == Role.id)
            .where(Role.code == "super_admin", AdminRoleAssignment.admin_user_id == target.id)
        )
        if is_super:
            total = await db.scalar(
                select(func.count())
                .select_from(AdminUser)
                .join(User, User.id == AdminUser.user_id)
                .join(AdminRoleAssignment, AdminRoleAssignment.admin_user_id == AdminUser.id)
                .join(Role, Role.id == AdminRoleAssignment.role_id)
                .where(
                    Role.code == "super_admin", AdminUser.enabled.is_(True), User.status == "active"
                )
            )
            if (total or 0) <= 1:
                raise SecurityError(409, "last_super_administrator")

    async def admin_action(
        self,
        raw: str,
        meta: RequestMetadata,
        action: str,
        body: Body = None,
        target: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
        kind: str = "",
    ) -> dict[str, Any]:
        async def operation(db: AsyncSession, ctx: Context) -> dict[str, Any]:
            await self._rate(meta)
            principal = await self._principal(db, raw, ctx)
            permission = ACTION_PERMISSIONS.get(action)
            if not permission:
                raise SecurityError(403, "permission_denied")
            self._require(principal, permission)
            if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
                raise SecurityError(422, "invalid_input")
            if action in WRITE_ACTIONS:
                assert isinstance(body, StepUpBody)
                await self._step_up(principal, body, ctx)
            if action.startswith("catalog."):
                from app.catalog.service import catalog_operation

                if body is not None and not isinstance(body, (CategoryBody, ProductBody, PlanBody)):
                    raise SecurityError(422, "invalid_input")
                return await catalog_operation(db, ctx, action, body, kind, target, limit, offset)
            if action.startswith("quotes."):
                from app.catalog.service import quote_operation

                if body is not None and not isinstance(body, QuoteBody):
                    raise SecurityError(422, "invalid_input")
                if action == "quotes.create" and body is None:
                    raise SecurityError(422, "invalid_input")
                return await quote_operation(
                    db, ctx, principal, body, target, self.settings.quote_seconds
                )
            if action == "users.read":
                user_rows = (
                    await db.scalars(
                        select(User)
                        .order_by(User.created_at.desc(), User.id)
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
                return {
                    "items": [
                        {"id": str(u.id), "display_name": u.display_name, "status": u.status}
                        for u in user_rows
                    ]
                }
            if action == "products.read":
                from app.models import Product

                product_rows = (
                    await db.scalars(
                        select(Product)
                        .order_by(Product.created_at.desc(), Product.id)
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
                return {
                    "items": [
                        {"id": str(p.id), "title": p.title, "enabled": p.enabled}
                        for p in product_rows
                    ]
                }
            if action == "admins.read":
                admin_rows = (
                    await db.scalars(
                        select(AdminUser)
                        .order_by(AdminUser.created_at.desc(), AdminUser.id)
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
                return {
                    "items": [
                        {"id": str(a.id), "username": a.username, "enabled": a.enabled}
                        for a in admin_rows
                    ]
                }
            if action == "roles.read":
                roles = (
                    await db.scalars(
                        select(Role)
                        .order_by(Role.is_system.desc(), Role.code)
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
                grants = (
                    await db.execute(
                        select(RolePermission.role_id, Permission.code).join(
                            Permission, Permission.id == RolePermission.permission_id
                        )
                    )
                ).all()
                return {
                    "permissions": sorted(PERMISSIONS),
                    "items": [
                        {
                            "id": str(r.id),
                            "code": r.code,
                            "title": r.title,
                            "is_system": r.is_system,
                            "permissions": sorted(p for rid, p in grants if rid == r.id),
                        }
                        for r in roles
                    ],
                }
            if action in {"roles.create", "roles.permissions"}:
                assert isinstance(body, RoleBody)
                if not set(body.permissions) <= principal.permissions:
                    raise SecurityError(403, "privilege_escalation_denied")
                if action == "roles.create":
                    if body.code in {r.code for r in (await db.scalars(select(Role))).all()}:
                        raise SecurityError(409, "role_exists")
                    role = Role(code=body.code, title=body.title, is_system=False)
                    db.add(role)
                    await db.flush()
                    before: list[str] = []
                else:
                    candidate_role = await db.get(Role, target)
                    if not candidate_role:
                        raise SecurityError(404, "role_not_found")
                    role = candidate_role
                    if role.is_system or role.id in principal.roles:
                        raise SecurityError(403, "protected_role")
                    before = list(
                        (
                            await db.scalars(
                                select(Permission.code)
                                .join(RolePermission, RolePermission.permission_id == Permission.id)
                                .where(RolePermission.role_id == role.id)
                            )
                        ).all()
                    )
                    if not set(before) <= principal.permissions:
                        raise SecurityError(403, "privilege_escalation_denied")
                    if role.code != body.code:
                        raise SecurityError(409, "role_code_is_stable")
                    role.title = body.title
                    await db.execute(
                        delete(RolePermission).where(RolePermission.role_id == role.id)
                    )
                ids = (
                    await db.scalars(
                        select(Permission.id).where(Permission.code.in_(body.permissions))
                    )
                ).all()
                db.add_all(RolePermission(role_id=role.id, permission_id=pid) for pid in ids)
                ctx.target_type, ctx.target_id = "role", str(role.id)
                ctx.details = {
                    "before_permissions": sorted(before),
                    "after_permissions": sorted(body.permissions),
                }
                return {"id": str(role.id)}
            if action == "admins.create":
                assert isinstance(body, CreateAdminBody)
                user = await db.get(User, body.user_id)
                if not user or user.status != "active":
                    raise SecurityError(404, "active_user_not_found")
                if await db.scalar(
                    select(AdminUser.id).where(
                        (AdminUser.user_id == user.id) | (AdminUser.username == body.username)
                    )
                ):
                    raise SecurityError(409, "administrator_exists")
                roles = await self._delegable_roles(db, body.role_ids, principal)
                created_admin = AdminUser(
                    user_id=user.id,
                    username=body.username,
                    enabled=True,
                    password_hash=await self._hash(body.new_password.get_secret_value()),
                )
                db.add(created_admin)
                await db.flush()
                db.add_all(
                    AdminRoleAssignment(admin_user_id=created_admin.id, role_id=r.id) for r in roles
                )
                ctx.target_type, ctx.target_id = "administrator", str(created_admin.id)
                ctx.details = {"role_ids": sorted(str(r.id) for r in roles)}
                return {"id": str(created_admin.id), "username": created_admin.username}
            if action == "admins.roles":
                assert isinstance(body, GrantBody)
                assigned_admin = await db.get(AdminUser, target)
                if not assigned_admin:
                    raise SecurityError(404, "administrator_not_found")
                if assigned_admin.id == principal.admin.id:
                    raise SecurityError(403, "self_role_change_denied")
                roles = await self._delegable_roles(db, body.role_ids, principal)
                before_role_ids = list(
                    (
                        await db.scalars(
                            select(AdminRoleAssignment.role_id).where(
                                AdminRoleAssignment.admin_user_id == assigned_admin.id
                            )
                        )
                    ).all()
                )
                await self._delegable_roles(db, before_role_ids, principal)
                if not any(r.code == "super_admin" for r in roles):
                    await self._last_super(db, assigned_admin)
                await db.execute(
                    delete(AdminRoleAssignment).where(
                        AdminRoleAssignment.admin_user_id == assigned_admin.id
                    )
                )
                db.add_all(
                    AdminRoleAssignment(admin_user_id=assigned_admin.id, role_id=r.id)
                    for r in roles
                )
                ctx.target_type, ctx.target_id = "administrator", str(assigned_admin.id)
                ctx.details = {
                    "before_role_ids": sorted(map(str, before_role_ids)),
                    "after_role_ids": sorted(str(r.id) for r in roles),
                }
                return {"updated": True}
            if action == "users.status":
                assert isinstance(body, StatusBody)
                user = await db.get(User, target)
                if not user:
                    raise SecurityError(404, "user_not_found")
                target_admin = await db.scalar(
                    select(AdminUser).where(AdminUser.user_id == user.id)
                )
                if target_admin:
                    self._require(principal, "admins.manage")
                    if target_admin.id == principal.admin.id:
                        raise SecurityError(403, "self_status_change_denied")
                    if body.status != "active":
                        await self._last_super(db, target_admin)
                        for sid in (
                            await db.scalars(
                                select(AdminSession.id)
                                .where(AdminSession.admin_user_id == target_admin.id)
                                .order_by(AdminSession.id)
                            )
                        ).all():
                            await self._revoke(db, sid)
                ctx.details = {"before_status": user.status, "after_status": body.status}
                if user.status != body.status:
                    db.add(
                        UserStatusHistory(
                            user_id=user.id,
                            changed_by_admin_id=principal.admin.id,
                            old_status=user.status,
                            new_status=body.status,
                            reason=body.reason,
                        )
                    )
                    user.status = body.status
                ctx.target_type, ctx.target_id = "user", str(user.id)
                return {"status": body.status}
            if action == "sessions.revoke":
                session = await db.get(AdminSession, target)
                if not session:
                    raise SecurityError(404, "session_not_found")
                await self._revoke(db, session.id)
                ctx.target_type, ctx.target_id = "session", str(session.id)
                return {"revoked": True}
            if action == "audit.read":
                audit_rows = (
                    await db.scalars(
                        select(AuditLog)
                        .order_by(AuditLog.occurred_at.desc(), AuditLog.id)
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
                return {
                    "items": [
                        {
                            "id": str(a.id),
                            "actor_id": str(a.actor_id) if a.actor_id else None,
                            "action": a.action,
                            "target_type": a.target_type,
                            "target_id": a.target_id,
                            "timestamp": a.occurred_at.isoformat(),
                            "outcome": a.outcome,
                            "request_id": str(a.request_id),
                            "reason_code": a.reason_code,
                            "client_hash": a.client_hash,
                            "user_agent_hash": a.user_agent_hash,
                            "details": a.details,
                        }
                        for a in audit_rows
                    ]
                }
            if action == "settings.read":
                value = await db.scalar(
                    select(ApplicationSetting.enabled).where(
                        ApplicationSetting.key == "admin_login_enabled"
                    )
                )
                return {"admin_login_enabled": value}
            if action == "settings.change":
                assert isinstance(body, SettingBody)
                row = await db.get(ApplicationSetting, "admin_login_enabled")
                if not row:
                    raise SecurityError(503, "setting_unavailable")
                ctx.target_type, ctx.target_id = "setting", row.key
                ctx.details = {"before_enabled": row.enabled, "after_enabled": body.enabled}
                row.enabled = body.enabled
                return {"admin_login_enabled": row.enabled}
            raise SecurityError(403, "permission_denied")

        return await self._execute(
            "admin." + action,
            meta,
            operation,
            locked=action in WRITE_ACTIONS or action == "quotes.create",
            target=target,
        )

    async def bootstrap(
        self, username: str, password: str, meta: RequestMetadata
    ) -> dict[str, Any]:
        """Host-operator-only entry point. Never exposed as an HTTP route."""
        from pydantic import SecretStr, ValidationError

        from app.security.schemas import LoginBody

        async def operation(db: AsyncSession, ctx: Context) -> dict[str, Any]:
            try:
                LoginBody(username=username, password=SecretStr(password))
            except ValidationError:
                raise SecurityError(422, "invalid_input") from None
            existing = await db.scalar(
                select(AdminUser.id)
                .join(AdminRoleAssignment, AdminRoleAssignment.admin_user_id == AdminUser.id)
                .join(Role, Role.id == AdminRoleAssignment.role_id)
                .where(Role.code == "super_admin", AdminUser.enabled.is_(True))
            )
            if existing:
                raise SecurityError(409, "bootstrap_already_completed")
            if await db.scalar(select(AdminUser.id).where(AdminUser.username == username)):
                raise SecurityError(409, "administrator_exists")
            role = await db.scalar(select(Role).where(Role.code == "super_admin"))
            if not role:
                raise SecurityError(503, "policy_missing")
            try:
                encoded = await self._hash(password)
            except ValueError:
                raise SecurityError(422, "invalid_input") from None
            user = User(display_name="مدیر سامانه")
            db.add(user)
            await db.flush()
            admin = AdminUser(
                user_id=user.id, username=username, password_hash=encoded, enabled=True
            )
            db.add(admin)
            await db.flush()
            db.add(AdminRoleAssignment(admin_user_id=admin.id, role_id=role.id))
            ctx.target_type, ctx.target_id = "administrator", str(admin.id)
            return {"id": str(admin.id)}

        return await self._execute("auth.bootstrap", meta, operation, locked=True)

    async def recover(
        self, username: str, password: str, meta: RequestMetadata, restore_login: bool = False
    ) -> dict[str, Any]:
        """Explicit trusted host-operator recovery; not an HTTP endpoint or a role-grant bypass."""

        async def operation(db: AsyncSession, ctx: Context) -> dict[str, Any]:
            admin = await db.scalar(
                select(AdminUser).where(AdminUser.username == username).with_for_update()
            )
            if not admin:
                raise SecurityError(404, "administrator_not_found")
            try:
                encoded = await self._hash(password)
            except ValueError:
                raise SecurityError(422, "invalid_input") from None
            admin.password_hash = encoded
            for sid in (
                await db.scalars(
                    select(AdminSession.id)
                    .where(AdminSession.admin_user_id == admin.id)
                    .order_by(AdminSession.id)
                )
            ).all():
                await self._revoke(db, sid)
            if restore_login:
                row = await db.get(ApplicationSetting, "admin_login_enabled")
                if not row:
                    raise SecurityError(503, "setting_unavailable")
                row.enabled = True
            ctx.target_type, ctx.target_id, ctx.reason = (
                "administrator",
                str(admin.id),
                "security_response",
            )
            ctx.details = {"login_restored": restore_login}
            return {"id": str(admin.id), "password_reset": True, "sessions_revoked": True}

        return await self._execute("auth.operator_recovery", meta, operation, locked=True)

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import AdminRoleAssignment, AdminUser, Role, User
from app.models.security import AdminSession, AdminToken, ApplicationSetting, AuditLog
from app.security.crypto import digest_token
from app.security.metadata import RequestMetadata, fingerprint
from app.security.service import SecurityError, SecurityService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@dataclass
class Harness:
    client: AsyncClient
    security: SecurityService
    admin_id: UUID
    username: str
    password: str = field(repr=False)
    tokens: dict[str, str] = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    factory: object


@pytest.fixture
async def harness():
    settings = Settings()
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        security = app.state.security
        factory = session_factory(app.state.dependencies.engine)
        username, password = "root-" + uuid4().hex, secrets.token_urlsafe(28)
        meta = RequestMetadata(uuid4(), fingerprint(username), fingerprint("test-harness"))
        async with factory() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(AdminUser)
                .join(AdminRoleAssignment, AdminRoleAssignment.admin_user_id == AdminUser.id)
                .join(Role, Role.id == AdminRoleAssignment.role_id)
                .where(Role.code == "super_admin", AdminUser.enabled.is_(True))
            )
        if not count:
            created = await security.bootstrap(username, password, meta)
            admin_id = UUID(created["id"])
        else:
            async with factory() as db, db.begin():
                role = await db.scalar(select(Role).where(Role.code == "super_admin"))
                user = User()
                db.add(user)
                await db.flush()
                admin = AdminUser(
                    user_id=user.id,
                    username=username,
                    password_hash=await security._hash(password),
                    enabled=True,
                )
                db.add(admin)
                await db.flush()
                db.add(AdminRoleAssignment(admin_user_id=admin.id, role_id=role.id))
                admin_id = admin.id
        async with AsyncClient(
            transport=ASGITransport(app=app, client=(username, 1234)), base_url="http://localhost"
        ) as client:
            response = await client.post(
                "/auth/login", json={"username": username, "password": password}
            )
            assert response.status_code == 200, response.text
            tokens = response.json()
            yield Harness(
                client,
                security,
                admin_id,
                username,
                password,
                tokens,
                {"Authorization": "Bearer " + tokens["access_token"]},
                factory,
            )


async def member(h: Harness, role_code: str) -> tuple[UUID, UUID, str, dict[str, str]]:
    username, password = "member-" + uuid4().hex, secrets.token_urlsafe(28)
    async with h.security.factory() as db, db.begin():
        role = await db.scalar(select(Role).where(Role.code == role_code))
        user = User()
        db.add(user)
        await db.flush()
        admin = AdminUser(
            user_id=user.id,
            username=username,
            password_hash=await h.security._hash(password),
            enabled=True,
        )
        db.add(admin)
        await db.flush()
        db.add(AdminRoleAssignment(admin_user_id=admin.id, role_id=role.id))
        aid, uid = admin.id, user.id
    response = await h.client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return aid, uid, password, {"Authorization": "Bearer " + response.json()["access_token"]}


def step(h: Harness) -> dict[str, str]:
    return {"current_password": h.password, "reason": "security_response"}


async def test_login_digests_bootstrap_and_generic_failures(harness: Harness) -> None:
    h = harness
    assert (await h.client.get("/auth/me", headers=h.headers)).json()["id"] == str(h.admin_id)
    async with h.security.factory() as db:
        admin = await db.get(AdminUser, h.admin_id)
        assert (
            admin.password_hash.startswith("$argon2id$") and h.password not in admin.password_hash
        )
        tokens = (
            await db.scalars(
                select(AdminToken).where(AdminToken.session_id == UUID(h.tokens["session_id"]))
            )
        ).all()
        assert len(tokens) == 2
        assert {t.token_digest for t in tokens} == {
            digest_token(h.tokens["access_token"], "access"),
            digest_token(h.tokens["refresh_token"], "refresh"),
        }
    wrong = await h.client.post(
        "/auth/login", json={"username": h.username, "password": "incorrect"}
    )
    unknown = await h.client.post(
        "/auth/login", json={"username": "unknown-" + uuid4().hex, "password": "incorrect"}
    )
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()
    with pytest.raises(SecurityError) as error:
        await h.security.bootstrap(
            "another-" + uuid4().hex,
            secrets.token_urlsafe(28),
            RequestMetadata(uuid4(), fingerprint("cli"), fingerprint("cli")),
        )
    assert error.value.status == 409


@pytest.mark.parametrize(
    "route",
    [
        "/auth/me",
        "/admin/users",
        "/admin/products",
        "/admin/admins",
        "/admin/roles",
        "/admin/audit",
        "/admin/settings/login",
    ],
)
async def test_every_read_endpoint_rejects_unauthenticated(harness: Harness, route: str) -> None:
    response = await harness.client.get(route)
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
    response = await harness.client.get(route, headers={"Authorization": "Bearer malformed"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "role,route,expected",
    [
        ("support", "/admin/users", 200),
        ("support", "/admin/products", 403),
        ("support", "/admin/roles", 403),
        ("support", "/admin/audit", 403),
        ("finance", "/admin/users", 200),
        ("finance", "/admin/admins", 403),
        ("inventory", "/admin/products", 200),
        ("inventory", "/admin/users", 403),
        ("marketing", "/admin/products", 200),
        ("marketing", "/admin/audit", 403),
        ("auditor", "/admin/audit", 200),
        ("auditor", "/admin/settings/login", 200),
        ("operations", "/admin/products", 200),
        ("operations", "/admin/admins", 403),
    ],
)
async def test_role_read_matrix(harness: Harness, role: str, route: str, expected: int) -> None:
    _, _, _, headers = await member(harness, role)
    assert (await harness.client.get(route, headers=headers)).status_code == expected


async def test_rotating_refresh_replay_and_logout(harness: Harness) -> None:
    h = harness
    response = await h.client.post(
        "/auth/refresh", json={"refresh_token": h.tokens["refresh_token"]}
    )
    assert response.status_code == 200
    fresh = response.json()
    assert fresh["access_token"] != h.tokens["access_token"]
    assert (await h.client.get("/auth/me", headers=h.headers)).status_code == 401
    headers = {"Authorization": "Bearer " + fresh["access_token"]}
    assert (await h.client.get("/auth/me", headers=headers)).status_code == 200
    assert (
        await h.client.post("/auth/refresh", json={"refresh_token": h.tokens["refresh_token"]})
    ).status_code == 401
    assert (await h.client.get("/auth/me", headers=headers)).status_code == 401
    login = await h.client.post(
        "/auth/login", json={"username": h.username, "password": h.password}
    )
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert (await h.client.post("/auth/logout", headers=headers)).status_code == 200
    assert (await h.client.get("/auth/me", headers=headers)).status_code == 401
    assert (
        await h.client.post("/auth/refresh", json={"refresh_token": login.json()["refresh_token"]})
    ).status_code == 401


async def test_refresh_race_revokes_session(harness: Harness) -> None:
    h = harness
    results = await asyncio.gather(
        *(
            h.client.post("/auth/refresh", json={"refresh_token": h.tokens["refresh_token"]})
            for _ in range(2)
        )
    )
    assert sorted(r.status_code for r in results) == [200, 401]
    fresh = next(r.json() for r in results if r.status_code == 200)
    assert (
        await h.client.get("/auth/me", headers={"Authorization": "Bearer " + fresh["access_token"]})
    ).status_code == 401


@pytest.mark.parametrize("kind", ["access", "refresh", "session"])
async def test_expiration(harness: Harness, kind: str) -> None:
    h = harness
    async with h.security.factory() as db, db.begin():
        model = AdminSession if kind == "session" else AdminToken
        where = (
            model.id == UUID(h.tokens["session_id"])
            if kind == "session"
            else (
                (AdminToken.session_id == UUID(h.tokens["session_id"])) & (AdminToken.kind == kind)
            )
        )
        now = await db.scalar(select(func.clock_timestamp()))
        await db.execute(
            update(model)
            .where(where)
            .values(created_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1))
        )
    response = (
        await h.client.post("/auth/refresh", json={"refresh_token": h.tokens["refresh_token"]})
        if kind == "refresh"
        else await h.client.get("/auth/me", headers=h.headers)
    )
    assert response.status_code == 401


async def test_brute_force_shared_account_and_ip_limits(harness: Harness) -> None:
    h = harness
    for _ in range(h.security.settings.login_account_limit - 1):
        assert (
            await h.client.post("/auth/login", json={"username": h.username, "password": "wrong"})
        ).status_code == 401
    blocked = await h.client.post(
        "/auth/login",
        json={"username": h.username, "password": h.password},
        headers={"X-Forwarded-For": "192.0.2.99"},
    )
    assert blocked.status_code == 429 and "retry-after" in blocked.headers
    async with h.security.factory() as db:
        last = await db.scalar(
            select(AuditLog)
            .where(AuditLog.reason_code == "rate_limited")
            .order_by(AuditLog.occurred_at.desc())
            .limit(1)
        )
        assert last.outcome == "denied"


async def test_invalid_input_never_echoes_secrets(harness: Harness) -> None:
    h = harness
    response = await h.client.post(
        "/auth/login",
        json={"username": "INVALID", "password": h.password, "permissions": ["admins.manage"]},
    )
    assert response.status_code == 422 and h.password not in response.text
    assert (
        await h.client.post("/auth/refresh", json={"refresh_token": "malformed"})
    ).status_code == 422
    assert (
        await h.client.post(
            "/auth/login", content="{invalid-json", headers={"Content-Type": "application/json"}
        )
    ).status_code == 422
    assert (await h.client.post("/auth/login", content=b"x" * 65537)).status_code == 413
    assert (await h.client.get("/admin/users?limit=10001", headers=h.headers)).status_code == 422


async def test_password_change_and_admin_revocation(harness: Harness) -> None:
    h = harness
    response = await h.client.post(
        "/admin/sessions/" + h.tokens["session_id"] + "/revoke", headers=h.headers, json=step(h)
    )
    assert response.status_code == 200
    assert (await h.client.get("/auth/me", headers=h.headers)).status_code == 401
    fresh = (
        await h.client.post("/auth/login", json={"username": h.username, "password": h.password})
    ).json()
    headers = {"Authorization": "Bearer " + fresh["access_token"]}
    password = secrets.token_urlsafe(28)
    response = await h.client.post(
        "/auth/password", headers=headers, json=step(h) | {"new_password": password}
    )
    assert response.status_code == 200
    assert (await h.client.get("/auth/me", headers=headers)).status_code == 401
    assert (
        await h.client.post("/auth/login", json={"username": h.username, "password": h.password})
    ).status_code == 401
    assert (
        await h.client.post("/auth/login", json={"username": h.username, "password": password})
    ).status_code == 200


async def test_role_creation_grants_live_changes_and_step_up(harness: Harness) -> None:
    h = harness
    aid, _, _, headers = await member(h, "support")
    body = step(h) | {
        "code": "custom-" + uuid4().hex,
        "title": "نقش سفارشی",
        "permissions": ["users.read"],
    }
    assert (await h.client.post("/admin/roles", headers=headers, json=body)).status_code == 403
    assert (
        await h.client.post(
            "/admin/roles", headers=h.headers, json=body | {"current_password": "wrong"}
        )
    ).status_code == 403
    created = await h.client.post("/admin/roles", headers=h.headers, json=body)
    assert created.status_code == 200
    rid = created.json()["id"]
    grant = await h.client.put(
        f"/admin/admins/{aid}/roles", headers=h.headers, json=step(h) | {"role_ids": [rid]}
    )
    assert grant.status_code == 200
    assert (await h.client.get("/admin/users", headers=headers)).status_code == 200
    assert (
        await h.client.put(
            f"/admin/roles/{rid}/permissions", headers=h.headers, json=body | {"permissions": []}
        )
    ).status_code == 200
    assert (await h.client.get("/admin/users", headers=headers)).status_code == 403
    assert (
        await h.client.put(
            f"/admin/admins/{h.admin_id}/roles", headers=h.headers, json=step(h) | {"role_ids": []}
        )
    ).status_code == 403
    roles = (await h.client.get("/admin/roles", headers=h.headers)).json()["items"]
    root = next(r for r in roles if r["code"] == "super_admin")
    assert (
        await h.client.put(
            f"/admin/roles/{root['id']}/permissions",
            headers=h.headers,
            json=body | {"code": "super_admin"},
        )
    ).status_code == 403


async def test_create_least_privilege_admin_status_and_configuration(harness: Harness) -> None:
    h = harness
    async with h.security.factory() as db, db.begin():
        user = User()
        db.add(user)
        await db.flush()
        uid = user.id
    new_name, new_password = "new-" + uuid4().hex, secrets.token_urlsafe(28)
    response = await h.client.post(
        "/admin/admins",
        headers=h.headers,
        json=step(h) | {"user_id": str(uid), "username": new_name, "new_password": new_password},
    )
    assert response.status_code == 200
    fresh = (
        await h.client.post("/auth/login", json={"username": new_name, "password": new_password})
    ).json()
    headers = {"Authorization": "Bearer " + fresh["access_token"]}
    assert (await h.client.get("/auth/me", headers=headers)).status_code == 200
    assert (await h.client.get("/admin/users", headers=headers)).status_code == 403
    assert (
        await h.client.patch(
            f"/admin/users/{uid}/status", headers=h.headers, json=step(h) | {"status": "blocked"}
        )
    ).status_code == 200
    assert (await h.client.get("/auth/me", headers=headers)).status_code == 401
    assert (
        await h.client.put(
            "/admin/settings/login", headers=h.headers, json=step(h) | {"enabled": False}
        )
    ).status_code == 200
    try:
        assert (
            await h.client.post(
                "/auth/login", json={"username": h.username, "password": h.password}
            )
        ).status_code == 401
    finally:
        assert (
            await h.client.put(
                "/admin/settings/login", headers=h.headers, json=step(h) | {"enabled": True}
            )
        ).status_code == 200


async def test_audit_content_atomicity_and_tamper_protection(harness: Harness) -> None:
    h = harness
    await h.client.get("/admin/users", headers=h.headers)
    await h.client.get("/admin/users", headers={"Authorization": "Bearer malformed"})
    async with h.security.factory() as db:
        rows = (await db.scalars(select(AuditLog).where(AuditLog.actor_id == h.admin_id))).all()
        text_data = json.dumps(
            [{c.name: str(getattr(r, c.name)) for c in AuditLog.__table__.columns} for r in rows]
        )
        assert h.password not in text_data
        assert (
            h.tokens["access_token"] not in text_data and h.tokens["refresh_token"] not in text_data
        )
        assert any(r.action == "auth.login" and r.outcome == "success" for r in rows)
        event = rows[0]
        event_id = event.id
    for statement in [
        "UPDATE audit_logs SET outcome='failure'",
        "DELETE FROM audit_logs",
        "TRUNCATE audit_logs",
    ]:
        with pytest.raises(DBAPIError):
            async with h.security.factory() as db, db.begin():
                await db.execute(text(statement))
    with pytest.raises(DBAPIError):
        async with h.security.factory() as db, db.begin():
            event = await db.get(AuditLog, event_id)
            event.action = "silently_changed"
    async with h.security.factory() as db:
        assert (await db.get(AuditLog, event_id)).action != "silently_changed"


async def test_redis_failure_fails_closed_and_is_audited(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness

    async def unavailable(*args: object, **kwargs: object) -> None:
        raise RedisConnectionError("private-cache-detail")

    monkeypatch.setattr(h.security.redis, "eval", unavailable)
    response = await h.client.get("/admin/users", headers=h.headers)
    assert response.status_code == 503 and "private-cache-detail" not in response.text
    async with h.security.factory() as db:
        assert await db.scalar(
            select(AuditLog.id).where(AuditLog.reason_code == "dependency_unavailable")
        )


async def test_negative_mutations_and_unknown_tokens(harness: Harness) -> None:
    h = harness
    body = step(h)
    role_body = body | {
        "code": "negative-" + uuid4().hex,
        "title": "Role",
        "permissions": ["users.read"],
    }
    missing = str(uuid4())
    cases = [
        ("POST", "/admin/roles", role_body),
        ("PUT", f"/admin/roles/{missing}/permissions", role_body),
        ("PUT", f"/admin/admins/{missing}/roles", body | {"role_ids": []}),
        ("PATCH", f"/admin/users/{missing}/status", body | {"status": "blocked"}),
        ("POST", f"/admin/sessions/{missing}/revoke", body),
        ("PUT", "/admin/settings/login", body | {"enabled": False}),
        (
            "POST",
            "/admin/admins",
            body
            | {
                "user_id": missing,
                "username": "missing-user",
                "new_password": secrets.token_urlsafe(28),
            },
        ),
        ("POST", "/auth/password", body | {"new_password": secrets.token_urlsafe(28)}),
        ("POST", "/auth/logout", None),
    ]
    for method, path, value in cases:
        response = await h.client.request(method, path, json=value)
        assert response.status_code == 401, (path, response.status_code)
    for method, path, value in cases[1:5]:
        assert (
            await h.client.request(method, path, headers=h.headers, json=value)
        ).status_code == 404
    for token in ["va_" + secrets.token_urlsafe(32), "vr_" + secrets.token_urlsafe(32)]:
        assert (
            await h.client.get("/auth/me", headers={"Authorization": "Bearer " + token})
        ).status_code == 401
    assert (
        await h.client.post(
            "/auth/refresh", json={"refresh_token": "vr_" + secrets.token_urlsafe(32)}
        )
    ).status_code == 401
    assert (
        await h.client.post("/auth/refresh", json={"refresh_token": h.tokens["access_token"]})
    ).status_code == 401
    assert (
        await h.client.get("/auth/me", headers={"Authorization": "Basic invalid"})
    ).status_code == 401
    assert (
        await h.client.get("/auth/me", headers={"Authorization": "Bearer " + "x" * 200})
    ).status_code == 401
    assert (
        await h.client.patch(
            f"/admin/users/{uuid4()}/status", headers=h.headers, json=body | {"status": "unknown"}
        )
    ).status_code == 422
    assert (
        await h.client.post(
            "/admin/admins",
            headers=h.headers,
            json=body
            | {
                "user_id": missing,
                "username": "missing-user",
                "new_password": secrets.token_urlsafe(28),
            },
        )
    ).status_code == 404
    with pytest.raises(SecurityError):
        await h.security.admin_action(
            h.tokens["access_token"],
            RequestMetadata(uuid4(), fingerprint("direct"), fingerprint("test")),
            "not-an-operation",
        )
    with pytest.raises(SecurityError):
        await h.security.admin_action(
            h.tokens["access_token"],
            RequestMetadata(uuid4(), fingerprint("direct"), fingerprint("test")),
            "users.read",
            limit=0,
        )


async def test_delegation_cannot_escalate_or_edit_own_role(harness: Harness) -> None:
    h = harness
    aid, _, password, headers = await member(h, "support")
    limited = step(h) | {
        "code": "limited-" + uuid4().hex,
        "title": "Limited",
        "permissions": ["users.read", "roles.manage", "admins.manage"],
    }
    rid = (await h.client.post("/admin/roles", headers=h.headers, json=limited)).json()["id"]
    assert (
        await h.client.put(
            f"/admin/admins/{aid}/roles", headers=h.headers, json=step(h) | {"role_ids": [rid]}
        )
    ).status_code == 200
    local_step = {"current_password": password, "reason": "role_review"}
    assert (
        await h.client.post(
            "/admin/roles",
            headers=headers,
            json=local_step
            | {"code": "elevated-" + uuid4().hex, "title": "No", "permissions": ["audit.read"]},
        )
    ).status_code == 403
    assert (
        await h.client.put(
            f"/admin/roles/{rid}/permissions",
            headers=headers,
            json=local_step | {"code": limited["code"], "title": "No", "permissions": []},
        )
    ).status_code == 403
    roles = (await h.client.get("/admin/roles", headers=h.headers)).json()["items"]
    super_role = next(r["id"] for r in roles if r["code"] == "super_admin")
    auditor_role = next(r["id"] for r in roles if r["code"] == "auditor")
    assert (
        await h.client.put(
            f"/admin/admins/{h.admin_id}/roles",
            headers=headers,
            json=local_step | {"role_ids": [super_role]},
        )
    ).status_code == 403
    assert (
        await h.client.put(
            f"/admin/admins/{h.admin_id}/roles",
            headers=headers,
            json=local_step | {"role_ids": [auditor_role]},
        )
    ).status_code == 403
    assert (
        await h.client.put(
            f"/admin/admins/{h.admin_id}/roles",
            headers=headers,
            json=local_step | {"role_ids": [str(uuid4())]},
        )
    ).status_code == 404
    another = step(h) | {
        "code": "powerful-" + uuid4().hex,
        "title": "Powerful",
        "permissions": ["audit.read"],
    }
    other_id = (await h.client.post("/admin/roles", headers=h.headers, json=another)).json()["id"]
    assert (
        await h.client.put(
            f"/admin/roles/{other_id}/permissions",
            headers=headers,
            json=local_step | {"code": another["code"], "title": "Changed", "permissions": []},
        )
    ).status_code == 403
    assert (
        await h.client.put(
            f"/admin/roles/{other_id}/permissions",
            headers=h.headers,
            json=another | {"code": "changed-code"},
        )
    ).status_code == 409
    assert (await h.client.post("/admin/roles", headers=h.headers, json=another)).status_code == 409


@pytest.mark.parametrize("disabled", ["admin", "user"])
async def test_inactive_accounts_cannot_login_access_or_refresh(
    harness: Harness, disabled: str
) -> None:
    h = harness
    async with h.security.factory() as db, db.begin():
        admin = await db.get(AdminUser, h.admin_id)
        if disabled == "admin":
            admin.enabled = False
        else:
            (await db.get(User, admin.user_id)).status = "suspended"
    assert (await h.client.get("/auth/me", headers=h.headers)).status_code == 401
    assert (
        await h.client.post("/auth/refresh", json={"refresh_token": h.tokens["refresh_token"]})
    ).status_code == 401
    assert (
        await h.client.post("/auth/login", json={"username": h.username, "password": h.password})
    ).status_code == 401


async def test_status_permissions_and_sensitive_operation_audits(harness: Harness) -> None:
    h = harness
    aid, uid, password, headers = await member(h, "operations")
    async with h.security.factory() as db, db.begin():
        user = User()
        db.add(user)
        await db.flush()
        regular_uid = user.id
    change = {"current_password": password, "reason": "account_review", "status": "blocked"}
    assert (
        await h.client.patch(f"/admin/users/{regular_uid}/status", headers=headers, json=change)
    ).status_code == 200
    assert (
        await h.client.patch(f"/admin/users/{uid}/status", headers=headers, json=change)
    ).status_code == 403
    async with h.security.factory() as db:
        root = await db.get(AdminUser, h.admin_id)
    assert (
        await h.client.patch(
            f"/admin/users/{root.user_id}/status",
            headers=h.headers,
            json=step(h) | {"status": "blocked"},
        )
    ).status_code == 403
    assert (
        await h.client.patch(
            f"/admin/users/{regular_uid}/status",
            headers=h.headers,
            json=step(h) | {"status": "blocked"},
        )
    ).status_code == 200
    assert (await h.client.get("/admin/admins", headers=h.headers)).status_code == 200
    async with h.security.factory() as db:
        events = (await db.scalars(select(AuditLog).where(AuditLog.actor_id == aid))).all()
        assert any(e.outcome == "denied" for e in events)
        assert any(e.details.get("request_method") == "PATCH" for e in events)
        assert all(h.password not in json.dumps(e.details) for e in events)


async def test_audit_failure_rolls_back_mutation_and_session_issuance(harness: Harness) -> None:
    h = harness
    async with h.security.factory() as db, db.begin():
        await db.execute(
            text("""CREATE FUNCTION test_reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'audit insertion rejected'; END; $$""")
        )
        await db.execute(
            text(
                "CREATE TRIGGER test_reject_audit BEFORE INSERT ON audit_logs FOR EACH ROW "
                "EXECUTE FUNCTION test_reject_audit()"
            )
        )
    try:
        response = await h.client.put(
            "/admin/settings/login", headers=h.headers, json=step(h) | {"enabled": False}
        )
        assert response.status_code == 503
        async with h.security.factory() as db:
            assert await db.scalar(select(ApplicationSetting.enabled)) is True
            before = await db.scalar(
                select(func.count())
                .select_from(AdminSession)
                .where(AdminSession.admin_user_id == h.admin_id)
            )
        response = await h.client.post(
            "/auth/login", json={"username": h.username, "password": h.password}
        )
        assert response.status_code == 503 and "access_token" not in response.text
        async with h.security.factory() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AdminSession)
                    .where(AdminSession.admin_user_id == h.admin_id)
                )
                == before
            )
        response = await h.client.get("/auth/me", headers={"Authorization": "Bearer malformed"})
        assert response.status_code == 503
    finally:
        async with h.security.factory() as db, db.begin():
            await db.execute(text("DROP TRIGGER test_reject_audit ON audit_logs"))
            await db.execute(text("DROP FUNCTION test_reject_audit()"))


async def test_last_super_status_protection(harness: Harness) -> None:
    h = harness
    aid, _, password, headers = await member(h, "support")
    roles = (await h.client.get("/admin/roles", headers=h.headers)).json()
    body = step(h) | {
        "code": "all-capabilities-" + uuid4().hex,
        "title": "Trusted operator",
        "permissions": roles["permissions"],
    }
    rid = (await h.client.post("/admin/roles", headers=h.headers, json=body)).json()["id"]
    assert (
        await h.client.put(
            f"/admin/admins/{aid}/roles", headers=h.headers, json=step(h) | {"role_ids": [rid]}
        )
    ).status_code == 200
    async with h.security.factory() as db, db.begin():
        other_ids = (
            await db.scalars(
                select(AdminUser.id)
                .join(AdminRoleAssignment, AdminRoleAssignment.admin_user_id == AdminUser.id)
                .join(Role, Role.id == AdminRoleAssignment.role_id)
                .where(
                    Role.code == "super_admin",
                    AdminUser.id != h.admin_id,
                    AdminUser.enabled.is_(True),
                )
            )
        ).all()
        await db.execute(update(AdminUser).where(AdminUser.id.in_(other_ids)).values(enabled=False))
        root_user_id = (await db.get(AdminUser, h.admin_id)).user_id
    try:
        response = await h.client.patch(
            f"/admin/users/{root_user_id}/status",
            headers=headers,
            json={"current_password": password, "reason": "security_response", "status": "blocked"},
        )
        assert response.status_code == 409
    finally:
        async with h.security.factory() as db, db.begin():
            await db.execute(
                update(AdminUser).where(AdminUser.id.in_(other_ids)).values(enabled=True)
            )


async def test_nonowner_audit_permissions(harness: Harness) -> None:
    h = harness
    # The test/migration connection needs CREATEROLE, never a production runtime login.
    name = "runtime_test_" + uuid4().hex
    async with h.security.factory() as db, db.begin():
        await db.execute(text(f'CREATE ROLE "{name}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE'))
        await db.execute(text(f'GRANT USAGE ON SCHEMA public TO "{name}"'))
        await db.execute(text(f'GRANT SELECT, INSERT ON audit_logs TO "{name}"'))
        await db.execute(text(f'GRANT "{name}" TO CURRENT_USER'))
    try:
        async with h.security.factory() as db, db.begin():
            await db.execute(text(f'SET LOCAL ROLE "{name}"'))
            assert await db.scalar(select(func.count()).select_from(AuditLog))
        for command in [
            "UPDATE audit_logs SET outcome='failure'",
            "DELETE FROM audit_logs",
            "TRUNCATE audit_logs",
            "ALTER TABLE audit_logs DISABLE TRIGGER ALL",
        ]:
            with pytest.raises(DBAPIError):
                async with h.security.factory() as db, db.begin():
                    await db.execute(text(f'SET LOCAL ROLE "{name}"'))
                    await db.execute(text(command))
    finally:
        async with h.security.factory() as db, db.begin():
            await db.execute(text(f'DROP OWNED BY "{name}"'))
            await db.execute(text(f'DROP ROLE "{name}"'))


async def test_refresh_replay_revocation_survives_audit_insert_failure(harness: Harness) -> None:
    h = harness
    fresh = (
        await h.client.post("/auth/refresh", json={"refresh_token": h.tokens["refresh_token"]})
    ).json()
    async with h.security.factory() as db, db.begin():
        await db.execute(
            text("""CREATE FUNCTION test_replay_audit_failure() RETURNS trigger
                LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'reject audit'; END; $$""")
        )
        await db.execute(
            text(
                "CREATE TRIGGER test_replay_audit_failure BEFORE INSERT ON audit_logs FOR "
                "EACH ROW EXECUTE FUNCTION test_replay_audit_failure()"
            )
        )
    try:
        response = await h.client.post(
            "/auth/refresh", json={"refresh_token": h.tokens["refresh_token"]}
        )
        assert response.status_code == 503
        async with h.security.factory() as db:
            assert (await db.get(AdminSession, UUID(h.tokens["session_id"]))).revoked_at is not None
    finally:
        async with h.security.factory() as db, db.begin():
            await db.execute(text("DROP TRIGGER test_replay_audit_failure ON audit_logs"))
            await db.execute(text("DROP FUNCTION test_replay_audit_failure()"))
    assert (
        await h.client.get("/auth/me", headers={"Authorization": "Bearer " + fresh["access_token"]})
    ).status_code == 401


async def test_direct_brute_limit_and_bad_service_inputs(harness: Harness) -> None:
    h = harness
    meta = RequestMetadata(uuid4(), fingerprint("rate-" + uuid4().hex), fingerprint("test"))
    assert not await h.security._verify("unused", "x" * 129)
    with pytest.raises(SecurityError) as error:
        await h.security.bootstrap("INVALID", "short", meta)
    assert error.value.status == 422
    settings = h.security.settings.model_copy(update={"admin_ip_limit": 1})
    second = SecurityService(h.security.factory, h.security.redis, settings)
    assert await second.identity(h.tokens["access_token"], meta)
    with pytest.raises(SecurityError) as error:
        await second.identity(h.tokens["access_token"], meta)
    assert error.value.status == 429


async def test_operator_recovery_has_no_public_route_and_revokes_sessions(harness: Harness) -> None:
    h = harness
    new_password = secrets.token_urlsafe(28)
    meta = RequestMetadata(uuid4(), fingerprint("local-operator"), fingerprint("test-recovery"))
    assert (await h.client.post("/auth/recover", json={"username": h.username})).status_code == 404
    async with h.security.factory() as db, db.begin():
        (await db.get(ApplicationSetting, "admin_login_enabled")).enabled = False
    try:
        result = await h.security.recover(h.username, new_password, meta, restore_login=True)
        assert result["sessions_revoked"]
        assert (await h.client.get("/auth/me", headers=h.headers)).status_code == 401
        assert (
            await h.client.post(
                "/auth/login", json={"username": h.username, "password": new_password}
            )
        ).status_code == 200
        with pytest.raises(SecurityError):
            await h.security.recover(h.username, "short", meta)
        with pytest.raises(SecurityError):
            await h.security.recover("unknown-" + uuid4().hex, new_password, meta)
    finally:
        async with h.security.factory() as db, db.begin():
            (await db.get(ApplicationSetting, "admin_login_enabled")).enabled = True

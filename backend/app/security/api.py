from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.security.metadata import request_metadata
from app.security.schemas import (
    CreateAdminBody,
    GrantBody,
    LoginBody,
    PasswordBody,
    RefreshBody,
    RoleBody,
    SettingBody,
    StatusBody,
    StepUpBody,
)
from app.security.service import SecurityService

auth = APIRouter(prefix="/auth", tags=["Authentication"])
admin = APIRouter(prefix="/admin", tags=["Administration"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]


def service(request: Request) -> SecurityService:
    return cast(SecurityService, request.app.state.security)


def access_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    scheme, _, raw = value.partition(" ")
    return raw if scheme.lower() == "bearer" and len(value) <= 128 else ""


@auth.post("/login")
async def login(request: Request, body: LoginBody) -> dict[str, Any]:
    return await service(request).login(
        body.username, body.password.get_secret_value(), request_metadata(request)
    )


@auth.post("/refresh")
async def refresh(request: Request, body: RefreshBody) -> dict[str, Any]:
    return await service(request).refresh(
        body.refresh_token.get_secret_value(), request_metadata(request)
    )


@auth.get("/me")
async def me(request: Request) -> dict[str, Any]:
    return await service(request).identity(access_token(request), request_metadata(request))


@auth.post("/logout")
async def logout(request: Request) -> dict[str, Any]:
    return await service(request).logout(access_token(request), request_metadata(request))


@auth.post("/password")
async def password(request: Request, body: PasswordBody) -> dict[str, Any]:
    return await service(request).change_password(
        access_token(request), body, request_metadata(request)
    )


@admin.get("/users")
async def users(request: Request, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "users.read", limit=limit, offset=offset
    )


@admin.patch("/users/{target}/status")
async def user_status(request: Request, target: UUID, body: StatusBody) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "users.status", body, target
    )


@admin.get("/products")
async def products(request: Request, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request),
        request_metadata(request),
        "products.read",
        limit=limit,
        offset=offset,
    )


@admin.get("/roles")
async def roles(request: Request, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "roles.read", limit=limit, offset=offset
    )


@admin.post("/roles")
async def create_role(request: Request, body: RoleBody) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "roles.create", body
    )


@admin.put("/roles/{target}/permissions")
async def role_permissions(request: Request, target: UUID, body: RoleBody) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "roles.permissions", body, target
    )


@admin.get("/admins")
async def admins(request: Request, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "admins.read", limit=limit, offset=offset
    )


@admin.post("/admins")
async def create_admin(request: Request, body: CreateAdminBody) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "admins.create", body
    )


@admin.put("/admins/{target}/roles")
async def admin_roles(request: Request, target: UUID, body: GrantBody) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "admins.roles", body, target
    )


@admin.post("/sessions/{target}/revoke")
async def revoke_session(request: Request, target: UUID, body: StepUpBody) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "sessions.revoke", body, target
    )


@admin.get("/audit")
async def audit(request: Request, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "audit.read", limit=limit, offset=offset
    )


@admin.get("/settings/login")
async def login_setting(request: Request) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "settings.read"
    )


@admin.put("/settings/login")
async def change_login_setting(request: Request, body: SettingBody) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "settings.change", body
    )

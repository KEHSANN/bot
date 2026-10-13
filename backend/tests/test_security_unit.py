import asyncio
import secrets
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from argon2 import Type, extract_parameters
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.body_limit import BodyLimitMiddleware
from app.security.crypto import (
    digest_token,
    hash_password,
    issue_token,
    strong_password,
    verify_password,
)
from app.security.metadata import RequestMetadata, fingerprint
from app.security.schemas import GrantBody, LoginBody, PasswordBody, RoleBody


def test_argon2_and_strong_passwords() -> None:
    password = secrets.token_urlsafe(28)
    encoded = hash_password(password)
    assert password not in encoded
    params = extract_parameters(encoded)
    assert params.type == Type.ID and params.memory_cost == 65536 and params.time_cost == 3
    assert verify_password(encoded, password)
    assert not verify_password(encoded, "wrong")
    assert not verify_password("corrupt-hash", password)
    assert not strong_password("a" * 16)
    with pytest.raises(ValueError):
        hash_password("short")


@pytest.mark.parametrize("kind", ["access", "refresh"])
def test_opaque_tokens(kind: str) -> None:
    raw, digest = issue_token(kind)
    assert len(raw) == 46 and len(digest) == 64 and digest != raw
    assert digest == digest_token(raw, kind)
    assert issue_token(kind)[0] != raw


@pytest.mark.parametrize(
    "raw,kind",
    [
        ("junk", "access"),
        ("", "refresh"),
        ("va_" + "x" * 43, "refresh"),
        ("vr_" + "x" * 43, "access"),
        ("anything", "unknown"),
    ],
)
def test_malformed_tokens(raw: str, kind: str) -> None:
    with pytest.raises(ValueError):
        digest_token(raw, kind)
    with pytest.raises(ValueError):
        issue_token("unknown")


def test_strict_secret_input_validation() -> None:
    value = LoginBody(username="operator", password="valid-input")
    assert "valid-input" not in repr(value)
    for body in [
        {"username": "OPERATOR", "password": "x"},
        {"username": "operator", "password": 123},
        {"username": "operator", "password": "x", "is_superadmin": True},
    ]:
        with pytest.raises(ValidationError):
            LoginBody.model_validate(body)
    with pytest.raises(ValidationError):
        PasswordBody.model_validate(
            {"current_password": "x", "new_password": "a" * 16, "reason": "security_response"}
        )
    with pytest.raises(ValidationError):
        RoleBody.model_validate(
            {
                "code": "role",
                "title": "Role",
                "permissions": ["unknown"],
                "current_password": "x",
                "reason": "role_review",
            }
        )
    uid = str(uuid4())
    with pytest.raises(ValidationError):
        GrantBody.model_validate(
            {"role_ids": [uid, uid], "current_password": "x", "reason": "role_review"}
        )


def test_bounded_body_middleware() -> None:
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, maximum=8)

    @app.post("/")
    async def endpoint() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.post("/", content=b"123456789").status_code == 413
        assert client.post("/", content=b"1234").status_code == 200


@pytest.mark.asyncio
async def test_disconnect_and_non_http_transport() -> None:
    inner = AsyncMock()
    middleware = BodyLimitMiddleware(inner)
    await middleware({"type": "websocket"}, AsyncMock(), AsyncMock())
    inner.assert_awaited_once()
    inner.reset_mock()
    await middleware(
        {"type": "http", "method": "POST"},
        AsyncMock(return_value={"type": "http.disconnect"}),
        AsyncMock(),
    )
    inner.assert_not_awaited()


@pytest.mark.asyncio
async def test_body_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class Timeout:
        async def __aenter__(self) -> None:
            raise TimeoutError

        async def __aexit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(asyncio, "timeout", lambda _: Timeout())
    send = AsyncMock()
    await BodyLimitMiddleware(AsyncMock())({"type": "http", "method": "POST"}, AsyncMock(), send)
    assert send.call_args_list[0].args[0]["status"] == 408


def test_metadata_has_no_plain_identifiers() -> None:
    assert fingerprint("peer") != "peer"
    assert RequestMetadata(uuid4(), fingerprint("peer"), fingerprint("agent")).method == "CLI"


def test_lifetimes_are_consistent(settings) -> None:
    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            database_url=settings.database_url,
            redis_url=settings.redis_url,
            refresh_token_seconds=3600,
            session_seconds=600,
        )


@pytest.mark.asyncio
async def test_body_replay_and_read_complete() -> None:
    seen = []
    source = AsyncMock(
        side_effect=[
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def inner(scope, receive, send):
        seen.append(await receive())
        seen.append(await receive())

    await BodyLimitMiddleware(inner)({"type": "http", "method": "POST"}, source, AsyncMock())
    assert seen[0]["body"] == b"abcd" and seen[1]["type"] == "http.disconnect"

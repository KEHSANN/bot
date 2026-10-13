import hashlib
from dataclasses import dataclass
from uuid import UUID

from fastapi import Request


@dataclass(frozen=True)
class RequestMetadata:
    request_id: UUID
    client_hash: str
    user_agent_hash: str
    method: str = "CLI"
    route: str = "operator"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request_metadata(request: Request) -> RequestMetadata:
    # Do not trust X-Forwarded-For. Proxy trust must be controlled at deployment ingress.
    peer = request.client.host if request.client else "unknown"
    return RequestMetadata(
        UUID(request.state.request_id),
        fingerprint(peer),
        fingerprint(request.headers.get("user-agent", "")[:2048]),
        request.method,
        getattr(request.scope.get("route"), "path", "unmatched")[:128],
    )

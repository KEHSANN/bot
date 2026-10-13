import hashlib
import re
import secrets

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16, type=Type.ID
)
TOKEN_PATTERN = re.compile(r"v[ar]_[A-Za-z0-9_-]{43}")
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))


def strong_password(value: str) -> bool:
    return 16 <= len(value) <= 128 and len(set(value)) >= 8


def hash_password(value: str) -> str:
    if not strong_password(value):
        raise ValueError("Password must contain 16-128 characters with sufficient variation")
    return HASHER.hash(value)


def verify_password(encoded: str, candidate: str) -> bool:
    try:
        return HASHER.verify(encoded, candidate)
    except (VerificationError, InvalidHashError):
        return False


def issue_token(kind: str) -> tuple[str, str]:
    if kind not in {"access", "refresh"}:
        raise ValueError("Invalid token kind")
    raw = ("va_" if kind == "access" else "vr_") + secrets.token_urlsafe(32)
    return raw, digest_token(raw, kind)


def digest_token(raw: str, kind: str) -> str:
    prefix = "va_" if kind == "access" else "vr_"
    if (
        kind not in {"access", "refresh"}
        or not TOKEN_PATTERN.fullmatch(raw)
        or not raw.startswith(prefix)
    ):
        raise ValueError("Invalid token")
    return hashlib.sha256(raw.encode("ascii")).hexdigest()

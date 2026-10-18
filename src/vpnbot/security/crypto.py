"""Cryptographic helpers: credential encryption and dedupe fingerprints.

* Inventory credentials (config + subscription URL) are encrypted at rest with
  Fernet (AES-128-CBC + HMAC-SHA256).
* Dedupe uses an HMAC-SHA256 fingerprint keyed with a server secret so that a
  leaked fingerprint column cannot be rainbow-tabled back into credentials.
* Raw credentials are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CryptoError(RuntimeError):
    pass


class CredentialCipher:
    def __init__(self, key: str, *, old_keys: list[str] | None = None) -> None:
        fernets = [Fernet(key.encode() if isinstance(key, str) else key)]
        for extra in old_keys or []:
            fernets.append(Fernet(extra.encode()))
        self._fernet = MultiFernet(fernets)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise CryptoError("Unable to decrypt inventory credential") from exc

    def rotate(self, token: str) -> str:
        return self._fernet.rotate(token.encode("ascii")).decode("ascii")


def normalize_for_fingerprint(value: str) -> str:
    """Stable normalization: trim, drop trailing slashes on URLs, keep content.

    VPN URIs are opaque payloads; we only remove insignificant whitespace and
    trailing slashes so the same subscription URL imported twice matches.
    """
    return value.strip().rstrip("/")


def fingerprint(value: str, secret: str) -> str:
    """Keyed HMAC-SHA256 hex digest used for duplicate detection."""
    normalized = normalize_for_fingerprint(value)
    if not normalized:
        raise ValueError("cannot fingerprint an empty value")
    return hmac.new(secret.encode(), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def random_token(length: int = 24) -> str:
    return secrets.token_urlsafe(length)


def redact(value: str, *, keep: int = 4) -> str:
    """Return a log-safe preview of a secret."""
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "…" + "*" * min(8, max(4, len(value) - keep))

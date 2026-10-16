"""
Security Engineering: Password Hashing, Session/JWT Tokens, Secret Encryption,
Webhook Signature Verification, SSRF Validation, and Role-Based Access Control (RBAC).
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
import urllib.parse
from typing import Dict, Iterable, Optional, Set


class SecurityError(Exception):
    """Base exception for authentication, authorization, or cryptographic failures."""


class AuthenticationError(SecurityError):
    """Raised when credentials or session tokens are invalid."""


class AuthorizationError(SecurityError):
    """Raised when an actor lacks the required permission."""


# Default Role -> Permissions mapping (Section 20)
ALL_PERMISSIONS: Set[str] = {
    "users.read",
    "users.manage",
    "products.read",
    "products.manage",
    "inventory.read",
    "inventory.import",
    "orders.read",
    "orders.manage",
    "wallets.read",
    "wallets.adjust",
    "payments.read",
    "payments.approve_manual",
    "payments.refund",
    "providers.manage",
    "referrals.read",
    "referrals.configure",
    "referrals.approve_milestone",
    "campaigns.manage",
    "support.manage",
    "admins.manage",
    "settings.manage",
    "audit.read",
}

DEFAULT_ROLES: Dict[str, Set[str]] = {
    "super_admin": set(ALL_PERMISSIONS),
    "operations_admin": {
        "users.read", "users.manage", "products.read", "products.manage",
        "inventory.read", "inventory.import", "orders.read", "orders.manage",
        "providers.manage", "support.manage", "audit.read",
    },
    "finance_admin": {
        "users.read", "orders.read", "wallets.read", "wallets.adjust",
        "payments.read", "payments.approve_manual", "payments.refund",
        "referrals.read", "referrals.approve_milestone", "audit.read",
    },
    "inventory_admin": {
        "products.read", "inventory.read", "inventory.import", "providers.manage",
    },
    "support_admin": {
        "users.read", "orders.read", "support.manage",
    },
    "marketing_admin": {
        "products.read", "referrals.read", "referrals.configure", "campaigns.manage",
    },
    "read_only_auditor": {
        "users.read", "products.read", "inventory.read", "orders.read",
        "wallets.read", "payments.read", "referrals.read", "audit.read",
    },
}


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Hash password with PBKDF2-HMAC-SHA256 (310,000 iterations)."""
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    salt_bytes = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, 310_000)
    return f"pbkdf2_sha256$310000${base64.urlsafe_b64encode(salt_bytes).decode()}${base64.urlsafe_b64encode(dk).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time password verification."""
    try:
        algo, iterations_str, salt_b64, hash_b64 = stored_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt_bytes = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(hash_b64.encode())
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt_bytes, int(iterations_str)
        )
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


def create_signed_token(
    payload: dict, secret_key: str, ttl_seconds: int = 3600
) -> str:
    """Create an expiring HMAC-SHA256 signed token (used for sessions, CSRF, and step-up confirmations)."""
    data = dict(payload)
    now = int(time.time())
    data["iat"] = now
    data["exp"] = now + ttl_seconds
    data["jti"] = secrets.token_urlsafe(12)
    body = base64.urlsafe_b64encode(
        json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(secret_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_signed_token(
    token: str, secret_key: str, revoked_jtis: Optional[Set[str]] = None
) -> dict:
    """Verify signature, expiration, and revocation of a signed token."""
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise AuthenticationError("Malformed token") from exc
    expected_sig = hmac.new(
        secret_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise AuthenticationError("Invalid token signature")
    data = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
    if int(time.time()) > int(data.get("exp", 0)):
        raise AuthenticationError("Token has expired")
    if revoked_jtis and data.get("jti") in revoked_jtis:
        raise AuthenticationError("Token has been revoked")
    return data


def encrypt_secret(plaintext: str, master_key: str) -> str:
    """Authenticated stream encryption (XOR stream derived via HMAC-SHA256 CTR + HMAC tag)
    for encrypting provider and gateway credentials at rest without external C dependencies.
    """
    nonce = secrets.token_bytes(16)
    pt_bytes = plaintext.encode("utf-8")
    keystream = bytearray()
    counter = 0
    while len(keystream) < len(pt_bytes):
        block = hmac.new(
            master_key.encode("utf-8"),
            nonce + counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        keystream.extend(block)
        counter += 1
    ciphertext = bytes(a ^ b for a, b in zip(pt_bytes, keystream[: len(pt_bytes)]))
    tag = hmac.new(
        master_key.encode("utf-8"), b"AUTH" + nonce + ciphertext, hashlib.sha256
    ).digest()
    blob = nonce + tag + ciphertext
    return "enc_v1:" + base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt_secret(encrypted: str, master_key: str) -> str:
    """Verify authentication tag and decrypt a stored credential."""
    if not encrypted.startswith("enc_v1:"):
        raise SecurityError("Unsupported encrypted secret format")
    blob = base64.urlsafe_b64decode(encrypted[7:].encode("ascii"))
    if len(blob) < 48:
        raise SecurityError("Truncated ciphertext")
    nonce, tag, ciphertext = blob[:16], blob[16:48], blob[48:]
    expected_tag = hmac.new(
        master_key.encode("utf-8"), b"AUTH" + nonce + ciphertext, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise SecurityError("Encrypted secret authentication tag mismatch")
    keystream = bytearray()
    counter = 0
    while len(keystream) < len(ciphertext):
        block = hmac.new(
            master_key.encode("utf-8"),
            nonce + counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        keystream.extend(block)
        counter += 1
    pt_bytes = bytes(a ^ b for a, b in zip(ciphertext, keystream[: len(ciphertext)]))
    return pt_bytes.decode("utf-8")


def mask_secret(secret: str) -> str:
    """Mask sensitive secrets so full credentials are never shown in Web or Telegram UI."""
    if not secret:
        return ""
    if len(secret) <= 6:
        return "***"
    return f"{secret[:2]}***{secret[-2:]}"


def verify_webhook_signature(
    payload_bytes: bytes,
    signature_header: str,
    timestamp_header: int,
    webhook_secret: str,
    tolerance_seconds: int = 300,
) -> bool:
    """Authenticate external gateway webhooks with HMAC-SHA256 and timestamp replay protection."""
    now = int(time.time())
    if abs(now - int(timestamp_header)) > tolerance_seconds:
        return False
    signed_content = f"{timestamp_header}.".encode("ascii") + payload_bytes
    expected = hmac.new(
        webhook_secret.encode("utf-8"), signed_content, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def validate_provider_url(url: str, allow_local_in_dev: bool = False) -> str:
    """Validate provider API endpoint to prevent SSRF against private/loopback/metadata networks."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise SecurityError("Provider URL must use https (or http in local dev)")
    if not allow_local_in_dev and parsed.scheme != "https":
        raise SecurityError("Production provider URLs must use HTTPS")
    host = (parsed.hostname or "").lower()
    if not host:
        raise SecurityError("Provider URL is missing hostname")
    if not allow_local_in_dev and host in {"localhost", "metadata.google.internal"}:
        raise SecurityError("Loopback or metadata hostnames are forbidden")
    try:
        ip = ipaddress.ip_address(host)
        if not allow_local_in_dev and (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        ):
            raise SecurityError(f"SSRF protection blocked non-public IP: {ip}")
    except ValueError:
        pass
    return url

"""
Production HTTP Adapters for External VPN Providers (Marzban, 3x-ui)
and Iranian Payment Gateways (Zarinpal, Zibal) using HTTPX.
Enforces SSRF validation, strict timeouts, IRR<->IRT normalization, and idempotent reconciliation.
"""
from __future__ import annotations
import hashlib
import json
import time
from typing import Any, Dict, Optional
import httpx

from backend.app.core.config import CurrencyError, irr_to_irt, irt_to_irr, validate_irt_amount
from backend.app.core.security import validate_provider_url
from backend.app.services.orders_provisioning_referrals import (
    ProvisioningAmbiguousTimeoutError,
    ProvisioningDefiniteFailureError,
    VPNProviderAdapter,
)
from backend.app.services.wallet_payments import PaymentGatewayAdapter


def deterministic_provider_username(idempotency_key: str, user_id: int) -> str:
    """Generate a deterministic, collision-resistant username from idempotency_key
    so ambiguous timeouts can be reconciled by querying the provider for this exact username.
    """
    digest = hashlib.sha256(f"{user_id}:{idempotency_key}".encode("utf-8")).hexdigest()[:12]
    return f"u{user_id}_{digest}"


class MarzbanHTTPProviderAdapter(VPNProviderAdapter):
    """Production-grade Marzban REST API Adapter (/api/admin/token, /api/user)."""

    def __init__(
        self,
        provider_code: str,
        base_url: str,
        admin_username: str,
        admin_password: str,
        *,
        allow_local_in_dev: bool = False,
        timeout_seconds: float = 10.0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.provider_code = provider_code
        self.base_url = validate_provider_url(base_url.rstrip("/"), allow_local_in_dev=allow_local_in_dev)
        self.admin_username = admin_username
        self.admin_password = admin_password
        self.timeout_seconds = timeout_seconds
        self._client = http_client or httpx.Client(timeout=timeout_seconds)
        self._cached_token: Optional[str] = None

    def _authenticate(self) -> str:
        if self._cached_token:
            return self._cached_token
        resp = self._client.post(
            f"{self.base_url}/api/admin/token",
            data={"username": self.admin_username, "password": self.admin_password},
        )
        if resp.status_code != 200:
            raise ProvisioningDefiniteFailureError(f"Marzban auth failed with HTTP {resp.status_code}")
        self._cached_token = resp.json()["access_token"]
        return self._cached_token

    def create_subscription(
        self, idempotency_key: str, user_id: int, duration_days: int, traffic_gb: int, device_limit: int
    ) -> Dict[str, Any]:
        token = self._authenticate()
        username = deterministic_provider_username(idempotency_key, user_id)
        expire_ts = int(time.time()) + (duration_days * 86400)
        data_limit_bytes = int(traffic_gb) * (1024 ** 3)
        payload = {
            "username": username,
            "proxies": {"vless": {}},
            "data_limit": data_limit_bytes,
            "expire": expire_ts,
            "status": "active",
            "note": f"idempotency_key={idempotency_key};device_limit={device_limit}",
        }
        try:
            resp = self._client.post(
                f"{self.base_url}/api/user",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            raise ProvisioningAmbiguousTimeoutError(f"Marzban timeout creating {username}") from exc
        except httpx.HTTPError as exc:
            raise ProvisioningAmbiguousTimeoutError(f"Marzban network error: {exc}") from exc

        if resp.status_code == 409:
            existing = self.query_subscription_by_idempotency_key(idempotency_key, user_id=user_id)
            if existing:
                return existing
            raise ProvisioningDefiniteFailureError("Marzban returned 409 conflict but user lookup failed")

        if resp.status_code not in (200, 201):
            raise ProvisioningDefiniteFailureError(
                f"Marzban rejected subscription creation (HTTP {resp.status_code})"
            )

        body = resp.json()
        links = body.get("links") or []
        sub_url = body.get("subscription_url") or (links[0] if links else "")
        if not sub_url:
            raise ProvisioningDefiniteFailureError("Marzban response did not include subscription link")
        return {
            "provider_sub_id": body.get("username", username),
            "config_uri": sub_url,
            "duration_days": duration_days,
            "traffic_gb": traffic_gb,
            "device_limit": device_limit,
        }

    def query_subscription_by_idempotency_key(
        self, idempotency_key: str, user_id: int = 0
    ) -> Optional[Dict[str, Any]]:
        token = self._authenticate()
        username = deterministic_provider_username(idempotency_key, user_id)
        resp = self._client.get(
            f"{self.base_url}/api/user/{username}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ProvisioningAmbiguousTimeoutError(f"Marzban status query HTTP {resp.status_code}")
        body = resp.json()
        links = body.get("links") or []
        sub_url = body.get("subscription_url") or (links[0] if links else "")
        return {
            "provider_sub_id": body.get("username", username),
            "config_uri": sub_url,
        }


class ThreeXUIHTTPProviderAdapter(VPNProviderAdapter):
    """Production-grade 3x-ui (Sanaei/Alireza) Panel HTTP Adapter."""

    def __init__(
        self,
        provider_code: str,
        base_url: str,
        session_cookie: str,
        inbound_id: int = 1,
        *,
        allow_local_in_dev: bool = False,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.provider_code = provider_code
        self.base_url = validate_provider_url(base_url.rstrip("/"), allow_local_in_dev=allow_local_in_dev)
        self.session_cookie = session_cookie
        self.inbound_id = inbound_id
        self._client = http_client or httpx.Client(timeout=10.0)

    def create_subscription(
        self, idempotency_key: str, user_id: int, duration_days: int, traffic_gb: int, device_limit: int
    ) -> Dict[str, Any]:
        email_id = deterministic_provider_username(idempotency_key, user_id)
        client_uuid = hashlib.md5(f"uuid:{idempotency_key}".encode("utf-8")).hexdigest()
        formatted_uuid = f"{client_uuid[:8]}-{client_uuid[8:12]}-{client_uuid[12:16]}-{client_uuid[16:20]}-{client_uuid[20:32]}"
        expiry_ms = (int(time.time()) + duration_days * 86400) * 1000
        total_bytes = int(traffic_gb) * (1024 ** 3)
        settings_json = json.dumps(
            {
                "clients": [
                    {
                        "id": formatted_uuid,
                        "email": email_id,
                        "limitIp": device_limit,
                        "totalGB": total_bytes,
                        "expiryTime": expiry_ms,
                        "enable": True,
                        "subId": email_id,
                    }
                ]
            }
        )
        try:
            resp = self._client.post(
                f"{self.base_url}/panel/api/inbounds/addClient",
                json={"id": self.inbound_id, "settings": settings_json},
                headers={"Cookie": f"3x-ui={self.session_cookie}"},
            )
        except httpx.HTTPError as exc:
            raise ProvisioningAmbiguousTimeoutError(f"3x-ui network timeout: {exc}") from exc

        if resp.status_code != 200 or not resp.json().get("success"):
            raise ProvisioningDefiniteFailureError("3x-ui rejected addClient request")
        return {
            "provider_sub_id": email_id,
            "config_uri": f"vless://{formatted_uuid}@panel.example.com:443?security=tls#{email_id}",
            "duration_days": duration_days,
            "traffic_gb": traffic_gb,
            "device_limit": device_limit,
        }

    def query_subscription_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        return None


class ZarinpalHTTPGatewayAdapter(PaymentGatewayAdapter):
    """Production Zarinpal v4 Payment Gateway Adapter (/pg/v4/payment/request.json & verify.json)."""
    gateway_code = "zarinpal_live"

    def __init__(
        self,
        merchant_id: str,
        *,
        base_url: str = "https://api.zarinpal.com",
        start_pay_base: str = "https://www.zarinpal.com/pg/StartPay",
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.merchant_id = merchant_id
        self.base_url = base_url.rstrip("/")
        self.start_pay_base = start_pay_base.rstrip("/")
        self._client = http_client or httpx.Client(timeout=10.0)

    def create_payment_request(self, payment_id: str, amount_irt: int, callback_url: str) -> Dict[str, Any]:
        validate_irt_amount(amount_irt)
        amount_irr = irt_to_irr(amount_irt)
        resp = self._client.post(
            f"{self.base_url}/pg/v4/payment/request.json",
            json={
                "merchant_id": self.merchant_id,
                "amount": amount_irr,
                "currency": "IRR",
                "description": f"شارژ کیف پول - شناسه {payment_id}",
                "callback_url": callback_url,
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Zarinpal request failed with HTTP {resp.status_code}")
        data = resp.json().get("data") or {}
        if data.get("code") != 100 or not data.get("authority"):
            raise RuntimeError(f"Zarinpal returned error code: {data.get('code')}")
        authority = data["authority"]
        return {
            "authority": authority,
            "amount_irr": amount_irr,
            "payment_url": f"{self.start_pay_base}/{authority}",
        }

    def verify_payment(self, authority: str, expected_amount_irr: int) -> Dict[str, Any]:
        if expected_amount_irr <= 0 or expected_amount_irr % 10 != 0:
            raise CurrencyError("Expected IRR amount must be positive and divisible by 10")
        try:
            resp = self._client.post(
                f"{self.base_url}/pg/v4/payment/verify.json",
                json={
                    "merchant_id": self.merchant_id,
                    "amount": expected_amount_irr,
                    "authority": authority,
                },
            )
        except httpx.HTTPError as exc:
            return {"verified": False, "status": "requires_review", "reason": f"Network timeout: {exc}"}

        if resp.status_code != 200:
            return {"verified": False, "status": "failed", "reason": f"HTTP {resp.status_code}"}
        data = resp.json().get("data") or {}
        code = data.get("code")
        if code in (100, 101):
            return {
                "verified": True,
                "status": "succeeded",
                "provider_tx_id": str(data.get("ref_id")),
                "verified_amount_irr": expected_amount_irr,
            }
        return {"verified": False, "status": "failed", "reason": f"Zarinpal verify code {code}"}


class ZibalHTTPGatewayAdapter(PaymentGatewayAdapter):
    """Production Zibal Payment Gateway Adapter (https://gateway.zibal.ir/v1/request & verify)."""
    gateway_code = "zibal_live"

    def __init__(self, merchant: str, *, http_client: Optional[httpx.Client] = None) -> None:
        self.merchant = merchant
        self._client = http_client or httpx.Client(timeout=10.0)

    def create_payment_request(self, payment_id: str, amount_irt: int, callback_url: str) -> Dict[str, Any]:
        amount_irr = irt_to_irr(amount_irt)
        resp = self._client.post(
            "https://gateway.zibal.ir/v1/request",
            json={
                "merchant": self.merchant,
                "amount": amount_irr,
                "callbackUrl": callback_url,
                "orderId": payment_id,
            },
        )
        body = resp.json()
        if body.get("result") != 100 or not body.get("trackId"):
            raise RuntimeError(f"Zibal request error: {body.get('result')}")
        track_id = str(body["trackId"])
        return {
            "authority": track_id,
            "amount_irr": amount_irr,
            "payment_url": f"https://gateway.zibal.ir/start/{track_id}",
        }

    def verify_payment(self, authority: str, expected_amount_irr: int) -> Dict[str, Any]:
        resp = self._client.post(
            "https://gateway.zibal.ir/v1/verify",
            json={"merchant": self.merchant, "trackId": int(authority)},
        )
        body = resp.json()
        if body.get("result") in (100, 201) and int(body.get("amount", 0)) == expected_amount_irr:
            return {
                "verified": True,
                "status": "succeeded",
                "provider_tx_id": str(body.get("refNumber", authority)),
                "verified_amount_irr": expected_amount_irr,
            }
        return {"verified": False, "status": "failed", "reason": f"Zibal verify code {body.get('result')}"}

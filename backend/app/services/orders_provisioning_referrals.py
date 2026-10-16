"""
Orders, Automated VPN Provisioning, Referral Rewards (N, W, P, M/K),
Mandatory Channel Membership Cache, Safe Templates, Campaigns, and Support Service.
"""
from __future__ import annotations
import re
import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from backend.app.core.config import format_persian_irt, validate_irt_amount
from backend.app.core.security import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    validate_provider_url,
)
from backend.app.models.database import DatabaseManager
from backend.app.services.catalog_pricing import CatalogAndPricingService
from backend.app.services.inventory import InventoryService
from backend.app.services.rbac_audit import AuditAndRBACService
from backend.app.services.wallet_payments import WalletAndPaymentService


class ProvisioningAmbiguousTimeoutError(RuntimeError):
    """Raised when an external VPN provider times out after potentially creating a subscription."""


class ProvisioningDefiniteFailureError(RuntimeError):
    """Raised when an external VPN provider definitely rejects subscription creation."""


class VPNProviderAdapter:
    """Common VPN Provider Adapter interface (Section 14)."""
    provider_code: str = "base"

    def create_subscription(
        self, idempotency_key: str, user_id: int, duration_days: int, traffic_gb: int, device_limit: int
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def query_subscription_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class FakeVPNProviderAdapter(VPNProviderAdapter):
    """Clearly labeled development/test VPN provider adapter supporting idempotency and timeout simulation."""
    provider_code = "fake_marzban_dev"

    def __init__(self, allow_in_env: bool = True) -> None:
        if not allow_in_env:
            raise RuntimeError("FakeVPNProviderAdapter is forbidden in production environment")
        self._created_by_idem: Dict[str, Dict[str, Any]] = {}
        self.simulate_mode: str = "normal"  # 'normal', 'timeout_after_create', 'definite_failure'

    def create_subscription(
        self, idempotency_key: str, user_id: int, duration_days: int, traffic_gb: int, device_limit: int
    ) -> Dict[str, Any]:
        if idempotency_key in self._created_by_idem:
            return self._created_by_idem[idempotency_key]
        if self.simulate_mode == "definite_failure":
            raise ProvisioningDefiniteFailureError("Provider rejected plan parameters (HTTP 422)")
        sub_id = f"sub_{secrets.token_hex(6)}"
        config_uri = f"vless://{secrets.token_hex(16)}@edge.vpnstore.example:443?security=tls&sni=edge.vpnstore.example#user_{user_id}_{sub_id}"
        record = {
            "provider_sub_id": sub_id,
            "config_uri": config_uri,
            "duration_days": duration_days,
            "traffic_gb": traffic_gb,
            "device_limit": device_limit,
        }
        self._created_by_idem[idempotency_key] = record
        if self.simulate_mode == "timeout_after_create":
            raise ProvisioningAmbiguousTimeoutError("ReadTimeout waiting for provider response")
        return record

    def query_subscription_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        return self._created_by_idem.get(idempotency_key)


class OrderAndProvisioningService:
    """Transactional purchase orchestration and saga recovery service (Sections 14 & 15)."""

    def __init__(
        self,
        db: DatabaseManager,
        rbac: AuditAndRBACService,
        catalog: CatalogAndPricingService,
        inventory: InventoryService,
        wallet: WalletAndPaymentService,
        master_key: str,
    ) -> None:
        self.db = db
        self.rbac = rbac
        self.catalog = catalog
        self.inventory = inventory
        self.wallet = wallet
        self.master_key = master_key
        self.providers: Dict[str, VPNProviderAdapter] = {}

    def register_provider_adapter(self, adapter: VPNProviderAdapter) -> None:
        self.providers[adapter.provider_code] = adapter

    def configure_provider_account(
        self,
        actor_admin_id: int,
        *,
        code: str,
        title_fa: str,
        adapter_type: str,
        endpoint_url: str,
        api_secret: str,
        allow_local_in_dev: bool = False,
        is_enabled: bool = True,
    ) -> Dict[str, Any]:
        self.rbac.require_permission(actor_admin_id, "providers.manage")
        validate_provider_url(endpoint_url, allow_local_in_dev=allow_local_in_dev)
        enc_creds = encrypt_secret(api_secret, self.master_key)
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO provider_accounts
                   (code, title_fa, adapter_type, endpoint_url, credentials_encrypted, is_enabled, health_status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'healthy', ?)""",
                (code, title_fa, adapter_type, endpoint_url, enc_creds, int(is_enabled), now),
            )
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action="provider.configure",
                target_type="provider_account",
                target_id=code,
                outcome="succeeded",
                metadata={"endpoint_url": endpoint_url, "secret_masked": mask_secret(api_secret)},
                cur=cur,
            )
            cur.execute("SELECT code, title_fa, adapter_type, endpoint_url, is_enabled, health_status, updated_at FROM provider_accounts WHERE code = ?", (code,))
            res = dict(cur.fetchone())
            res["credentials_masked"] = mask_secret(api_secret)
            return res

    def purchase_with_wallet(
        self,
        user_id: int,
        quote_id: str,
        idempotency_key: str,
        *,
        client_supplied_price_irt: Optional[int] = None,
        now_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute a concurrency-safe wallet purchase (Section 15).
        - Rejects client-supplied price tampering.
        - Idempotent on `idempotency_key`.
        - For manual inventory: claims inventory + debits wallet atomically in one DB transaction.
        - For automated provider: debits wallet + queues durable provisioning job in DB transaction,
          then runs external provider provisioning outside the DB lock.
        """
        existing_order = self.db.fetch_one(
            "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
        )
        if existing_order:
            return self._enrich_order_for_user(existing_order)

        now = now_override if now_override is not None else int(time.time())
        order_id = f"ord_{secrets.token_hex(10)}"
        needs_external_provisioning = False
        prov_job_id: Optional[str] = None

        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,))
            already = cur.fetchone()
            if already:
                return self._enrich_order_for_user(dict(already))

            cur.execute("SELECT * FROM price_quotes WHERE id = ? AND user_id = ?", (quote_id, user_id))
            quote = cur.fetchone()
            if not quote:
                raise ValueError("پیش‌فاکتور معتبر یافت نشد.")
            if quote["status"] == "consumed":
                cur.execute("SELECT * FROM orders WHERE quote_id = ?", (quote_id,))
                prior_ord = cur.fetchone()
                if prior_ord:
                    return self._enrich_order_for_user(dict(prior_ord))
                raise ValueError("این پیش‌فاکتور قبلاً استفاده شده است.")
            if quote["status"] != "active" or int(quote["expires_at"]) <= now:
                cur.execute("UPDATE price_quotes SET status = 'expired' WHERE id = ?", (quote_id,))
                raise ValueError("مهلت پیش‌فاکتور به پایان رسیده است؛ لطفاً قیمت جدید دریافت کنید.")

            quoted_price = int(quote["final_amount_irt"])
            if client_supplied_price_irt is not None and int(client_supplied_price_irt) != quoted_price:
                raise ValueError("تغییر قیمت پیش‌فاکتور توسط کلاینت مجاز نیست.")

            cur.execute("SELECT * FROM products WHERE id = ? AND is_active = 1", (quote["product_id"],))
            product = cur.fetchone()
            if not product:
                raise ValueError("محصول انتخاب‌شده در حال حاضر فعال نیست.")

            # Mark quote consumed
            cur.execute(
                "UPDATE price_quotes SET status = 'consumed' WHERE id = ? AND status = 'active'",
                (quote_id,),
            )
            if cur.rowcount != 1:
                raise ValueError("پیش‌فاکتور هم‌زمان در درخواست دیگری مصرف شد.")

            if quote["coupon_code"]:
                cur.execute(
                    "UPDATE coupons SET used_count = used_count + 1 WHERE code = ? AND is_active = 1 AND used_count < max_uses",
                    (quote["coupon_code"],),
                )
                if cur.rowcount != 1:
                    raise ValueError("ظرفیت کد تخفیف تکمیل شده است.")

            # Debit wallet atomically (if quoted_price > 0)
            ledger_tx_id = None
            if quoted_price > 0:
                tx = self.wallet.apply_ledger_mutation_in_tx(
                    cur,
                    user_id=user_id,
                    tx_type="purchase_debit",
                    delta_irt=-quoted_price,
                    idempotency_key=f"order_debit:{order_id}",
                    reference_type="order",
                    reference_id=order_id,
                    reason=f"Purchase order {order_id} for product {product['code']}",
                )
                ledger_tx_id = tx["id"]

            if product["inventory_mode"] == "manual":
                _, enc_config = self.inventory.claim_and_assign_inventory_in_tx(
                    cur, int(product["id"]), order_id, now
                )
                cur.execute(
                    """INSERT INTO orders
                       (id, user_id, product_id, quote_id, amount_irt, currency, status,
                        delivery_status, idempotency_key, ledger_tx_id, delivered_config_encrypted,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'IRT', 'delivered', 'delivered', ?, ?, ?, ?, ?)""",
                    (
                        order_id,
                        user_id,
                        product["id"],
                        quote_id,
                        quoted_price,
                        idempotency_key,
                        ledger_tx_id,
                        enc_config,
                        now,
                        now,
                    ),
                )
            else:
                needs_external_provisioning = True
                prov_job_id = f"pjob_{secrets.token_hex(10)}"
                cur.execute(
                    """INSERT INTO orders
                       (id, user_id, product_id, quote_id, amount_irt, currency, status,
                        delivery_status, idempotency_key, ledger_tx_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'IRT', 'provisioning', 'provisioning_queued', ?, ?, ?, ?)""",
                    (
                        order_id,
                        user_id,
                        product["id"],
                        quote_id,
                        quoted_price,
                        idempotency_key,
                        ledger_tx_id,
                        now,
                        now,
                    ),
                )
                cur.execute(
                    """INSERT INTO provisioning_jobs
                       (id, order_id, provider_code, idempotency_key, attempts, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 0, 'queued', ?, ?)""",
                    (
                        prov_job_id,
                        order_id,
                        product["provider_code"],
                        f"prov_idem:{order_id}",
                        now,
                        now,
                    ),
                )

        if needs_external_provisioning and prov_job_id:
            self.process_provisioning_job(prov_job_id)

        final_order = self.db.fetch_one("SELECT * FROM orders WHERE id = ?", (order_id,))
        return self._enrich_order_for_user(final_order)

    def process_provisioning_job(self, job_id: str) -> Dict[str, Any]:
        """Execute or reconcile an automated provider provisioning job outside long DB locks (Section 14.1)."""
        job = self.db.fetch_one("SELECT * FROM provisioning_jobs WHERE id = ?", (job_id,))
        if not job:
            raise ValueError("Provisioning job not found")
        if job["status"] in {"succeeded", "compensated"}:
            return job

        order = self.db.fetch_one("SELECT * FROM orders WHERE id = ?", (job["order_id"],))
        product = self.db.fetch_one("SELECT * FROM products WHERE id = ?", (order["product_id"],))
        adapter = self.providers.get(job["provider_code"])
        if not adapter:
            raise ValueError(f"Provider adapter '{job['provider_code']}' not registered")

        now = int(time.time())
        try:
            # If previously ambiguous, query provider by idempotency key first before creating!
            if job["status"] == "ambiguous_reconcile":
                reconciled = adapter.query_subscription_by_idempotency_key(job["idempotency_key"])
                if reconciled:
                    return self._complete_provisioning_delivery(job["id"], order["id"], reconciled)
            result = adapter.create_subscription(
                idempotency_key=job["idempotency_key"],
                user_id=int(order["user_id"]),
                duration_days=int(product["duration_days"]),
                traffic_gb=int(product["traffic_gb"]),
                device_limit=int(product["device_limit"]),
            )
            return self._complete_provisioning_delivery(job["id"], order["id"], result)
        except ProvisioningAmbiguousTimeoutError as exc:
            with self.db.transaction() as cur:
                cur.execute(
                    """UPDATE provisioning_jobs
                       SET status = 'ambiguous_reconcile', attempts = attempts + 1, last_error = ?, updated_at = ?
                       WHERE id = ?""",
                    (str(exc), now, job_id),
                )
                cur.execute(
                    "UPDATE orders SET status = 'failed_Requires_reconciliation', updated_at = ? WHERE id = ?",
                    (now, order["id"]),
                )
            return self.db.fetch_one("SELECT * FROM provisioning_jobs WHERE id = ?", (job_id,))
        except ProvisioningDefiniteFailureError as exc:
            # Definite failure -> automatic idempotent compensating refund!
            return self._compensate_failed_order(job["id"], order["id"], str(exc))

    def _complete_provisioning_delivery(
        self, job_id: str, order_id: str, prov_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        now = int(time.time())
        enc_cfg = encrypt_secret(prov_result["config_uri"], self.master_key)
        with self.db.transaction() as cur:
            cur.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
            current_ord = cur.fetchone()
            if current_ord["status"] == "refunded":
                raise RuntimeError("Cannot deliver an order that has already been refunded")
            cur.execute(
                """UPDATE provisioning_jobs
                   SET status = 'succeeded', attempts = attempts + 1, external_request_id = ?, updated_at = ?
                   WHERE id = ?""",
                (prov_result["provider_sub_id"], now, job_id),
            )
            cur.execute(
                """UPDATE orders
                   SET status = 'delivered', delivery_status = 'delivered',
                       delivered_config_encrypted = ?, provider_sub_id = ?, updated_at = ?
                   WHERE id = ?""",
                (enc_cfg, prov_result["provider_sub_id"], now, order_id),
            )
        return self.db.fetch_one("SELECT * FROM provisioning_jobs WHERE id = ?", (job_id,))

    def _compensate_failed_order(self, job_id: str, order_id: str, error_msg: str) -> Dict[str, Any]:
        """Issue an idempotent compensating wallet refund when provider provisioning permanently fails."""
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            ord_row = cur.fetchone()
            if ord_row["status"] in {"refunded", "delivered"}:
                cur.execute("SELECT * FROM provisioning_jobs WHERE id = ?", (job_id,))
                return dict(cur.fetchone())

            refund_tx_id = None
            if int(ord_row["amount_irt"]) > 0:
                tx = self.wallet.apply_ledger_mutation_in_tx(
                    cur,
                    user_id=int(ord_row["user_id"]),
                    tx_type="refund_credit",
                    delta_irt=int(ord_row["amount_irt"]),
                    idempotency_key=f"order_comp_refund:{order_id}",
                    reference_type="order_refund",
                    reference_id=order_id,
                    reason=f"Automatic compensating refund for failed provisioning on {order_id}: {error_msg}",
                )
                refund_tx_id = tx["id"]

            cur.execute(
                """UPDATE orders
                   SET status = 'refunded', delivery_status = 'compensated',
                       refund_ledger_tx_id = ?, updated_at = ?
                   WHERE id = ?""",
                (refund_tx_id, now, order_id),
            )
            cur.execute(
                """UPDATE provisioning_jobs
                   SET status = 'compensated', attempts = attempts + 1, last_error = ?, updated_at = ?
                   WHERE id = ?""",
                (error_msg, now, job_id),
            )
            cur.execute("SELECT * FROM provisioning_jobs WHERE id = ?", (job_id,))
            return dict(cur.fetchone())

    def _enrich_order_for_user(self, order_row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(order_row)
        if out.get("delivered_config_encrypted"):
            out["delivered_config"] = decrypt_secret(out["delivered_config_encrypted"], self.master_key)
        else:
            out["delivered_config"] = None
        out.pop("delivered_config_encrypted", None)
        return out


class ReferralService:
    """Complete Referral Attribution, Rewards (N, W, P), Milestone Approval (M, K), and Anti-Abuse (Section 16)."""

    def __init__(
        self,
        db: DatabaseManager,
        rbac: AuditAndRBACService,
        wallet: WalletAndPaymentService,
    ) -> None:
        self.db = db
        self.rbac = rbac
        self.wallet = wallet
        # Register hook on wallet verified payments to automatically evaluate P reward!
        self.wallet.on_first_verified_payment_hooks.append(self.handle_first_verified_payment_in_tx)

    def get_or_create_referral_code(self, user_id: int) -> str:
        existing = self.db.fetch_one("SELECT code FROM referral_codes WHERE user_id = ?", (user_id,))
        if existing:
            return existing["code"]
        code = f"ref_{secrets.token_urlsafe(10)}"
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO referral_codes (code, user_id, created_at) VALUES (?, ?, ?)",
                (code, user_id, now),
            )
            cur.execute("SELECT code FROM referral_codes WHERE user_id = ?", (user_id,))
            return cur.fetchone()["code"]

    def get_rules(self) -> Dict[str, Any]:
        return self.db.fetch_one("SELECT * FROM referral_reward_rules WHERE id = 1")

    def update_rules(
        self,
        actor_admin_id: int,
        *,
        is_enabled: bool = True,
        referrer_reward_n_irt: int = 15000,
        referrer_n_trigger: str = "membership_verified",
        new_user_reward_w_irt: int = 10000,
        new_user_w_requires_membership: bool = True,
        first_payment_reward_p_irt: int = 25000,
        milestone_threshold_m: int = 5,
        milestone_reward_k_irt: int = 100000,
        milestone_repeatable: bool = False,
    ) -> Dict[str, Any]:
        self.rbac.require_permission(actor_admin_id, "referrals.configure")
        if referrer_n_trigger not in {"registration", "membership_verified", "first_payment"}:
            raise ValueError("Invalid referrer_n_trigger")
        if milestone_threshold_m <= 0:
            raise ValueError("milestone_threshold_m must be > 0")
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                """UPDATE referral_reward_rules
                   SET is_enabled = ?, referrer_reward_n_irt = ?, referrer_n_trigger = ?,
                       new_user_reward_w_irt = ?, new_user_w_requires_membership = ?,
                       first_payment_reward_p_irt = ?, milestone_threshold_m = ?,
                       milestone_reward_k_irt = ?, milestone_repeatable = ?, updated_at = ?
                   WHERE id = 1""",
                (
                    int(is_enabled),
                    referrer_reward_n_irt,
                    referrer_n_trigger,
                    new_user_reward_w_irt,
                    int(new_user_w_requires_membership),
                    first_payment_reward_p_irt,
                    milestone_threshold_m,
                    milestone_reward_k_irt,
                    int(milestone_repeatable),
                    now,
                ),
            )
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action="referrals.update_rules",
                target_type="referral_reward_rules",
                target_id="1",
                outcome="succeeded",
                metadata={
                    "N": referrer_reward_n_irt,
                    "W": new_user_reward_w_irt,
                    "P": first_payment_reward_p_irt,
                    "M": milestone_threshold_m,
                    "K": milestone_reward_k_irt,
                },
                cur=cur,
            )
            cur.execute("SELECT * FROM referral_reward_rules WHERE id = 1")
            return dict(cur.fetchone())

    def attribute_referral_on_start(
        self, referred_user_id: int, referral_code: str
    ) -> Dict[str, Any]:
        """Immutable referral attribution on `/start` (Section 16.1).
        - Rejects self-referrals by Telegram user ID and internal user ID.
        - Never overwrites an existing referrer or duplicates attribution on repeated `/start`.
        """
        rules = self.get_rules()
        if not rules["is_enabled"]:
            return {"attributed": False, "reason": "disabled"}

        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM referrals WHERE referred_user_id = ?", (referred_user_id,))
            existing_ref = cur.fetchone()
            if existing_ref:
                return {"attributed": False, "reason": "already_attributed", "referral": dict(existing_ref)}

            cur.execute("SELECT * FROM referral_codes WHERE code = ?", (referral_code.strip(),))
            code_row = cur.fetchone()
            if not code_row:
                return {"attributed": False, "reason": "invalid_code"}

            referrer_user_id = int(code_row["user_id"])
            cur.execute("SELECT * FROM users WHERE id = ?", (referred_user_id,))
            referred_user = cur.fetchone()
            cur.execute("SELECT * FROM users WHERE id = ?", (referrer_user_id,))
            referrer_user = cur.fetchone()

            if (
                referrer_user_id == referred_user_id
                or int(referrer_user["telegram_id"]) == int(referred_user["telegram_id"])
            ):
                return {"attributed": False, "reason": "self_referral_rejected"}

            now = int(time.time())
            # Anti-abuse burst detection: >20 referrals in 60 seconds flags for review
            cur.execute(
                "SELECT COUNT(*) AS recent_cnt FROM referrals WHERE referrer_user_id = ? AND created_at >= ?",
                (referrer_user_id, now - 60),
            )
            burst_cnt = int(cur.fetchone()["recent_cnt"])
            status = "flagged_suspicious" if burst_cnt >= 20 else "attributed"
            risk_flag = "burst_registration_rate" if burst_cnt >= 20 else None

            cur.execute(
                """INSERT INTO referrals
                   (referrer_user_id, referred_user_id, referral_code, status, risk_flag, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (referrer_user_id, referred_user_id, referral_code.strip(), status, risk_flag, now),
            )
            ref_id = int(cur.lastrowid)
            cur.execute(
                "UPDATE users SET referred_by_user_id = ? WHERE id = ? AND referred_by_user_id IS NULL",
                (referrer_user_id, referred_user_id),
            )

            if status != "flagged_suspicious":
                if not rules["new_user_w_requires_membership"] and int(rules["new_user_reward_w_irt"]) > 0:
                    self._issue_reward_in_tx(
                        cur,
                        referral_id=ref_id,
                        recipient_user_id=referred_user_id,
                        reward_type="W_new_user",
                        amount_irt=int(rules["new_user_reward_w_irt"]),
                        idempotency_key=f"ref_W:{referred_user_id}",
                        reason="پاداش خوش‌آمدگویی دعوت (W)",
                    )
                if rules["referrer_n_trigger"] == "registration" and int(rules["referrer_reward_n_irt"]) > 0:
                    cur.execute(
                        "UPDATE referrals SET status = 'eligible', qualified_at = ? WHERE id = ?",
                        (now, ref_id),
                    )
                    self._issue_reward_in_tx(
                        cur,
                        referral_id=ref_id,
                        recipient_user_id=referrer_user_id,
                        reward_type="N_referrer",
                        amount_irt=int(rules["referrer_reward_n_irt"]),
                        idempotency_key=f"ref_N:{ref_id}",
                        reason=f"پاداش دعوت کاربر جدید (N) برای دعوت #{ref_id}",
                    )
                    self._check_and_create_milestone_in_tx(cur, referrer_user_id, rules, now)

            cur.execute("SELECT * FROM referrals WHERE id = ?", (ref_id,))
            return {"attributed": True, "referral": dict(cur.fetchone())}

    def on_user_membership_verified(self, user_id: int) -> None:
        """Trigger W (if gated by membership) and N (if triggered on membership_verified)."""
        rules = self.get_rules()
        if not rules["is_enabled"]:
            return
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM referrals WHERE referred_user_id = ?", (user_id,))
            ref = cur.fetchone()
            if not ref or ref["status"] == "flagged_suspicious":
                return
            ref_id = int(ref["id"])
            referrer_id = int(ref["referrer_user_id"])

            if int(rules["new_user_reward_w_irt"]) > 0:
                self._issue_reward_in_tx(
                    cur,
                    referral_id=ref_id,
                    recipient_user_id=user_id,
                    reward_type="W_new_user",
                    amount_irt=int(rules["new_user_reward_w_irt"]),
                    idempotency_key=f"ref_W:{user_id}",
                    reason="پاداش عضویت از طریق لینک دعوت (W)",
                )

            if ref["status"] == "attributed":
                cur.execute(
                    "UPDATE referrals SET status = 'eligible', qualified_at = ? WHERE id = ?",
                    (now, ref_id),
                )
            if rules["referrer_n_trigger"] == "membership_verified" and int(rules["referrer_reward_n_irt"]) > 0:
                self._issue_reward_in_tx(
                    cur,
                    referral_id=ref_id,
                    recipient_user_id=referrer_id,
                    reward_type="N_referrer",
                    amount_irt=int(rules["referrer_reward_n_irt"]),
                    idempotency_key=f"ref_N:{ref_id}",
                    reason=f"پاداش دعوت کاربر تاییدشده (N) برای دعوت #{ref_id}",
                )
            self._check_and_create_milestone_in_tx(cur, referrer_id, rules, now)

    def handle_first_verified_payment_in_tx(
        self, referred_user_id: int, payment_ref: str, cur: Any
    ) -> None:
        """Issue First Verified Payment Reward (P) to the referrer at most once per referred user (Section 16.2.C)."""
        cur.execute("SELECT * FROM referral_reward_rules WHERE id = 1")
        rules = cur.fetchone()
        if not rules or not rules["is_enabled"]:
            return
        cur.execute("SELECT * FROM referrals WHERE referred_user_id = ?", (referred_user_id,))
        ref = cur.fetchone()
        if not ref or ref["status"] == "flagged_suspicious":
            return
        ref_id = int(ref["id"])
        referrer_id = int(ref["referrer_user_id"])
        now = int(time.time())

        cur.execute(
            "UPDATE referrals SET status = 'qualified_payment', qualified_at = COALESCE(qualified_at, ?) WHERE id = ?",
            (now, ref_id),
        )
        if rules["referrer_n_trigger"] == "first_payment" and int(rules["referrer_reward_n_irt"]) > 0:
            self._issue_reward_in_tx(
                cur,
                referral_id=ref_id,
                recipient_user_id=referrer_id,
                reward_type="N_referrer",
                amount_irt=int(rules["referrer_reward_n_irt"]),
                idempotency_key=f"ref_N:{ref_id}",
                reason=f"پاداش دعوت (N) پس از اولین پرداخت کاربر #{referred_user_id}",
            )
        if int(rules["first_payment_reward_p_irt"]) > 0:
            self._issue_reward_in_tx(
                cur,
                referral_id=ref_id,
                recipient_user_id=referrer_id,
                reward_type="P_first_payment",
                amount_irt=int(rules["first_payment_reward_p_irt"]),
                idempotency_key=f"ref_P_first_pay:{referred_user_id}",
                reason=f"پاداش اولین پرداخت موفق کاربر دعوت‌شده (P) - مرجع {payment_ref}",
            )
        self._check_and_create_milestone_in_tx(cur, referrer_id, rules, now)

    def _issue_reward_in_tx(
        self,
        cur: Any,
        *,
        referral_id: Optional[int],
        recipient_user_id: int,
        reward_type: str,
        amount_irt: int,
        idempotency_key: str,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        cur.execute("SELECT * FROM referral_rewards WHERE idempotency_key = ?", (idempotency_key,))
        existing = cur.fetchone()
        if existing:
            return dict(existing)
        tx = self.wallet.apply_ledger_mutation_in_tx(
            cur,
            user_id=recipient_user_id,
            tx_type="referral_reward",
            delta_irt=amount_irt,
            idempotency_key=f"ledger_{idempotency_key}",
            reference_type="referral_reward",
            reference_id=idempotency_key,
            reason=reason,
        )
        rew_id = f"rrew_{secrets.token_hex(10)}"
        now = int(time.time())
        cur.execute(
            """INSERT INTO referral_rewards
               (id, referral_id, recipient_user_id, reward_type, amount_irt,
                idempotency_key, status, ledger_tx_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'credited', ?, ?)""",
            (rew_id, referral_id, recipient_user_id, reward_type, amount_irt, idempotency_key, tx["id"], now),
        )
        cur.execute("SELECT * FROM referral_rewards WHERE id = ?", (rew_id,))
        return dict(cur.fetchone())

    def _check_and_create_milestone_in_tx(
        self, cur: Any, referrer_user_id: int, rules: Any, now: int
    ) -> None:
        """When eligible referrals reach M, create a pending milestone request for K (Section 16.2.D).
        Never auto-credits K without administrator approval!
        """
        cur.execute(
            """SELECT COUNT(*) AS cnt FROM referrals
               WHERE referrer_user_id = ? AND status IN ('eligible', 'qualified_payment')""",
            (referrer_user_id,),
        )
        eligible_cnt = int(cur.fetchone()["cnt"])
        threshold_m = int(rules["milestone_threshold_m"])
        if eligible_cnt < threshold_m:
            return
        milestone_idx = (eligible_cnt // threshold_m) if int(rules["milestone_repeatable"]) else 1
        cur.execute(
            "SELECT * FROM referral_milestone_requests WHERE user_id = ? AND milestone_index = ?",
            (referrer_user_id, milestone_idx),
        )
        if cur.fetchone() is not None:
            return
        req_id = f"mreq_{secrets.token_hex(10)}"
        cur.execute(
            """INSERT INTO referral_milestone_requests
               (id, user_id, milestone_index, threshold_m, eligible_count_snapshot,
                reward_k_irt, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending_review', ?)""",
            (
                req_id,
                referrer_user_id,
                milestone_idx,
                threshold_m,
                eligible_cnt,
                int(rules["milestone_reward_k_irt"]),
                now,
            ),
        )

    def review_milestone_request(
        self,
        actor_admin_id: int,
        request_id: str,
        *,
        approve: bool,
        reason: str,
    ) -> Dict[str, Any]:
        """Concurrency-safe administrator approval or rejection of milestone reward K (Section 16.2.D)."""
        self.rbac.require_permission(actor_admin_id, "referrals.approve_milestone")
        if not reason or not reason.strip():
            raise ValueError("ثبت دلیل تایید یا رد پاداش حدنصاب (Milestone) الزامی است.")
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM referral_milestone_requests WHERE id = ?", (request_id,))
            req = cur.fetchone()
            if not req:
                raise ValueError("درخواست پاداش حدنصاب یافت نشد.")
            if req["status"] != "pending_review":
                raise ValueError(f"این درخواست قبلاً بررسی شده است ({req['status']}).")

            new_status = "approved" if approve else "rejected"
            cur.execute(
                """UPDATE referral_milestone_requests
                   SET status = ?, reviewed_by_admin_id = ?, review_reason = ?, reviewed_at = ?
                   WHERE id = ? AND status = 'pending_review'""",
                (new_status, actor_admin_id, reason.strip(), now, request_id),
            )
            if cur.rowcount != 1:
                raise ValueError("این درخواست هم‌زمان توسط مدیر دیگری بررسی شد.")

            reward_id = None
            if approve:
                rew = self._issue_reward_in_tx(
                    cur,
                    referral_id=None,
                    recipient_user_id=int(req["user_id"]),
                    reward_type="K_milestone",
                    amount_irt=int(req["reward_k_irt"]),
                    idempotency_key=f"ref_K_milestone:{request_id}",
                    reason=f"پاداش حدنصاب دعوت (K) - تایید توسط مدیر #{actor_admin_id}",
                )
                reward_id = rew["id"]
                cur.execute(
                    "UPDATE referral_milestone_requests SET reward_id = ? WHERE id = ?",
                    (reward_id, request_id),
                )

            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action=f"referrals.milestone_{new_status}",
                target_type="referral_milestone_request",
                target_id=request_id,
                outcome="succeeded",
                reason=reason.strip(),
                metadata={"reward_k_irt": req["reward_k_irt"], "reward_id": reward_id},
                cur=cur,
            )
            cur.execute("SELECT * FROM referral_milestone_requests WHERE id = ?", (request_id,))
            return dict(cur.fetchone())

    def reverse_referral_reward(
        self, actor_admin_id: int, reward_id: str, reason: str
    ) -> Dict[str, Any]:
        """Reverse an incorrect referral reward via a compensating ledger transaction (Section 16.4)."""
        self.rbac.require_permission(actor_admin_id, "wallets.adjust")
        if not reason or not reason.strip():
            raise ValueError("Reason is required to reverse a referral reward")
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM referral_rewards WHERE id = ?", (reward_id,))
            rew = cur.fetchone()
            if not rew or rew["status"] != "credited":
                raise ValueError("Reward not found or already reversed")
            tx = self.wallet.apply_ledger_mutation_in_tx(
                cur,
                user_id=int(rew["recipient_user_id"]),
                tx_type="referral_reversal",
                delta_irt=-int(rew["amount_irt"]),
                idempotency_key=f"rev_reward:{reward_id}",
                reference_type="referral_reward_reversal",
                reference_id=reward_id,
                reason=f"Reversal of {reward_id}: {reason.strip()}",
                actor_admin_id=actor_admin_id,
            )
            cur.execute(
                "UPDATE referral_rewards SET status = 'reversed', reversal_ledger_tx_id = ? WHERE id = ?",
                (tx["id"], reward_id),
            )
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action="referrals.reverse_reward",
                target_type="referral_reward",
                target_id=reward_id,
                outcome="succeeded",
                reason=reason.strip(),
                cur=cur,
            )
            cur.execute("SELECT * FROM referral_rewards WHERE id = ?", (reward_id,))
            return dict(cur.fetchone())

    def get_user_referral_dashboard(self, user_id: int, bot_username: str = "VPNStoreBot") -> Dict[str, Any]:
        code = self.get_or_create_referral_code(user_id)
        rules = self.get_rules()
        refs = self.db.fetch_all("SELECT status FROM referrals WHERE referrer_user_id = ?", (user_id,))
        total = len(refs)
        eligible = sum(1 for r in refs if r["status"] in {"eligible", "qualified_payment"})
        qualified_payment = sum(1 for r in refs if r["status"] == "qualified_payment")
        pending = sum(1 for r in refs if r["status"] == "attributed")
        rewards = self.db.fetch_all(
            "SELECT * FROM referral_rewards WHERE recipient_user_id = ? AND status = 'credited'",
            (user_id,),
        )
        earned_irt = sum(int(r["amount_irt"]) for r in rewards)
        milestones = self.db.fetch_all(
            "SELECT * FROM referral_milestone_requests WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        pending_review_irt = sum(
            int(m["reward_k_irt"]) for m in milestones if m["status"] == "pending_review"
        )
        return {
            "referral_code": code,
            "referral_link": f"https://t.me/{bot_username}?start={code}",
            "total_referrals": total,
            "eligible_referrals": eligible,
            "pending_referrals": pending,
            "qualified_payment_referrals": qualified_payment,
            "earned_rewards_irt": earned_irt,
            "pending_review_rewards_irt": pending_review_irt,
            "milestone_threshold_m": int(rules["milestone_threshold_m"]),
            "milestone_reward_k_irt": int(rules["milestone_reward_k_irt"]),
            "milestone_progress": f"{min(eligible, int(rules['milestone_threshold_m']))}/{rules['milestone_threshold_m']}",
            "milestones": milestones,
        }


class MembershipMessagingAndOpsService:
    """Mandatory Telegram Channel Membership with 24h TTL Cache (Section 9),
    Safe Message Template Engine (Section 17), Campaigns, Support, and System Reconciliation.
    """
    PLACEHOLDER_PATTERN = re.compile(r"\{([a-zA-Z0-9_]+)\}")

    def __init__(
        self,
        db: DatabaseManager,
        rbac: AuditAndRBACService,
        referrals: ReferralService,
        telegram_checker: Optional[Callable[[int, str], bool]] = None,
    ) -> None:
        self.db = db
        self.rbac = rbac
        self.referrals = referrals
        self.telegram_checker = telegram_checker or (lambda tg_id, ch_id: True)
        self._singleflight_lock = threading.Lock()
        self.external_api_calls_count = 0

    def add_mandatory_channel(
        self,
        actor_admin_id: int,
        channel_id: str,
        title_fa: str,
        join_url: str,
        cache_ttl_seconds: int = 86400,
    ) -> Dict[str, Any]:
        self.rbac.require_permission(actor_admin_id, "settings.manage")
        with self.db.transaction() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO mandatory_channels
                   (channel_id, title_fa, join_url, is_active, cache_ttl_seconds)
                   VALUES (?, ?, ?, 1, ?)""",
                (channel_id, title_fa, join_url, cache_ttl_seconds),
            )
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action="membership.add_channel",
                target_type="mandatory_channel",
                target_id=channel_id,
                outcome="succeeded",
                cur=cur,
            )
            cur.execute("SELECT * FROM mandatory_channels WHERE channel_id = ?", (channel_id,))
            return dict(cur.fetchone())

    def check_user_membership(
        self,
        user_id: int,
        *,
        force_recheck: bool = False,
        now_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Verify mandatory channel membership using `(user_id, channel_id)` TTL cache (Section 9.1).
        Fails closed (treats as non-member) if Telegram API raises an exception.
        """
        now = now_override if now_override is not None else int(time.time())
        user = self.db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if not user:
            raise ValueError("User not found")
        channels = self.db.fetch_all("SELECT * FROM mandatory_channels WHERE is_active = 1")
        if not channels:
            self.referrals.on_user_membership_verified(user_id)
            return {"all_verified": True, "missing_channels": []}

        missing: List[Dict[str, Any]] = []
        with self._singleflight_lock:
            for ch in channels:
                ch_id = ch["channel_id"]
                cached = self.db.fetch_one(
                    "SELECT * FROM membership_cache WHERE user_id = ? AND channel_id = ?",
                    (user_id, ch_id),
                )
                if (
                    not force_recheck
                    and cached
                    and int(cached["expires_at"]) > now
                    and int(cached["is_member"]) == 1
                ):
                    continue

                # Query Telegram API safely; fail closed on any error
                try:
                    self.external_api_calls_count += 1
                    is_mem = bool(self.telegram_checker(int(user["telegram_id"]), ch_id))
                except Exception:
                    is_mem = False

                ttl = int(ch["cache_ttl_seconds"]) if is_mem else 30
                with self.db.transaction() as cur:
                    cur.execute(
                        """INSERT OR REPLACE INTO membership_cache
                           (user_id, channel_id, is_member, checked_at, expires_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (user_id, ch_id, int(is_mem), now, now + ttl),
                    )
                if not is_mem:
                    missing.append(ch)

        if not missing:
            self.referrals.on_user_membership_verified(user_id)
        return {"all_verified": len(missing) == 0, "missing_channels": missing}

    def render_template(self, key: str, variables: Dict[str, Any]) -> str:
        """Render a configurable Persian template using a strict allowlist of placeholders (Section 17.1).
        Never evaluates code or allows format-string attribute access.
        """
        tpl = self.db.fetch_one("SELECT * FROM message_templates WHERE key = ? AND is_enabled = 1", (key,))
        if not tpl:
            return ""
        allowed: Set[str] = {
            p.strip() for p in tpl["allowed_placeholders"].split(",") if p.strip()
        }
        body: str = tpl["body_fa"]

        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            if var_name not in allowed:
                return f"{{{var_name}}}"
            val = variables.get(var_name, "")
            return str(val)

        return self.PLACEHOLDER_PATTERN.sub(_replace, body)

    def update_message_template(
        self, actor_admin_id: int, key: str, body_fa: str
    ) -> Dict[str, Any]:
        self.rbac.require_permission(actor_admin_id, "settings.manage")
        tpl = self.db.fetch_one("SELECT * FROM message_templates WHERE key = ?", (key,))
        if not tpl:
            raise ValueError("Template key not found")
        allowed = {p.strip() for p in tpl["allowed_placeholders"].split(",") if p.strip()}
        used = set(self.PLACEHOLDER_PATTERN.findall(body_fa))
        unknown = used - allowed
        if unknown:
            raise ValueError(f"Disallowed template placeholders: {sorted(unknown)}")
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                "UPDATE message_templates SET body_fa = ?, version = version + 1, updated_at = ? WHERE key = ?",
                (body_fa, now, key),
            )
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action="messaging.update_template",
                target_type="message_template",
                target_id=key,
                outcome="succeeded",
                cur=cur,
            )
            cur.execute("SELECT * FROM message_templates WHERE key = ?", (key,))
            return dict(cur.fetchone())

    def create_support_ticket(self, user_id: int, subject: str, message_body: str) -> Dict[str, Any]:
        if not subject.strip() or not message_body.strip():
            raise ValueError("عنوان و متن پیام پشتیبانی الزامی است.")
        t_id = f"tkt_{secrets.token_hex(8)}"
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                """INSERT INTO support_tickets
                   (id, user_id, subject, status, priority, created_at, updated_at)
                   VALUES (?, ?, ?, 'open', 'normal', ?, ?)""",
                (t_id, user_id, subject.strip(), now, now),
            )
            cur.execute(
                """INSERT INTO support_messages
                   (ticket_id, sender_type, sender_id, body, created_at)
                   VALUES (?, 'user', ?, ?, ?)""",
                (t_id, user_id, message_body.strip(), now),
            )
            cur.execute("SELECT * FROM support_tickets WHERE id = ?", (t_id,))
            return dict(cur.fetchone())

    def reply_support_ticket(
        self, actor_admin_id: int, ticket_id: str, body: str, internal_note: bool = False
    ) -> Dict[str, Any]:
        self.rbac.require_permission(actor_admin_id, "support.manage")
        now = int(time.time())
        sender_type = "internal_note" if internal_note else "admin"
        with self.db.transaction() as cur:
            if not internal_note:
                cur.execute(
                    "UPDATE support_tickets SET status = 'answered', assigned_admin_id = ?, updated_at = ? WHERE id = ?",
                    (actor_admin_id, now, ticket_id),
                )
            cur.execute(
                """INSERT INTO support_messages
                   (ticket_id, sender_type, sender_id, body, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (ticket_id, sender_type, actor_admin_id, body.strip(), now),
            )
            cur.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,))
            return dict(cur.fetchone())

    def get_dashboard_metrics(self, actor_admin_id: int) -> Dict[str, Any]:
        """Compile accounting-separated metrics for Web and Telegram Admin Dashboards (Section 18.1 & 19.2)."""
        self.rbac.require_permission(actor_admin_id, "orders.read")
        total_users = self.db.fetch_one("SELECT COUNT(*) AS c FROM users")["c"]
        active_users = self.db.fetch_one("SELECT COUNT(*) AS c FROM users WHERE status = 'active'")["c"]
        total_orders = self.db.fetch_one("SELECT COUNT(*) AS c FROM orders")["c"]
        delivered_orders = self.db.fetch_one("SELECT COUNT(*) AS c FROM orders WHERE status = 'delivered'")["c"]
        failed_deliveries = self.db.fetch_one(
            "SELECT COUNT(*) AS c FROM orders WHERE status = 'failed_Requires_reconciliation'"
        )["c"]
        order_revenue_irt = self.db.fetch_one(
            "SELECT IFNULL(SUM(amount_irt), 0) AS s FROM orders WHERE status = 'delivered'"
        )["s"]
        gateway_deposits_irt = self.db.fetch_one(
            "SELECT IFNULL(SUM(amount_irt), 0) AS s FROM payments WHERE status = 'succeeded'"
        )["s"]
        manual_deposits_irt = self.db.fetch_one(
            "SELECT IFNULL(SUM(amount_irt), 0) AS s FROM manual_deposit_requests WHERE status = 'approved'"
        )["s"]
        wallet_liabilities_irt = self.db.fetch_one(
            "SELECT IFNULL(SUM(balance_irt), 0) AS s FROM wallets"
        )["s"]
        pending_manual_deposits = self.db.fetch_one(
            "SELECT COUNT(*) AS c FROM manual_deposit_requests WHERE status = 'pending'"
        )["c"]
        pending_milestones = self.db.fetch_one(
            "SELECT COUNT(*) AS c FROM referral_milestone_requests WHERE status = 'pending_review'"
        )["c"]
        available_inventory = self.db.fetch_one(
            "SELECT COUNT(*) AS c FROM inventory_items WHERE status = 'available'"
        )["c"]

        return {
            "total_users": int(total_users),
            "active_users": int(active_users),
            "total_orders": int(total_orders),
            "delivered_orders": int(delivered_orders),
            "failed_deliveries": int(failed_deliveries),
            "order_revenue_irt": int(order_revenue_irt),
            "order_revenue_formatted": format_persian_irt(int(order_revenue_irt)),
            "gross_gateway_deposits_irt": int(gateway_deposits_irt),
            "gross_manual_deposits_irt": int(manual_deposits_irt),
            "wallet_liabilities_irt": int(wallet_liabilities_irt),
            "pending_manual_deposits": int(pending_manual_deposits),
            "pending_milestones": int(pending_milestones),
            "available_inventory": int(available_inventory),
        }

import hashlib
import json
import httpx
from backend.app.core.config import CurrencyError, irr_to_irt, irt_to_irr
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

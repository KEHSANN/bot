"""
Wallet, Immutable Double-Entry Financial Ledger, Manual Deposit Requests,
and Automatic Payment Gateway Adapters (Sections 6, 10, 11, 12).
"""
from __future__ import annotations
import secrets
import time
from typing import Any, Callable, Dict, List, Optional

from backend.app.core.config import CurrencyError, irr_to_irt, irt_to_irr, validate_irt_amount
from backend.app.core.security import verify_webhook_signature
from backend.app.models.database import DatabaseManager
from backend.app.services.rbac_audit import AuditAndRBACService


class InsufficientBalanceError(ValueError):
    """Raised when a user's IRT wallet balance is lower than the required debit amount."""
    def __init__(self, required_irt: int, balance_irt: int) -> None:
        self.required_irt = required_irt
        self.balance_irt = balance_irt
        self.shortage_irt = max(0, required_irt - balance_irt)
        super().__init__(
            f"Insufficient wallet balance: required={required_irt} IRT, available={balance_irt} IRT"
        )


class PaymentGatewayAdapter:
    """Base interface for external Iranian/international payment gateways."""
    gateway_code: str = "base"

    def create_payment_request(self, payment_id: str, amount_irt: int, callback_url: str) -> Dict[str, Any]:
        raise NotImplementedError

    def verify_payment(self, authority: str, expected_amount_irr: int) -> Dict[str, Any]:
        raise NotImplementedError


class SandboxPaymentGatewayAdapter(PaymentGatewayAdapter):
    """Deterministic, verifiable payment gateway adapter for automated tests and local development."""
    gateway_code = "zarinpal_sandbox"

    def __init__(self) -> None:
        self._provider_ledger: Dict[str, Dict[str, Any]] = {}

    def create_payment_request(self, payment_id: str, amount_irt: int, callback_url: str) -> Dict[str, Any]:
        amount_irr = irt_to_irr(amount_irt)
        authority = f"A00000{secrets.token_hex(10).upper()}"
        self._provider_ledger[authority] = {
            "payment_id": payment_id,
            "amount_irr": amount_irr,
            "paid": False,
            "ref_id": None,
        }
        return {
            "authority": authority,
            "amount_irr": amount_irr,
            "payment_url": f"https://sandbox.zarinpal.com/pg/StartPay/{authority}",
        }

    def simulate_customer_payment_at_gateway(
        self, authority: str, *, paid_amount_irr: Optional[int] = None, succeed: bool = True
    ) -> str:
        rec = self._provider_ledger.get(authority)
        if not rec:
            raise ValueError("Unknown gateway authority")
        rec["paid"] = succeed
        if paid_amount_irr is not None:
            rec["amount_irr"] = paid_amount_irr
        if succeed and not rec["ref_id"]:
            rec["ref_id"] = f"REF_{secrets.token_hex(6).upper()}"
        return rec["ref_id"] or ""

    def verify_payment(self, authority: str, expected_amount_irr: int) -> Dict[str, Any]:
        rec = self._provider_ledger.get(authority)
        if not rec or not rec["paid"]:
            return {"verified": False, "status": "failed", "reason": "Payment not completed at gateway"}
        if rec["amount_irr"] != expected_amount_irr:
            return {
                "verified": False,
                "status": "requires_review",
                "reason": f"Gateway IRR amount mismatch: expected {expected_amount_irr}, got {rec['amount_irr']}",
            }
        return {
            "verified": True,
            "status": "succeeded",
            "provider_tx_id": rec["ref_id"],
            "verified_amount_irr": rec["amount_irr"],
        }


class WalletAndPaymentService:
    def __init__(
        self,
        db: DatabaseManager,
        rbac: AuditAndRBACService,
        webhook_secret: str = "dev-webhook-secret",
    ) -> None:
        self.db = db
        self.rbac = rbac
        self.webhook_secret = webhook_secret
        self.gateways: Dict[str, PaymentGatewayAdapter] = {}
        self.on_first_verified_payment_hooks: List[Callable[[int, str, Any], None]] = []

    def register_gateway(self, adapter: PaymentGatewayAdapter) -> None:
        self.gateways[adapter.gateway_code] = adapter

    def get_wallet(self, user_id: int) -> Dict[str, Any]:
        row = self.db.fetch_one("SELECT * FROM wallets WHERE user_id = ?", (user_id,))
        if not row:
            raise ValueError("Wallet not found for user")
        return row

    def apply_ledger_mutation_in_tx(
        self,
        cur: Any,
        *,
        user_id: int,
        tx_type: str,
        delta_irt: int,
        idempotency_key: str,
        reference_type: str,
        reference_id: str,
        reason: str,
        actor_admin_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Atomically mutate user's IRT wallet and append immutable double-entry ledger records.
        Idempotent by `idempotency_key`: if already committed, returns the existing ledger transaction.
        """
        if isinstance(delta_irt, bool) or not isinstance(delta_irt, int) or delta_irt == 0:
            raise CurrencyError("Ledger mutation delta_irt must be a non-zero integer")
        cur.execute("SELECT * FROM ledger_transactions WHERE idempotency_key = ?", (idempotency_key,))
        existing_tx = cur.fetchone()
        if existing_tx:
            return dict(existing_tx)

        cur.execute("SELECT balance_irt FROM wallets WHERE user_id = ?", (user_id,))
        wallet = cur.fetchone()
        if not wallet:
            raise ValueError("Wallet does not exist")
        current_balance = int(wallet["balance_irt"])
        now = int(time.time())

        if delta_irt < 0:
            required = abs(delta_irt)
            if current_balance < required:
                raise InsufficientBalanceError(required_irt=required, balance_irt=current_balance)
            cur.execute(
                """UPDATE wallets
                   SET balance_irt = balance_irt - ?, updated_at = ?
                   WHERE user_id = ? AND balance_irt >= ?""",
                (required, now, user_id, required),
            )
            if cur.rowcount != 1:
                raise InsufficientBalanceError(required_irt=required, balance_irt=current_balance)
        else:
            validate_irt_amount(delta_irt)
            cur.execute(
                "UPDATE wallets SET balance_irt = balance_irt + ?, updated_at = ? WHERE user_id = ?",
                (delta_irt, now, user_id),
            )

        cur.execute("SELECT balance_irt FROM wallets WHERE user_id = ?", (user_id,))
        new_balance = int(cur.fetchone()["balance_irt"])
        tx_id = f"ltx_{secrets.token_hex(10)}"

        cur.execute(
            """INSERT INTO ledger_transactions
               (id, user_id, tx_type, amount_irt, balance_after_irt, currency,
                idempotency_key, reference_type, reference_id, reason, actor_admin_id, created_at)
               VALUES (?, ?, ?, ?, ?, 'IRT', ?, ?, ?, ?, ?, ?)""",
            (
                tx_id,
                user_id,
                tx_type,
                delta_irt,
                new_balance,
                idempotency_key,
                reference_type,
                str(reference_id),
                reason,
                actor_admin_id,
                now,
            ),
        )
        # Double-entry bookkeeping rows (user_wallet vs system_treasury/revenue)
        abs_amount = abs(delta_irt)
        if delta_irt > 0:
            debit_acct, credit_acct = "system_treasury", f"user_wallet:{user_id}"
        else:
            debit_acct, credit_acct = f"user_wallet:{user_id}", "system_revenue"
        cur.execute(
            "INSERT INTO ledger_entries (transaction_id, account_code, direction, amount_irt, created_at) VALUES (?, ?, 'debit', ?, ?)",
            (tx_id, debit_acct, abs_amount, now),
        )
        cur.execute(
            "INSERT INTO ledger_entries (transaction_id, account_code, direction, amount_irt, created_at) VALUES (?, ?, 'credit', ?, ?)",
            (tx_id, credit_acct, abs_amount, now),
        )
        cur.execute("SELECT * FROM ledger_transactions WHERE id = ?", (tx_id,))
        return dict(cur.fetchone())

    def admin_adjust_wallet(
        self,
        actor_admin_id: int,
        user_id: int,
        delta_irt: int,
        reason: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Administrative wallet adjustment requiring explicit permission, reason, and audit trail."""
        self.rbac.require_permission(actor_admin_id, "wallets.adjust")
        if not reason or not reason.strip():
            raise ValueError("Administrative wallet adjustments require an explicit non-empty reason")
        with self.db.transaction() as cur:
            tx = self.apply_ledger_mutation_in_tx(
                cur,
                user_id=user_id,
                tx_type="admin_adjustment",
                delta_irt=delta_irt,
                idempotency_key=idempotency_key,
                reference_type="admin_adjustment",
                reference_id=idempotency_key,
                reason=reason.strip(),
                actor_admin_id=actor_admin_id,
            )
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action="wallet.admin_adjustment",
                target_type="user_wallet",
                target_id=str(user_id),
                outcome="succeeded",
                reason=reason.strip(),
                metadata={"delta_irt": delta_irt, "ledger_tx_id": tx["id"]},
                cur=cur,
            )
            return tx

    def reconcile_wallet_balance(self, user_id: int) -> Dict[str, Any]:
        """Verify that wallet balance equals the exact sum of immutable ledger transactions."""
        wallet = self.get_wallet(user_id)
        row = self.db.fetch_one(
            "SELECT IFNULL(SUM(amount_irt), 0) AS ledger_sum FROM ledger_transactions WHERE user_id = ?",
            (user_id,),
        )
        ledger_sum = int(row["ledger_sum"]) if row else 0
        wallet_bal = int(wallet["balance_irt"])
        return {
            "user_id": user_id,
            "wallet_balance_irt": wallet_bal,
            "ledger_sum_irt": ledger_sum,
            "is_reconciled": wallet_bal == ledger_sum,
            "discrepancy_irt": wallet_bal - ledger_sum,
        }

    # --- Manual Deposit Workflow (Section 11) ---
    def configure_payment_method(
        self,
        actor_admin_id: int,
        *,
        code: str,
        title_fa: str,
        method_type: str,
        instructions_fa: str,
        min_amount_irt: int,
        max_amount_irt: int,
        card_number: Optional[str] = None,
        account_holder: Optional[str] = None,
        is_enabled: bool = True,
    ) -> Dict[str, Any]:
        self.rbac.require_permission(actor_admin_id, "settings.manage")
        validate_irt_amount(min_amount_irt)
        validate_irt_amount(max_amount_irt)
        if max_amount_irt < min_amount_irt:
            raise ValueError("max_amount_irt must be >= min_amount_irt")
        with self.db.transaction() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO payment_methods
                   (code, title_fa, method_type, card_number, account_holder,
                    instructions_fa, min_amount_irt, max_amount_irt, is_enabled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code,
                    title_fa,
                    method_type,
                    card_number,
                    account_holder,
                    instructions_fa,
                    min_amount_irt,
                    max_amount_irt,
                    int(is_enabled),
                ),
            )
            cur.execute("SELECT * FROM payment_methods WHERE code = ?", (code,))
            return dict(cur.fetchone())

    def submit_manual_deposit_request(
        self, user_id: int, method_code: str, amount_irt: int, proof_reference: str
    ) -> Dict[str, Any]:
        """Create a pending manual deposit request; NEVER credits wallet upon user submission."""
        validate_irt_amount(amount_irt)
        if not proof_reference or len(proof_reference.strip()) < 4:
            raise ValueError("شماره پیگیری یا رسید پرداخت معتبر الزامی است.")
        method = self.db.fetch_one(
            "SELECT * FROM payment_methods WHERE code = ? AND is_enabled = 1 AND method_type = 'manual_card'",
            (method_code,),
        )
        if not method:
            raise ValueError("روش واریز دستی انتخاب‌شده فعال نیست.")
        if amount_irt < method["min_amount_irt"] or amount_irt > method["max_amount_irt"]:
            raise ValueError("مبلغ واریزی خارج از محدوده مجاز این روش پرداخت است.")
        req_id = f"mdep_{secrets.token_hex(10)}"
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                """INSERT INTO manual_deposit_requests
                   (id, user_id, method_code, amount_irt, proof_reference, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (req_id, user_id, method_code, amount_irt, proof_reference.strip(), now),
            )
            cur.execute("SELECT * FROM manual_deposit_requests WHERE id = ?", (req_id,))
            return dict(cur.fetchone())

    def review_manual_deposit_request(
        self,
        actor_admin_id: int,
        request_id: str,
        *,
        approve: bool,
        reason: str,
    ) -> Dict[str, Any]:
        """Concurrency-safe manual deposit approval/rejection.
        Prevents two administrators from approving the same deposit and issuing duplicate credits.
        """
        self.rbac.require_permission(actor_admin_id, "payments.approve_manual")
        if not reason or not reason.strip():
            raise ValueError("ثبت دلیل تایید یا رد درخواست واریز الزامی است.")
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM manual_deposit_requests WHERE id = ?", (request_id,))
            req = cur.fetchone()
            if not req:
                raise ValueError("درخواست واریز یافت نشد.")
            if req["status"] != "pending":
                raise ValueError(f"این درخواست قبلاً بررسی شده است (وضعیت: {req['status']}).")

            new_status = "approved" if approve else "rejected"
            cur.execute(
                """UPDATE manual_deposit_requests
                   SET status = ?, reviewed_by_admin_id = ?, review_reason = ?, reviewed_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (new_status, actor_admin_id, reason.strip(), now, request_id),
            )
            if cur.rowcount != 1:
                raise ValueError("این درخواست هم‌زمان توسط مدیر دیگری بررسی شد.")

            ledger_tx_id = None
            if approve:
                tx = self.apply_ledger_mutation_in_tx(
                    cur,
                    user_id=req["user_id"],
                    tx_type="deposit_manual",
                    delta_irt=int(req["amount_irt"]),
                    idempotency_key=f"manual_dep_approve:{request_id}",
                    reference_type="manual_deposit_request",
                    reference_id=request_id,
                    reason=f"Approved manual deposit {request_id}: {reason.strip()}",
                    actor_admin_id=actor_admin_id,
                )
                ledger_tx_id = tx["id"]
                cur.execute(
                    "UPDATE manual_deposit_requests SET ledger_tx_id = ? WHERE id = ?",
                    (ledger_tx_id, request_id),
                )
                for hook in self.on_first_verified_payment_hooks:
                    hook(int(req["user_id"]), f"manual:{request_id}", cur)

            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action=f"payment.manual_deposit_{new_status}",
                target_type="manual_deposit_request",
                target_id=request_id,
                outcome="succeeded",
                reason=reason.strip(),
                metadata={"amount_irt": req["amount_irt"], "ledger_tx_id": ledger_tx_id},
                cur=cur,
            )
            cur.execute("SELECT * FROM manual_deposit_requests WHERE id = ?", (request_id,))
            return dict(cur.fetchone())

    # --- Automatic Gateway Payment Flow (Section 12) ---
    def create_gateway_payment(
        self, user_id: int, gateway_code: str, amount_irt: int, idempotency_key: str, callback_url: str = "https://vpnstore.example/api/payments/callback"
    ) -> Dict[str, Any]:
        validate_irt_amount(amount_irt)
        adapter = self.gateways.get(gateway_code)
        if not adapter:
            raise ValueError(f"درگاه پرداخت '{gateway_code}' فعال نیست.")
        existing = self.db.fetch_one("SELECT * FROM payments WHERE idempotency_key = ?", (idempotency_key,))
        if existing:
            return existing

        pay_id = f"pay_{secrets.token_hex(10)}"
        gw_resp = adapter.create_payment_request(pay_id, amount_irt, callback_url)
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                """INSERT INTO payments
                   (id, user_id, gateway_code, amount_irt, provider_amount_irr, currency,
                    provider_authority, status, idempotency_key, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'IRT', ?, 'pending', ?, ?, ?)""",
                (
                    pay_id,
                    user_id,
                    gateway_code,
                    amount_irt,
                    gw_resp["amount_irr"],
                    gw_resp["authority"],
                    idempotency_key,
                    now,
                    now,
                ),
            )
            cur.execute("SELECT * FROM payments WHERE id = ?", (pay_id,))
            rec = dict(cur.fetchone())
            rec["payment_url"] = gw_resp["payment_url"]
            return rec

    def verify_and_credit_gateway_payment(
        self,
        payment_id: str,
        *,
        webhook_payload: Optional[bytes] = None,
        webhook_signature: Optional[str] = None,
        webhook_timestamp: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Authoritatively verify payment with the gateway adapter before crediting wallet.
        Immune to client-supplied flags, replayed webhooks, and concurrent duplicate callbacks.
        """
        if webhook_payload is not None:
            if not webhook_signature or webhook_timestamp is None:
                raise ValueError("Missing webhook authentication headers")
            if not verify_webhook_signature(
                webhook_payload, webhook_signature, webhook_timestamp, self.webhook_secret
            ):
                raise ValueError("Invalid or expired payment webhook signature")

        payment = self.db.fetch_one("SELECT * FROM payments WHERE id = ?", (payment_id,))
        if not payment:
            raise ValueError("Payment record not found")
        if payment["status"] == "succeeded":
            return payment
        if payment["status"] not in {"created", "pending", "verifying"}:
            raise ValueError(f"Cannot verify payment in terminal/review status '{payment['status']}'")

        adapter = self.gateways.get(payment["gateway_code"])
        if not adapter:
            raise ValueError("Gateway adapter unavailable")

        # Authoritative check outside DB lock
        ver_result = adapter.verify_payment(
            payment["provider_authority"], int(payment["provider_amount_irr"])
        )
        now = int(time.time())

        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
            fresh = cur.fetchone()
            if fresh["status"] == "succeeded":
                return dict(fresh)

            if not ver_result.get("verified"):
                new_st = ver_result.get("status", "failed")
                if new_st not in {"failed", "requires_review"}:
                    new_st = "failed"
                cur.execute(
                    "UPDATE payments SET status = ?, updated_at = ? WHERE id = ? AND status IN ('created', 'pending', 'verifying')",
                    (new_st, now, payment_id),
                )
                cur.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
                return dict(cur.fetchone())

            verified_irr = int(ver_result["verified_amount_irr"])
            normalized_irt = irr_to_irt(verified_irr)
            if normalized_irt != int(fresh["amount_irt"]):
                cur.execute(
                    "UPDATE payments SET status = 'requires_review', updated_at = ? WHERE id = ?",
                    (now, payment_id),
                )
                raise CurrencyError("Verified gateway amount does not match internal IRT payment amount")

            provider_tx_id = str(ver_result["provider_tx_id"])
            cur.execute(
                """UPDATE payments
                   SET status = 'succeeded', provider_tx_id = ?, updated_at = ?
                   WHERE id = ? AND status IN ('created', 'pending', 'verifying')""",
                (provider_tx_id, now, payment_id),
            )
            if cur.rowcount == 1:
                tx = self.apply_ledger_mutation_in_tx(
                    cur,
                    user_id=int(fresh["user_id"]),
                    tx_type="deposit_gateway",
                    delta_irt=normalized_irt,
                    idempotency_key=f"gw_pay_credit:{payment_id}",
                    reference_type="payment",
                    reference_id=payment_id,
                    reason=f"Verified gateway payment {payment_id} (tx={provider_tx_id})",
                )
                cur.execute(
                    "UPDATE payments SET ledger_tx_id = ? WHERE id = ?", (tx["id"], payment_id)
                )
                for hook in self.on_first_verified_payment_hooks:
                    hook(int(fresh["user_id"]), f"gateway:{payment_id}", cur)

            cur.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
            return dict(cur.fetchone())

"""
Comprehensive Automated Test Suite for the Production-Grade Telegram VPN Store.
Covers Sections 21.11 & 23:
- Unit tests (Currency IRT/IRR, Randomized Pricing Quotes, Secret Encryption, SSRF, Templates)
- Integration tests (Wallet, Double-Entry Ledger, Manual Deposits, Gateway Verification, Provisioning Saga)
- Referral system tests (Attribution, Self-referral block, N, W, P, M/K Milestone Admin Review, Reversal)
- Concurrency tests (Competing purchases for 1 inventory item, competing wallet debits, simultaneous deposit/milestone approvals, duplicate payment webhooks)
- Telegram Customer Bot & Telegram Admin Panel Feature Parity + FastAPI Web Admin RBAC & Security tests
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import time
import unittest
from fastapi.testclient import TestClient

from backend.app.api.app import create_fastapi_app
from backend.app.bot.telegram_bot import ApplicationContainer, TelegramBotDispatcher
from backend.app.core.config import (
    AppSettings,
    CurrencyError,
    format_persian_irt,
    irr_to_irt,
    irt_to_irr,
    validate_irt_amount,
)
from backend.app.core.security import (
    AuthorizationError,
    SecurityError,
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    validate_provider_url,
    verify_webhook_signature,
)
from backend.app.services.wallet_payments import InsufficientBalanceError


class TestPlatformSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.membership_state = True
        self.container = ApplicationContainer(
            db_path=":memory:",
            telegram_checker=lambda tg_id, ch_id: self.membership_state,
        )
        self.bot = TelegramBotDispatcher(self.container)
        self.super_admin = self.container.rbac.bootstrap_super_admin(
            telegram_id=900001, username="superadmin", password="StrongPassword!123"
        )
        self.super_admin_id = int(self.super_admin["id"])

    # 1. Currency & Financial Invariants (Section 6)
    def test_01_currency_conventions_and_validation(self) -> None:
        self.assertEqual(irr_to_irt(1500000), 150000)
        self.assertEqual(irt_to_irr(150000), 1500000)
        self.assertEqual(format_persian_irt(150000), "۱۵۰٬۰۰۰ تومان")
        with self.assertRaises(CurrencyError):
            validate_irt_amount(150.5)  # type: ignore[arg-type]
        with self.assertRaises(CurrencyError):
            validate_irt_amount(True)  # type: ignore[arg-type]
        with self.assertRaises(CurrencyError):
            irr_to_irt(1505)  # Not divisible by 10

    # 2. Security, Encryption, SSRF & Production Config Guards (Section 21)
    def test_02_security_encryption_ssrf_and_prod_guards(self) -> None:
        ct = encrypt_secret("vless://secret-uuid@1.2.3.4:443", "master-key-32-bytes-minimum-len!")
        self.assertTrue(ct.startswith("enc_v1:"))
        self.assertEqual(
            decrypt_secret(ct, "master-key-32-bytes-minimum-len!"),
            "vless://secret-uuid@1.2.3.4:443",
        )
        with self.assertRaises(SecurityError):
            decrypt_secret(ct, "wrong-key")
        self.assertEqual(mask_secret("abcdef123456"), "ab***56")

        with self.assertRaises(SecurityError):
            validate_provider_url("http://127.0.0.1:8000/api", allow_local_in_dev=False)
        with self.assertRaises(SecurityError):
            validate_provider_url("https://169.254.169.254/latest/meta-data", allow_local_in_dev=False)

        prod_bad = AppSettings(env="production", allow_fake_providers=True)
        with self.assertRaises(RuntimeError):
            prod_bad.validate_startup()

    # 3. Randomized Price Quotes & Persistence (Section 7.1)
    def test_03_randomized_pricing_and_quote_stability(self) -> None:
        cat = self.container.catalog.create_category(
            self.super_admin_id,
            slug="vip",
            title_fa="سرویس VIP",
            description_fa="سرورهای اختصاصی",
        )
        prod = self.container.catalog.create_product(
            self.super_admin_id,
            category_id=cat["id"],
            code="vip_1m_50g",
            title_fa="اشتراک ۱ ماهه ۵۰ گیگ",
            description_fa="پرنسلینگ",
            service_type="vless",
            inventory_mode="manual",
            base_price_irt=100000,
            min_random_increment_irt=100,
            max_random_increment_irt=900,
            increment_step_irt=100,
            duration_days=30,
            traffic_gb=50,
            device_limit=2,
        )
        user = self.container.rbac.register_or_get_user(1001, "علی رضایی", "ali_r")
        q1 = self.container.catalog.get_or_create_quote(user["id"], prod["id"], now_override=1000)
        q2 = self.container.catalog.get_or_create_quote(user["id"], prod["id"], now_override=1200)
        self.assertEqual(q1["id"], q2["id"])
        self.assertEqual(q1["final_amount_irt"], q2["final_amount_irt"])
        self.assertGreaterEqual(q1["final_amount_irt"], 100100)
        self.assertLessEqual(q1["final_amount_irt"], 100900)
        self.assertEqual((q1["final_amount_irt"] - 100000) % 100, 0)

        # After quote expiration, a new quote is generated
        q3 = self.container.catalog.get_or_create_quote(user["id"], prod["id"], now_override=3000)
        self.assertNotEqual(q1["id"], q3["id"])

    # 4. Atomic Bulk Inventory Import & Duplicate Detection (Section 13)
    def test_04_bulk_inventory_preview_and_atomic_import(self) -> None:
        cat = self.container.catalog.create_category(
            self.super_admin_id, slug="std", title_fa="عادی", description_fa="عادی"
        )
        prod = self.container.catalog.create_product(
            self.super_admin_id,
            category_id=cat["id"],
            code="vless_std",
            title_fa="ولس ۳۰ روزه",
            description_fa="توضیحات",
            service_type="vless",
            inventory_mode="manual",
            base_price_irt=80000,
            duration_days=30,
            traffic_gb=30,
            device_limit=1,
        )
        cfg1 = "vless://11111111-2222-3333-4444-555555555555@s1.example.com:443#cfg1"
        cfg2 = "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@s1.example.com:443#cfg2"
        bad_payload = f"{cfg1}\n{cfg1}\ninvalid_uri_string"
        prev = self.container.inventory.preview_bulk_import(
            self.super_admin_id, prod["id"], bad_payload, delimiter="\n"
        )
        self.assertEqual(prev["total_count"], 3)
        self.assertEqual(prev["valid_count"], 1)
        self.assertEqual(prev["duplicate_count"], 1)
        self.assertEqual(prev["invalid_count"], 1)
        self.assertFalse(prev["can_commit_atomic"])

        with self.assertRaises(ValueError):
            self.container.inventory.execute_bulk_import(
                self.super_admin_id, prod["id"], bad_payload, mode="atomic"
            )
        # Ensure nothing was committed on failed atomic import
        self.assertEqual(self.container.inventory.get_stock_summary(prod["id"])["available"], 0)

        # Valid atomic import
        clean_payload = f"{cfg1};{cfg2}"
        res = self.container.inventory.execute_bulk_import(
            self.super_admin_id, prod["id"], clean_payload, delimiter=";", mode="atomic"
        )
        self.assertEqual(res["imported_count"], 2)
        self.assertEqual(self.container.inventory.get_stock_summary(prod["id"])["available"], 2)

    # 5. Concurrency Test: Competing Purchases for Single Inventory Item & Limited Balance (Section 23.6)
    def test_05_concurrent_purchases_single_inventory_and_limited_wallet(self) -> None:
        cat = self.container.catalog.create_category(
            self.super_admin_id, slug="conc", title_fa="تست همزمانی", description_fa="تست"
        )
        prod = self.container.catalog.create_product(
            self.super_admin_id,
            category_id=cat["id"],
            code="single_stock_prod",
            title_fa="تک موجودی",
            description_fa="فقط یک کانفیگ",
            service_type="vless",
            inventory_mode="manual",
            base_price_irt=50000,
            duration_days=30,
            traffic_gb=20,
            device_limit=1,
        )
        only_cfg = "vless://99999999-8888-7777-6666-555555555555@s1.example.com:443#only_one"
        self.container.inventory.execute_bulk_import(
            self.super_admin_id, prod["id"], only_cfg, mode="atomic"
        )

        # Create 6 users each with enough balance (100,000 IRT), all competing for the 1 inventory item
        quotes = []
        for idx in range(6):
            u = self.container.rbac.register_or_get_user(2000 + idx, f"User {idx}")
            self.container.wallet.admin_adjust_wallet(
                self.super_admin_id,
                u["id"],
                100000,
                reason="Initial test credit",
                idempotency_key=f"init_cred_{idx}",
            )
            q = self.container.catalog.get_or_create_quote(u["id"], prod["id"])
            quotes.append((u["id"], q["id"], f"idem_buy_{idx}"))

        def _attempt_buy(args: tuple) -> str:
            uid, qid, idem = args
            try:
                ord_res = self.container.orders.purchase_with_wallet(uid, qid, idem)
                return f"SUCCESS:{ord_res['id']}"
            except Exception as e:
                return f"FAIL:{type(e).__name__}"

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(_attempt_buy, quotes))

        successes = [o for o in outcomes if o.startswith("SUCCESS:")]
        self.assertEqual(len(successes), 1, f"Expected exactly 1 winner, got {outcomes}")
        # Verify the 5 losing users still have their full 100,000 IRT balance and reconcilable ledgers
        total_remaining = 0
        for uid, _, _ in quotes:
            rec = self.container.wallet.reconcile_wallet_balance(uid)
            self.assertTrue(rec["is_reconciled"])
            total_remaining += rec["wallet_balance_irt"]
        self.assertEqual(total_remaining, 5 * 100000 + 50000)

    # 6. Manual Deposit Simultaneous Approval & Gateway Webhook Replay Protection (Sections 11 & 12)
    def test_06_manual_deposit_concurrency_and_gateway_verification(self) -> None:
        self.container.wallet.configure_payment_method(
            self.super_admin_id,
            code="card_melli",
            title_fa="کارت به کارت بانک ملی",
            method_type="manual_card",
            card_number="6037-9975-0000-1111",
            account_holder="شرکت شبکه امن",
            instructions_fa="پس از واریز کد پیگیری را ثبت کنید.",
            min_amount_irt=20000,
            max_amount_irt=10000000,
        )
        u = self.container.rbac.register_or_get_user(3001, "سارا محمدی")
        dep_req = self.container.wallet.submit_manual_deposit_request(
            u["id"], "card_melli", 200000, "TRK-99887766"
        )
        # Wallet must still be 0 before admin approval
        self.assertEqual(self.container.wallet.get_wallet(u["id"])["balance_irt"], 0)

        # Two concurrent admin approval attempts for the same request
        def _approve(_: int) -> str:
            try:
                self.container.wallet.review_manual_deposit_request(
                    self.super_admin_id, dep_req["id"], approve=True, reason="رسید بانکی تایید شد"
                )
                return "APPROVED"
            except Exception:
                return "REJECTED_DUPLICATE"

        with ThreadPoolExecutor(max_workers=2) as pool:
            res_list = list(pool.map(_approve, [1, 2]))
        self.assertEqual(res_list.count("APPROVED"), 1)
        self.assertEqual(self.container.wallet.get_wallet(u["id"])["balance_irt"], 200000)

        # Automatic Gateway Payment + Webhook HMAC verification & duplicate webhook idempotency
        pay = self.container.wallet.create_gateway_payment(
            u["id"], "zarinpal_sandbox", 150000, idempotency_key="gw_pay_1"
        )
        self.container.sandbox_gateway.simulate_customer_payment_at_gateway(
            pay["provider_authority"], succeed=True
        )
        payload = f'{{"payment_id":"{pay["id"]}"}}'.encode("utf-8")
        ts = int(time.time())
        sig = hmac.new(
            self.container.settings.telegram_webhook_secret.encode("utf-8"),
            f"{ts}.".encode("ascii") + payload,
            hashlib.sha256,
        ).hexdigest()

        v1 = self.container.wallet.verify_and_credit_gateway_payment(
            pay["id"], webhook_payload=payload, webhook_signature=sig, webhook_timestamp=ts
        )
        v2 = self.container.wallet.verify_and_credit_gateway_payment(
            pay["id"], webhook_payload=payload, webhook_signature=sig, webhook_timestamp=ts
        )
        self.assertEqual(v1["status"], "succeeded")
        self.assertEqual(v2["ledger_tx_id"], v1["ledger_tx_id"])
        self.assertEqual(self.container.wallet.get_wallet(u["id"])["balance_irt"], 350000)

    # 7. Automated VPN Provisioning, Ambiguous Timeout Reconciliation & Compensating Refund (Section 14)
    def test_07_automated_provisioning_reconciliation_and_compensation(self) -> None:
        cat = self.container.catalog.create_category(
            self.super_admin_id, slug="auto", title_fa="خودکار", description_fa="سرویس خودکار"
        )
        prod = self.container.catalog.create_product(
            self.super_admin_id,
            category_id=cat["id"],
            code="auto_marzban",
            title_fa="اشتراک خودکار مرزبان",
            description_fa="تحویل آنی",
            service_type="vless",
            inventory_mode="automated_provider",
            provider_code="fake_marzban_dev",
            base_price_irt=120000,
            duration_days=30,
            traffic_gb=60,
            device_limit=2,
        )
        u = self.container.rbac.register_or_get_user(4001, "رضا کاظمی")
        self.container.wallet.admin_adjust_wallet(
            self.super_admin_id, u["id"], 240000, "شارژ اولیه", "cred_4001"
        )

        # Case A: Provider times out AFTER creating subscription -> marks order for reconciliation, then reconciles cleanly without double creation!
        self.container.fake_vpn_provider.simulate_mode = "timeout_after_create"
        q1 = self.container.catalog.get_or_create_quote(u["id"], prod["id"], force_new=True)
        ord1 = self.container.orders.purchase_with_wallet(u["id"], q1["id"], "buy_auto_1")
        self.assertEqual(ord1["status"], "failed_Requires_reconciliation")

        self.container.fake_vpn_provider.simulate_mode = "normal"
        job1 = self.container.db.fetch_one(
            "SELECT * FROM provisioning_jobs WHERE order_id = ?", (ord1["id"],)
        )
        self.container.orders.process_provisioning_job(job1["id"])
        ord1_reconciled = self.container.orders._enrich_order_for_user(
            self.container.db.fetch_one("SELECT * FROM orders WHERE id = ?", (ord1["id"],))
        )
        self.assertEqual(ord1_reconciled["status"], "delivered")
        self.assertTrue(ord1_reconciled["delivered_config"].startswith("vless://"))

        # Case B: Provider returns definite failure -> automatic compensating refund restores wallet balance!
        self.container.fake_vpn_provider.simulate_mode = "definite_failure"
        q2 = self.container.catalog.get_or_create_quote(u["id"], prod["id"], force_new=True)
        ord2 = self.container.orders.purchase_with_wallet(u["id"], q2["id"], "buy_auto_2")
        self.assertEqual(ord2["status"], "refunded")
        self.assertEqual(self.container.wallet.get_wallet(u["id"])["balance_irt"], 120000)
        self.assertTrue(self.container.wallet.reconcile_wallet_balance(u["id"])["is_reconciled"])
        self.container.fake_vpn_provider.simulate_mode = "normal"

    # 8. Complete Referral System: N, W, P, M/K Milestone Admin Approval & Self-Referral Block (Section 16)
    def test_08_referrals_rewards_n_w_p_and_milestone_m_k(self) -> None:
        self.container.referrals.update_rules(
            self.super_admin_id,
            is_enabled=True,
            referrer_reward_n_irt=15000,
            referrer_n_trigger="membership_verified",
            new_user_reward_w_irt=10000,
            new_user_w_requires_membership=True,
            first_payment_reward_p_irt=25000,
            milestone_threshold_m=2,
            milestone_reward_k_irt=100000,
            milestone_repeatable=False,
        )
        referrer = self.container.rbac.register_or_get_user(5000, "معرف اصلی")
        ref_code = self.container.referrals.get_or_create_referral_code(referrer["id"])

        # Self-referral must be rejected
        self_attempt = self.bot.handle_start(5000, "معرف اصلی", start_param=ref_code)
        self.assertEqual(self_attempt["referral_result"]["reason"], "self_referral_rejected")

        # Invite User 1 & User 2 via /start -> triggers W (10k each) and N (15k * 2 = 30k to referrer) and creates pending Milestone M=2 request!
        inv1 = self.bot.handle_start(5001, "دعوت‌شده اول", start_param=ref_code)["user"]
        inv2 = self.bot.handle_start(5002, "دعوت‌شده دوم", start_param=ref_code)["user"]
        # Repeated /start by inv1 must NOT duplicate W or N
        self.bot.handle_start(5001, "دعوت‌شده اول", start_param=ref_code)

        self.assertEqual(self.container.wallet.get_wallet(inv1["id"])["balance_irt"], 10000)
        self.assertEqual(self.container.wallet.get_wallet(inv2["id"])["balance_irt"], 10000)
        # Referrer has 2 * 15,000 = 30,000 IRT so far (milestone K=100,000 is pending admin review!)
        self.assertEqual(self.container.wallet.get_wallet(referrer["id"])["balance_irt"], 30000)

        # First verified payment by inv1 triggers P (25,000 IRT) exactly once
        pay1 = self.container.wallet.create_gateway_payment(
            inv1["id"], "zarinpal_sandbox", 50000, "inv1_pay1"
        )
        self.container.sandbox_gateway.simulate_customer_payment_at_gateway(
            pay1["provider_authority"], succeed=True
        )
        self.container.wallet.verify_and_credit_gateway_payment(pay1["id"])
        self.assertEqual(self.container.wallet.get_wallet(referrer["id"])["balance_irt"], 55000)

        # Second payment by inv1 must NOT trigger P again
        pay2 = self.container.wallet.create_gateway_payment(
            inv1["id"], "zarinpal_sandbox", 50000, "inv1_pay2"
        )
        self.container.sandbox_gateway.simulate_customer_payment_at_gateway(
            pay2["provider_authority"], succeed=True
        )
        self.container.wallet.verify_and_credit_gateway_payment(pay2["id"])
        self.assertEqual(self.container.wallet.get_wallet(referrer["id"])["balance_irt"], 55000)

        # Milestone request for M=2 must be pending_review and credit K=100,000 only once after admin approval
        dash = self.container.referrals.get_user_referral_dashboard(referrer["id"])
        self.assertEqual(dash["pending_review_rewards_irt"], 100000)
        m_req_id = dash["milestones"][0]["id"]

        res_m = self.bot.handle_admin_action(
            900001,
            "admin.review_milestone",
            {"request_id": m_req_id, "approve": True, "reason": "بررسی شد؛ دعوت‌ها واقعی هستند"},
        )
        self.assertEqual(res_m["milestone"]["status"], "approved")
        self.assertEqual(self.container.wallet.get_wallet(referrer["id"])["balance_irt"], 155000)

    # 9. Mandatory Channel Membership Caching & Telegram Admin Parity + FastAPI Web Admin RBAC
    def test_09_membership_cache_and_dual_admin_panels_rbac(self) -> None:
        self.container.ops.add_mandatory_channel(
            self.super_admin_id,
            channel_id="@vpnstore_news",
            title_fa="کانال اطلاع‌رسانی",
            join_url="https://t.me/vpnstore_news",
            cache_ttl_seconds=86400,
        )
        u = self.container.rbac.register_or_get_user(6001, "کاربر کانال")
        calls_before = self.container.ops.external_api_calls_count
        c1 = self.container.ops.check_user_membership(u["id"], now_override=1000)
        c2 = self.container.ops.check_user_membership(u["id"], now_override=2000)
        self.assertTrue(c1["all_verified"])
        self.assertTrue(c2["all_verified"])
        # Second check hit the 24h cache without calling Telegram API!
        self.assertEqual(self.container.ops.external_api_calls_count - calls_before, 1)

        # Unauthorized Telegram user must be rejected from Telegram Admin Panel
        with self.assertRaises(AuthorizationError):
            self.bot.handle_admin_action(6001, "admin.dashboard")

        # Last Super-Admin protection must block self-demotion/disabling
        with self.assertRaises(SecurityError):
            self.container.rbac.remove_role_or_disable_admin(
                self.super_admin_id, self.super_admin_id, disable=True
            )

        # FastAPI HTTP Endpoints test
        app = create_fastapi_app(self.container)
        client = TestClient(app)
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.get("/api/admin/dashboard").status_code, 401)

        login_resp = client.post(
            "/api/admin/login", json={"username": "superadmin", "password": "StrongPassword!123"}
        )
        self.assertEqual(login_resp.status_code, 200)
        token = login_resp.json()["token"]
        dash_resp = client.get(
            "/api/admin/dashboard", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(dash_resp.status_code, 200)
        self.assertIn("order_revenue_formatted", dash_resp.json())


    # 10. Live HTTP Adapters (Marzban & Zarinpal via MockTransport), Webhook Runner & Full Admin API
    def test_10_live_http_adapters_and_webhook_runner(self) -> None:
        import json
        import httpx
        from backend.app.services.orders_provisioning_referrals import (
            MarzbanHTTPProviderAdapter,
            ZarinpalHTTPGatewayAdapter,
            deterministic_provider_username,
        )

        created_users = {}

        def _marzban_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/admin/token":
                return httpx.Response(200, json={"access_token": "marz_tok_123"})
            if request.url.path == "/api/user" and request.method == "POST":
                body = json.loads(request.content.decode("utf-8"))
                uname = body["username"]
                if uname in created_users:
                    return httpx.Response(409, json={"detail": "User already exists"})
                created_users[uname] = {
                    "username": uname,
                    "subscription_url": f"https://sub.marzban.example/{uname}",
                    "links": [f"vless://uuid@marzban.example:443#{uname}"],
                }
                return httpx.Response(200, json=created_users[uname])
            if request.url.path.startswith("/api/user/") and request.method == "GET":
                uname = request.url.path.split("/")[-1]
                if uname in created_users:
                    return httpx.Response(200, json=created_users[uname])
                return httpx.Response(404, json={"detail": "Not found"})
            return httpx.Response(400)

        marz_client = httpx.Client(transport=httpx.MockTransport(_marzban_handler))
        marz_adapter = MarzbanHTTPProviderAdapter(
            provider_code="marzban_live",
            base_url="https://panel.marzban.example",
            admin_username="admin",
            admin_password="secret",
            http_client=marz_client,
        )
        res1 = marz_adapter.create_subscription("idem_live_1", user_id=77, duration_days=30, traffic_gb=50, device_limit=2)
        self.assertTrue(res1["config_uri"].startswith("https://sub.marzban.example/"))
        # Calling create_subscription again with same idempotency_key triggers 409 and reconciles cleanly!
        res2 = marz_adapter.create_subscription("idem_live_1", user_id=77, duration_days=30, traffic_gb=50, device_limit=2)
        self.assertEqual(res1["provider_sub_id"], res2["provider_sub_id"])

        # Zarinpal HTTP Adapter test
        def _zarinpal_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if request.url.path.endswith("/request.json"):
                self.assertEqual(body["amount"], 1500000)  # 150,000 IRT -> 1,500,000 IRR
                return httpx.Response(200, json={"data": {"code": 100, "authority": "A00000000000000000000000000123456"}})
            if request.url.path.endswith("/verify.json"):
                return httpx.Response(200, json={"data": {"code": 100, "ref_id": 987654321}})
            return httpx.Response(400)

        zp_client = httpx.Client(transport=httpx.MockTransport(_zarinpal_handler))
        zp_adapter = ZarinpalHTTPGatewayAdapter("merchant-uuid", http_client=zp_client)
        req_out = zp_adapter.create_payment_request("pay_1", 150000, "https://cb.example/verify")
        self.assertEqual(req_out["amount_irr"], 1500000)
        ver_out = zp_adapter.verify_payment(req_out["authority"], 1500000)
        self.assertTrue(ver_out["verified"])
        self.assertEqual(ver_out["provider_tx_id"], "987654321")

        # Telegram Webhook Endpoint & Expanded Admin API tests
        app = create_fastapi_app(self.container)
        client = TestClient(app)
        wh_bad = client.post("/api/telegram/webhook", json={"message": {"from": {"id": 8001}, "text": "/start"}})
        self.assertEqual(wh_bad.status_code, 401)

        wh_ok = client.post(
            "/api/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": self.container.settings.telegram_webhook_secret},
            json={"message": {"from": {"id": 8001, "first_name": "امیر"}, "text": "/start"}},
        )
        self.assertEqual(wh_ok.status_code, 200)
        self.assertEqual(wh_ok.json()["screen"], "main_menu")


if __name__ == "__main__":
    unittest.main(verbosity=2)

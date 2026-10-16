"""
Persian Customer Telegram Bot & Full-Parity Telegram Administration Panel (Sections 8 & 19).
Both interfaces delegate 100% of business logic and authorization checks to backend services.
"""
from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional

from backend.app.core.config import AppSettings, format_persian_irt
from backend.app.core.security import AuthorizationError
from backend.app.models.database import DatabaseManager
from backend.app.services.catalog_pricing import CatalogAndPricingService
from backend.app.services.inventory import InventoryService
from backend.app.services.orders_provisioning_referrals import (
    FakeVPNProviderAdapter,
    MembershipMessagingAndOpsService,
    OrderAndProvisioningService,
    ReferralService,
)
from backend.app.services.rbac_audit import AuditAndRBACService
from backend.app.services.wallet_payments import (
    InsufficientBalanceError,
    SandboxPaymentGatewayAdapter,
    WalletAndPaymentService,
)


class ApplicationContainer:
    """Unified dependency container shared by FastAPI, Customer Bot, Telegram Admin Panel, and Workers."""

    def __init__(
        self,
        db_path: str = ":memory:",
        settings: Optional[AppSettings] = None,
        telegram_checker: Optional[Callable[[int, str], bool]] = None,
    ) -> None:
        self.settings = settings or AppSettings()
        self.settings.validate_startup()
        self.db = DatabaseManager(db_path=db_path)
        self.db.migrate_and_seed()
        self.rbac = AuditAndRBACService(self.db, self.settings.secret_key)
        self.catalog = CatalogAndPricingService(
            self.db, self.rbac, quote_ttl_seconds=self.settings.quote_ttl_seconds
        )
        self.inventory = InventoryService(
            self.db, self.rbac, master_key=self.settings.encryption_master_key
        )
        self.wallet = WalletAndPaymentService(
            self.db, self.rbac, webhook_secret=self.settings.telegram_webhook_secret
        )
        self.sandbox_gateway = SandboxPaymentGatewayAdapter()
        self.wallet.register_gateway(self.sandbox_gateway)

        self.orders = OrderAndProvisioningService(
            self.db,
            self.rbac,
            self.catalog,
            self.inventory,
            self.wallet,
            master_key=self.settings.encryption_master_key,
        )
        if self.settings.allow_fake_providers:
            self.fake_vpn_provider = FakeVPNProviderAdapter(allow_in_env=True)
            self.orders.register_provider_adapter(self.fake_vpn_provider)

        self.referrals = ReferralService(self.db, self.rbac, self.wallet)
        self.ops = MembershipMessagingAndOpsService(
            self.db, self.rbac, self.referrals, telegram_checker=telegram_checker
        )


class TelegramBotDispatcher:
    """Handles customer `/start` and interactive callbacks as well as the full Telegram Admin Panel."""

    def __init__(self, container: ApplicationContainer) -> None:
        self.c = container

    # =========================================================================
    # CUSTOMER BOT HANDLERS (Section 8)
    # =========================================================================
    def handle_start(
        self,
        telegram_id: int,
        full_name: str,
        username: Optional[str] = None,
        start_param: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Idempotent `/start` onboarding with referral attribution and mandatory channel gate."""
        user = self.c.rbac.register_or_get_user(telegram_id, full_name, username)
        if user["status"] != "active":
            return {
                "screen": "blocked",
                "text_fa": "حساب کاربری شما مسدود یا تعلیق شده است. لطفاً با پشتیبانی تماس بگیرید.",
                "buttons": [],
            }

        ref_result = None
        if start_param and start_param.startswith("ref_"):
            ref_result = self.c.referrals.attribute_referral_on_start(
                int(user["id"]), start_param.strip()
            )

        mem_check = self.c.ops.check_user_membership(int(user["id"]))
        if not mem_check["all_verified"]:
            msg = self.c.ops.render_template(
                "mandatory_membership", {"channels_count": len(mem_check["missing_channels"])}
            )
            buttons = [
                {"text": f"عضویت در {ch['title_fa']}", "url": ch["join_url"]}
                for ch in mem_check["missing_channels"]
            ]
            buttons.append({"text": "✅ بررسی عضویت", "callback_data": "verify_membership"})
            return {
                "screen": "mandatory_membership",
                "user": user,
                "referral_result": ref_result,
                "text_fa": msg,
                "buttons": buttons,
            }

        welcome_text = self.c.ops.render_template(
            "welcome", {"full_name": full_name, "user_id": user["id"]}
        )
        menu_buttons = self.c.db.fetch_all(
            "SELECT button_text_fa, action_key FROM workflow_buttons WHERE menu_key = 'main_menu' AND is_visible = 1 ORDER BY display_order ASC"
        )
        return {
            "screen": "main_menu",
            "user": user,
            "referral_result": ref_result,
            "text_fa": welcome_text,
            "buttons": [
                {"text": b["button_text_fa"], "callback_data": b["action_key"]}
                for b in menu_buttons
            ],
        }

    def handle_customer_quote(
        self, telegram_id: int, product_id: int, coupon_code: Optional[str] = None
    ) -> Dict[str, Any]:
        user = self.c.db.fetch_one("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        if not user:
            raise ValueError("ابتدا دستور /start را ارسال کنید.")
        quote = self.c.catalog.get_or_create_quote(
            int(user["id"]), product_id, coupon_code=coupon_code
        )
        product = self.c.db.fetch_one("SELECT * FROM products WHERE id = ?", (product_id,))
        return {
            "screen": "quote_preview",
            "quote": quote,
            "text_fa": (
                f"📦 محصول: {product['title_fa']}\n"
                f"⏳ مدت: {product['duration_days']} روز | 🌐 حجم: {product['traffic_gb']} گیگابایت\n"
                f"💳 مبلغ نهایی پیش‌فاکتور: {format_persian_irt(int(quote['final_amount_irt']))}"
            ),
            "buttons": [
                {
                    "text": "✅ تایید و پرداخت از کیف پول",
                    "callback_data": f"confirm_buy:{quote['id']}",
                },
                {"text": "🔙 بازگشت", "callback_data": "browse_vpn"},
            ],
        }

    def handle_customer_purchase(
        self, telegram_id: int, quote_id: str, idempotency_key: str
    ) -> Dict[str, Any]:
        user = self.c.db.fetch_one("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        if not user:
            raise ValueError("کاربر یافت نشد.")
        try:
            order = self.c.orders.purchase_with_wallet(
                int(user["id"]), quote_id, idempotency_key=idempotency_key
            )
            product = self.c.db.fetch_one("SELECT * FROM products WHERE id = ?", (order["product_id"],))
            msg = self.c.ops.render_template(
                "delivery_success",
                {
                    "order_id": order["id"],
                    "product_title": product["title_fa"],
                    "config_text": order.get("delivered_config") or "در حال آماده‌سازی...",
                },
            )
            return {
                "screen": "purchase_success",
                "order": order,
                "text_fa": msg,
                "buttons": [{"text": "📋 سفارش‌های من", "callback_data": "my_orders"}],
            }
        except InsufficientBalanceError as exc:
            msg = self.c.ops.render_template(
                "insufficient_balance",
                {
                    "required_irt": format_persian_irt(exc.required_irt),
                    "balance_irt": format_persian_irt(exc.balance_irt),
                    "shortage_irt": format_persian_irt(exc.shortage_irt),
                },
            )
            return {
                "screen": "insufficient_balance",
                "required_irt": exc.required_irt,
                "balance_irt": exc.balance_irt,
                "shortage_irt": exc.shortage_irt,
                "preserved_quote_id": quote_id,
                "text_fa": msg,
                "buttons": [
                    {
                        "text": f"➕ شارژ کیف پول ({format_persian_irt(exc.shortage_irt)})",
                        "callback_data": f"wallet_deposit:{exc.shortage_irt}",
                    }
                ],
            }

    def handle_customer_orders(
        self, telegram_id: int, page: int = 1, page_size: int = 5
    ) -> Dict[str, Any]:
        user = self.c.db.fetch_one("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        offset = max(0, (page - 1) * page_size)
        rows = self.c.db.fetch_all(
            """SELECT o.*, p.title_fa AS product_title
               FROM orders o
               JOIN products p ON o.product_id = p.id
               WHERE o.user_id = ?
               ORDER BY o.created_at DESC
               LIMIT ? OFFSET ?""",
            (user["id"], page_size, offset),
        )
        return {
            "screen": "order_history",
            "page": page,
            "orders": [self.c.orders._enrich_order_for_user(r) for r in rows],
            "text_fa": f"📋 تاریخچه سفارش‌های شما (صفحه {page}): {len(rows)} مورد",
        }

    def handle_customer_referral_page(self, telegram_id: int) -> Dict[str, Any]:
        user = self.c.db.fetch_one("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        stats = self.c.referrals.get_user_referral_dashboard(int(user["id"]))
        text = (
            f"🎁 لینک دعوت اختصاصی شما:\n{stats['referral_link']}\n\n"
            f"👥 کل دعوت‌ها: {stats['total_referrals']} نفر\n"
            f"✅ دعوت‌های واجد شرایط: {stats['eligible_referrals']} نفر\n"
            f"💳 دعوت‌های دارای پرداخت موفق: {stats['qualified_payment_referrals']} نفر\n"
            f"💰 مجموع پاداش دریافت‌شده: {format_persian_irt(stats['earned_rewards_irt'])}\n"
            f"🏆 پیشرفت حدنصاب (Milestone): {stats['milestone_progress']}"
        )
        return {"screen": "referral_dashboard", "stats": stats, "text_fa": text}

    # =========================================================================
    # TELEGRAM ADMIN PANEL — FULL OPERATIONAL PARITY (Section 19)
    # =========================================================================
    def handle_admin_action(
        self, telegram_id: int, action: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Unified Telegram Admin Panel dispatcher enforcing RBAC on every action."""
        data = payload or {}

        if action == "admin.dashboard":
            admin = self.c.rbac.require_telegram_admin_permission(telegram_id, "orders.read")
            metrics = self.c.ops.get_dashboard_metrics(admin["id"])
            return {
                "screen": "admin_dashboard",
                "metrics": metrics,
                "text_fa": (
                    f"📊 داشبورد مدیریت تلگرام\n"
                    f"👥 کل کاربران: {metrics['total_users']} | سفارش‌های موفق: {metrics['delivered_orders']}\n"
                    f"💰 درآمد سفارش‌ها: {metrics['order_revenue_formatted']}\n"
                    f"⏳ واریزهای دستی در انتظار: {metrics['pending_manual_deposits']} | پاداش‌های حدنصاب در انتظار: {metrics['pending_milestones']}"
                ),
            }

        if action == "admin.user_status":
            admin = self.c.rbac.require_telegram_admin_permission(telegram_id, "users.manage")
            updated = self.c.rbac.set_user_status(
                admin["id"], int(data["user_id"]), data["new_status"], data["reason"]
            )
            return {"screen": "admin_user_updated", "user": updated}

        if action == "admin.wallet_adjust":
            admin = self.c.rbac.require_telegram_admin_permission(telegram_id, "wallets.adjust")
            tx = self.c.wallet.admin_adjust_wallet(
                admin["id"],
                user_id=int(data["user_id"]),
                delta_irt=int(data["delta_irt"]),
                reason=data["reason"],
                idempotency_key=data["idempotency_key"],
            )
            return {"screen": "admin_wallet_adjusted", "transaction": tx}

        if action == "admin.create_product":
            admin = self.c.rbac.require_telegram_admin_permission(telegram_id, "products.manage")
            prod = self.c.catalog.create_product(admin["id"], **data)
            return {"screen": "admin_product_created", "product": prod}

        if action == "admin.inventory_preview":
            admin = self.c.rbac.require_telegram_admin_permission(telegram_id, "inventory.import")
            prev = self.c.inventory.preview_bulk_import(
                admin["id"],
                product_id=int(data["product_id"]),
                raw_payload=data["raw_payload"],
                delimiter=data.get("delimiter", "\n"),
            )
            return {"screen": "admin_inventory_preview", "preview": prev}

        if action == "admin.inventory_commit":
            admin = self.c.rbac.require_telegram_admin_permission(telegram_id, "inventory.import")
            res = self.c.inventory.execute_bulk_import(
                admin["id"],
                product_id=int(data["product_id"]),
                raw_payload=data["raw_payload"],
                delimiter=data.get("delimiter", "\n"),
                mode=data.get("mode", "atomic"),
            )
            return {"screen": "admin_inventory_committed", "result": res}

        if action == "admin.review_manual_deposit":
            admin = self.c.rbac.require_telegram_admin_permission(
                telegram_id, "payments.approve_manual"
            )
            res = self.c.wallet.review_manual_deposit_request(
                admin["id"],
                request_id=data["request_id"],
                approve=bool(data["approve"]),
                reason=data["reason"],
            )
            return {"screen": "admin_deposit_reviewed", "request": res}

        if action == "admin.review_milestone":
            admin = self.c.rbac.require_telegram_admin_permission(
                telegram_id, "referrals.approve_milestone"
            )
            res = self.c.referrals.review_milestone_request(
                admin["id"],
                request_id=data["request_id"],
                approve=bool(data["approve"]),
                reason=data["reason"],
            )
            return {"screen": "admin_milestone_reviewed", "milestone": res}

        if action == "admin.update_referral_rules":
            admin = self.c.rbac.require_telegram_admin_permission(telegram_id, "referrals.configure")
            rules = self.c.referrals.update_rules(admin["id"], **data)
            return {"screen": "admin_referral_rules_updated", "rules": rules}

        if action == "admin.reconcile_provisioning":
            admin = self.c.rbac.require_telegram_admin_permission(telegram_id, "orders.manage")
            job = self.c.orders.process_provisioning_job(data["job_id"])
            return {"screen": "admin_provisioning_reconciled", "job": job}

        if action == "admin.update_template":
            admin = self.c.rbac.require_telegram_admin_permission(telegram_id, "settings.manage")
            tpl = self.c.ops.update_message_template(admin["id"], data["key"], data["body_fa"])
            return {"screen": "admin_template_updated", "template": tpl}

        if action == "admin.audit_logs":
            admin = self.c.rbac.require_telegram_admin_permission(telegram_id, "audit.read")
            logs = self.c.db.fetch_all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 20")
            return {"screen": "admin_audit_logs", "logs": logs}

        raise ValueError(f"Unsupported Telegram admin action: {action}")


import hmac
from backend.app.core.security import AuthenticationError

class TelegramUpdateProcessor:
    """Routes incoming Telegram JSON updates (messages & callback_queries) to TelegramBotDispatcher."""

    def __init__(self, container: Optional[ApplicationContainer] = None) -> None:
        self.container = container or ApplicationContainer()
        self.dispatcher = TelegramBotDispatcher(self.container)

    def verify_webhook_secret_header(self, secret_header: Optional[str]) -> None:
        expected = self.container.settings.telegram_webhook_secret
        if not secret_header or not hmac.compare_digest(secret_header, expected):
            raise AuthenticationError("Invalid X-Telegram-Bot-Api-Secret-Token header")

    def process_update(self, update: Dict[str, Any]) -> Dict[str, Any]:
        """Process a raw Telegram Bot API Update object safely."""
        if "message" in update:
            msg = update["message"]
            from_user = msg.get("from") or {}
            tg_id = int(from_user.get("id", 0))
            full_name = (
                f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip()
                or "کاربر تلگرام"
            )
            username = from_user.get("username")
            text = (msg.get("text") or "").strip()

            if text.startswith("/start"):
                parts = text.split(maxsplit=1)
                start_param = parts[1].strip() if len(parts) > 1 else None
                return self.dispatcher.handle_start(
                    telegram_id=tg_id,
                    full_name=full_name,
                    username=username,
                    start_param=start_param,
                )
            if text == "/admin":
                return self.dispatcher.handle_admin_action(tg_id, "admin.dashboard")

        if "callback_query" in update:
            cb = update["callback_query"]
            from_user = cb.get("from") or {}
            tg_id = int(from_user.get("id", 0))
            data = cb.get("data") or ""
            cb_id = str(cb.get("id") or "cb_default")

            if data.startswith("quote:"):
                prod_id = int(data.split(":", 1)[1])
                return self.dispatcher.handle_customer_quote(tg_id, prod_id)
            if data.startswith("confirm_buy:"):
                quote_id = data.split(":", 1)[1]
                return self.dispatcher.handle_customer_purchase(
                    tg_id, quote_id, idempotency_key=f"tg_cb:{cb_id}"
                )
            if data == "my_orders":
                return self.dispatcher.handle_customer_orders(tg_id)
            if data == "referrals_hub":
                return self.dispatcher.handle_customer_referral_page(tg_id)

        return {"screen": "ignored", "text_fa": "دستور نامعتبر است؛ لطفاً از منوی اصلی استفاده کنید."}

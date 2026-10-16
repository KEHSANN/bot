"""
FastAPI HTTP Application for the Web Administration Panel (all 9 sections),
Customer API, Authenticated Payment Gateway Webhooks, Telegram Bot Webhook,
and Observability Health/Metrics Endpoints.
"""
from __future__ import annotations
from typing import Any, Dict, Optional
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from backend.app.bot.telegram_bot import ApplicationContainer, TelegramUpdateProcessor
from backend.app.core.security import AuthenticationError, AuthorizationError


class LoginRequest(BaseModel):
    username: str
    password: str


class UserStatusUpdateRequest(BaseModel):
    new_status: str
    reason: str


class CategoryCreateRequest(BaseModel):
    slug: str
    title_fa: str
    description_fa: str
    is_economic: bool = False
    display_order: int = 0


class ProductCreateRequest(BaseModel):
    category_id: int
    code: str
    title_fa: str
    description_fa: str
    service_type: str
    inventory_mode: str
    base_price_irt: int
    duration_days: int
    traffic_gb: int
    device_limit: int
    min_random_increment_irt: int = 0
    max_random_increment_irt: int = 0
    increment_step_irt: int = 100
    promo_price_irt: Optional[int] = None
    provider_code: Optional[str] = None
    is_economic: bool = False


class BulkImportRequest(BaseModel):
    product_id: int
    raw_payload: str
    delimiter: str = "newline"
    mode: str = "atomic"


class ReviewDecisionRequest(BaseModel):
    approve: bool
    reason: str


class WalletAdjustRequest(BaseModel):
    user_id: int
    delta_irt: int
    reason: str
    idempotency_key: str


class ReferralRulesUpdateRequest(BaseModel):
    is_enabled: bool = True
    referrer_reward_n_irt: int = 15000
    referrer_n_trigger: str = "membership_verified"
    new_user_reward_w_irt: int = 10000
    new_user_w_requires_membership: bool = True
    first_payment_reward_p_irt: int = 25000
    milestone_threshold_m: int = 5
    milestone_reward_k_irt: int = 100000
    milestone_repeatable: bool = False


class MandatoryChannelRequest(BaseModel):
    channel_id: str
    title_fa: str
    join_url: str
    cache_ttl_seconds: int = 86400


class TemplateUpdateRequest(BaseModel):
    body_fa: str


def create_fastapi_app(container: Optional[ApplicationContainer] = None) -> FastAPI:
    c = container or ApplicationContainer()
    bot_processor = TelegramUpdateProcessor(c)
    app = FastAPI(title="Telegram VPN Store API", version="2.0.0")
    app.state.container = c

    def _get_admin(authorization: Optional[str]) -> Dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer session token")
        token = authorization.split(" ", 1)[1]
        try:
            return c.rbac.verify_web_session(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/healthz")
    def healthz() -> Dict[str, Any]:
        return {"status": "ok", "currency": c.settings.currency, "env": c.settings.env}

    @app.post("/api/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        try:
            bot_processor.verify_webhook_secret_header(x_telegram_bot_api_secret_token)
            update_json = await request.json()
            return bot_processor.process_update(update_json)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.post("/api/admin/login")
    def admin_login(req: LoginRequest) -> Dict[str, Any]:
        try:
            return c.rbac.authenticate_web_admin(req.username, req.password)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/api/admin/dashboard")
    def admin_dashboard(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.ops.get_dashboard_metrics(admin["id"])
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/api/admin/users")
    def admin_list_users(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            c.rbac.require_permission(admin["id"], "users.read")
            rows = c.db.fetch_all(
                """SELECT u.*, w.balance_irt
                   FROM users u
                   LEFT JOIN wallets w ON u.id = w.user_id
                   ORDER BY u.id DESC LIMIT 100"""
            )
            return {"items": rows}
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/admin/users/{user_id}/status")
    def admin_set_user_status(
        user_id: int,
        req: UserStatusUpdateRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.rbac.set_user_status(admin["id"], user_id, req.new_status, req.reason)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/admin/products")
    def admin_list_products(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            c.rbac.require_permission(admin["id"], "products.read")
            return {
                "categories": c.db.fetch_all("SELECT * FROM product_categories ORDER BY display_order ASC"),
                "products": c.db.fetch_all("SELECT * FROM products ORDER BY display_order ASC, id ASC"),
            }
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/admin/categories")
    def admin_create_category(
        req: CategoryCreateRequest, authorization: Optional[str] = Header(default=None)
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.catalog.create_category(admin["id"], **req.dict())
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/admin/products")
    def admin_create_product(
        req: ProductCreateRequest, authorization: Optional[str] = Header(default=None)
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.catalog.create_product(admin["id"], **req.dict())
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/admin/inventory/preview")
    def admin_inventory_preview(
        req: BulkImportRequest, authorization: Optional[str] = Header(default=None)
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            prev = c.inventory.preview_bulk_import(
                admin["id"], req.product_id, req.raw_payload, delimiter=req.delimiter
            )
            prev.pop("_valid_items", None)
            return prev
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/admin/inventory/import")
    def admin_inventory_import(
        req: BulkImportRequest, authorization: Optional[str] = Header(default=None)
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.inventory.execute_bulk_import(
                admin["id"], req.product_id, req.raw_payload, delimiter=req.delimiter, mode=req.mode
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/admin/orders")
    def admin_list_orders(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            c.rbac.require_permission(admin["id"], "orders.read")
            orders = c.db.fetch_all(
                "SELECT id, user_id, product_id, quote_id, amount_irt, status, delivery_status, provider_sub_id, created_at FROM orders ORDER BY created_at DESC LIMIT 100"
            )
            jobs = c.db.fetch_all("SELECT * FROM provisioning_jobs ORDER BY updated_at DESC LIMIT 100")
            return {"orders": orders, "provisioning_jobs": jobs}
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/admin/provisioning/{job_id}/reconcile")
    def admin_reconcile_job(
        job_id: str, authorization: Optional[str] = Header(default=None)
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            c.rbac.require_permission(admin["id"], "orders.manage")
            return c.orders.process_provisioning_job(job_id)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/api/admin/payments")
    def admin_list_payments(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            c.rbac.require_permission(admin["id"], "payments.read")
            return {
                "manual_deposits": c.db.fetch_all("SELECT * FROM manual_deposit_requests ORDER BY created_at DESC LIMIT 100"),
                "gateway_payments": c.db.fetch_all("SELECT * FROM payments ORDER BY created_at DESC LIMIT 100"),
            }
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/admin/deposits/{request_id}/review")
    def admin_review_deposit(
        request_id: str,
        req: ReviewDecisionRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.wallet.review_manual_deposit_request(
                admin["id"], request_id, approve=req.approve, reason=req.reason
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/admin/wallets/adjust")
    def admin_adjust_wallet(
        req: WalletAdjustRequest, authorization: Optional[str] = Header(default=None)
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.wallet.admin_adjust_wallet(
                admin["id"],
                user_id=req.user_id,
                delta_irt=req.delta_irt,
                reason=req.reason,
                idempotency_key=req.idempotency_key,
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/admin/referrals")
    def admin_get_referrals(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            c.rbac.require_permission(admin["id"], "referrals.read")
            return {
                "rules": c.referrals.get_rules(),
                "milestones": c.db.fetch_all("SELECT * FROM referral_milestone_requests ORDER BY created_at DESC LIMIT 100"),
                "rewards": c.db.fetch_all("SELECT * FROM referral_rewards ORDER BY created_at DESC LIMIT 100"),
            }
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.put("/api/admin/referrals/rules")
    def admin_update_referral_rules(
        req: ReferralRulesUpdateRequest, authorization: Optional[str] = Header(default=None)
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.referrals.update_rules(admin["id"], **req.dict())
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/admin/milestones/{request_id}/review")
    def admin_review_milestone(
        request_id: str,
        req: ReviewDecisionRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.referrals.review_milestone_request(
                admin["id"], request_id, approve=req.approve, reason=req.reason
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/admin/messaging")
    def admin_get_messaging(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            c.rbac.require_permission(admin["id"], "settings.manage")
            return {
                "channels": c.db.fetch_all("SELECT * FROM mandatory_channels"),
                "templates": c.db.fetch_all("SELECT * FROM message_templates"),
            }
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/admin/channels")
    def admin_add_channel(
        req: MandatoryChannelRequest, authorization: Optional[str] = Header(default=None)
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.ops.add_mandatory_channel(admin["id"], **req.dict())
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.put("/api/admin/templates/{key}")
    def admin_update_template(
        key: str, req: TemplateUpdateRequest, authorization: Optional[str] = Header(default=None)
    ) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            return c.ops.update_message_template(admin["id"], key, req.body_fa)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/admin/audit-logs")
    def admin_audit_logs(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        admin = _get_admin(authorization)
        try:
            c.rbac.require_permission(admin["id"], "audit.read")
            return {"items": c.db.fetch_all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 100")}
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    return app

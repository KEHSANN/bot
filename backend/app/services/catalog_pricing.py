"""
Product Catalog, Economic Plans, Coupons, and Randomized Price Quote Engine (Section 7).
Ensures integer IRT prices, configurable randomized increments, and stable persisted quotes
that never change across callback retries or network timeouts.
"""
from __future__ import annotations
import secrets
import time
from typing import Any, Dict, List, Optional

from backend.app.core.config import validate_irt_amount
from backend.app.models.database import DatabaseManager
from backend.app.services.rbac_audit import AuditAndRBACService


class CatalogAndPricingService:
    def __init__(self, db: DatabaseManager, rbac: AuditAndRBACService, quote_ttl_seconds: int = 900) -> None:
        self.db = db
        self.rbac = rbac
        self.quote_ttl_seconds = quote_ttl_seconds

    def create_category(
        self,
        actor_admin_id: int,
        *,
        slug: str,
        title_fa: str,
        description_fa: str,
        is_economic: bool = False,
        display_order: int = 0,
    ) -> Dict[str, Any]:
        self.rbac.require_permission(actor_admin_id, "products.manage")
        if not slug.strip() or not title_fa.strip():
            raise ValueError("Category slug and Persian title are required")
        with self.db.transaction() as cur:
            cur.execute(
                """INSERT INTO product_categories
                   (slug, title_fa, description_fa, is_economic, display_order, is_active)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (slug.strip(), title_fa.strip(), description_fa.strip(), int(is_economic), display_order),
            )
            cat_id = int(cur.lastrowid)
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action="catalog.create_category",
                target_type="product_category",
                target_id=str(cat_id),
                outcome="succeeded",
                metadata={"slug": slug, "title_fa": title_fa},
                cur=cur,
            )
            cur.execute("SELECT * FROM product_categories WHERE id = ?", (cat_id,))
            return dict(cur.fetchone())

    def create_product(
        self,
        actor_admin_id: int,
        *,
        category_id: int,
        code: str,
        title_fa: str,
        description_fa: str,
        service_type: str,
        inventory_mode: str,
        base_price_irt: int,
        duration_days: int,
        traffic_gb: int,
        device_limit: int,
        min_random_increment_irt: int = 0,
        max_random_increment_irt: int = 0,
        increment_step_irt: int = 100,
        promo_price_irt: Optional[int] = None,
        provider_code: Optional[str] = None,
        max_purchases_per_user: int = 100,
        is_economic: bool = False,
        display_order: int = 0,
    ) -> Dict[str, Any]:
        self.rbac.require_permission(actor_admin_id, "products.manage")
        validate_irt_amount(base_price_irt, allow_zero=False)
        validate_irt_amount(min_random_increment_irt, allow_zero=True, min_amount=0)
        validate_irt_amount(max_random_increment_irt, allow_zero=True, min_amount=0)
        validate_irt_amount(increment_step_irt, allow_zero=False, min_amount=1)
        if max_random_increment_irt < min_random_increment_irt:
            raise ValueError("max_random_increment_irt must be >= min_random_increment_irt")
        if promo_price_irt is not None:
            validate_irt_amount(promo_price_irt, allow_zero=False)
            if promo_price_irt >= base_price_irt:
                raise ValueError("Promotional price must be strictly lower than base price")
        if service_type not in {"vless", "vmess", "trojan", "Outline", "subscription_url"}:
            raise ValueError(f"Unsupported service_type: {service_type}")
        if inventory_mode not in {"manual", "automated_provider"}:
            raise ValueError(f"Unsupported inventory_mode: {inventory_mode}")
        if inventory_mode == "automated_provider" and not provider_code:
            raise ValueError("automated_provider products require a provider_code")

        with self.db.transaction() as cur:
            cur.execute(
                """INSERT INTO products
                   (category_id, code, title_fa, description_fa, service_type, inventory_mode,
                    provider_code, base_price_irt, promo_price_irt, min_random_increment_irt,
                    max_random_increment_irt, increment_step_irt, duration_days, traffic_gb,
                    device_limit, max_purchases_per_user, is_economic, is_active, pricing_version, display_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?)""",
                (
                    category_id,
                    code.strip(),
                    title_fa.strip(),
                    description_fa.strip(),
                    service_type,
                    inventory_mode,
                    provider_code,
                    base_price_irt,
                    promo_price_irt,
                    min_random_increment_irt,
                    max_random_increment_irt,
                    increment_step_irt,
                    duration_days,
                    traffic_gb,
                    device_limit,
                    max_purchases_per_user,
                    int(is_economic),
                    display_order,
                ),
            )
            prod_id = int(cur.lastrowid)
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action="catalog.create_product",
                target_type="product",
                target_id=str(prod_id),
                outcome="succeeded",
                metadata={"code": code, "base_price_irt": base_price_irt},
                cur=cur,
            )
            cur.execute("SELECT * FROM products WHERE id = ?", (prod_id,))
            return dict(cur.fetchone())

    def update_product_pricing(
        self,
        actor_admin_id: int,
        product_id: int,
        *,
        base_price_irt: int,
        min_random_increment_irt: int = 0,
        max_random_increment_irt: int = 0,
        increment_step_irt: int = 100,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        self.rbac.require_permission(actor_admin_id, "products.manage")
        validate_irt_amount(base_price_irt)
        if max_random_increment_irt < min_random_increment_irt or increment_step_irt <= 0:
            raise ValueError("Invalid random increment bounds")
        with self.db.transaction() as cur:
            cur.execute(
                """UPDATE products
                   SET base_price_irt = ?, min_random_increment_irt = ?, max_random_increment_irt = ?,
                       increment_step_irt = ?, is_active = ?, pricing_version = pricing_version + 1
                   WHERE id = ?""",
                (
                    base_price_irt,
                    min_random_increment_irt,
                    max_random_increment_irt,
                    increment_step_irt,
                    int(is_active),
                    product_id,
                ),
            )
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action="catalog.update_product_pricing",
                target_type="product",
                target_id=str(product_id),
                outcome="succeeded",
                metadata={"base_price_irt": base_price_irt, "is_active": is_active},
                cur=cur,
            )
            cur.execute("SELECT * FROM products WHERE id = ?", (product_id,))
            return dict(cur.fetchone())

    def create_coupon(
        self, actor_admin_id: int, code: str, discount_irt: int, min_order_irt: int = 0, max_uses: int = 100
    ) -> Dict[str, Any]:
        self.rbac.require_permission(actor_admin_id, "campaigns.manage")
        validate_irt_amount(discount_irt)
        norm_code = code.strip().upper()
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO coupons (code, discount_irt, min_order_irt, max_uses, used_count, is_active) VALUES (?, ?, ?, ?, 0, 1)",
                (norm_code, discount_irt, min_order_irt, max_uses),
            )
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action="coupon.create",
                target_type="coupon",
                target_id=norm_code,
                outcome="succeeded",
                metadata={"discount_irt": discount_irt},
                cur=cur,
            )
            cur.execute("SELECT * FROM coupons WHERE code = ?", (norm_code,))
            return dict(cur.fetchone())

    def _compute_random_increment(self, min_inc: int, max_inc: int, step: int) -> int:
        if max_inc <= min_inc:
            return min_inc
        total_steps = (max_inc - min_inc) // step
        if total_steps <= 0:
            return min_inc
        chosen_step = secrets.randbelow(total_steps + 1)
        return min_inc + (chosen_step * step)

    def get_or_create_quote(
        self,
        user_id: int,
        product_id: int,
        *,
        coupon_code: Optional[str] = None,
        force_new: bool = False,
        now_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Generate or reuse an active server-side price quote (Section 7.1).
        Guarantees that callback retries or network timeouts never generate a new random price.
        """
        now = now_override if now_override is not None else int(time.time())
        norm_coupon = coupon_code.strip().upper() if coupon_code else None

        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            user = cur.fetchone()
            if not user or user["status"] != "active":
                raise ValueError("حساب کاربری شما فعال نیست.")

            cur.execute("SELECT * FROM products WHERE id = ? AND is_active = 1", (product_id,))
            product = cur.fetchone()
            if not product:
                raise ValueError("محصول مورد نظر یافت نشد یا غیرفعال است.")

            # Enforce per-user purchase limit
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM orders WHERE user_id = ? AND product_id = ? AND status IN ('paid', 'provisioning', 'delivered')",
                (user_id, product_id),
            )
            if cur.fetchone()["cnt"] >= product["max_purchases_per_user"]:
                raise ValueError("سقف مجاز خرید این محصول برای حساب شما تکمیل شده است.")

            # Expire stale quotes
            cur.execute(
                "UPDATE price_quotes SET status = 'expired' WHERE user_id = ? AND product_id = ? AND status = 'active' AND expires_at <= ?",
                (user_id, product_id, now),
            )

            if not force_new:
                cur.execute(
                    """SELECT * FROM price_quotes
                       WHERE user_id = ? AND product_id = ? AND status = 'active'
                         AND pricing_version = ? AND IFNULL(coupon_code, '') = ?
                         AND expires_at > ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (user_id, product_id, product["pricing_version"], norm_coupon or "", now),
                )
                existing_quote = cur.fetchone()
                if existing_quote:
                    return dict(existing_quote)
            else:
                cur.execute(
                    "UPDATE price_quotes SET status = 'expired' WHERE user_id = ? AND product_id = ? AND status = 'active'",
                    (user_id, product_id),
                )

            effective_base = (
                product["promo_price_irt"]
                if product["promo_price_irt"] is not None
                else product["base_price_irt"]
            )
            rand_inc = self._compute_random_increment(
                product["min_random_increment_irt"],
                product["max_random_increment_irt"],
                product["increment_step_irt"],
            )
            discount = 0
            if norm_coupon:
                cur.execute(
                    "SELECT * FROM coupons WHERE code = ? AND is_active = 1", (norm_coupon,)
                )
                coupon = cur.fetchone()
                if not coupon or coupon["used_count"] >= coupon["max_uses"]:
                    raise ValueError("کد تخفیف نامعتبر یا منقضی شده است.")
                if effective_base < coupon["min_order_irt"]:
                    raise ValueError("مبلغ سفارش کمتر از حداقل مجاز کد تخفیف است.")
                discount = min(coupon["discount_irt"], effective_base + rand_inc)

            final_irt = max(0, effective_base + rand_inc - discount)
            quote_id = f"qt_{secrets.token_hex(10)}"
            expires_at = now + self.quote_ttl_seconds
            cur.execute(
                """INSERT INTO price_quotes
                   (id, user_id, product_id, base_amount_irt, random_increment_irt, discount_irt,
                    final_amount_irt, currency, pricing_version, coupon_code, status, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'IRT', ?, ?, 'active', ?, ?)""",
                (
                    quote_id,
                    user_id,
                    product_id,
                    effective_base,
                    rand_inc,
                    discount,
                    final_irt,
                    product["pricing_version"],
                    norm_coupon,
                    expires_at,
                    now,
                ),
            )
            cur.execute("SELECT * FROM price_quotes WHERE id = ?", (quote_id,))
            return dict(cur.fetchone())

    def list_active_products(self, category_id: Optional[int] = None, economic_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM products WHERE is_active = 1"
        params: List[Any] = []
        if category_id is not None:
            sql += " AND category_id = ?"
            params.append(category_id)
        if economic_only:
            sql += " AND is_economic = 1"
        sql += " ORDER BY display_order ASC, id ASC"
        return self.db.fetch_all(sql, tuple(params))

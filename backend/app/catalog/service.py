import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.schemas import CategoryBody, PlanBody, ProductBody, QuoteBody
from app.core.money import validate_amount
from app.models import Product, ProductCategory, ProductPlan, User
from app.models.quote import PurchaseQuote
from app.security.service import Context, Principal, SecurityError

CatalogRow = ProductCategory | Product | ProductPlan
MODELS: dict[str, type[ProductCategory] | type[Product] | type[ProductPlan]] = {
    "categories": ProductCategory,
    "products": Product,
    "plans": ProductPlan,
}


async def category_chain(
    db: AsyncSession,
    category_id: UUID | None,
    forbidden: UUID | None = None,
    available: bool = False,
) -> None:
    seen = {forbidden} if forbidden else set()
    for _ in range(64):
        if category_id is None:
            return
        if category_id in seen:
            raise SecurityError(409, "category_cycle")
        seen.add(category_id)
        row = await db.get(ProductCategory, category_id)
        if not row:
            raise SecurityError(404, "category_not_found")
        if available and not row.enabled:
            raise SecurityError(409, "category_unavailable")
        category_id = row.parent_id
    raise SecurityError(409, "category_depth_limit")


def serialize(row: ProductCategory | Product | ProductPlan | PurchaseQuote) -> dict[str, Any]:
    # Catalog/quote columns only; no provider credentials or inventory secrets.
    result = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    return {
        key: value.isoformat()
        if isinstance(value, datetime)
        else str(value)
        if isinstance(value, UUID)
        else value
        for key, value in result.items()
    }


async def catalog_operation(
    db: AsyncSession,
    ctx: Context,
    action: str,
    body: CategoryBody | ProductBody | PlanBody | None,
    kind: str,
    target: UUID | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    model = MODELS.get(kind)
    if model is None:
        raise SecurityError(422, "invalid_catalog_kind")
    ctx.target_type = kind
    if action == "catalog.read":
        if target:
            row = cast(CatalogRow | None, await db.get(model, target))
            if not row:
                raise SecurityError(404, "catalog_not_found")
            return serialize(row)
        rows = (
            await db.scalars(
                select(model)
                .order_by(model.created_at.desc(), model.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return {"items": [serialize(cast(CatalogRow, row)) for row in rows]}
    if not body or body.kind != kind:
        raise SecurityError(422, "invalid_catalog_kind")
    if isinstance(body, CategoryBody):
        await category_chain(db, body.parent_id, forbidden=target)
    elif isinstance(body, ProductBody):
        await category_chain(db, body.category_id)
    elif not await db.get(Product, body.product_id):
        raise SecurityError(404, "product_not_found")
    duplicate = select(model.id).where(model.slug == body.slug)
    if isinstance(body, PlanBody):
        duplicate = duplicate.where(ProductPlan.product_id == body.product_id)
    if target:
        duplicate = duplicate.where(model.id != target)
    if await db.scalar(duplicate):
        raise SecurityError(409, "catalog_slug_exists")
    payload = body.model_dump(exclude={"current_password", "reason", "kind"})
    if action == "catalog.create":
        row = model(**payload)
        db.add(row)
    else:
        row = await db.scalar(select(model).where(model.id == target).with_for_update())
        if not row:
            raise SecurityError(404, "catalog_not_found")
        if isinstance(row, ProductPlan):
            # Existing quotes preserve their own price/version; edits never rewrite them.
            row.pricing_version += 1
        for key, value in payload.items():
            setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    ctx.target_id = str(row.id)
    ctx.details = {"entity_id": str(row.id)}
    if isinstance(row, ProductPlan):
        ctx.details["pricing_version"] = row.pricing_version
    return serialize(row)


async def quote_operation(
    db: AsyncSession,
    ctx: Context,
    principal: Principal,
    body: QuoteBody | None,
    target: UUID | None,
    ttl: int,
) -> dict[str, Any]:
    if "users.read" not in principal.permissions:
        raise SecurityError(403, "permission_denied")
    ctx.target_type = "quote"
    now = cast(datetime, await db.scalar(select(func.clock_timestamp())))
    if body is None:
        row = await db.get(PurchaseQuote, target)
        if not row:
            raise SecurityError(404, "quote_not_found")
        ctx.target_id = str(row.id)
        return serialize(row) | {"expired": row.expires_at <= now}
    user = await db.get(User, body.user_id)
    if not user or user.status != "active":
        raise SecurityError(409, "user_unavailable")
    lock = int.from_bytes(
        hashlib.blake2b(body.user_id.bytes + body.request_key.bytes, digest_size=8).digest(),
        signed=True,
    )
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    old = await db.scalar(
        select(PurchaseQuote).where(
            PurchaseQuote.user_id == body.user_id, PurchaseQuote.request_key == body.request_key
        )
    )
    if old and old.plan_id != body.plan_id:
        raise SecurityError(409, "quote_key_conflict_or_expired")
    plan = await db.scalar(
        select(ProductPlan).where(ProductPlan.id == body.plan_id).with_for_update()
    )
    product = await db.get(Product, plan.product_id) if plan else None
    if not plan or not product or not plan.enabled or not product.enabled:
        raise SecurityError(409, "plan_unavailable")
    await category_chain(db, product.category_id, available=True)
    now = cast(datetime, await db.scalar(select(func.clock_timestamp())))
    if old and old.expires_at <= now:
        raise SecurityError(409, "quote_key_conflict_or_expired")
    if old:
        ctx.target_id = str(old.id)
        return serialize(old) | {"expired": False}
    if plan.randomization_method != "uniform_discrete":
        raise SecurityError(503, "pricing_method_unavailable")
    count = (plan.max_increment - plan.min_increment) // plan.increment_step + 1
    increment = plan.min_increment + secrets.randbelow(count) * plan.increment_step
    amount = validate_amount(plan.price_minor + increment, plan.currency)
    row = PurchaseQuote(
        user_id=user.id,
        plan_id=plan.id,
        request_key=body.request_key,
        amount_minor=amount,
        currency="IRT",
        pricing_version=plan.pricing_version,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl),
        snapshot={
            "product_id": str(product.id),
            "product_title": product.title,
            "plan_title": plan.title,
            "duration_days": plan.duration_days,
            "traffic_bytes": plan.traffic_bytes,
            "device_limit": plan.device_limit,
            "base_price_minor": plan.price_minor,
            "increment_minor": increment,
            "randomization_method": plan.randomization_method,
        },
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    ctx.target_id = str(row.id)
    ctx.details = {
        "user_id": str(user.id),
        "plan_id": str(plan.id),
        "pricing_version": plan.pricing_version,
    }
    return serialize(row) | {"expired": False}

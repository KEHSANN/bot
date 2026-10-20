"""Product catalog: categories, products, plans and stock counts."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.base import utcnow
from vpnbot.db.enums import InventoryStatus, ProductType
from vpnbot.db.models.catalog import Category, Plan, Product
from vpnbot.db.models.inventory import InventoryItem


class CatalogError(ValueError):
    pass


async def list_categories(session: AsyncSession, *, active_only: bool = True) -> list[Category]:
    stmt = select(Category).order_by(Category.sort_order, Category.id)
    if active_only:
        stmt = stmt.where(Category.is_active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def list_products(
    session: AsyncSession,
    *,
    category_id: int | None = None,
    active_only: bool = True,
    offset: int = 0,
    limit: int = 8,
) -> tuple[list[Product], int]:
    conditions = []
    if active_only:
        conditions.extend([Product.is_active.is_(True), Product.archived_at.is_(None)])
    if category_id is not None:
        conditions.append(Product.category_id == category_id)
    base = select(Product).where(*conditions)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    stmt = base.order_by(Product.sort_order, Product.id).offset(offset).limit(limit)
    return list((await session.execute(stmt)).scalars()), int(total)


async def get_product(session: AsyncSession, product_id: int) -> Product | None:
    return (
        await session.execute(select(Product).where(Product.id == product_id))
    ).scalar_one_or_none()


async def get_plan(session: AsyncSession, plan_id: int) -> Plan | None:
    return (
        await session.execute(select(Plan).where(Plan.id == plan_id))
    ).scalar_one_or_none()


async def available_plan_ids(session: AsyncSession, product_id: int) -> dict[int, int]:
    """Sellable stock per plan of a product.

    Inventory rows may be bound to a specific plan or left product-wide
    (``plan_id IS NULL``); product-wide stock is usable by every plan.
    """
    rows = (
        await session.execute(
            select(InventoryItem.plan_id, func.count(InventoryItem.id))
            .where(
                InventoryItem.product_id == product_id,
                InventoryItem.status == InventoryStatus.AVAILABLE,
            )
            .group_by(InventoryItem.plan_id)
        )
    ).all()
    unassigned = sum(int(count) for plan_id, count in rows if plan_id is None)
    per_plan = {int(plan_id): int(count) for plan_id, count in rows if plan_id is not None}

    plan_ids = (
        await session.execute(
            select(Plan.id).where(
                Plan.product_id == product_id,
                Plan.is_active.is_(True),
                Plan.archived_at.is_(None),
            )
        )
    ).scalars().all()
    return {int(plan_id): per_plan.get(int(plan_id), 0) + unassigned for plan_id in plan_ids}


async def available_counts(session: AsyncSession, product_ids: list[int]) -> dict[int, int]:
    """Sellable pairs per product in one query (avoids N+1 on listing screens)."""
    if not product_ids:
        return {}
    rows = (
        await session.execute(
            select(InventoryItem.product_id, func.count(InventoryItem.id))
            .where(
                InventoryItem.product_id.in_(product_ids),
                InventoryItem.status == InventoryStatus.AVAILABLE,
            )
            .group_by(InventoryItem.product_id)
        )
    ).all()
    counts = {int(product_id): int(count) for product_id, count in rows}
    return {product_id: counts.get(product_id, 0) for product_id in product_ids}


async def stock_for_plan(session: AsyncSession, product_id: int, plan_id: int | None) -> int:
    """Count sellable pairs usable for a plan (plan-specific + product-wide)."""
    conditions = [
        InventoryItem.product_id == product_id,
        InventoryItem.status == InventoryStatus.AVAILABLE,
    ]
    if plan_id is not None:
        conditions.append((InventoryItem.plan_id == plan_id) | InventoryItem.plan_id.is_(None))
    else:
        conditions.append(InventoryItem.plan_id.is_(None))
    return int(
        (await session.execute(select(func.count(InventoryItem.id)).where(*conditions))).scalar_one()
    )


async def create_category(session: AsyncSession, *, title: str, sort_order: int = 0) -> Category:
    category = Category(title=title.strip(), sort_order=sort_order)
    session.add(category)
    await session.flush()
    return category


async def create_product(
    session: AsyncSession,
    *,
    title: str,
    description: str | None,
    category_id: int | None,
    product_type: str | ProductType = "CONFIG",
    image_file_id: str | None = None,
    sort_order: int = 0,
) -> Product:
    if not title.strip():
        raise CatalogError("نام محصول الزامی است")
    # Normalise to the enum member, never a bare string: the ORM keeps the
    # assigned value on the instance for the rest of the session, and a plain
    # ``str`` there breaks every later ``product.product_type.value`` lookup
    # (the product-creation wizard crashed exactly that way and, thanks to
    # rollback-on-error, the tap on the type buttons looked like a no-op).
    if isinstance(product_type, ProductType):
        resolved_type = product_type
    else:
        try:
            resolved_type = ProductType(str(product_type).strip().upper())
        except ValueError as exc:
            raise CatalogError("نوع محصول نامعتبر است") from exc
    product = Product(
        title=title.strip(),
        description=description,
        category_id=category_id,
        product_type=resolved_type,
        image_file_id=image_file_id,
        sort_order=sort_order,
        is_active=False,
        sales_enabled=False,
    )
    session.add(product)
    await session.flush()
    return product


async def create_plan(
    session: AsyncSession, *, product_id: int, title: str, price_irt: int, **extras: int | None
) -> Plan:
    if price_irt <= 0:
        raise CatalogError("قیمت باید بزرگ‌تر از صفر باشد")
    if await get_product(session, product_id) is None:
        # Without this, a product deleted mid-wizard surfaced as a raw FK
        # IntegrityError — a silent crash instead of a clean message.
        raise CatalogError("محصول یافت نشد")
    plan = Plan(
        product_id=product_id,
        title=title.strip(),
        price_irt=price_irt,
        duration_days=extras.get("duration_days"),
        traffic_gb=extras.get("traffic_gb"),
        device_limit=extras.get("device_limit"),
        sort_order=int(extras.get("sort_order") or 0),
    )
    session.add(plan)
    await session.flush()
    return plan


async def archive_product(session: AsyncSession, product_id: int) -> Product:
    product = await get_product(session, product_id)
    if product is None:
        raise CatalogError("محصول یافت نشد")
    product.archived_at = utcnow()
    product.is_active = False
    product.sales_enabled = False
    await session.flush()
    return product


async def duplicate_product(session: AsyncSession, product_id: int) -> Product:
    source = await get_product(session, product_id)
    if source is None:
        raise CatalogError("محصول یافت نشد")
    suffix = " (کپی)"
    clone = Product(
        # The wizard allows 160-char titles; appending the suffix blindly
        # overflowed the String(160) column and the duplicate tap crashed.
        title=f"{source.title[: 160 - len(suffix)]}{suffix}",
        description=source.description,
        category_id=source.category_id,
        image_file_id=source.image_file_id,
        product_type=source.product_type,
        specs=dict(source.specs or {}),
        is_active=False,
        sales_enabled=False,
        sort_order=source.sort_order,
    )
    session.add(clone)
    await session.flush()
    for plan in source.plans:
        session.add(
            Plan(
                product_id=clone.id,
                title=plan.title,
                description=plan.description,
                price_irt=plan.price_irt,
                duration_days=plan.duration_days,
                traffic_gb=plan.traffic_gb,
                device_limit=plan.device_limit,
                specs=dict(plan.specs or {}),
                is_active=plan.is_active,
                sort_order=plan.sort_order,
            )
        )
    await session.flush()
    return clone

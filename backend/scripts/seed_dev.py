"""Idempotent, disabled sample catalog. Never creates admins, wallets or VPN credentials."""

import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.session import session_factory, transaction
from app.models import Product, ProductCategory, ProductPlan


async def seed() -> None:
    settings = Settings()
    if settings.environment not in {"development", "test"}:
        raise RuntimeError("Development seeding is forbidden in staging/production")
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        async with transaction(session_factory(engine)) as session:
            await session.execute(
                insert(ProductCategory)
                .values(
                    slug="sample-budget",
                    title="نمونهٔ محصولات اقتصادی",
                    enabled=False,
                )
                .on_conflict_do_nothing(index_elements=["slug"])
            )
            category = await session.scalar(
                select(ProductCategory.id).where(ProductCategory.slug == "sample-budget")
            )
            await session.execute(
                insert(Product)
                .values(
                    slug="sample-manual",
                    title="محصول نمونه (غیرفعال)",
                    category_id=category,
                    service_type="vless",
                    inventory_mode="manual",
                    economic=True,
                    enabled=False,
                )
                .on_conflict_do_nothing(index_elements=["slug"])
            )
            product = await session.scalar(
                select(Product.id).where(Product.slug == "sample-manual")
            )
            await session.execute(
                insert(ProductPlan)
                .values(
                    product_id=product,
                    slug="sample-30-day",
                    title="نمونهٔ ۳۰ روزه",
                    price_minor=100000,
                    duration_days=30,
                    traffic_bytes=1073741824,
                    enabled=False,
                )
                .on_conflict_do_nothing(index_elements=["product_id", "slug"])
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())

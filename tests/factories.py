"""Small async factories shared by the DB-backed tests."""

from __future__ import annotations

import itertools

from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.enums import InventoryStatus, ProductType
from vpnbot.db.models.catalog import Category, Plan, Product
from vpnbot.db.models.inventory import InventoryItem
from vpnbot.db.models.users import User
from vpnbot.security.crypto import CredentialCipher, fingerprint
from vpnbot.services import auth as auth_service

_counter = itertools.count(1)


async def make_user(
    session: AsyncSession, telegram_id: int | None = None, *, first_name: str = "کاربر"
) -> User:
    tid = telegram_id or 700_000_000 + next(_counter)
    return await auth_service.upsert_user(
        session, telegram_id=tid, first_name=first_name, username=None
    )


async def make_catalog(
    session: AsyncSession,
    *,
    price_irt: int = 250_000,
    title: str = "پلن تست",
) -> tuple[Category, Product, Plan]:
    n = next(_counter)
    category = Category(title=f"دسته {n}", is_active=True, sort_order=0)
    session.add(category)
    await session.flush()

    product = Product(
        category_id=category.id,
        title=f"محصول {n}",
        product_type=ProductType.CONFIG,
        is_active=True,
        sales_enabled=True,
        sort_order=0,
    )
    session.add(product)
    await session.flush()

    plan = Plan(
        product_id=product.id,
        title=title,
        price_irt=price_irt,
        duration_days=30,
        traffic_gb=50,
        device_limit=2,
        is_active=True,
        sort_order=0,
    )
    session.add(plan)
    await session.flush()
    return category, product, plan


async def make_inventory(
    session: AsyncSession,
    *,
    cipher: CredentialCipher,
    product: Product,
    plan: Plan | None,
    secret: str,
    count: int = 1,
    with_sub: bool = True,
    status: InventoryStatus = InventoryStatus.AVAILABLE,
) -> list[InventoryItem]:
    items: list[InventoryItem] = []
    for _ in range(count):
        n = next(_counter)
        config = f"vless://test-{n}-{product.id}@example.com:443"
        sub_url = f"https://sub.example.com/{n}" if with_sub else None
        item = InventoryItem(
            product_id=product.id,
            plan_id=plan.id if plan is not None else None,
            config_cipher=cipher.encrypt(config),
            config_fingerprint=fingerprint(config, secret),
            sub_url_cipher=cipher.encrypt(sub_url) if sub_url else None,
            sub_url_fingerprint=fingerprint(sub_url, secret) if sub_url else None,
            status=status,
        )
        session.add(item)
        items.append(item)
    await session.flush()
    return items

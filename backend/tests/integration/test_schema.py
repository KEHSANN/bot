import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.session import session_factory, transaction
from app.models import AdminUser, Product, ProductCategory, ProductPlan, User

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_user_uniqueness_and_rollback() -> None:
    engine = create_async_engine(Settings().database_url.get_secret_value())
    factory = session_factory(engine)
    telegram_id = int(uuid4().int % 10**14) + 1
    try:
        async with transaction(factory) as session:
            user = User(telegram_id=telegram_id, display_name="کاربر آزمایشی")
            session.add(user)
        async with factory() as session:
            assert (await session.get(User, user.id)).status == "active"
        with pytest.raises(IntegrityError):
            async with transaction(factory) as session:
                session.add(User(telegram_id=telegram_id))
        with pytest.raises(RuntimeError):
            async with transaction(factory) as session:
                session.add(User(telegram_id=telegram_id + 1))
                await session.flush()
                raise RuntimeError("rollback")
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.telegram_id == telegram_id + 1)
                )
                == 0
            )
        with pytest.raises(IntegrityError):
            async with transaction(factory) as session:
                session.add(User(telegram_id=-1))
        with pytest.raises(IntegrityError):
            async with transaction(factory) as session:
                session.add(User(status="unknown"))
    finally:
        async with transaction(factory) as session:
            await session.execute(delete(User).where(User.telegram_id == telegram_id))
        await engine.dispose()


async def test_duplicate_onboarding_race() -> None:
    engine = create_async_engine(Settings().database_url.get_secret_value())
    factory = session_factory(engine)
    telegram_id = int(uuid4().int % 10**14) + 1

    async def insert_user() -> bool:
        try:
            async with transaction(factory) as session:
                session.add(User(telegram_id=telegram_id))
            return True
        except IntegrityError:
            return False

    try:
        results = await asyncio.gather(*(insert_user() for _ in range(20)))
        assert sum(results) == 1
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(User).where(User.telegram_id == telegram_id)
                )
                == 1
            )
    finally:
        async with transaction(factory) as session:
            await session.execute(delete(User).where(User.telegram_id == telegram_id))
        await engine.dispose()


@pytest.mark.parametrize(
    "invalid",
    [
        {"price_minor": -1},
        {"currency": "IRR"},
        {"device_limit": 0},
        {"duration_days": 0},
        {"traffic_bytes": -1},
        {"increment_step": 0},
        {"min_increment": 10, "max_increment": 5},
        {"min_increment": 3, "max_increment": 6, "increment_step": 2},
        {"price_minor": 2**63 - 1, "max_increment": 1},
        {"attributes": []},
    ],
)
async def test_plan_constraints(invalid: dict[str, object]) -> None:
    engine = create_async_engine(Settings().database_url.get_secret_value())
    factory = session_factory(engine)
    try:
        async with transaction(factory) as session:
            category = ProductCategory(slug=str(uuid4()), title="آزمایش")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                slug=str(uuid4()),
                title="نمونه",
                inventory_mode="manual",
                service_type="vless",
            )
            session.add(product)
            await session.flush()
        with pytest.raises(IntegrityError):
            async with transaction(factory) as session:
                session.add(
                    ProductPlan(
                        **(
                            {
                                "product_id": product.id,
                                "slug": "plan",
                                "title": "طرح",
                                "price_minor": 100000,
                            }
                            | invalid
                        )
                    )
                )
    finally:
        async with transaction(factory) as session:
            await session.execute(delete(ProductPlan).where(ProductPlan.product_id == product.id))
            await session.execute(delete(Product).where(Product.id == product.id))
            await session.execute(delete(ProductCategory).where(ProductCategory.id == category.id))
        await engine.dispose()


async def test_foreign_keys_and_safe_defaults() -> None:
    engine = create_async_engine(Settings().database_url.get_secret_value())
    factory = session_factory(engine)
    try:
        with pytest.raises(IntegrityError):
            async with transaction(factory) as session:
                session.add(AdminUser(user_id=uuid4()))
        with pytest.raises(IntegrityError):
            async with transaction(factory) as session:
                session.add(
                    Product(
                        category_id=uuid4(),
                        slug=str(uuid4()),
                        title="آزمایش",
                        inventory_mode="manual",
                        service_type="vless",
                    )
                )
        async with transaction(factory) as session:
            user = User()
            session.add(user)
            await session.flush()
            admin = AdminUser(user_id=user.id)
            session.add(admin)
        assert admin.enabled is False
        with pytest.raises(IntegrityError):
            async with transaction(factory) as session:
                await session.execute(delete(User).where(User.id == user.id))
    finally:
        async with transaction(factory) as session:
            await session.execute(delete(AdminUser).where(AdminUser.user_id == user.id))
            await session.execute(delete(User).where(User.id == user.id))
        await engine.dispose()


async def test_rbac_unique_grants_and_history() -> None:
    from app.models import AdminRoleAssignment, Permission, Role, RolePermission, UserStatusHistory

    engine = create_async_engine(Settings().database_url.get_secret_value())
    factory = session_factory(engine)
    try:
        async with transaction(factory) as session:
            user = User()
            role = Role(code=str(uuid4()), title="نقش آزمایشی")
            permission = Permission(code=str(uuid4()), description="مجوز آزمایشی")
            session.add_all([user, role, permission])
            await session.flush()
            admin = AdminUser(user_id=user.id)
            session.add(admin)
            await session.flush()
            session.add_all(
                [
                    RolePermission(role_id=role.id, permission_id=permission.id),
                    AdminRoleAssignment(admin_user_id=admin.id, role_id=role.id),
                ]
            )
        with pytest.raises(IntegrityError):
            async with transaction(factory) as session:
                session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        with pytest.raises(IntegrityError):
            async with transaction(factory) as session:
                session.add(AdminRoleAssignment(admin_user_id=admin.id, role_id=role.id))
        with pytest.raises(IntegrityError):
            async with transaction(factory) as session:
                session.add(
                    UserStatusHistory(
                        user_id=user.id,
                        old_status="active",
                        new_status="active",
                        reason="invalid no-op",
                    )
                )
    finally:
        async with transaction(factory) as session:
            await session.execute(
                delete(AdminRoleAssignment).where(AdminRoleAssignment.admin_user_id == admin.id)
            )
            await session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
            await session.execute(delete(AdminUser).where(AdminUser.id == admin.id))
            await session.execute(delete(User).where(User.id == user.id))
            await session.execute(delete(Role).where(Role.id == role.id))
            await session.execute(delete(Permission).where(Permission.id == permission.id))
        await engine.dispose()


async def test_development_seed_is_disabled_and_idempotent() -> None:
    from scripts.seed_dev import seed

    await seed()
    await seed()
    engine = create_async_engine(Settings().database_url.get_secret_value())
    factory = session_factory(engine)
    try:
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(Product).where(Product.slug == "sample-manual")
                )
                == 1
            )
            product = await session.scalar(select(Product).where(Product.slug == "sample-manual"))
            assert product.enabled is False
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ProductPlan)
                    .where(ProductPlan.product_id == product.id)
                )
                == 1
            )
    finally:
        await engine.dispose()

"""Invariant #1: a configuration pair is single-use and never resold.

These tests exercise the real database constraints and triggers, not just the
service layer: a delivered pair cannot move back into the pool even if a future
code path forgets to check.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from tests.factories import make_catalog, make_inventory, make_user

from vpnbot.db.enums import InventoryStatus, OrderStatus
from vpnbot.db.models.inventory import InventoryItem
from vpnbot.services import inventory as inventory_service
from vpnbot.services import orders as order_service


async def _order(session, user, product, plan):
    return await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )


async def test_reservation_is_atomic_and_idempotent(session, cipher, fingerprint_secret) -> None:
    user = await make_user(session)
    _, product, plan = await make_catalog(session)
    items = await make_inventory(
        session, cipher=cipher, product=product, plan=plan,
        secret=fingerprint_secret, count=1,
    )
    order = await _order(session, user, product, plan)

    first = await inventory_service.reserve_for_order(session, order=order)
    second = await inventory_service.reserve_for_order(session, order=order)

    assert first.id == second.id == items[0].id
    assert first.status is InventoryStatus.RESERVED
    assert first.assigned_order_id == order.id

    counts = await inventory_service.counts_for_product(session, product.id)
    assert (counts.available, counts.reserved, counts.delivered) == (0, 1, 0)


async def test_empty_pool_never_fabricates_a_pair(session, cipher, fingerprint_secret) -> None:
    user = await make_user(session)
    _, product, plan = await make_catalog(session)
    await make_inventory(
        session, cipher=cipher, product=product, plan=plan,
        secret=fingerprint_secret, count=1,
    )
    # Checkout succeeds while stock exists; the last pair disappears before
    # the reservation runs, which is the race the service must handle.
    order = await _order(session, user, product, plan)
    await session.execute(
        text("UPDATE inventory_items SET status = 'DISABLED' WHERE product_id = :p"),
        {"p": product.id},
    )
    await session.flush()

    with pytest.raises(inventory_service.NoInventoryAvailable):
        await inventory_service.reserve_for_order(session, order=order)


async def test_delivered_pair_cannot_be_delivered_to_another_order(
    session, cipher, fingerprint_secret
) -> None:
    user = await make_user(session)
    _, product, plan = await make_catalog(session)
    await make_inventory(
        session, cipher=cipher, product=product, plan=plan,
        secret=fingerprint_secret, count=2,
    )
    first = await _order(session, user, product, plan)
    item = await inventory_service.reserve_for_order(session, order=first)
    await inventory_service.mark_delivered(session, order_id=first.id, item_id=item.id)
    await session.commit()

    # Only the second (still available) pair can go to the next buyer.
    second = await _order(session, user, product, plan)
    second_item = await inventory_service.reserve_for_order(session, order=second)
    assert second_item.id != item.id

    # mark_delivered refuses a pair that belongs to someone else.
    with pytest.raises(inventory_service.InventoryError):
        await inventory_service.mark_delivered(
            session, order_id=second.id, item_id=item.id
        )


async def test_pool_does_not_hand_out_a_delivered_pair(session, cipher, fingerprint_secret) -> None:
    user = await make_user(session)
    _, product, plan = await make_catalog(session)
    await make_inventory(
        session, cipher=cipher, product=product, plan=plan,
        secret=fingerprint_secret, count=1,
    )
    first = await _order(session, user, product, plan)
    item = await inventory_service.reserve_for_order(session, order=first)
    await inventory_service.mark_delivered(session, order_id=first.id, item_id=item.id)
    await session.commit()

    # create_order refuses an empty pool up front...
    with pytest.raises(order_service.OrderError):
        await _order(session, user, product, plan)

    counts = await inventory_service.counts_for_product(session, product.id)
    assert counts.available == 0
    assert counts.delivered == 1


async def test_database_trigger_blocks_reuse_even_on_direct_update(
    session, cipher, fingerprint_secret
) -> None:
    user = await make_user(session)
    _, product, plan = await make_catalog(session)
    await make_inventory(
        session, cipher=cipher, product=product, plan=plan,
        secret=fingerprint_secret, count=1,
    )
    order = await _order(session, user, product, plan)
    item = await inventory_service.reserve_for_order(session, order=order)
    await inventory_service.mark_delivered(session, order_id=order.id, item_id=item.id)
    await session.commit()

    # Bypass the service completely and try to put the pair back on sale.
    with pytest.raises(DBAPIError):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE inventory_items SET status = 'AVAILABLE' WHERE id = :id"),
                {"id": item.id},
            )


async def test_disabled_pair_cannot_be_reactivated(session, cipher, fingerprint_secret) -> None:
    _, product, plan = await make_catalog(session)
    (item,) = await make_inventory(
        session, cipher=cipher, product=product, plan=plan,
        secret=fingerprint_secret, count=1,
    )
    await inventory_service.disable_item(session, item.id, "خراب")
    await session.commit()

    with pytest.raises(DBAPIError):
        await session.execute(
            text("UPDATE inventory_items SET status = 'AVAILABLE' WHERE id = :id"),
            {"id": item.id},
        )
        await session.commit()


async def test_duplicate_config_is_rejected_by_fingerprint(
    session, cipher, fingerprint_secret
) -> None:
    _, product, plan = await make_catalog(session)
    (existing,) = await make_inventory(
        session, cipher=cipher, product=product, plan=plan,
        secret=fingerprint_secret, count=1,
    )
    # Same config, different row: the unique fingerprint is the only guard.
    duplicate = InventoryItem(
        product_id=product.id,
        plan_id=plan.id,
        config_cipher=existing.config_cipher,
        config_fingerprint=existing.config_fingerprint,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_reservation_releases_back_to_pool_when_status_resets(
    session, cipher, fingerprint_secret
) -> None:
    """RESERVED -> AVAILABLE stays legal (a cancelled checkout releases stock)."""
    user = await make_user(session)
    _, product, plan = await make_catalog(session)
    (item,) = await make_inventory(
        session, cipher=cipher, product=product, plan=plan,
        secret=fingerprint_secret, count=1,
    )
    order = await _order(session, user, product, plan)
    await inventory_service.reserve_for_order(session, order=order)
    await orders_cancel(session, order)

    item = (
        await session.execute(
            select(InventoryItem)
            .where(InventoryItem.id == item.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert item.status is InventoryStatus.AVAILABLE
    assert item.assigned_order_id is None


async def orders_cancel(session, order) -> None:
    """Mirror of what the checkout cancellation path does."""
    await session.execute(
        text(
            """
            UPDATE inventory_items
            SET status = 'AVAILABLE', assigned_order_id = NULL, reserved_at = NULL
            WHERE assigned_order_id = :order_id AND status = 'RESERVED'
            """
        ),
        {"order_id": order.id},
    )
    order.status = OrderStatus.CANCELLED
    await session.flush()


async def test_parallel_reservations_never_hand_out_the_same_pair(
    session_factory, cipher, fingerprint_secret
) -> None:
    """Two orders reserving at the same instant with one pair in stock."""
    async with session_factory() as setup:
        user = await make_user(setup)
        _, product, plan = await make_catalog(setup)
        await make_inventory(
            setup, cipher=cipher, product=product, plan=plan,
            secret=fingerprint_secret, count=1,
        )
        order_a = await _order(setup, user, product, plan)
        order_b = await _order(setup, user, product, plan)
        await setup.commit()
        product_id, plan_id, user_id = product.id, plan.id, user.id
        order_ids = (order_a.id, order_b.id)

    async def reserve(order_id: int) -> str:
        async with session_factory() as db:
            order = await order_service.get_order(db, order_id, for_update=True)
            assert order is not None
            try:
                item = await inventory_service.reserve_for_order(db, order=order)
            except inventory_service.NoInventoryAvailable:
                await db.commit()
                return "none"
            await db.commit()
            return f"item:{item.id}"

    results = await asyncio.gather(*(reserve(oid) for oid in order_ids))

    handed_out = sorted(r for r in results if r != "none")
    assert len(handed_out) == 1, results
    assert results.count("none") == 1

    async with session_factory() as check:
        rows = await check.execute(
            text(
                "SELECT assigned_order_id FROM inventory_items "
                "WHERE product_id = :p AND status = 'RESERVED'"
            ),
            {"p": product_id},
        )
        assigned = [r[0] for r in rows.all()]
        assert len(assigned) == 1
        assert assigned[0] in order_ids
        assert user_id and plan_id  # keep the fixture values meaningful

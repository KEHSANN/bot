"""Single-use VPN inventory: reservation, binding and delivery.

Concurrency strategy (PostgreSQL):

* A pair is claimed with ``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`` so that
  two concurrent purchases can never claim the same row.
* ``InventoryAssignment.order_id`` and ``.inventory_item_id`` are unique: even if
  a bug retried the claim, the database refuses a second binding.
* ``inventory_assignments`` / DELIVERED rows are never deleted; a DB trigger
  forbids reverting DELIVERED inventory to any other state.
* Reservation is *permanent binding*: once reserved for an order it is only ever
  delivered to that order's customer, including every retry afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.base import utcnow
from vpnbot.db.enums import InventoryStatus
from vpnbot.db.models.inventory import DeliveryAttempt, InventoryAssignment, InventoryItem
from vpnbot.db.models.orders import Order
from vpnbot.security.crypto import CredentialCipher


class InventoryError(ValueError):
    pass


class NoInventoryAvailable(InventoryError):
    pass


@dataclass(frozen=True)
class RevealedPair:
    config: str
    subscription_url: str | None


@dataclass(frozen=True)
class InventoryCounts:
    available: int
    reserved: int
    delivered: int
    disabled: int


async def counts_for_product(
    session: AsyncSession, product_id: int, plan_id: int | None = None
) -> InventoryCounts:
    conditions = [InventoryItem.product_id == product_id]
    if plan_id is not None:
        conditions.append(InventoryItem.plan_id == plan_id)
    rows = (
        await session.execute(
            select(InventoryItem.status, func.count(InventoryItem.id))
            .where(*conditions)
            .group_by(InventoryItem.status)
        )
    ).all()
    data = {status: int(count) for status, count in rows}
    return InventoryCounts(
        available=data.get(InventoryStatus.AVAILABLE, 0),
        reserved=data.get(InventoryStatus.RESERVED, 0),
        delivered=data.get(InventoryStatus.DELIVERED, 0),
        disabled=data.get(InventoryStatus.DISABLED, 0),
    )


async def reserve_for_order(
    session: AsyncSession, *, order: Order, allow_product_wide: bool = True
) -> InventoryItem:
    """Atomically bind one AVAILABLE pair to ``order``.

    Must be called inside a transaction. Raises ``NoInventoryAvailable`` when the
    pool is empty; never fabricates a pair.
    """
    existing = (
        await session.execute(
            select(InventoryItem)
            .where(InventoryItem.assigned_order_id == order.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Idempotent: the order already owns a pair (retry path).
        return existing

    conditions = [
        InventoryItem.product_id == order.product_id,
        InventoryItem.status == InventoryStatus.AVAILABLE,
    ]
    if order.plan_id is not None:
        if allow_product_wide:
            conditions.append(
                (InventoryItem.plan_id == order.plan_id) | InventoryItem.plan_id.is_(None)
            )
        else:
            conditions.append(InventoryItem.plan_id == order.plan_id)
    else:
        conditions.append(InventoryItem.plan_id.is_(None))

    row = (
        await session.execute(
            select(InventoryItem.id)
            .where(*conditions)
            .order_by(InventoryItem.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).first()
    if row is None:
        raise NoInventoryAvailable("موجودی آزاد برای این محصول وجود ندارد")

    item_id = int(row[0])
    now = utcnow()
    await session.execute(
        update(InventoryItem)
        .where(InventoryItem.id == item_id, InventoryItem.status == InventoryStatus.AVAILABLE)
        .values(
            status=InventoryStatus.RESERVED,
            assigned_order_id=order.id,
            reserved_at=now,
        )
    )
    session.add(
        InventoryAssignment(
            inventory_item_id=item_id,
            order_id=order.id,
            user_id=order.user_id,
            assigned_at=now,
        )
    )
    await session.flush()
    # The UPDATE above is a Core statement, so an item already in the identity
    # map (loaded earlier in this transaction) would keep its stale fields.
    # Re-read from the row the reservation actually touched.
    item = (
        await session.execute(
            select(InventoryItem)
            .where(InventoryItem.id == item_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if item is None or item.assigned_order_id != order.id:
        raise InventoryError("رزرو موجودی ناموفق بود")
    return item


async def mark_delivered(
    session: AsyncSession, *, order_id: int, item_id: int
) -> InventoryItem:
    item = await session.get(InventoryItem, item_id)
    if item is None or item.assigned_order_id != order_id:
        raise InventoryError("این موجودی متعلق به این سفارش نیست")
    if item.status is InventoryStatus.DELIVERED:
        return item  # idempotent
    if item.status is not InventoryStatus.RESERVED:
        raise InventoryError(f"انتقال نامعتبر از وضعیت {item.status.value} به DELIVERED")
    now = utcnow()
    item.status = InventoryStatus.DELIVERED
    item.delivered_at = now
    assignment = (
        await session.execute(
            select(InventoryAssignment).where(InventoryAssignment.order_id == order_id)
        )
    ).scalar_one_or_none()
    if assignment is not None:
        assignment.delivered_at = now
    await session.flush()
    return item


async def disable_item(session: AsyncSession, item_id: int, reason: str) -> InventoryItem:
    item = await session.get(InventoryItem, item_id, with_for_update=True)
    if item is None:
        raise InventoryError("آیتم یافت نشد")
    if item.status in (InventoryStatus.DELIVERED, InventoryStatus.RESERVED):
        raise InventoryError("آیتم تحویل‌شده یا رزروشده قابل غیرفعال‌سازی نیست")
    item.status = InventoryStatus.DISABLED
    item.disabled_at = utcnow()
    item.disabled_reason = reason[:500]
    await session.flush()
    return item


async def reveal_pair(
    session: AsyncSession, *, cipher: CredentialCipher, item_id: int
) -> RevealedPair:
    item = await session.get(InventoryItem, item_id)
    if item is None:
        raise InventoryError("آیتم یافت نشد")
    return RevealedPair(
        config=cipher.decrypt(item.config_cipher),
        subscription_url=cipher.decrypt(item.sub_url_cipher) if item.sub_url_cipher else None,
    )


async def item_for_order(session: AsyncSession, order_id: int) -> InventoryItem | None:
    return (
        await session.execute(
            select(InventoryItem).where(InventoryItem.assigned_order_id == order_id)
        )
    ).scalar_one_or_none()


async def log_attempt(
    session: AsyncSession,
    *,
    order_id: int,
    item_id: int,
    outcome: str,
    error_code: str | None = None,
) -> DeliveryAttempt:
    last = (
        await session.execute(
            select(func.coalesce(func.max(DeliveryAttempt.attempt_no), 0)).where(
                DeliveryAttempt.order_id == order_id
            )
        )
    ).scalar_one()
    attempt = DeliveryAttempt(
        order_id=order_id,
        inventory_item_id=item_id,
        attempt_no=int(last) + 1,
        outcome=outcome,
        error_code=(error_code or None) and error_code[:120],
    )
    session.add(attempt)
    await session.flush()
    return attempt


def item_expired(item: InventoryItem, now: datetime) -> bool:
    # Placeholder for expiry only when a plan defines time-limited inventory.
    return False

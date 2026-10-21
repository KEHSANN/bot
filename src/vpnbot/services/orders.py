"""Order creation and lifecycle helpers."""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vpnbot.db.enums import DeliveryStatus, OrderStatus, PaymentMethod
from vpnbot.db.models.catalog import Plan, Product
from vpnbot.db.models.orders import Order, Payment
from vpnbot.services import catalog as catalog_service


class OrderError(ValueError):
    pass


def _public_id() -> str:
    return secrets.token_hex(4).upper()


async def create_order(
    session: AsyncSession, *, user_id: int, plan_id: int, product: Product, plan: Plan
) -> Order:
    if not product.is_on_sale or not plan.is_on_sale or plan.product_id != product.id:
        raise OrderError("این محصول یا پلن در حال حاضر قابل خرید نیست")
    if plan.price_irt <= 0:
        raise OrderError("قیمت پلن نامعتبر است")
    if await catalog_service.stock_for_plan(session, product.id, plan.id) <= 0:
        raise OrderError("موجودی این محصول به پایان رسیده است")

    order = Order(
        public_id=_public_id(),
        user_id=user_id,
        product_id=product.id,
        plan_id=plan.id,
        product_title_snapshot=product.title,
        plan_title_snapshot=plan.title,
        specs_snapshot={
            "duration_days": plan.duration_days,
            "traffic_gb": plan.traffic_gb,
            "device_limit": plan.device_limit,
        },
        unit_price_irt=plan.price_irt,
        total_irt=plan.price_irt,
        status=OrderStatus.PENDING_PAYMENT,
        delivery_status=DeliveryStatus.NOT_STARTED,
    )
    session.add(order)
    await session.flush()
    return order


async def get_order(
    session: AsyncSession, order_id: int, *, for_update: bool = False, with_payment: bool = True
) -> Order | None:
    stmt = select(Order).where(Order.id == order_id)
    if with_payment:
        stmt = stmt.options(selectinload(Order.payment))
    if for_update:
        stmt = stmt.with_for_update(of=Order)
    return (await session.execute(stmt)).scalar_one_or_none()


async def user_order(
    session: AsyncSession, *, order_id: int, user_id: int
) -> Order | None:
    order = await get_order(session, order_id)
    if order is None or order.user_id != user_id:
        return None
    return order


async def list_user_orders(
    session: AsyncSession, user_id: int, *, offset: int = 0, limit: int = 6
) -> list[Order]:
    result = await session.execute(
        select(Order)
        .options(selectinload(Order.payment))
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars())


async def count_user_orders(session: AsyncSession, user_id: int) -> int:
    from sqlalchemy import func

    return int(
        (await session.execute(select(func.count(Order.id)).where(Order.user_id == user_id))).scalar_one()
    )


async def attach_payment(session: AsyncSession, order: Order) -> Payment:
    if order.payment is not None:
        return order.payment
    # Enum member, not the bare string: a plain ``str`` stays on the cached
    # instance and breaks later ``payment.method.value`` lookups in-session.
    payment = Payment(order_id=order.id, amount_irt=order.total_irt, method=PaymentMethod.CARD)
    session.add(payment)
    await session.flush()
    return payment


async def cancel_order(session: AsyncSession, order_id: int, *, reason: str | None = None) -> Order:
    order = await get_order(session, order_id, for_update=True)
    if order is None:
        raise OrderError("سفارش یافت نشد")
    if order.status in (OrderStatus.FULFILLED, OrderStatus.REFUNDED):
        raise OrderError("سفارش تحویل‌شده قابل لغو نیست")
    order.status = OrderStatus.CANCELLED
    order.notes = reason

    # Release a reserved-but-undelivered inventory item back to the
    # pool; before this, cancelling a paid order leaked the unit as
    # RESERVED forever. The assignment row documents the binding, so it
    # goes with it. (DELIVERED items never reach here — fulfilled orders
    # are refused above.)
    from vpnbot.db.enums import InventoryStatus, LedgerKind, PaymentStatus
    from vpnbot.db.models.inventory import InventoryAssignment
    from vpnbot.services import inventory as inventory_service
    from vpnbot.services import wallet as wallet_service

    item = await inventory_service.item_for_order(session, order.id)
    if item is not None and item.status is InventoryStatus.RESERVED:
        item.status = InventoryStatus.AVAILABLE
        item.assigned_order_id = None
        item.reserved_at = None
        assignment = (
            await session.execute(
                select(InventoryAssignment).where(InventoryAssignment.order_id == order.id)
            )
        ).scalar_one_or_none()
        if assignment is not None:
            await session.delete(assignment)

    # Wallet-paid orders refund themselves back to the wallet, exactly
    # once (ledger idempotency key). Card/crypto payments were settled
    # outside the bot — refunding those stays a manual admin action.
    payment = order.payment
    if (
        payment is not None
        and payment.method is PaymentMethod.WALLET
        and payment.status is PaymentStatus.APPROVED
    ):
        await wallet_service.credit(
            session,
            user_id=order.user_id,
            amount_irt=order.total_irt,
            kind=LedgerKind.REFUND,
            key=f"order:{order.id}:refund",
            reason=f"استرداد لغو سفارش #{order.public_id}",
        )
    await session.flush()
    return order

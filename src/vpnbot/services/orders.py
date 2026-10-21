"""Order creation and lifecycle helpers."""

from __future__ import annotations

import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vpnbot.db.enums import DeliveryStatus, OrderStatus, PaymentMethod
from vpnbot.db.models.catalog import Plan, Product
from vpnbot.db.models.orders import Order, Payment
from vpnbot.db.models.users import User
from vpnbot.services import catalog as catalog_service


class OrderError(ValueError):
    pass


#: Maximum simultaneous unpaid orders per customer (see create_order).
MAX_OPEN_ORDERS = 3


def _public_id() -> str:
    return secrets.token_hex(4).upper()


async def create_order(
    session: AsyncSession, *, user_id: int, plan_id: int, product: Product, plan: Plan
) -> Order:
    # Serialize per-user creation: the open-order cap below is a
    # COUNT under READ COMMITTED — ten simultaneous requests could
    # each count < cap and all insert. Locking the user's row makes
    # count + insert atomic per user.
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())
    if not product.is_on_sale or not plan.is_on_sale or plan.product_id != product.id:
        raise OrderError("این محصول یا پلن در حال حاضر قابل خرید نیست")
    if plan.price_irt <= 0:
        raise OrderError("قیمت پلن نامعتبر است")
    # Anti-spam cap: unlimited open orders let one account flood the
    # admin payment queue with fake receipts. Three concurrent unpaid
    # orders is generous for a real customer.
    open_orders = int(
        (
            await session.execute(
                select(func.count(Order.id)).where(
                    Order.user_id == user_id, Order.status == OrderStatus.PENDING_PAYMENT
                )
            )
        ).scalar_one()
    )
    if open_orders >= MAX_OPEN_ORDERS:
        raise OrderError(
            "سفارش پرداخت‌نشده‌ی باز دارید؛ ابتدا آن‌ها را تکمیل یا لغو کنید"
        )
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
        # populate_existing: without it, a session-cached instance
        # keeps its stale attribute values even though the row was
        # just locked and re-read — callers deciding on order.status
        # (cancel vs pay races) would act on the old state.
        stmt = stmt.with_for_update(of=Order).execution_options(populate_existing=True)
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


async def expire_stale_orders(session: AsyncSession) -> int:
    """Auto-cancel unpaid orders older than the admin-set TTL.

    The TTL is the shop setting ``order_ttl_hours`` (0 disables it).
    Only orders whose ball is with the *customer* expire — payment
    still AWAITING details or REJECTED (they may resubmit until the
    deadline). A payment in PENDING_REVIEW waits on an admin, not the
    customer, so those orders never expire here. Without this sweep,
    abandoned orders (customer tapped cancel, receipt rejected and
    forgotten) sat in PENDING_PAYMENT forever and eventually blocked
    the customer via the open-order cap.
    """
    from datetime import timedelta

    from vpnbot.db.base import utcnow
    from vpnbot.db.enums import PaymentStatus
    from vpnbot.services.settings import SHOP_SETTINGS, SettingsService

    config = await SettingsService(session).get(SHOP_SETTINGS)
    ttl_hours = int(config.get("order_ttl_hours", 48) or 0)
    if ttl_hours <= 0:
        return 0
    cutoff = utcnow() - timedelta(hours=ttl_hours)
    orders = (
        await session.execute(
            select(Order)
            .options(selectinload(Order.payment))
            .where(Order.status == OrderStatus.PENDING_PAYMENT, Order.created_at < cutoff)
        )
    ).scalars()
    cancelled = 0
    for order in orders:
        payment = order.payment
        if payment is not None and payment.status not in (
            PaymentStatus.AWAITING_PAYMENT_DETAILS,
            PaymentStatus.REJECTED,
        ):
            continue
        if payment is not None and payment.status is PaymentStatus.AWAITING_PAYMENT_DETAILS:
            payment.status = PaymentStatus.EXPIRED
            payment.txid = None
            # Flush BEFORE delegating: cancel_order re-reads the order
            # with populate_existing, which would otherwise overwrite
            # this unflushed EXPIRED with the stored AWAITING value
            # and then "cancel" the payment a second time.
            await session.flush()
        await cancel_order(session, order.id, reason="انقضای مهلت تکمیل سفارش")
        cancelled += 1
    return cancelled


async def cancel_order(session: AsyncSession, order_id: int, *, reason: str | None = None) -> Order:
    order = await get_order(session, order_id, for_update=True)
    if order is None:
        raise OrderError("سفارش یافت نشد")
    if order.status in (OrderStatus.FULFILLED, OrderStatus.REFUNDED):
        raise OrderError("سفارش تحویل‌شده قابل لغو نیست")
    if order.delivery_status is DeliveryStatus.SENDING:
        # The worker is between "reserved" and "finalised" right now:
        # the config may already be in the customer's chat. Cancelling
        # here would refund the money, return the (delivered) config
        # to the pool and let it be sold twice.
        raise OrderError("سفارش در حال ارسال است؛ چند لحظه دیگر دوباره تلاش کنید")

    # A delivery attempt means a send was tried: the pair may already
    # be with the customer even if the worker recorded a failure.
    # Auto-cancelling such an order cannot be made safe in one step —
    # repooling risks a double sale, refunding risks a free config —
    # so it is refused and left to manual review (the item stays
    # RESERVED to this order and is never sold again either way).
    from vpnbot.db.enums import InventoryStatus, LedgerKind, PaymentStatus
    from vpnbot.db.models.inventory import DeliveryAttempt, InventoryAssignment
    from vpnbot.services import inventory as inventory_service
    from vpnbot.services import wallet as wallet_service

    attempts = int(
        (
            await session.execute(
                select(func.count(DeliveryAttempt.id)).where(
                    DeliveryAttempt.order_id == order.id
                )
            )
        ).scalar_one()
    )
    if attempts > 0:
        raise OrderError(
            "برای این سفارش تلاش تحویل ثبت شده است؛ لغو خودکار ممکن نیست "
            "(احتمال تحویل کانفیگ). برای بررسی دستی با پشتیبانی هماهنگ کنید"
        )

    order.status = OrderStatus.CANCELLED
    order.notes = reason

    # The payment leaves the review queue with the order; before this,
    # a cancelled order's receipt sat in PENDING_REVIEW forever.
    payment = order.payment
    if payment is not None and payment.status in (
        PaymentStatus.AWAITING_PAYMENT_DETAILS,
        PaymentStatus.PENDING_REVIEW,
    ):
        payment.status = PaymentStatus.CANCELLED
        # Free the TXID like reject/expire do: the txid column is
        # unique and txid_taken ignores status, so a cancelled
        # payment's hash stayed burned forever and the customer could
        # never resubmit that (real) transfer for a new order.
        payment.txid = None

    if attempts == 0:
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

        # Wallet-paid orders refund themselves back to the wallet,
        # exactly once (ledger idempotency key). Card/crypto payments
        # were settled outside the bot — refunding those stays manual.
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

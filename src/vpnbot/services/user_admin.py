"""Admin user moderation: block / unblock a customer.

Blocking is deliberately destructive about *evidence*:

* the customer's status flips to BLOCKED (the bot then refuses to process
  anything they send — see ``BlockedUserMiddleware``);
* every receipt they ever sent is wiped (``receipt_file_id``/type are
  cleared on all of their order payments and wallet top-ups, so the
  evidence can no longer be viewed or forwarded from the panel);
* anything still awaiting review is CANCELLED so it leaves the queues
  instead of sitting there waiting to be approved by mistake.

Approved history keeps its amounts/statuses (accounting stays intact) —
only the receipt files go. Unblocking restores access but never the
receipts: the customer must send fresh evidence.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.enums import AuditActor, OrderStatus, PaymentStatus, UserStatus
from vpnbot.db.models.orders import Order, Payment
from vpnbot.db.models.users import Admin, User
from vpnbot.db.models.wallet import WalletTopUp
from vpnbot.services import audit as audit_service


class UserAdminError(ValueError):
    pass


_PENDING = (PaymentStatus.AWAITING_PAYMENT_DETAILS, PaymentStatus.PENDING_REVIEW)


async def block_user(session: AsyncSession, *, user_id: int, admin_id: int) -> dict[str, int]:
    # Lock in the global order — user row, then orders, then
    # payments/top-ups. This used to mutate payments from a plain
    # unlocked select before ever touching the orders, the reverse
    # of every payment path (and racy against a concurrent submit).
    user = (
        await session.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        raise UserAdminError("کاربر یافت نشد")
    admin_row = (
        await session.execute(
            select(Admin).where(Admin.user_id == user.id, Admin.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if admin_row is not None:
        raise UserAdminError("این کاربر ادمین است و نمی‌توان او را بلاک کرد")

    user.status = UserStatus.BLOCKED

    receipts_cleared = 0
    wiped_file_ids: list[str] = []
    payments_cancelled = 0
    orders_cancelled = 0
    payments = (
        await session.execute(
            select(Payment)
            .join(Order, Order.id == Payment.order_id)
            .where(Order.user_id == user_id)
            .with_for_update()
        )
    ).scalars()
    for payment in payments:
        if payment.receipt_file_id:
            receipts_cleared += 1
            wiped_file_ids.append(payment.receipt_file_id)
            payment.receipt_file_id = None
            payment.receipt_file_type = None
        if payment.status in _PENDING:
            payment.status = PaymentStatus.CANCELLED
            payment.txid = None
            payments_cancelled += 1

    # Cancel EVERY still-unpaid order straight from the Order table.
    # The old code cancelled an order only via its payment row, so
    # PENDING_PAYMENT orders with no payment row yet (a checkout
    # preview created before the payment row exists) or with a
    # REJECTED payment stayed open — and after unblock the customer
    # faced zombie orders and the open-order cap. Paid orders
    # (FULFILLMENT_PENDING) are deliberately untouched: blocking is
    # not a refund, and their delivery is gated separately.
    orders = (
        await session.execute(
            select(Order)
            .where(Order.user_id == user_id, Order.status == OrderStatus.PENDING_PAYMENT)
            .with_for_update()
        )
    ).scalars()
    for order in orders:
        order.status = OrderStatus.CANCELLED
        order.notes = "لغو به‌دلیل مسدود شدن کاربر"
        orders_cancelled += 1

    topups_cancelled = 0
    topups = (
        await session.execute(
            select(WalletTopUp).where(WalletTopUp.user_id == user_id).with_for_update()
        )
    ).scalars()
    for topup in topups:
        if topup.receipt_file_id:
            receipts_cleared += 1
            wiped_file_ids.append(topup.receipt_file_id)
            topup.receipt_file_id = None
            topup.receipt_file_type = None
        if topup.status in _PENDING:
            topup.status = PaymentStatus.CANCELLED
            topup.txid = None
            topups_cancelled += 1

    result = {
        "receipts_cleared": receipts_cleared,
        "payments_cancelled": payments_cancelled,
        "orders_cancelled": orders_cancelled,
        "topups_cancelled": topups_cancelled,
    }
    await audit_service.record(
        session,
        actor_type=AuditActor.ADMIN,
        actor_id=admin_id,
        action="user.blocked",
        entity_type="user",
        entity_id=user.id,
        # The wipe is the owner's requested behaviour, but the file
        # ids are preserved in the (append-only, DB-protected) audit
        # log — the evidence is recoverable, never silently destroyed.
        data={**result, "receipt_file_ids": wiped_file_ids},
    )
    await session.flush()
    return result


async def unblock_user(session: AsyncSession, *, user_id: int, admin_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise UserAdminError("کاربر یافت نشد")
    user.status = UserStatus.ACTIVE
    await audit_service.record(
        session,
        actor_type=AuditActor.ADMIN,
        actor_id=admin_id,
        action="user.unblocked",
        entity_type="user",
        entity_id=user.id,
        data={},
    )
    await session.flush()
    return user

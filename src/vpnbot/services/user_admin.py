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

from vpnbot.db.enums import AuditActor, PaymentStatus, UserStatus
from vpnbot.db.models.orders import Order, Payment
from vpnbot.db.models.users import Admin, User
from vpnbot.db.models.wallet import WalletTopUp
from vpnbot.services import audit as audit_service


class UserAdminError(ValueError):
    pass


_PENDING = (PaymentStatus.AWAITING_PAYMENT_DETAILS, PaymentStatus.PENDING_REVIEW)


async def block_user(session: AsyncSession, *, user_id: int, admin_id: int) -> dict[str, int]:
    user = await session.get(User, user_id)
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
    payments_cancelled = 0
    payments = (
        await session.execute(
            select(Payment).join(Order, Order.id == Payment.order_id).where(Order.user_id == user_id)
        )
    ).scalars()
    for payment in payments:
        if payment.receipt_file_id:
            receipts_cleared += 1
            payment.receipt_file_id = None
            payment.receipt_file_type = None
        if payment.status in _PENDING:
            payment.status = PaymentStatus.CANCELLED
            payment.txid = None
            payments_cancelled += 1

    topups_cancelled = 0
    topups = (
        await session.execute(select(WalletTopUp).where(WalletTopUp.user_id == user_id))
    ).scalars()
    for topup in topups:
        if topup.receipt_file_id:
            receipts_cleared += 1
            topup.receipt_file_id = None
            topup.receipt_file_type = None
        if topup.status in _PENDING:
            topup.status = PaymentStatus.CANCELLED
            topup.txid = None
            topups_cancelled += 1

    result = {
        "receipts_cleared": receipts_cleared,
        "payments_cancelled": payments_cancelled,
        "topups_cancelled": topups_cancelled,
    }
    await audit_service.record(
        session,
        actor_type=AuditActor.ADMIN,
        actor_id=admin_id,
        action="user.blocked",
        entity_type="user",
        entity_id=user.id,
        data=result,
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

"""Post-approval fulfilment: the FINAL PAYMENT GATE and delivery.

Sequence (each step is crash-safe):

1. tx¹: lock the order **and its payment row**, independently re-check
   ``payment.status == APPROVED`` straight from the database (caller state is
   never trusted), atomically reserve one AVAILABLE pair for this order, and
   mark the order ``FULFILLMENT_PENDING`` / ``SENDING``.
2. send the pair to the customer's private chat (outside any transaction).
3. tx²: mark the pair ``DELIVERED`` (permanent), order ``FULFILLED``, log the
   attempt, issue referral rewards, write an audit event.

Failure handling:
  * send failed → pair stays RESERVED to this same order, delivery becomes
    ``RETRY_REQUIRED``; retries reuse the same pair.
  * crash after step 2 but before step 3 → the order is still ``SENDING`` with a
    RESERVED pair; the job re-runs, finds the existing binding, re-sends to the
    same customer (safe duplicate) and finalizes.
  * no free pair → payment stays APPROVED, order stays ``FULFILLMENT_PENDING``,
    admins are alerted; nothing is invented.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vpnbot.db.base import utcnow
from vpnbot.db.enums import AuditActor, DeliveryStatus, OrderStatus, PaymentStatus
from vpnbot.db.models.orders import Payment
from vpnbot.db.models.users import User
from vpnbot.security.crypto import CredentialCipher
from vpnbot.services import audit as audit_service
from vpnbot.services import inventory as inventory_service
from vpnbot.services import orders as order_service
from vpnbot.services import referrals as referral_service

if TYPE_CHECKING:
    from vpnbot.db.models.orders import Order

DeliverFn = Callable[[int, str, str | None, str, str], Awaitable[None]]
"""DeliverFn args: telegram_id, config, sub_url, product_title, order_public_id"""


class FulfillOutcome(str, Enum):
    DELIVERED = "DELIVERED"
    ALREADY = "ALREADY_FULFILLED"
    RETRY = "RETRY"
    NO_INVENTORY = "NO_INVENTORY"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class FulfillResult:
    outcome: FulfillOutcome
    order_id: int
    detail: str = ""

    @property
    def needs_retry(self) -> bool:
        return self.outcome is FulfillOutcome.RETRY


class PaymentGateError(RuntimeError):
    """Raised when fulfilment is attempted without an approved payment."""


async def enqueue_fulfill_job(session: AsyncSession, order: Order) -> bool:
    """(Re)queue the one canonical fulfilment job for a paid order.

    Returns False when a live job (QUEUED/RUNNING) already exists —
    the caller is then just early, not wrong. A finished or failed
    job is revived in place under the same ``fulfill:<id>`` key: the
    old ``:retry:<timestamp>`` key minted a SECOND live job whenever
    it raced a restock requeue, and the config was delivered twice.

    Shared by the admin panel's requeue button and the customer's
    «receive again» button (the unblock-then-resend path: a delivery
    that failed with TelegramForbiddenError can be retried by the
    customer once they unblock the bot, without burning the pair).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from vpnbot.db.enums import JobKind, JobStatus
    from vpnbot.db.models.system import Job

    existing = (
        await session.execute(
            select(Job.id).where(
                Job.kind == JobKind.FULFILL_ORDER,
                Job.idempotency_key == f"fulfill:{order.id}",
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
    ).first()
    if existing is not None:
        return False
    await session.execute(
        pg_insert(Job)
        .values(
            kind=JobKind.FULFILL_ORDER,
            payload={"order_id": order.id},
            idempotency_key=f"fulfill:{order.id}",
            status=JobStatus.QUEUED,
        )
        .on_conflict_do_update(
            index_elements=["idempotency_key"],
            set_={
                "status": JobStatus.QUEUED,
                "attempt": 0,
                "scheduled_at": utcnow(),
                "started_at": None,
                "finished_at": None,
                "last_error": None,
            },
            where=Job.status.in_([JobStatus.FAILED, JobStatus.DONE, JobStatus.QUEUED]),
        )
    )
    order.delivery_status = DeliveryStatus.PENDING
    await session.flush()
    return True


async def requeue_pending_for_product(session: AsyncSession, *, product_id: int) -> int:
    """Revive fulfilment for paid orders of a product after restock.

    A paid order that hits an empty shelf raises NO_INVENTORY in the
    worker, which burns the job's retry attempts (8 with backoff —
    gone in a few minutes) and leaves the job FAILED for good: the
    customer paid, and restocking did nothing. Called after a stock
    import, this resets every FULFILLMENT_PENDING order's job to
    QUEUED with a fresh attempt budget (or inserts it if missing).
    RUNNING jobs are left alone; a QUEUED job still waiting out its
    backoff is pulled forward to now (its schedule resets below).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from vpnbot.db.enums import JobKind, JobStatus
    from vpnbot.db.models.orders import Order
    from vpnbot.db.models.system import Job

    order_ids = (
        await session.execute(
            select(Order.id).where(
                Order.product_id == product_id,
                Order.status == OrderStatus.FULFILLMENT_PENDING,
            )
        )
    ).scalars()
    count = 0
    for order_id in order_ids:
        await session.execute(
            pg_insert(Job)
            .values(
                kind=JobKind.FULFILL_ORDER,
                payload={"order_id": order_id},
                idempotency_key=f"fulfill:{order_id}",
                status=JobStatus.QUEUED,
            )
            .on_conflict_do_update(
                index_elements=["idempotency_key"],
                set_={
                    "status": JobStatus.QUEUED,
                    "attempt": 0,
                    "scheduled_at": utcnow(),
                    "started_at": None,
                    "finished_at": None,
                    "last_error": None,
                },
                where=Job.status.in_([JobStatus.FAILED, JobStatus.DONE, JobStatus.QUEUED]),
            )
        )
        count += 1
    await session.flush()
    return count


async def fulfill_order(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    cipher: CredentialCipher,
    deliver: DeliverFn,
) -> FulfillResult:
    prepared = await _reserve_and_gate(session_factory, order_id=order_id, cipher=cipher)
    if isinstance(prepared, FulfillResult):
        return prepared

    telegram_id, pair, product_title, public_id, item_id = prepared

    try:
        await deliver(telegram_id, pair.config, pair.subscription_url, product_title, public_id)
    except Exception as exc:  # noqa: BLE001 - any send failure is retriable
        await _record_failure(session_factory, order_id=order_id, item_id=item_id, exc=exc)
        return FulfillResult(FulfillOutcome.RETRY, order_id, type(exc).__name__)

    await _finalize(session_factory, order_id=order_id, item_id=item_id)
    return FulfillResult(FulfillOutcome.DELIVERED, order_id)


async def _reserve_and_gate(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    cipher: CredentialCipher,
) -> tuple[int, inventory_service.RevealedPair, str, str, int] | FulfillResult:
    """Phase 1. Returns delivery inputs, or a terminal result that skips sending."""
    async with session_factory() as session, session.begin():
        order = await order_service.get_order(session, order_id, for_update=True)
        if order is None:
            raise PaymentGateError(f"order {order_id} not found")

        # Lock the payment row too, so an approval cannot slip between the check
        # and the reservation.
        payment = (
            await session.execute(
                select(Payment)
                .where(Payment.order_id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

        # Terminal order states FIRST: a paid order an admin cancelled
        # has payment CANCELLED too, so checking the payment first
        # turned it into a PaymentGateError and the worker burned all
        # 8 job attempts instead of finishing as SKIPPED once.
        if order.status is OrderStatus.FULFILLED:
            return FulfillResult(FulfillOutcome.ALREADY, order_id)

        if order.status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
            await audit_service.record(
                session,
                actor_type=AuditActor.SYSTEM,
                actor_id=None,
                action="fulfillment.skipped_cancelled_order",
                entity_type="order",
                entity_id=order_id,
                data={"order_status": order.status.value},
            )
            return FulfillResult(FulfillOutcome.SKIPPED, order_id, order.status.value)

        if order.delivery_status is DeliveryStatus.SENDING:
            # Another execution is between reserve and finalize for
            # this order (or crashed there). A fresh SENDING means
            # "in flight right now": bouncing keeps two live jobs
            # from delivering the same config twice. An OLD SENDING
            # is a crash remnant — fall through and let the
            # idempotent reserve sort it out.
            from datetime import timedelta as _td

            if order.updated_at and order.updated_at > utcnow() - _td(minutes=10):
                return FulfillResult(FulfillOutcome.RETRY, order_id, "delivery already in progress")

        if payment is None or payment.status is not PaymentStatus.APPROVED:
            payment_status = payment.status.value if payment else None
            # The audit must OUTLIVE the refusal: recording it in this
            # transaction and then raising rolled it back with
            # everything else, so the security log of a blocked
            # delivery never existed. Write it on its own committed
            # transaction, then raise.
            async with session_factory() as audit_session, audit_session.begin():
                await audit_service.record(
                    audit_session,
                    actor_type=AuditActor.SYSTEM,
                    actor_id=None,
                    action="fulfillment.blocked_unapproved_payment",
                    entity_type="order",
                    entity_id=order_id,
                    data={"payment_status": payment_status},
                )
            raise PaymentGateError(
                f"order {order_id}: payment is not APPROVED; delivery refused"
            )

        try:
            # Idempotent: returns the pair already bound to this order, if any.
            item = await inventory_service.reserve_for_order(session, order=order)
        except inventory_service.NoInventoryAvailable:
            order.status = OrderStatus.FULFILLMENT_PENDING
            order.delivery_status = DeliveryStatus.PENDING
            await audit_service.record(
                session,
                actor_type=AuditActor.SYSTEM,
                actor_id=None,
                action="fulfillment.no_inventory",
                entity_type="order",
                entity_id=order_id,
            )
            from vpnbot.services import notify

            await notify.enqueue_admins(
                session,
                text=(
                    f"⚠️ موجودی انبار برای سفارش #{order.public_id} تمام شده است؛ "
                    "سفارش پرداخت‌شده در انتظار شارژ موجودی است."
                ),
                idempotency_key=f"notify:order:{order.id}:no-inventory",
            )
            return FulfillResult(FulfillOutcome.NO_INVENTORY, order_id, "inventory exhausted")

        order.status = OrderStatus.FULFILLMENT_PENDING
        order.delivery_status = DeliveryStatus.SENDING

        telegram_id = (
            await session.execute(select(User.telegram_id).where(User.id == order.user_id))
        ).scalar_one()
        pair = await inventory_service.reveal_pair(session, cipher=cipher, item_id=item.id)
        return (
            int(telegram_id),
            pair,
            order.product_title_snapshot,
            order.public_id,
            item.id,
        )


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: int,
    item_id: int,
    exc: Exception,
) -> None:
    async with session_factory() as session, session.begin():
        order = await order_service.get_order(session, order_id, for_update=True)
        if order is None:
            return
        if order.status is not OrderStatus.FULFILLED:
            order.delivery_status = DeliveryStatus.RETRY_REQUIRED
        await inventory_service.log_attempt(
            session,
            order_id=order_id,
            item_id=item_id,
            outcome="FAILED",
            error_code=type(exc).__name__,
        )
        await audit_service.record(
            session,
            actor_type=AuditActor.SYSTEM,
            actor_id=None,
            action="delivery.failed",
            entity_type="order",
            entity_id=order_id,
            data={"error": type(exc).__name__},
        )


async def _finalize(
    session_factory: async_sessionmaker[AsyncSession], *, order_id: int, item_id: int
) -> None:
    async with session_factory() as session, session.begin():
        order = await order_service.get_order(session, order_id, for_update=True)
        if order is None:
            raise PaymentGateError(f"order {order_id} disappeared during delivery")
        if order.status is OrderStatus.CANCELLED:
            # Cancelled between the gate and the send (cancel paths
            # refuse during SENDING / after attempts, so this is
            # defence in depth): never resurrect it as FULFILLED.
            return

        await inventory_service.mark_delivered(session, order_id=order_id, item_id=item_id)
        order.status = OrderStatus.FULFILLED
        order.delivery_status = DeliveryStatus.DELIVERED
        order.fulfilled_at = order.fulfilled_at or utcnow()
        await inventory_service.log_attempt(
            session, order_id=order_id, item_id=item_id, outcome="SENT"
        )
        await referral_service.grant_for_paid_order(session, order)
        await audit_service.record(
            session,
            actor_type=AuditActor.SYSTEM,
            actor_id=None,
            action="delivery.sent",
            entity_type="order",
            entity_id=order_id,
            data={"inventory_item_id": item_id},
        )

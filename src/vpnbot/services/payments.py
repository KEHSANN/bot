"""Manual payment workflows: evidence submission and admin review.

The two supported methods are card-to-card (CARD) and crypto (CRYPTO). Nothing
here ever contacts a gateway or blockchain. Approval is a human decision and is
the ONLY event that releases fulfilment; card evidence or a syntactically valid
TXID never approve themselves.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.base import utcnow
from vpnbot.db.enums import (
    AuditActor,
    DeliveryStatus,
    JobKind,
    JobStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from vpnbot.db.models.orders import Order, Payment, PaymentReview
from vpnbot.db.models.system import Job
from vpnbot.services import audit as audit_service
from vpnbot.services.settings import CARD_SETTINGS, CRYPTO_SETTINGS, SettingsService


class PaymentError(ValueError):
    pass


async def start_payment(
    session: AsyncSession, *, order: Order, method: PaymentMethod
) -> Payment:
    settings = SettingsService(session)
    key = CARD_SETTINGS if method is PaymentMethod.CARD else CRYPTO_SETTINGS
    details = await settings.get(key)
    if not details.get("enabled"):
        raise PaymentError("این روش پرداخت در حال حاضر فعال نیست")
    if method is PaymentMethod.CARD and not details.get("card_number"):
        raise PaymentError("اطلاعات کارت تنظیم نشده است")
    if method is PaymentMethod.CRYPTO and not details.get("address"):
        raise PaymentError("اطلاعات کیف پول تنظیم نشده است")

    # Never touch ``order.payment`` here: a freshly flushed order does not have
    # the relationship loaded, and a lazy load inside async code raises
    # MissingGreenlet. Query the row explicitly instead.
    payment = (
        await session.execute(select(Payment).where(Payment.order_id == order.id))
    ).scalar_one_or_none()
    if payment is None:
        payment = Payment(order_id=order.id, amount_irt=order.total_irt, method=method)
        session.add(payment)
    else:
        if payment.status in (PaymentStatus.APPROVED, PaymentStatus.PENDING_REVIEW):
            # Never reset a payment under review: the customer already sent
            # evidence and an admin may be looking at it right now.
            raise PaymentError("این سفارش در حال بررسی یا تأیید است")
        payment.method = method
        payment.status = PaymentStatus.AWAITING_PAYMENT_DETAILS
        payment.submitted_at = None
        payment.rejection_reason = None
    payment.instructions_snapshot = details
    payment.expires_at = utcnow() + timedelta(hours=48)
    await session.flush()
    return payment


async def submit_card_receipt(
    session: AsyncSession, *, payment_id: int, user_id: int, file_id: str, file_type: str
) -> Payment:
    payment = await _payment_for_user(session, payment_id, user_id)
    if payment.method is not PaymentMethod.CARD:
        raise PaymentError("این پرداخت از نوع کارت‌به‌کارت نیست")
    if payment.status not in (
        PaymentStatus.AWAITING_PAYMENT_DETAILS,
        PaymentStatus.REJECTED,
    ):
        raise PaymentError("امکان ارسال مجدد رسید در این وضعیت وجود ندارد")
    payment.receipt_file_id = file_id
    payment.receipt_file_type = file_type.upper() if file_type else file_type
    payment.status = PaymentStatus.PENDING_REVIEW
    payment.submitted_at = utcnow()
    payment.rejection_reason = None
    await session.flush()
    return payment


async def submit_crypto_evidence(
    session: AsyncSession,
    *,
    payment_id: int,
    user_id: int,
    txid: str,
    file_id: str | None = None,
    file_type: str | None = None,
) -> Payment:
    payment = await _payment_for_user(session, payment_id, user_id)
    if payment.method is not PaymentMethod.CRYPTO:
        raise PaymentError("این پرداخت از نوع کریپتو نیست")
    if payment.status not in (
        PaymentStatus.AWAITING_PAYMENT_DETAILS,
        PaymentStatus.REJECTED,
    ):
        raise PaymentError("امکان ارسال مجدد شواهد در این وضعیت وجود ندارد")

    normalized = normalize_txid(txid)
    if len(normalized) < 8 or len(normalized) > 200:
        raise PaymentError("هش تراکنش نامعتبر است")
    # Case-insensitive: the same hash typed with different casing must not be
    # accepted twice.
    duplicate = (
        await session.execute(
            select(Payment.id).where(
                func.lower(Payment.txid) == normalized.lower(), Payment.id != payment.id
            )
        )
    ).first()
    if duplicate is not None:
        raise PaymentError("این هش تراکنش قبلاً ثبت شده است")

    payment.txid = normalized
    payment.receipt_file_id = file_id
    payment.receipt_file_type = file_type.upper() if file_type else file_type
    payment.status = PaymentStatus.PENDING_REVIEW
    payment.submitted_at = utcnow()
    payment.rejection_reason = None
    payment.expires_at = utcnow() + timedelta(hours=48)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        # Savepoint only: the surrounding transaction (and the rest of the
        # admin/foreground work) stays usable.
        raise PaymentError("این هش تراکنش قبلاً ثبت شده است") from exc
    return payment


def normalize_txid(raw: str) -> str:
    value = (raw or "").strip()
    if value.lower().startswith("0x"):
        return "0x" + value[2:].lower()
    return value


async def _payment_for_user(session: AsyncSession, payment_id: int, user_id: int) -> Payment:
    payment = (
        await session.execute(
            select(Payment).join(Order, Order.id == Payment.order_id).where(Payment.id == payment_id)
        )
    ).scalar_one_or_none()
    if payment is None:
        raise PaymentError("پرداخت یافت نشد")
    order = await session.get(Order, payment.order_id)
    if order is None or order.user_id != user_id:
        raise PaymentError("پرداخت یافت نشد")
    return payment


async def approve_payment(
    session: AsyncSession, *, payment_id: int, admin_id: int, note: str | None = None
) -> Payment:
    """Idempotent, concurrency-safe approval that queues fulfilment.

    * Locks the payment row; a second concurrent approval blocks then observes
      APPROVED and returns without side effects.
    * Writes an immutable PaymentReview + audit event.
    * Enqueues exactly one FULFILL_ORDER job (unique idempotency key).
    """
    payment = (
        await session.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if payment is None:
        raise PaymentError("پرداخت یافت نشد")
    if payment.status is PaymentStatus.APPROVED:
        return payment  # idempotent success
    if payment.status is not PaymentStatus.PENDING_REVIEW:
        raise PaymentError("فقط پرداخت‌های در انتظار بررسی قابل تأیید هستند")

    order = (
        await session.execute(select(Order).where(Order.id == payment.order_id).with_for_update())
    ).scalar_one()
    if order.status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAID):
        raise PaymentError("وضعیت سفارش اجازه تأیید پرداخت را نمی‌دهد")

    now = utcnow()
    payment.status = PaymentStatus.APPROVED
    payment.reviewed_at = now
    payment.reviewed_by_admin_id = admin_id
    payment.rejection_reason = None
    order.status = OrderStatus.FULFILLMENT_PENDING
    order.paid_at = order.paid_at or now
    order.delivery_status = DeliveryStatus.PENDING

    session.add(PaymentReview(payment_id=payment.id, admin_id=admin_id, decision="APPROVED", reason=note))
    await audit_service.record(
        session,
        actor_type=AuditActor.ADMIN,
        actor_id=admin_id,
        action="payment.approved",
        entity_type="payment",
        entity_id=payment.id,
        data={
            "order_id": order.id,
            "amount_irt": payment.amount_irt,
            "method": payment.method.value,
            "txid": payment.txid,
        },
    )
    await session.execute(
        pg_insert(Job)
        .values(
            kind=JobKind.FULFILL_ORDER,
            payload={"order_id": order.id},
            idempotency_key=f"fulfill:{order.id}",
            status=JobStatus.QUEUED,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    await session.flush()
    return payment


async def reject_payment(
    session: AsyncSession, *, payment_id: int, admin_id: int, reason: str
) -> Payment:
    if not reason.strip():
        raise PaymentError("دلیل رد پرداخت الزامی است")
    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    ).scalar_one_or_none()
    if payment is None:
        raise PaymentError("پرداخت یافت نشد")
    if payment.status is PaymentStatus.REJECTED:
        return payment
    if payment.status not in (PaymentStatus.PENDING_REVIEW, PaymentStatus.AWAITING_PAYMENT_DETAILS):
        raise PaymentError("این پرداخت در وضعیت قابل رد نیست")

    payment.status = PaymentStatus.REJECTED
    payment.reviewed_at = utcnow()
    payment.reviewed_by_admin_id = admin_id
    payment.rejection_reason = reason[:500]
    session.add(
        PaymentReview(payment_id=payment.id, admin_id=admin_id, decision="REJECTED", reason=reason[:500])
    )
    await audit_service.record(
        session,
        actor_type=AuditActor.ADMIN,
        actor_id=admin_id,
        action="payment.rejected",
        entity_type="payment",
        entity_id=payment.id,
        data={"reason": reason[:500]},
    )
    await session.flush()
    return payment


async def expire_stale_payments(session: AsyncSession) -> int:
    now = utcnow()
    rows = (
        await session.execute(
            select(Payment)
            .where(
                Payment.status.in_(
                    [PaymentStatus.AWAITING_PAYMENT_DETAILS, PaymentStatus.PENDING_REVIEW]
                ),
                Payment.expires_at.is_not(None),
                Payment.expires_at < now,
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars()
    count = 0
    for payment in rows:
        payment.status = PaymentStatus.EXPIRED
        count += 1
    await session.flush()
    return count


async def pending_reviews(session: AsyncSession, *, offset: int = 0, limit: int = 10) -> list[Payment]:
    result = await session.execute(
        select(Payment)
        .where(Payment.status == PaymentStatus.PENDING_REVIEW)
        .order_by(Payment.submitted_at.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars())


async def count_pending_reviews(session: AsyncSession) -> int:
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                select(func.count(Payment.id)).where(Payment.status == PaymentStatus.PENDING_REVIEW)
            )
        ).scalar_one()
    )

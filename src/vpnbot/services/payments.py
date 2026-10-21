"""Manual payment workflows: evidence submission and admin review.

The two supported methods are card-to-card (CARD) and crypto (CRYPTO). Nothing
here ever contacts a gateway or blockchain. Approval is a human decision and is
the ONLY event that releases fulfilment; card evidence or a syntactically valid
TXID never approve themselves.
"""

from __future__ import annotations

import re
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
    LedgerKind,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from vpnbot.db.models.orders import Order, Payment, PaymentReview
from vpnbot.db.models.system import Job
from vpnbot.services import audit as audit_service
from vpnbot.services import notify
from vpnbot.services import wallet as wallet_service
from vpnbot.services.settings import (
    CARD_SETTINGS,
    CRYPTO_SETTINGS,
    SHOP_SETTINGS,
    SettingsService,
)


class PaymentError(ValueError):
    pass


def _review_ttl() -> timedelta:
    """Payment evidence window — from configuration, not hardcoded.

    ``PAYMENT_REVIEW_TTL_HOURS`` has been in .env/config since the
    beginning but the services ignored it and always used 48h.
    """
    from vpnbot.config import get_settings

    return timedelta(hours=get_settings().payment_review_ttl_hours)


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
    payment.expires_at = utcnow() + _review_ttl()
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
    await notify.enqueue_admins(
        session,
        text=f"🧾 رسید پرداخت جدید برای سفارش #{payment.order_id} — در انتظار بررسی.",
        idempotency_key=f"notify:payment:{payment.id}:receipt",
    )
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
    if not valid_txid(normalized):
        raise PaymentError("هش تراکنش نامعتبر است")
    # One transfer funds one thing: check top-ups as well as payments —
    # this path used to look at payments only, so a TXID already used
    # for a wallet top-up could be submitted again for an order.
    if await txid_taken(session, normalized, exclude_payment_id=payment.id):
        raise PaymentError("این هش تراکنش قبلاً ثبت شده است")

    payment.txid = normalized
    payment.receipt_file_id = file_id
    payment.receipt_file_type = file_type.upper() if file_type else file_type
    payment.status = PaymentStatus.PENDING_REVIEW
    payment.submitted_at = utcnow()
    payment.rejection_reason = None
    payment.expires_at = utcnow() + _review_ttl()
    await notify.enqueue_admins(
        session,
        text=f"🧾 هش تراکنش جدید برای سفارش #{payment.order_id} — در انتظار بررسی.",
        idempotency_key=f"notify:payment:{payment.id}:receipt",
    )
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        # Savepoint only: the surrounding transaction (and the rest of the
        # admin/foreground work) stays usable.
        raise PaymentError("این هش تراکنش قبلاً ثبت شده است") from exc
    return payment


def normalize_txid(raw: str) -> str:
    """Canonical TXID form: stripped and fully lowercased.

    TRC20 hashes (the default network) are 64 hex chars *without* a 0x
    prefix, so the old "lowercase only after 0x" rule stored ``ABC…`` and
    ``abc…`` as different values — the case-sensitive DB unique index
    then let the same transfer be submitted twice concurrently. Now the
    stored value is canonical, so the unique index itself enforces
    case-insensitive uniqueness.
    """
    return (raw or "").strip().lower()


_TXID_RE = re.compile(r"^[0-9a-z_-]{8,200}$")


def valid_txid(normalized: str) -> bool:
    """A normalized TXID is 8-200 chars of [0-9a-z_-] — no control
    characters, no whitespace, no free-form text."""
    return bool(_TXID_RE.fullmatch(normalized or ""))


async def txid_taken(
    session: AsyncSession,
    normalized: str,
    *,
    exclude_payment_id: int | None = None,
    exclude_topup_id: int | None = None,
) -> bool:
    """Is this TXID already recorded on ANY payment or wallet top-up?

    One transfer funds exactly one thing. Both submission paths must ask
    this — checking only the payments table let a TXID used for a
    top-up be reused for an order (and under concurrency, vice versa).
    Stored values are canonical (see normalize_txid), and the comparison
    lowercases defensively for rows written before canonicalisation.
    """
    from vpnbot.db.models.wallet import WalletTopUp

    payment_q = select(Payment.id).where(func.lower(Payment.txid) == normalized)
    if exclude_payment_id is not None:
        payment_q = payment_q.where(Payment.id != exclude_payment_id)
    if (await session.execute(payment_q)).first() is not None:
        return True
    topup_q = select(WalletTopUp.id).where(func.lower(WalletTopUp.txid) == normalized)
    if exclude_topup_id is not None:
        topup_q = topup_q.where(WalletTopUp.id != exclude_topup_id)
    return (await session.execute(topup_q)).first() is not None


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


async def pay_order_with_wallet(
    session: AsyncSession, *, order: Order, user_id: int
) -> Payment:
    """Pay an order from the customer's wallet balance — no manual review.

    The money is already inside the system, so the payment is approved
    the moment the debit lands: debit (ledger idempotency key
    ``order:<id>:wallet`` makes a double tap charge once), payment row
    marked APPROVED with method WALLET, order moved to fulfilment and
    exactly one FULFILL_ORDER job enqueued — the same tail as an admin
    approval, so fulfilment and referral rewards behave identically.
    """
    if order.user_id != user_id:
        raise PaymentError("سفارش یافت نشد")
    shop = await SettingsService(session).get(SHOP_SETTINGS)
    if not shop.get("wallet_enabled", True):
        raise PaymentError("پرداخت با کیف پول در حال حاضر غیرفعال است")

    payment = (
        await session.execute(
            select(Payment).where(Payment.order_id == order.id).with_for_update()
        )
    ).scalar_one_or_none()
    if payment is not None and payment.status is PaymentStatus.APPROVED:
        return payment  # already paid (any method) — never charge twice
    if payment is not None and payment.status is PaymentStatus.PENDING_REVIEW:
        raise PaymentError("این سفارش در حال بررسی پرداخت است و نمی‌توان روش آن را عوض کرد")
    if order.status is not OrderStatus.PENDING_PAYMENT:
        raise PaymentError("این سفارش قابل پرداخت نیست")

    balance = await wallet_service.balance(session, user_id)
    if balance < order.total_irt:
        from vpnbot.money import format_irt_plain

        raise PaymentError(
            f"موجودی کیف پول کافی نیست (موجودی: {format_irt_plain(balance)} تومان، "
            f"مبلغ سفارش: {format_irt_plain(order.total_irt)} تومان)"
        )

    try:
        await wallet_service.debit(
            session,
            user_id=user_id,
            amount_irt=order.total_irt,
            kind=LedgerKind.PURCHASE,
            key=f"order:{order.id}:wallet",
            reason=f"خرید سفارش {order.public_id}",
            ref_type="order",
            ref_id=str(order.id),
        )
    except wallet_service.WalletError as exc:
        # A concurrent spend drained the balance between the check above
        # and the debit; surface it as a payment problem, not a crash.
        raise PaymentError(str(exc)) from exc

    now = utcnow()
    if payment is None:
        payment = Payment(order_id=order.id, amount_irt=order.total_irt, method=PaymentMethod.WALLET)
        session.add(payment)
    payment.method = PaymentMethod.WALLET
    payment.amount_irt = order.total_irt
    payment.status = PaymentStatus.APPROVED
    payment.instructions_snapshot = {}
    payment.submitted_at = now
    payment.reviewed_at = now
    payment.rejection_reason = None

    order.status = OrderStatus.FULFILLMENT_PENDING
    order.paid_at = order.paid_at or now
    order.delivery_status = DeliveryStatus.PENDING
    await session.flush()  # assigns payment.id for the audit row below

    await audit_service.record(
        session,
        actor_type=AuditActor.CUSTOMER,
        actor_id=user_id,
        action="payment.wallet_paid",
        entity_type="payment",
        entity_id=payment.id,
        data={"order_id": order.id, "amount_irt": order.total_irt},
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

    old_txid = payment.txid
    payment.status = PaymentStatus.REJECTED
    payment.reviewed_at = utcnow()
    payment.reviewed_by_admin_id = admin_id
    payment.rejection_reason = reason[:500]
    # Free the TXID: a rejected submission must not burn the customer's
    # real transfer forever (nor let a squatter lock someone else's
    # TXID). The value stays visible in the audit trail below.
    payment.txid = None
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
        data={"reason": reason[:500], "txid": old_txid},
    )
    # The customer was told in the panel copy that the reason is
    # recorded for them — actually tell them.
    order = await session.get(Order, payment.order_id)
    if order is not None:
        await notify.enqueue_user(
            session,
            user_id=order.user_id,
            text=(
                f"❌ پرداخت سفارش #{order.public_id} رد شد.\n"
                f"دلیل: {reason[:500]}\n"
                "می‌توانید از بخش سفارش‌ها، رسید یا هش صحیح را دوباره ارسال کنید."
            ),
            idempotency_key=f"notify:payment:{payment.id}:rejected",
        )
    await session.flush()
    return payment


async def expire_stale_payments(session: AsyncSession) -> int:
    """Expire payments whose evidence window closed with nothing sent.

    Only ``AWAITING_PAYMENT_DETAILS`` expires. A payment in
    ``PENDING_REVIEW`` means the customer already sent money/evidence —
    expiring it after 48h silently dropped paid orders from the review
    queue (the previous behaviour). Those wait for a human, however long
    that takes. Expiring also cancels the still-unpaid order and frees
    its TXID.
    """
    now = utcnow()
    rows = (
        await session.execute(
            select(Payment)
            .where(
                Payment.status == PaymentStatus.AWAITING_PAYMENT_DETAILS,
                Payment.expires_at.is_not(None),
                Payment.expires_at < now,
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars()
    count = 0
    for payment in rows:
        payment.status = PaymentStatus.EXPIRED
        payment.txid = None
        order = await session.get(Order, payment.order_id)
        if order is not None and order.status is OrderStatus.PENDING_PAYMENT:
            order.status = OrderStatus.CANCELLED
            order.cancelled_at = now
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

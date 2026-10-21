"""Wallet top-ups: a customer loads their wallet through the same manual
review as an order payment — card receipt or crypto TXID, admin approves,
the ledger is credited exactly once (``topup:<id>`` idempotency key).

The minimum amount and the on/off switch are admin settings
(``WALLET_SETTINGS``), so the shop owner controls them from the panel.
"""

from __future__ import annotations

import html

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.base import utcnow
from vpnbot.db.enums import AuditActor, LedgerKind, PaymentMethod, PaymentStatus
from vpnbot.db.models.wallet import WalletTopUp
from vpnbot.services import audit as audit_service
from vpnbot.services import notify
from vpnbot.services import wallet as wallet_service
from vpnbot.services.payments import _review_ttl, normalize_txid, txid_taken, valid_txid
from vpnbot.services.settings import (
    CARD_SETTINGS,
    CRYPTO_SETTINGS,
    WALLET_SETTINGS,
    SettingsService,
)


class TopUpError(ValueError):
    pass


async def min_topup_irt(session: AsyncSession) -> int:
    settings = await SettingsService(session).get(WALLET_SETTINGS)
    return max(0, int(settings.get("min_topup_irt", 0)))


async def topup_enabled(session: AsyncSession) -> bool:
    settings = await SettingsService(session).get(WALLET_SETTINGS)
    return bool(settings.get("topup_enabled", True))


async def create_topup(
    session: AsyncSession, *, user_id: int, amount_irt: int, method: PaymentMethod
) -> WalletTopUp:
    if not await topup_enabled(session):
        raise TopUpError("شارژ کیف پول در حال حاضر غیرفعال است")
    minimum = await min_topup_irt(session)
    if amount_irt < minimum:
        from vpnbot.money import format_irt_plain

        raise TopUpError(f"حداقل مبلغ شارژ {format_irt_plain(minimum)} تومان است")
    if amount_irt <= 0:
        raise TopUpError("مبلغ معتبر نیست")
    # Abandoned top-ups (amount chosen, method tapped, then /cancel or
    # a menu detour) used to sit in AWAITING_PAYMENT_DETAILS until the
    # review TTL and — counting toward the open cap below — locked the
    # customer out of topping up at all. A fresh request supersedes
    # them: evidence was never submitted for an AWAITING top-up, so
    # cancelling it loses nothing. PENDING_REVIEW ones are untouched
    # (the admin owes those a decision).
    stale = (
        await session.execute(
            select(WalletTopUp).where(
                WalletTopUp.user_id == user_id,
                WalletTopUp.status == PaymentStatus.AWAITING_PAYMENT_DETAILS,
            )
        )
    ).scalars()
    for old in stale:
        old.status = PaymentStatus.CANCELLED
    await session.flush()
    # Anti-spam cap, mirroring orders: unlimited open top-ups would let
    # one account flood the review queue and the admins' chats.
    open_topups = int(
        (
            await session.execute(
                select(func.count(WalletTopUp.id)).where(
                    WalletTopUp.user_id == user_id,
                    WalletTopUp.status.in_(
                        [PaymentStatus.AWAITING_PAYMENT_DETAILS, PaymentStatus.PENDING_REVIEW]
                    ),
                )
            )
        ).scalar_one()
    )
    if open_topups >= 3:
        raise TopUpError("درخواست شارژ باز دارید؛ منتظر بررسی آن بمانید")

    settings = SettingsService(session)
    key = CARD_SETTINGS if method is PaymentMethod.CARD else CRYPTO_SETTINGS
    details = await settings.get(key)
    if not details.get("enabled"):
        raise TopUpError("این روش پرداخت در حال حاضر فعال نیست")
    if method is PaymentMethod.CARD and not details.get("card_number"):
        raise TopUpError("اطلاعات کارت تنظیم نشده است")
    if method is PaymentMethod.CRYPTO and not details.get("address"):
        raise TopUpError("اطلاعات کیف پول تنظیم نشده است")

    topup = WalletTopUp(
        user_id=user_id,
        amount_irt=amount_irt,
        method=method,
        instructions_snapshot=details,
        expires_at=utcnow() + _review_ttl(),
    )
    session.add(topup)
    await session.flush()
    return topup


async def _topup_for_user(session: AsyncSession, topup_id: int, user_id: int) -> WalletTopUp:
    topup = await session.get(WalletTopUp, topup_id)
    if topup is None or topup.user_id != user_id:
        raise TopUpError("درخواست شارژ یافت نشد")
    return topup


def _require_submittable(topup: WalletTopUp) -> None:
    if topup.status not in (PaymentStatus.AWAITING_PAYMENT_DETAILS, PaymentStatus.REJECTED):
        raise TopUpError("امکان ارسال مجدد رسید در این وضعیت وجود ندارد")


async def submit_card_receipt(
    session: AsyncSession, *, topup_id: int, user_id: int, file_id: str, file_type: str
) -> WalletTopUp:
    topup = await _topup_for_user(session, topup_id, user_id)
    if topup.method is not PaymentMethod.CARD:
        raise TopUpError("این شارژ از نوع کارت‌به‌کارت نیست")
    _require_submittable(topup)
    topup.receipt_file_id = file_id
    topup.receipt_file_type = file_type.upper() if file_type else file_type
    topup.status = PaymentStatus.PENDING_REVIEW
    topup.submitted_at = utcnow()
    topup.rejection_reason = None
    await notify.enqueue_admins(
        session,
        text=f"🧾 رسید شارژ کیف پول جدید (#{topup.id}) — در انتظار بررسی.",
    )
    await session.flush()
    return topup


async def submit_crypto_evidence(
    session: AsyncSession,
    *,
    topup_id: int,
    user_id: int,
    txid: str,
    file_id: str | None = None,
    file_type: str | None = None,
) -> WalletTopUp:
    topup = await _topup_for_user(session, topup_id, user_id)
    if topup.method is not PaymentMethod.CRYPTO:
        raise TopUpError("این شارژ از نوع رمزارز نیست")
    _require_submittable(topup)

    normalized = normalize_txid(txid)
    if not valid_txid(normalized):
        raise TopUpError("هش تراکنش نامعتبر است")
    # A TXID is one transfer: it must not fund two top-ups or an order too.
    if await txid_taken(session, normalized, exclude_topup_id=topup.id):
        raise TopUpError("این هش تراکنش قبلاً ثبت شده است")

    topup.txid = normalized
    topup.receipt_file_id = file_id
    topup.receipt_file_type = file_type.upper() if file_type else file_type
    topup.status = PaymentStatus.PENDING_REVIEW
    topup.submitted_at = utcnow()
    topup.rejection_reason = None
    topup.expires_at = utcnow() + _review_ttl()
    await notify.enqueue_admins(
        session,
        text=f"🧾 هش تراکنش شارژ کیف پول جدید (#{topup.id}) — در انتظار بررسی.",
    )
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise TopUpError("این هش تراکنش قبلاً ثبت شده است") from exc
    return topup


async def approve_topup(
    session: AsyncSession, *, topup_id: int, admin_id: int, note: str | None = None
) -> WalletTopUp:
    """Idempotent approval: credits the wallet exactly once."""
    topup = (
        await session.execute(select(WalletTopUp).where(WalletTopUp.id == topup_id).with_for_update())
    ).scalar_one_or_none()
    if topup is None:
        raise TopUpError("درخواست شارژ یافت نشد")
    # Self-approval guard (mirrors payments): a non-owner admin must
    # not approve their own top-up and mint themselves balance.
    from vpnbot.db.models.users import Admin

    admin = await session.get(Admin, admin_id)
    if (
        admin is not None
        and admin.user_id == topup.user_id
        and not (admin.role is not None and admin.role.key == "owner")
    ):
        raise TopUpError("ادمین نمی‌تواند تراکنش خودش را تأیید کند")
    if topup.status is PaymentStatus.APPROVED:
        return topup  # idempotent success
    if topup.status is not PaymentStatus.PENDING_REVIEW:
        raise TopUpError("فقط شارژهای در انتظار بررسی قابل تأیید هستند")

    topup.status = PaymentStatus.APPROVED
    topup.reviewed_at = utcnow()
    topup.reviewed_by_admin_id = admin_id
    topup.rejection_reason = None
    await wallet_service.credit(
        session,
        user_id=topup.user_id,
        amount_irt=topup.amount_irt,
        kind=LedgerKind.TOPUP,
        key=f"topup:{topup.id}",
        reason="شارژ کیف پول",
        ref_type="topup",
        ref_id=str(topup.id),
    )
    await audit_service.record(
        session,
        actor_type=AuditActor.ADMIN,
        actor_id=admin_id,
        action="wallet.topup_approved",
        entity_type="topup",
        entity_id=topup.id,
        data={"user_id": topup.user_id, "amount_irt": topup.amount_irt, "note": note},
    )
    await session.flush()
    return topup


async def reject_topup(
    session: AsyncSession, *, topup_id: int, admin_id: int, reason: str
) -> WalletTopUp:
    if not reason.strip():
        raise TopUpError("دلیل رد الزامی است")
    topup = (
        await session.execute(select(WalletTopUp).where(WalletTopUp.id == topup_id).with_for_update())
    ).scalar_one_or_none()
    if topup is None:
        raise TopUpError("درخواست شارژ یافت نشد")
    if topup.status is PaymentStatus.REJECTED:
        return topup
    if topup.status not in (PaymentStatus.PENDING_REVIEW, PaymentStatus.AWAITING_PAYMENT_DETAILS):
        raise TopUpError("این شارژ در وضعیت قابل رد نیست")

    old_txid = topup.txid
    topup.status = PaymentStatus.REJECTED
    topup.reviewed_at = utcnow()
    topup.reviewed_by_admin_id = admin_id
    topup.rejection_reason = reason[:500]
    topup.txid = None  # freed for a legitimate resubmission; kept in audit
    await audit_service.record(
        session,
        actor_type=AuditActor.ADMIN,
        actor_id=admin_id,
        action="wallet.topup_rejected",
        entity_type="topup",
        entity_id=topup.id,
        data={"reason": reason[:500], "txid": old_txid},
    )
    await notify.enqueue_user(
        session,
        user_id=topup.user_id,
        text=(
            "❌ درخواست شارژ کیف پول شما رد شد.\n"
            f"دلیل: {html.escape(reason[:500])}\n"
            "می‌توانید از بخش کیف پول، شارژ جدیدی با رسید صحیح ثبت کنید."
        ),
    )
    await session.flush()
    return topup


async def pending_topups(session: AsyncSession, *, offset: int = 0, limit: int = 10) -> list[WalletTopUp]:
    result = await session.execute(
        select(WalletTopUp)
        .where(WalletTopUp.status == PaymentStatus.PENDING_REVIEW)
        .order_by(WalletTopUp.submitted_at.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars())


async def count_pending_topups(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count(WalletTopUp.id)).where(
                    WalletTopUp.status == PaymentStatus.PENDING_REVIEW
                )
            )
        ).scalar_one()
    )


async def expire_stale_topups(session: AsyncSession) -> int:
    """Expire top-ups that never received evidence (mirrors payments).

    Only ``AWAITING_PAYMENT_DETAILS`` expires; a submitted top-up in
    ``PENDING_REVIEW`` waits for a human indefinitely. The TXID (if a
    method switch ever recorded one) is freed.
    """
    now = utcnow()
    rows = (
        await session.execute(
            select(WalletTopUp)
            .where(
                WalletTopUp.status == PaymentStatus.AWAITING_PAYMENT_DETAILS,
                WalletTopUp.expires_at.is_not(None),
                WalletTopUp.expires_at < now,
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars()
    count = 0
    for topup in rows:
        topup.status = PaymentStatus.EXPIRED
        topup.txid = None
        count += 1
    await session.flush()
    return count

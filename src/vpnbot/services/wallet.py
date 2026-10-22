"""Wallet + append-only ledger.

* Amounts are integer Toman.
* Every balance change is a ledger row with a unique ``idempotency_key``; the
  balance update and the row insert happen in the same transaction.
* Duplicate credits/debits are impossible: unique violation -> returns the
  original entry instead of applying twice.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.base import utcnow
from vpnbot.db.enums import LedgerKind
from vpnbot.db.models.wallet import LedgerEntry, WalletAccount


class WalletError(ValueError):
    pass


@dataclass(frozen=True)
class LedgerResult:
    entry: LedgerEntry
    balance_irt: int
    created: bool


async def get_or_create_account(session: AsyncSession, user_id: int) -> WalletAccount:
    account = (
        await session.execute(select(WalletAccount).where(WalletAccount.user_id == user_id))
    ).scalar_one_or_none()
    if account is None:
        account = WalletAccount(user_id=user_id, balance_irt=0)
        session.add(account)
        try:
            # Savepoint: a full rollback here would amputate the caller's
            # transaction (see referrals._issue for the fulfilment case).
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            account = (
                await session.execute(select(WalletAccount).where(WalletAccount.user_id == user_id))
            ).scalar_one()
    return account


async def _existing(session: AsyncSession, key: str) -> LedgerEntry | None:
    return (
        await session.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key == key))
    ).scalar_one_or_none()


async def apply(
    session: AsyncSession,
    *,
    user_id: int,
    amount_irt: int,
    kind: LedgerKind,
    idempotency_key: str,
    reason: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    actor_type: str = "SYSTEM",
    actor_id: int | None = None,
    allow_negative: bool = False,
) -> LedgerResult:
    if isinstance(amount_irt, bool) or not isinstance(amount_irt, int) or amount_irt == 0:
        raise WalletError("مبلغ تراکنش باید عدد صحیح غیر صفر باشد")
    if amount_irt < 0 and not allow_negative:
        raise WalletError("برای برداشت از متد کسر استفاده کنید")
    if not idempotency_key:
        raise WalletError("کلید یکتای تراکنش الزامی است")

    prior = await _existing(session, idempotency_key)
    if prior is not None:
        return LedgerResult(entry=prior, balance_irt=prior.balance_after_irt, created=False)

    account = await get_or_create_account(session, user_id)
    locked = (
        await session.execute(
            select(WalletAccount)
            .where(WalletAccount.id == account.id)
            .with_for_update()
            # populate_existing is load-bearing: get_or_create_account
            # already cached this row in the identity map, and without
            # it the FOR UPDATE query waits for the row lock but
            # returns the STALE cached balance — three concurrent
            # wallet payments could each pass the balance check
            # against the same funds (double-spend).
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    new_balance = locked.balance_irt + amount_irt
    if new_balance < 0:
        raise WalletError("موجودی کیف پول کافی نیست")

    entry = LedgerEntry(
        wallet_id=locked.id,
        user_id=user_id,
        kind=kind,
        amount_irt=amount_irt,
        balance_after_irt=new_balance,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
    )
    try:
        # The balance UPDATE and the ledger insert share one savepoint: on
        # a duplicate-key race both must roll back together, but the
        # caller's outer transaction must survive (a full rollback here
        # used to undo unrelated work, e.g. a completed fulfilment).
        async with session.begin_nested():
            await session.execute(
                update(WalletAccount)
                .where(WalletAccount.id == locked.id)
                .values(balance_irt=new_balance, updated_at=utcnow())
            )
            session.add(entry)
            await session.flush()
    except IntegrityError:
        prior = await _existing(session, idempotency_key)
        if prior is None:
            raise
        return LedgerResult(entry=prior, balance_irt=prior.balance_after_irt, created=False)
    return LedgerResult(entry=entry, balance_irt=new_balance, created=True)


async def credit(
    session: AsyncSession, *, user_id: int, amount_irt: int, kind: LedgerKind, key: str, **kw: object
) -> LedgerResult:
    if amount_irt <= 0:
        raise WalletError("مبلغ واریز باید مثبت باشد")
    return await apply(session, user_id=user_id, amount_irt=amount_irt, kind=kind, idempotency_key=key, **kw)  # type: ignore[arg-type]


async def debit(
    session: AsyncSession, *, user_id: int, amount_irt: int, kind: LedgerKind, key: str, **kw: object
) -> LedgerResult:
    if amount_irt <= 0:
        raise WalletError("مبلغ برداشت باید مثبت باشد")
    # allow_negative must be passed explicitly: without it apply() rejects
    # every debit ("use the debit method") — debit() IS that method, so it
    # could never succeed. The balance floor is still enforced inside
    # apply() via the new_balance < 0 check.
    return await apply(
        session,
        user_id=user_id,
        amount_irt=-amount_irt,
        kind=kind,
        idempotency_key=key,
        allow_negative=True,
        **kw,  # type: ignore[arg-type]
    )


async def balance(session: AsyncSession, user_id: int) -> int:
    account = (
        await session.execute(select(WalletAccount).where(WalletAccount.user_id == user_id))
    ).scalar_one_or_none()
    return account.balance_irt if account else 0


async def history(session: AsyncSession, user_id: int, limit: int = 10) -> list[LedgerEntry]:
    result = await session.execute(
        select(LedgerEntry)
        .where(LedgerEntry.user_id == user_id)
        .order_by(LedgerEntry.id.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def reconcile(session: AsyncSession, user_id: int) -> tuple[int, int]:
    """Return (stored_balance, sum_of_ledger_amounts) for verification."""
    account = (
        await session.execute(select(WalletAccount).where(WalletAccount.user_id == user_id))
    ).scalar_one_or_none()
    from sqlalchemy import func

    ledger_sum = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(LedgerEntry.amount_irt), 0)).where(
                    LedgerEntry.user_id == user_id
                )
            )
        ).scalar_one()
    )
    return (account.balance_irt if account else 0), ledger_sum

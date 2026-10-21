"""Referral rewards — issued only on an APPROVED qualifying purchase.

Tier 1: reward the inviter once, at the first approved purchase of the invitee.
Tier 2: an extra reward when the inviter's total qualified invitees crosses the
configured threshold and the tier is enabled. Tier 2 is granted at most once,
to the inviter whose Nth qualified invitee triggers it (admin-controlled).

Idempotency:
  * ``uq_reward_referred_tier`` – one grant per invitee per tier.
  * wallet ledger ``idempotency_key`` – never credits twice.
  * Referral attributes are set once and never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.enums import LedgerKind, OrderStatus, PaymentStatus, RewardStatus
from vpnbot.db.models.orders import Order
from vpnbot.db.models.referral import ReferralReward
from vpnbot.db.models.users import User
from vpnbot.services import wallet as wallet_service
from vpnbot.services.settings import REFERRAL_SETTINGS, SettingsService


@dataclass
class RewardGrant:
    inviter_id: int
    tier: int
    amount_irt: int


async def grant_for_paid_order(session: AsyncSession, order: Order) -> list[RewardGrant]:
    """Called after payment approval, in the same transaction."""
    settings = await SettingsService(session).get(REFERRAL_SETTINGS)
    if not settings.get("enabled", True):
        return []
    from vpnbot.services.settings import SHOP_SETTINGS

    shop = await SettingsService(session).get(SHOP_SETTINGS)
    if not shop.get("referral_enabled", True):
        return []
    if order.status not in (OrderStatus.FULFILLMENT_PENDING, OrderStatus.PAID, OrderStatus.FULFILLED):
        return []

    customer = await session.get(User, order.user_id)
    if customer is None or customer.referred_by_id is None:
        return []
    inviter_id = customer.referred_by_id
    if inviter_id == customer.id:
        return []  # defensive: self referral is rejected at attribution

    grants: list[RewardGrant] = []
    tier1_amount = int(settings.get("tier1_reward_irt", 0))
    if tier1_amount > 0 and order.total_irt < tier1_amount:
        # The purchase must at least cover the reward it generates;
        # otherwise alt accounts could buy the cheapest plan, harvest
        # a reward worth more than the plan, and farm free balance.
        return []
    tier1 = await _issue(
        session,
        inviter_id=inviter_id,
        referred_user_id=customer.id,
        order_id=order.id,
        tier=1,
        amount=tier1_amount,
    )
    if tier1 is not None:
        grants.append(tier1)

    if settings.get("tier2_enabled") and int(settings.get("tier2_reward_irt", 0)) > 0:
        threshold = max(1, int(settings.get("tier2_threshold", 5)))
        qualified = await _qualified_invitees(session, inviter_id)
        already = await session.execute(
            select(func.count(ReferralReward.id)).where(
                ReferralReward.inviter_id == inviter_id, ReferralReward.tier == 2
            )
        )
        if int(already.scalar_one()) == 0 and qualified >= threshold:
            tier2 = await _issue(
                session,
                inviter_id=inviter_id,
                referred_user_id=customer.id,
                order_id=order.id,
                tier=2,
                amount=int(settings.get("tier2_reward_irt", 0)),
            )
            if tier2 is not None:
                grants.append(tier2)
    return grants


async def _qualified_invitees(session: AsyncSession, inviter_id: int) -> int:
    result = await session.execute(
        select(func.count(ReferralReward.referred_user_id)).where(
            ReferralReward.inviter_id == inviter_id, ReferralReward.tier == 1
        )
    )
    return int(result.scalar_one())


async def _issue(
    session: AsyncSession,
    *,
    inviter_id: int,
    referred_user_id: int,
    order_id: int,
    tier: int,
    amount: int,
) -> RewardGrant | None:
    if amount <= 0:
        return None
    reward = ReferralReward(
        inviter_id=inviter_id,
        referred_user_id=referred_user_id,
        qualifying_order_id=order_id,
        tier=tier,
        amount_irt=amount,
        status=RewardStatus.ISSUED,
    )
    session.add(reward)
    try:
        # Savepoint, NOT session.rollback(): this runs inside the caller's
        # transaction (fulfilment has already marked the order delivered).
        # A full rollback on a duplicate grant undid the delivery writes
        # while the config had already been sent to the customer.
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return None  # already granted for this invitee/tier
    result = await wallet_service.credit(
        session,
        user_id=inviter_id,
        amount_irt=amount,
        kind=LedgerKind.REFERRAL_REWARD,
        key=f"referral:{referred_user_id}:tier{tier}",
        reason=f"پاداش دعوت سطح {tier}",
        ref_type="order",
        ref_id=str(order_id),
    )
    reward.ledger_entry_id = result.entry.id
    await session.flush()
    return RewardGrant(inviter_id=inviter_id, tier=tier, amount_irt=amount)


async def referral_stats(session: AsyncSession, user_id: int) -> dict[str, int]:
    invited = int(
        (
            await session.execute(select(func.count(User.id)).where(User.referred_by_id == user_id))
        ).scalar_one()
    )
    rewards = (
        await session.execute(
            select(func.count(ReferralReward.id), func.coalesce(func.sum(ReferralReward.amount_irt), 0))
            .where(ReferralReward.inviter_id == user_id, ReferralReward.status == RewardStatus.ISSUED)
        )
    ).one()
    return {"invited": invited, "reward_count": int(rewards[0]), "reward_total_irt": int(rewards[1])}


async def inviter_history(session: AsyncSession, user_id: int, limit: int = 20) -> list[ReferralReward]:
    result = await session.execute(
        select(ReferralReward)
        .where(ReferralReward.inviter_id == user_id)
        .order_by(ReferralReward.id.desc())
        .limit(limit)
    )
    return list(result.scalars())


__all__ = ["grant_for_paid_order", "referral_stats", "inviter_history", "PaymentStatus"]

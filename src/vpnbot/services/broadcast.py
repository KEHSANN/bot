"""Broadcast campaign materialization (worker-side).

Recipient rows used to be built inside the admin's "start" click
handler: for a large audience the insert ran for seconds inside the
Telegram callback's transaction, the callback answer timed out, and
the whole campaign start rolled back. Materialization now happens
in the worker's dispatch job (idempotent via the recipients'
unique key), and the handler only flips the campaign to RUNNING
and enqueues the dispatch.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.enums import RecipientStatus, UserStatus
from vpnbot.db.models.broadcast import BroadcastCampaign, BroadcastRecipient
from vpnbot.db.models.users import Admin, User


async def audience_pairs(
    session: AsyncSession, audience: str, *, test_admin: Admin | None = None
) -> list[tuple[int, int]]:
    """(user_id, telegram_id) pairs for an audience, in ONE query."""
    if audience == "TEST":
        if test_admin is None:
            return []
        return [(test_admin.user_id, test_admin.user.telegram_id)]
    stmt = select(User.id, User.telegram_id).where(User.status == UserStatus.ACTIVE)
    if audience == "BUYERS":
        from vpnbot.db.models.orders import Order

        stmt = stmt.where(User.id.in_(select(Order.user_id).distinct()))
    elif audience == "NO_BUY":
        from vpnbot.db.models.orders import Order

        stmt = stmt.where(User.id.not_in(select(Order.user_id).distinct()))
    return [(int(r[0]), int(r[1])) for r in (await session.execute(stmt)).all()]


async def materialize_recipients(session: AsyncSession, campaign: BroadcastCampaign) -> int:
    """Insert PENDING recipient rows for a campaign (idempotent)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    test_admin = None
    if campaign.audience == "TEST" and campaign.created_by_admin_id:
        from sqlalchemy.orm import selectinload

        test_admin = (
            await session.execute(
                select(Admin)
                .options(selectinload(Admin.user))
                .where(Admin.id == campaign.created_by_admin_id)
            )
        ).scalar_one_or_none()
    pairs = await audience_pairs(session, campaign.audience, test_admin=test_admin)
    if not pairs:
        campaign.total_recipients = 0
        return 0
    rows = [
        {
            "campaign_id": campaign.id,
            "user_id": user_id,
            "telegram_id": telegram_id,
            "status": RecipientStatus.PENDING,
            "attempts": 0,
        }
        for user_id, telegram_id in pairs
    ]
    # Chunked like the inventory import: one INSERT of every
    # recipient exceeds asyncpg's 32,767-parameter cap at ~6,553
    # users (5 params per row).
    for start in range(0, len(rows), 1000):
        await session.execute(
            pg_insert(BroadcastRecipient.__table__)
            .values(rows[start : start + 1000])
            .on_conflict_do_nothing(index_elements=["campaign_id", "user_id"])
        )
    campaign.total_recipients = len(rows)
    return len(rows)


async def recipient_count(session: AsyncSession, campaign_id: int) -> int:
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                select(func.count(BroadcastRecipient.id)).where(
                    BroadcastRecipient.campaign_id == campaign_id
                )
            )
        ).scalar_one()
    )

"""One tier-2 referral reward per inviter, enforced by the database.

The tier-2 eligibility check in services/referrals counted existing
tier-2 rows (``already == 0``) inside the fulfilment transaction. Two
of an inviter's invitees whose orders were fulfilled in the same
worker batch each saw zero and both granted — the table's unique
constraint is on (referred_user_id, tier), which differs per invitee,
and the ledger key was invitee-scoped too. This partial unique index
makes the second insert fail; the service treats that as "already
granted" and skips, and the ledger key is now inviter-scoped as a
second line of defence.

Revision ID: f6d94b3a8c21
Revises: e5c83a2b7f41
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6d94b3a8c21"
down_revision = "e5c83a2b7f41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_reward_inviter_tier2",
        "referral_rewards",
        ["inviter_id"],
        unique=True,
        postgresql_where=sa.text("tier = 2"),
    )


def downgrade() -> None:
    op.drop_index("uq_reward_inviter_tier2", table_name="referral_rewards")

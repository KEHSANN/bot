"""Add wallet_topups (customer wallet top-up requests).

Revision ID: d5b72e1c8a3f
Revises: c4a61f0b9d2e
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d5b72e1c8a3f"
down_revision: str | None = "c4a61f0b9d2e"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "wallet_topups",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "method",
            sa.Enum("CARD", "CRYPTO", name="paymentmethod", native_enum=False, length=48),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "AWAITING_PAYMENT_DETAILS",
                "PENDING_REVIEW",
                "APPROVED",
                "REJECTED",
                "EXPIRED",
                "CANCELLED",
                name="paymentstatus",
                native_enum=False,
                length=48,
            ),
            nullable=False,
        ),
        sa.Column("amount_irt", sa.BigInteger(), nullable=False),
        sa.Column("txid", sa.String(length=200), nullable=True),
        sa.Column("receipt_file_id", sa.String(length=300), nullable=True),
        sa.Column("receipt_file_type", sa.String(length=24), nullable=True),
        sa.Column("instructions_snapshot", sa.JSON(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["reviewed_by_admin_id"], ["admins.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("txid"),
    )
    op.create_index(op.f("ix_wallet_topups_user_id"), "wallet_topups", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_wallet_topups_user_id"), table_name="wallet_topups")
    op.drop_table("wallet_topups")

"""Add products.button_style (per-product shop button colour).

Revision ID: c4a61f0b9d2e
Revises: 1776e4e6437a
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c4a61f0b9d2e"
down_revision: str | None = "1776e4e6437a"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "button_style",
            sa.Enum("DEFAULT", "PRIMARY", "SUCCESS", "DANGER", name="buttonstyle", native_enum=False, length=48),
            nullable=False,
            server_default="DEFAULT",
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "button_style")

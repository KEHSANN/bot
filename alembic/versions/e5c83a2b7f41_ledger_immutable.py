"""Make the wallet ledger append-only at the database level.

The wallet service has always treated ``ledger_entries`` as append-only
(the model docstring even claimed a DB rule enforced it), but unlike
``audit_events`` no trigger actually existed. Mirror the audit guard:
row UPDATE/DELETE raise; TRUNCATE stays available for test fixtures.

Revision ID: e5c83a2b7f41
Revises: d5b72e1c8a3f
Create Date: 2026-10-21
"""

from __future__ import annotations

from alembic import op

revision = "e5c83a2b7f41"
down_revision = "d5b72e1c8a3f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ledger_entries_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'ledger_entries is append-only (% attempted)', TG_OP;
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_ledger_entries_immutable "
        "BEFORE UPDATE OR DELETE ON ledger_entries "
        "FOR EACH ROW EXECUTE FUNCTION ledger_entries_immutable();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_immutable ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS ledger_entries_immutable()")

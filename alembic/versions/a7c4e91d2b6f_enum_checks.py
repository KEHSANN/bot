"""Add the CHECK constraints the enum columns were documented to have.

``enum_type`` declares VARCHAR + CHECK, and new schemas get the
checks from the metadata, but every schema built by the earlier
migrations has plain VARCHAR columns — an invalid enum value (say a
payment method outside the enum) is accepted by the database. This
migration adds the missing checks with the current enum values.

Revision ID: a7c4e91d2b6f
Revises: f6d94b3a8c21
"""

from alembic import op

revision = "a7c4e91d2b6f"
down_revision = "f6d94b3a8c21"
branch_labels = None
depends_on = None

CHECKS = {
    ("audit_events", "actor_type"): ["CUSTOMER", "ADMIN", "SYSTEM", "WORKER"],
    ("broadcast_campaigns", "status"): ["DRAFT", "RUNNING", "PAUSED", "STOPPED", "FINISHED", "FAILED"],
    ("broadcast_recipients", "status"): ["PENDING", "SENT", "FAILED", "SKIPPED", "UNREACHABLE"],
    ("inventory_items", "status"): ["AVAILABLE", "RESERVED", "DELIVERED", "DISABLED"],
    ("jobs", "kind"): ["FULFILL_ORDER", "DELIVERY_RETRY", "BROADCAST_DISPATCH", "BROADCAST_RECIPIENT", "EXPIRE_PAYMENT", "NOTIFY_ADMIN"],
    ("jobs", "status"): ["QUEUED", "RUNNING", "DONE", "FAILED", "CANCELLED"],
    ("ledger_entries", "kind"): ["TOPUP", "PURCHASE", "REFUND", "REFERRAL_REWARD", "ADMIN_ADJUSTMENT"],
    ("orders", "delivery_status"): ["NOT_STARTED", "PENDING", "SENDING", "DELIVERED", "RETRY_REQUIRED", "MANUAL_REVIEW"],
    ("orders", "status"): ["PENDING_PAYMENT", "PAID", "FULFILLMENT_PENDING", "FULFILLED", "CANCELLED", "REFUNDED"],
    ("payments", "method"): ["CARD", "CRYPTO", "WALLET"],
    ("payments", "status"): ["AWAITING_PAYMENT_DETAILS", "PENDING_REVIEW", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"],
    ("products", "button_style"): ["DEFAULT", "PRIMARY", "SUCCESS", "DANGER"],
    ("products", "product_type"): ["CONFIG", "SUBSCRIPTION", "COMBO"],
    ("referral_rewards", "status"): ["ISSUED", "REVOKED"],
    ("ticket_messages", "sender_type"): ["CUSTOMER", "ADMIN", "SYSTEM"],
    ("tickets", "status"): ["OPEN", "ANSWERED", "CLOSED"],
    ("users", "status"): ["ACTIVE", "BLOCKED"],
    ("wallet_topups", "method"): ["CARD", "CRYPTO", "WALLET"],
    ("wallet_topups", "status"): ["AWAITING_PAYMENT_DETAILS", "PENDING_REVIEW", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"],
}


def _name(table: str, column: str) -> str:
    return f"ck_{table}_{column}_enum"


def upgrade() -> None:
    for (table, column), values in CHECKS.items():
        in_list = ", ".join(f"'{v}'" for v in values)
        op.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT {_name(table, column)} '
            f'CHECK ({column} IN ({in_list}))'
        )


def downgrade() -> None:
    for table, column in CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {_name(table, column)}")

"""Domain enums and the permission registry.

Every enum used in the database lives here so that models, services and
handlers agree on a single vocabulary. Values are stored as strings.
"""

from __future__ import annotations

import enum


class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"


class ProductType(str, enum.Enum):
    CONFIG = "CONFIG"  # single configuration
    SUBSCRIPTION = "SUBSCRIPTION"  # subscription URL based
    COMBO = "COMBO"  # both


class InventoryStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DELIVERED = "DELIVERED"
    DISABLED = "DISABLED"


class AssignmentStatus(str, enum.Enum):
    RESERVED = "RESERVED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class OrderStatus(str, enum.Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAID = "PAID"
    FULFILLMENT_PENDING = "FULFILLMENT_PENDING"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class PaymentStatus(str, enum.Enum):
    AWAITING_PAYMENT_DETAILS = "AWAITING_PAYMENT_DETAILS"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PaymentMethod(str, enum.Enum):
    CARD = "CARD"
    CRYPTO = "CRYPTO"
    WALLET = "WALLET"


class DeliveryStatus(str, enum.Enum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    SENDING = "SENDING"
    DELIVERED = "DELIVERED"
    RETRY_REQUIRED = "RETRY_REQUIRED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class LedgerKind(str, enum.Enum):
    TOPUP = "TOPUP"
    PURCHASE = "PURCHASE"
    REFUND = "REFUND"
    REFERRAL_REWARD = "REFERRAL_REWARD"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"


class RewardStatus(str, enum.Enum):
    ISSUED = "ISSUED"
    REVOKED = "REVOKED"


class TicketStatus(str, enum.Enum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    CLOSED = "CLOSED"


class TicketSender(str, enum.Enum):
    CUSTOMER = "CUSTOMER"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"


class CampaignStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


class RecipientStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    UNREACHABLE = "UNREACHABLE"


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobKind(str, enum.Enum):
    FULFILL_ORDER = "FULFILL_ORDER"
    DELIVERY_RETRY = "DELIVERY_RETRY"
    BROADCAST_DISPATCH = "BROADCAST_DISPATCH"
    BROADCAST_RECIPIENT = "BROADCAST_RECIPIENT"
    EXPIRE_PAYMENT = "EXPIRE_PAYMENT"
    NOTIFY_ADMIN = "NOTIFY_ADMIN"


class MessageType(str, enum.Enum):
    TEXT = "TEXT"
    PHOTO = "PHOTO"
    VIDEO = "VIDEO"
    DOCUMENT = "DOCUMENT"


class ButtonStyle(str, enum.Enum):
    DEFAULT = "DEFAULT"
    PRIMARY = "PRIMARY"
    SUCCESS = "SUCCESS"
    DANGER = "DANGER"


class AuditActor(str, enum.Enum):
    CUSTOMER = "CUSTOMER"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"
    WORKER = "WORKER"


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------
class Permission(str, enum.Enum):
    STATS_VIEW = "stats.view"
    PRODUCT_MANAGE = "product.manage"
    INVENTORY_MANAGE = "inventory.manage"
    INVENTORY_VIEW_SECRET = "inventory.view_secret"  # noqa: S105 - permission name
    ORDER_VIEW = "order.view"
    PAYMENT_REVIEW = "payment.review"
    WALLET_ADJUST = "wallet.adjust"
    REFERRAL_MANAGE = "referral.manage"
    CONTENT_MANAGE = "content.manage"
    BROADCAST_MANAGE = "broadcast.manage"
    SUPPORT_MANAGE = "support.manage"
    ADMIN_MANAGE = "admin.manage"
    AUDIT_VIEW = "audit.view"
    SETTINGS_MANAGE = "settings.manage"


ALL_PERMISSIONS: frozenset[str] = frozenset(p.value for p in Permission)

ROLE_PRESETS: dict[str, dict[str, object]] = {
    "owner": {
        "title": "مالک",
        "permissions": sorted(ALL_PERMISSIONS),
    },
    "manager": {
        "title": "مدیر",
        "permissions": sorted(
            {
                Permission.STATS_VIEW.value,
                Permission.PRODUCT_MANAGE.value,
                Permission.INVENTORY_MANAGE.value,
                Permission.ORDER_VIEW.value,
                Permission.PAYMENT_REVIEW.value,
                Permission.REFERRAL_MANAGE.value,
                Permission.CONTENT_MANAGE.value,
                Permission.BROADCAST_MANAGE.value,
                Permission.SUPPORT_MANAGE.value,
                Permission.AUDIT_VIEW.value,
            }
        ),
    },
    "finance": {
        "title": "مالی",
        "permissions": sorted(
            {
                Permission.STATS_VIEW.value,
                Permission.PAYMENT_REVIEW.value,
                Permission.ORDER_VIEW.value,
                Permission.WALLET_ADJUST.value,
                Permission.AUDIT_VIEW.value,
            }
        ),
    },
    "support": {
        "title": "پشتیبان",
        "permissions": sorted(
            {
                Permission.STATS_VIEW.value,
                Permission.ORDER_VIEW.value,
                Permission.SUPPORT_MANAGE.value,
            }
        ),
    },
}

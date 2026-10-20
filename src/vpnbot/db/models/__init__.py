"""Import all models so metadata is complete for Alembic and tests."""

from vpnbot.db.base import Base
from vpnbot.db.models.broadcast import BroadcastCampaign, BroadcastRecipient
from vpnbot.db.models.catalog import Category, Plan, Product
from vpnbot.db.models.content import MessageDispatchLog, ScreenDefinition, ScreenVersion
from vpnbot.db.models.inventory import DeliveryAttempt, InventoryAssignment, InventoryItem
from vpnbot.db.models.orders import Order, Payment, PaymentReview
from vpnbot.db.models.referral import ReferralReward
from vpnbot.db.models.support import Ticket, TicketMessage
from vpnbot.db.models.system import AppSetting, AuditEvent, Job
from vpnbot.db.models.users import Admin, Role, User
from vpnbot.db.models.wallet import LedgerEntry, WalletAccount, WalletTopUp

__all__ = [
    "Admin",
    "AppSetting",
    "AuditEvent",
    "Base",
    "BroadcastCampaign",
    "BroadcastRecipient",
    "Category",
    "DeliveryAttempt",
    "InventoryAssignment",
    "InventoryItem",
    "Job",
    "LedgerEntry",
    "MessageDispatchLog",
    "Order",
    "Payment",
    "PaymentReview",
    "Plan",
    "Product",
    "ReferralReward",
    "Role",
    "ScreenDefinition",
    "ScreenVersion",
    "Ticket",
    "TicketMessage",
    "User",
    "WalletAccount",
    "WalletTopUp",
]

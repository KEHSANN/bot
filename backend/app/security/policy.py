PERMISSIONS = frozenset(
    {
        "users.read",
        "users.manage",
        "products.read",
        "products.manage",
        "inventory.read",
        "inventory.import",
        "orders.read",
        "orders.manage",
        "wallets.read",
        "wallets.adjust",
        "payments.read",
        "payments.approve_manual",
        "payments.refund",
        "providers.manage",
        "referrals.read",
        "referrals.configure",
        "referrals.approve_milestone",
        "campaigns.manage",
        "support.manage",
        "admins.read",
        "admins.manage",
        "roles.read",
        "roles.manage",
        "sessions.manage",
        "settings.read",
        "settings.manage",
        "audit.read",
    }
)

DEFAULT_ROLES = {
    "super_admin": PERMISSIONS,
    "operations": frozenset(
        {
            "users.read",
            "users.manage",
            "products.read",
            "products.manage",
            "inventory.read",
            "orders.read",
            "orders.manage",
            "support.manage",
        }
    ),
    "finance": frozenset(
        {
            "users.read",
            "wallets.read",
            "wallets.adjust",
            "payments.read",
            "payments.approve_manual",
            "payments.refund",
            "orders.read",
        }
    ),
    "inventory": frozenset({"products.read", "inventory.read", "inventory.import"}),
    "support": frozenset({"users.read", "orders.read", "support.manage"}),
    "marketing": frozenset({"products.read", "referrals.read", "campaigns.manage"}),
    "auditor": frozenset(
        {
            "users.read",
            "products.read",
            "inventory.read",
            "orders.read",
            "wallets.read",
            "payments.read",
            "referrals.read",
            "admins.read",
            "roles.read",
            "settings.read",
            "audit.read",
        }
    ),
}

"""Admin panel router assembly.

Every sub-router is a self-contained ``AdminCB`` section; the order here only
decides which router gets a callback first, and sections are disjoint, so the
order is stable and predictable.
"""

from __future__ import annotations

from aiogram import Router

from vpnbot.bot.handlers.admin import (
    admins,
    audit,
    broadcast,
    catalog,
    dashboard,
    import_wizard,
    inventory,
    orders,
    payments,
    screens_admin,
    settings_manager,
    support,
    wallet_admin,
)

__all__ = ["build_admin_router"]


def build_admin_router() -> Router:
    router = Router(name="admin")
    router.include_router(dashboard.router)
    router.include_router(catalog.router)
    router.include_router(inventory.router)
    router.include_router(import_wizard.router)
    router.include_router(orders.router)
    router.include_router(payments.router)
    router.include_router(support.router)
    router.include_router(broadcast.router)
    router.include_router(screens_admin.router)
    router.include_router(settings_manager.router)
    router.include_router(admins.router)
    router.include_router(wallet_admin.router)
    router.include_router(audit.router)
    return router

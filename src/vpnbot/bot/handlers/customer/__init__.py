"""Customer-facing routers.

Order matters where two handlers could match the same update: the menu router
owns generic navigation and commands, the shop owns catalog callbacks, and the
state-scoped routers (checkout, support) only match while their FSM state is
active.
"""

from __future__ import annotations

from aiogram import Router

from vpnbot.bot.handlers.customer import checkout, menu, orders, referral, shop, support, wallet


def build_customer_router() -> Router:
    router = Router(name="customer")
    router.include_router(menu.router)
    router.include_router(shop.router)
    router.include_router(checkout.router)
    router.include_router(orders.router)
    router.include_router(wallet.router)
    router.include_router(referral.router)
    router.include_router(support.router)
    return router


__all__ = ["build_customer_router"]

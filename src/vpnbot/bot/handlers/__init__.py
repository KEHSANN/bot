"""Root router assembly.

The admin router is included first: ``/admin`` is an admin command and the
admin callbacks (``a|`` prefix) are disjoint from customer callbacks (``m|``,
``c|``, ``p|`` …), so ordering only affects commands that could overlap.
"""

from __future__ import annotations

from aiogram import Router


def build_root_router() -> Router:
    from vpnbot.bot.handlers.admin import build_admin_router
    from vpnbot.bot.handlers.customer import build_customer_router

    router = Router(name="root")
    router.include_router(build_admin_router())
    router.include_router(build_customer_router())
    return router


__all__ = ["build_root_router"]

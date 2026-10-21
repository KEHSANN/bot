"""Root router assembly.

The global commands router is included first: ``/cancel``, ``/start``,
``/menu`` and ``/admin`` must be claimed before any state-filtered
message handler (admin or customer) can consume them as wizard input.
After that, the admin router precedes the customer router — their
callback prefixes are disjoint (``a|`` vs ``m|``, ``c|``, ``p|`` …), so
ordering only ever mattered for commands, which are now handled above.
"""

from __future__ import annotations

from aiogram import F, Router


def build_root_router() -> Router:
    from vpnbot.bot.handlers import commands
    from vpnbot.bot.handlers.admin import build_admin_router
    from vpnbot.bot.handlers.customer import build_customer_router

    router = Router(name="root")
    # The bot is private-chat only. Individual handlers used to check
    # chat type ad hoc (and a few — checkout, wallet, orders admin —
    # did not), so in a group the bot could answer, and /cancel there
    # even cleared state. One filter at the root settles it for every
    # current and future handler.
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")
    router.include_router(commands.build_commands_router())
    router.include_router(build_admin_router())
    router.include_router(build_customer_router())
    return router


__all__ = ["build_root_router"]

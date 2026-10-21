"""Global bot commands, registered BEFORE every other router.

Telegram commands must never be swallowed as wizard input. The admin
router used to be matched first, so an admin typing ``/cancel`` inside
a state (rejecting a payment, answering a ticket, creating a product)
fed the literal text "/cancel" into the wizard — a payment got rejected
with the reason "/cancel", a ticket was answered with "/cancel", a
product was created named "/cancel". The bot's own prompts tell admins
to cancel with ``/cancel``, so this was guaranteed to happen.

This router claims ``/cancel``, ``/start``, ``/menu`` and ``/admin``
before any state-filtered message handler can see them, and delegates
to the same handler functions the feature routers use.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin.dashboard import open_panel
from vpnbot.bot.handlers.customer.menu import cmd_menu, cmd_start
from vpnbot.db.models.users import Admin, User


async def _cancel(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    """Abort whatever wizard/state the user is in.

    For customers this also cancels a still-unpaid order bound to the
    state, so abandoning a checkout frees the open-order slot at once
    instead of at the TTL sweep. Admin states carry no order_id, so
    for admins this remains a pure state clear.
    """
    if user is not None and session is not None:
        from vpnbot.bot.handlers.customer.menu import cancel_open_order_from_state

        await cancel_open_order_from_state(session, user, state)
    await state.clear()
    await message.answer("عملیات لغو شد.")


async def _start(
    message: Message,
    session: AsyncSession,
    user: User | None,
    command: CommandObject,
    state: FSMContext,
) -> None:
    await cmd_start(message, session, user, command, state)


async def _menu(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    await cmd_menu(message, session, user, state)


async def _panel(message: Message, session: AsyncSession, admin: Admin | None) -> None:
    await open_panel(message, session, admin)


def build_commands_router() -> Router:
    router = Router(name="global.commands")
    # NOTE: router.message(...) is a decorator *factory* in aiogram 3 —
    # calling it with the handler as an argument registers NOTHING (the
    # returned decorator is discarded). Registration must go through
    # .register(). A test pins the handler count so this cannot regress
    # silently again.
    router.message.register(_cancel, Command("cancel"))
    router.message.register(_start, CommandStart())
    router.message.register(_menu, Command("menu"))
    router.message.register(_panel, Command("admin", "panel"))
    return router


# Kept for tests/tools that drive the cancel behaviour directly.
cmd_cancel = _cancel

__all__ = ["build_commands_router", "cmd_cancel"]

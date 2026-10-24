"""Main menu, /start and generic navigation callbacks."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.customer.common import base_context, sender_for
from vpnbot.bot.keyboards.callbacks import MenuCB
from vpnbot.bot.sender import ScreenSender
from vpnbot.db.models.users import User
from vpnbot.services import auth as auth_service
from vpnbot.services.screens import (
    STAGE_BLOCKED,
    STAGE_GUIDE,
    STAGE_MAIN_MENU,
    STAGE_TERMS,
)

log = logging.getLogger(__name__)
router = Router(name="customer.menu")


async def cancel_open_order_from_state(
    session: AsyncSession, user: User, state: FSMContext
) -> None:
    """Cancel the in-progress order when a checkout flow is abandoned.

    The ❌ button and /cancel used to only clear the FSM state, leaving
    the order in PENDING_PAYMENT forever — three abandoned checkouts
    and the open-order cap locked the customer out of buying entirely.
    Only a still-unpaid order (payment AWAITING details) is cancelled;
    a submitted receipt (PENDING_REVIEW) is the admin's to decide.
    """
    data = await state.get_data()
    order_id = data.get("order_id")
    if not order_id:
        return
    from vpnbot.db.enums import PaymentStatus
    from vpnbot.services import orders as order_service

    order = await order_service.user_order(session, order_id=int(order_id), user_id=user.id)
    if order is None or order.status is not order_service.OrderStatus.PENDING_PAYMENT:
        return
    if order.payment is not None and order.payment.status not in (
        PaymentStatus.AWAITING_PAYMENT_DETAILS,
        PaymentStatus.REJECTED,
    ):
        return
    import contextlib

    with contextlib.suppress(order_service.OrderError):
        # May race with fulfilment — then there is nothing to cancel.
        await order_service.cancel_order(session, order.id, reason="لغو توسط مشتری", by_customer=True)

#: Simple action -> screen mapping for actions with no dynamic content.
STATIC_ACTIONS = {
    "home": STAGE_MAIN_MENU,
    "guide": STAGE_GUIDE,
    "terms": STAGE_TERMS,
    "blocked": STAGE_BLOCKED,
}


def _referral_from_start(args: str | None) -> str | None:
    if not args:
        return None
    args = args.strip()
    if args.startswith("ref_"):
        return args[4:]
    if args.startswith("ref-"):
        return args[4:]
    return args


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    user: User | None,
    command: CommandObject,
    state: FSMContext,
) -> None:
    if message.chat.type != "private" or user is None:
        await state.clear()
        return
    # Abandoning via /start cancels the state-bound unpaid order too
    # (same helper as /cancel): /start used to only clear state, so
    # the order kept a cap slot until its TTL — and the wallet screen
    # even tells users to send /start to cancel.
    await cancel_open_order_from_state(session, user, state)
    await state.clear()

    sender: ScreenSender = sender_for(message.bot, session)
    if not user.is_active:
        # Blocked screen: no dispatch-log row per view (the middleware
        # already rate-limits how often a blocked user reaches this).
        await sender.show(message, STAGE_BLOCKED, log_outcome=False)
        return

    code = _referral_from_start(command.args)
    if code and code != user.referral_code:
        from vpnbot.services.settings import REFERRAL_SETTINGS, SHOP_SETTINGS, SettingsService

        settings = SettingsService(session)
        referral_on = (await settings.get(REFERRAL_SETTINGS)).get("enabled", True) and (
            await settings.get(SHOP_SETTINGS)
        ).get("referral_enabled", True)
        if referral_on:
            bound = await auth_service.bind_referral(session, user, code)
            if bound:
                log.info("referral attributed: user=%s inviter_code=%s", user.id, code)

    await sender.show(message, STAGE_MAIN_MENU, base_context(user))


@router.message(Command("menu", "cancel"))
async def cmd_menu(message: Message, session: AsyncSession, user: User | None, state: FSMContext) -> None:
    if message.chat.type != "private" or user is None:
        await state.clear()
        return
    await cancel_open_order_from_state(session, user, state)
    await state.clear()
    sender = sender_for(message.bot, session)
    await sender.show(message, STAGE_MAIN_MENU, base_context(user))


@router.callback_query(MenuCB.filter(F.action.in_(set(STATIC_ACTIONS))))
async def static_nav(
    callback: CallbackQuery,
    callback_data: MenuCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
) -> None:
    if user is None:
        await callback.answer()
        return
    # Navigation is a state reset: without this, an admin mid-wizard
    # (payment note, wallet adjust, broadcast…) who taps a customer
    # menu button stays in the wizard state, and their next text
    # message is consumed as wizard input — the customer-section
    # entries were the ones the earlier bleed fixes missed.
    # Abandoning a checkout from here cancels its unpaid order too
    # (same as /cancel): otherwise three abandoned checkouts lock
    # the customer out via the open-order cap.
    await cancel_open_order_from_state(session, user, state)
    await state.clear()
    sender = sender_for(callback.bot, session)
    await sender.show(callback, STATIC_ACTIONS[callback_data.action], base_context(user))


@router.callback_query(MenuCB.filter(F.action == "noop"))
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(MenuCB.filter(F.action == "cancel"))
async def cancel_flow(callback: CallbackQuery, session: AsyncSession, user: User | None, state: FSMContext) -> None:
    if user is not None:
        await cancel_open_order_from_state(session, user, state)
    await state.clear()
    await callback.answer("لغو شد")
    if user is None:
        return
    sender = sender_for(callback.bot, session)
    await sender.show(callback, STAGE_MAIN_MENU, base_context(user))


@router.message(Command("cancel"))
async def cancel_command(message: Message, session: AsyncSession, user: User | None, state: FSMContext) -> None:
    await state.clear()
    if user is not None:
        sender = sender_for(message.bot, session)
        await sender.show(message, STAGE_MAIN_MENU, base_context(user))

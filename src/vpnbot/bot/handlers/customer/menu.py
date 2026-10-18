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
    await state.clear()
    if message.chat.type != "private" or user is None:
        return

    code = _referral_from_start(command.args)
    if code and code != user.referral_code:
        bound = await auth_service.bind_referral(session, user, code)
        if bound:
            log.info("referral attributed: user=%s inviter_code=%s", user.id, code)

    sender: ScreenSender = sender_for(message.bot, session)
    if not user.is_active:
        await sender.show(message, STAGE_BLOCKED)
        return
    await sender.show(message, STAGE_MAIN_MENU, base_context(user))


@router.message(Command("menu", "cancel"))
async def cmd_menu(message: Message, session: AsyncSession, user: User | None, state: FSMContext) -> None:
    await state.clear()
    if message.chat.type != "private" or user is None:
        return
    sender = sender_for(message.bot, session)
    await sender.show(message, STAGE_MAIN_MENU, base_context(user))


@router.callback_query(MenuCB.filter(F.action.in_(set(STATIC_ACTIONS))))
async def static_nav(
    callback: CallbackQuery, callback_data: MenuCB, session: AsyncSession, user: User | None
) -> None:
    if user is None:
        await callback.answer()
        return
    sender = sender_for(callback.bot, session)
    await sender.show(callback, STATIC_ACTIONS[callback_data.action], base_context(user))


@router.callback_query(MenuCB.filter(F.action == "noop"))
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(MenuCB.filter(F.action == "cancel"))
async def cancel_flow(callback: CallbackQuery, session: AsyncSession, user: User | None, state: FSMContext) -> None:
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

"""Customer support: opening a ticket from the bot.

Every message becomes a ticket the admin panel can answer; the reply is sent
back to the same chat by the admin handler, not here.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.customer.common import alert, base_context, edit_or_answer, sender_for
from vpnbot.bot.keyboards.callbacks import MenuCB
from vpnbot.bot.states import SupportStates
from vpnbot.db.models.users import User
from vpnbot.services import support as support_service
from vpnbot.services.screens import STAGE_MAIN_MENU, STAGE_SUPPORT
from vpnbot.services.settings import SUPPORT_SETTINGS, SettingsService

router = Router(name="customer.support")


@router.callback_query(MenuCB.filter(F.action == "support"))
async def open_support(
    callback: CallbackQuery, session: AsyncSession, user: User | None
) -> None:
    if user is None:
        await callback.answer()
        return
    config = await SettingsService(session).get(SUPPORT_SETTINGS)
    if not config.get("enabled", True):
        await alert(callback, "پشتیبانی در حال حاضر غیرفعال است")
        return
    await sender_for(callback.bot, session).show(callback, STAGE_SUPPORT, base_context(user))


@router.callback_query(MenuCB.filter(F.action == "support_new"))
async def begin_ticket(
    callback: CallbackQuery, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    if user is None:
        await callback.answer()
        return
    config = await SettingsService(session).get(SUPPORT_SETTINGS)
    if not config.get("enabled", True):
        await alert(callback, "پشتیبانی در حال حاضر غیرفعال است")
        return
    await state.set_state(SupportStates.awaiting_message)
    await callback.answer()
    if isinstance(callback.message, Message):
        await edit_or_answer(
            callback.message,
            "✍️ پیام خود را بنویسید.\n"
            "هرچه دقیق‌تر توضیح دهید، سریع‌تر پاسخ می‌گیرید.\n\n"
            "برای انصراف /cancel را بفرستید.",
        )


@router.message(SupportStates.awaiting_message)
async def receive_ticket(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    if user is None:
        return
    config = await SettingsService(session).get(SUPPORT_SETTINGS)
    if not config.get("enabled", True):
        # The ticket-creation point itself is gated (not only the
        # button): a state armed before the admin disabled support
        # must not slip a ticket through.
        await state.clear()
        await message.answer("پشتیبانی در حال حاضر غیرفعال است.")
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ لطفاً پیام خود را به صورت متن ارسال کنید.")
        return

    try:
        ticket = await support_service.create_ticket(session, user=user, text=text)
    except support_service.SupportError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    await state.clear()
    await message.answer(
        f"✅ پیام شما ثبت شد (تیکت #{ticket.id}).\n"
        "پاسخ پشتیبانی در همین گفتگو برای شما ارسال می‌شود."
    )
    await sender_for(message.bot, session).show(
        message, STAGE_MAIN_MENU, base_context(user), log_outcome=False
    )

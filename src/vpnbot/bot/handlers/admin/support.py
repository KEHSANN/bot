"""Support inbox: ticket queue, thread view and admin replies."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.bot.states import AdminStates
from vpnbot.db.enums import Permission, TicketSender, TicketStatus
from vpnbot.db.models.support import Ticket
from vpnbot.db.models.users import Admin, User
from vpnbot.services import support as support_service
from vpnbot.services import user_admin as user_admin_service

router = Router(name="admin.support")

STATUS_FA = {
    TicketStatus.OPEN: "باز",
    TicketStatus.ANSWERED: "پاسخ داده‌شده",
    TicketStatus.CLOSED: "بسته",
}
FILTERS = {"open": TicketStatus.OPEN, "answered": TicketStatus.ANSWERED, "all": None}
FILTER_LABELS = {"open": "باز", "answered": "پاسخ داده‌شده", "all": "همه"}
THREAD_LIMIT = 12


async def _show_list(
    target: CallbackQuery | Message,
    session: AsyncSession,
    admin: Admin,
    filter_key: str,
    page: int,
) -> None:
    status = FILTERS.get(filter_key, TicketStatus.OPEN)
    total = await support_service.count_tickets(session, status=status)
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    tickets = await support_service.list_tickets(
        session, status=status, offset=common.clamp_page(page) * common.ADMIN_PAGE_SIZE, limit=common.ADMIN_PAGE_SIZE
    )

    rows = [
        [
            common.btn(
                f"🎫 #{ticket.id} · {common.escape(ticket.subject[:40])} · "
                f"{STATUS_FA[ticket.status]}",
                "support",
                "view",
                ticket.id,
            )
        ]
        for ticket in tickets
    ]
    nav = common.pager("support", f"list:{filter_key}", page, total_pages)
    if nav:
        rows.append(nav)
    rows.append(
        [common.btn(label, "support", f"list:{key}") for key, label in FILTER_LABELS.items()]
    )
    rows.append([common.back()])
    await common.reply(
        target,
        f"<b>🎫 پشتیبانی</b>\n\nفیلتر: {FILTER_LABELS.get(filter_key, 'باز')} · "
        f"تعداد: <b>{common.money(total)}</b>",
        rows,
    )


async def _show_thread(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, ticket_id: int
) -> None:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        await common.reply(target, "تیکت یافت نشد.", [[common.back("support")]])
        return
    user = await session.get(User, ticket.user_id)
    messages = await support_service.ticket_messages(session, ticket_id)

    lines = [
        f"<b>🎫 تیکت #{ticket.id}</b>",
        "",
        f"موضوع: {common.escape(ticket.subject)}",
        f"وضعیت: {STATUS_FA[ticket.status]}",
        f"کاربر: {(f'<code>{user.telegram_id}</code>') if user else '—'}"
        + (f" · @{user.username}" if user and user.username else ""),
        f"آخرین پیام: {common.date(ticket.last_message_at)}",
        "",
        "<b>— گفتگو —</b>",
    ]
    for message in messages[-THREAD_LIMIT:]:
        who = "👤 کاربر" if message.sender_type is TicketSender.CUSTOMER else "🛠 پشتیبانی"
        stamp = common.date(message.created_at, with_time=True)
        lines.append(f"\n<b>{who}</b> · {stamp}\n{common.escape(message.text)}")
    if len(messages) > THREAD_LIMIT:
        lines.insert(8, f"(نمایش {THREAD_LIMIT} پیام آخر از {len(messages)})")

    rows: list[list] = []
    if ticket.status is not TicketStatus.CLOSED:
        rows.append([common.btn("✍️ پاسخ", "support", "reply", ticket.id)])
        rows.append([common.btn("✅ بستن تیکت", "support", "close", ticket.id)])
    if user is not None:
        if user.is_blocked:
            rows.append([common.btn("♻️ رفع بلاک کاربر", "support", "unblock", ticket.id)])
        else:
            rows.append([common.btn("🚫 بلاک کاربر", "support", "block", ticket.id)])
    rows.append([common.btn("🔄 بروزرسانی", "support", "view", ticket.id)])
    rows.append([common.back("support")])
    await common.reply(target, "\n".join(lines), rows)


@router.callback_query(AdminCB.filter(F.section == "support"))
async def support_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.SUPPORT_MANAGE)
    action = callback_data.action
    # Navigating in this section abandons any half-finished wizard
    # (see the note in the payments router): flow entries re-set
    # their state after this clear.
    await state.clear()
    if action in ("block", "block_yes", "unblock"):
        # Blocking also wipes the user's receipts — a financial action,
        # not a support one. A pure support-role admin (no wallet
        # permission) must not be able to trigger it from a ticket.
        admin = common.guard(admin, Permission.WALLET_ADJUST)

    if action == "view":
        await _show_thread(callback, session, admin, callback_data.item_id)
        return

    if action == "reply":
        ticket = await session.get(Ticket, callback_data.item_id)
        if ticket is None:
            await common.alert(callback, "تیکت یافت نشد.")
            return
        if ticket.status is TicketStatus.CLOSED:
            await common.alert(callback, "این تیکت بسته شده است.")
            return
        await state.set_state(AdminStates.support_reply)
        await state.update_data(ticket_id=ticket.id)
        await callback.answer()
        await common.reply(
            callback,
            f"✍️ <b>پاسخ به تیکت #{ticket.id}</b>\n\nمتن پاسخ را بنویسید؛ "
            "همین‌جا ثبت و برای کاربر ارسال می‌شود.",
            [common.cancel_row()],
        )
        return

    if action == "close":
        ticket = await session.get(Ticket, callback_data.item_id)
        if ticket is None:
            await common.alert(callback, "تیکت یافت نشد.")
            return
        ticket.status = TicketStatus.CLOSED
        await common.audit(
            session, admin, "support.ticket_closed", entity_type="ticket", entity_id=ticket.id
        )
        await callback.answer("تیکت بسته شد")
        await _show_thread(callback, session, admin, ticket.id)
        return

    if action == "block":
        ticket = await session.get(Ticket, callback_data.item_id)
        if ticket is None:
            await common.alert(callback, "تیکت یافت نشد.")
            return
        user = await session.get(User, ticket.user_id)
        await callback.answer()
        await common.reply(
            callback,
            f"<b>🚫 بلاک «{common.escape(user.display_name) if user else 'کاربر'}»</b>\n\n"
            "کاربر دیگر هیچ پیامی نمی‌تواند بدهد، همه رسیدهایش پاک و "
            "پرداخت‌ها و شارژهای در انتظارش لغو می‌شوند.\n\nمطمئنید؟",
            [common.yes_no("support", "block_yes", ticket.id), [common.back("support")]],
        )
        return

    if action == "block_yes":
        ticket = await session.get(Ticket, callback_data.item_id)
        if ticket is None:
            await common.alert(callback, "تیکت یافت نشد.")
            return
        try:
            result = await user_admin_service.block_user(
                session, user_id=ticket.user_id, admin_id=admin.id
            )
        except user_admin_service.UserAdminError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        ticket.status = TicketStatus.CLOSED
        await callback.answer("کاربر بلاک و تیکت بسته شد")
        await common.reply(
            callback,
            "🚫 <b>کاربر بلاک شد و تیکت بسته شد.</b>\n\n"
            f"رسیدهای پاک‌شده: {common.money(result['receipts_cleared'])}\n"
            f"پرداخت‌های لغوشده: {common.money(result['payments_cancelled'])}\n"
            f"شارژهای لغوشده: {common.money(result['topups_cancelled'])}",
            [[common.back("support")]],
        )
        return

    if action == "unblock":
        ticket = await session.get(Ticket, callback_data.item_id)
        if ticket is None:
            await common.alert(callback, "تیکت یافت نشد.")
            return
        try:
            await user_admin_service.unblock_user(session, user_id=ticket.user_id, admin_id=admin.id)
        except user_admin_service.UserAdminError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        await callback.answer("بلاک کاربر برداشته شد")
        await _show_thread(callback, session, admin, ticket.id)
        return

    if action.startswith("list:"):
        await _show_list(callback, session, admin, action.split(":", 1)[1], callback_data.page)
        return

    await _show_list(callback, session, admin, "open", callback_data.page)


@router.message(AdminStates.support_reply, F.chat.type == "private")
async def reply_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    admin = common.guard(admin, Permission.SUPPORT_MANAGE)
    data = await state.get_data()
    ticket_id = int(data.get("ticket_id") or 0)
    text = (message.text or "").strip()
    if not text:
        await message.answer("متن پاسخ خالی است. برای انصراف /cancel را بفرستید.")
        return
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        await state.clear()
        await message.answer("تیکت یافت نشد.")
        return
    try:
        await support_service.add_message(
            session,
            ticket=ticket,
            text=text,
            sender_type=TicketSender.ADMIN,
            sender_id=admin.id,
        )
    except support_service.SupportError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    # Queue the delivery so it survives a restart and never races the commit.
    from vpnbot.services import notify as notify_service

    user = await session.get(User, ticket.user_id)
    if user is not None:
        await notify_service.enqueue(
            session,
            chat_id=user.telegram_id,
            text=f"<b>پاسخ پشتیبانی · تیکت #{ticket.id}</b>\n\n{common.escape(text)}",
        )
    await common.audit(
        session,
        admin,
        "support.replied",
        entity_type="ticket",
        entity_id=ticket.id,
        data={"length": len(text)},
    )
    await state.clear()
    await message.answer(f"✅ پاسخ در تیکت #{ticket.id} ثبت شد.")
    await _show_thread(message, session, admin, ticket.id)

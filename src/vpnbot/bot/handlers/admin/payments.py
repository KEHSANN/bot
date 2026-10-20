"""Payment review: the manual gate between money and delivery.

Approval is the only path to fulfilment. It is idempotent and row-locked in
``services.payments``; this handler only authorizes, prompts for the note and
then reports the outcome. Nothing here can reveal a config.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.bot.states import AdminStates
from vpnbot.db.enums import PaymentMethod, PaymentStatus, Permission
from vpnbot.db.models.orders import Payment
from vpnbot.db.models.users import Admin
from vpnbot.services import orders as order_service
from vpnbot.services import payments as payment_service

log = logging.getLogger(__name__)
router = Router(name="admin.payments")

METHOD_LABELS = {PaymentMethod.CARD: "کارت به کارت", PaymentMethod.CRYPTO: "رمزارز"}


async def _show_list(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, page: int
) -> None:
    total = await payment_service.count_pending_reviews(session)
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    payments = await payment_service.pending_reviews(
        session, offset=page * common.ADMIN_PAGE_SIZE, limit=common.ADMIN_PAGE_SIZE
    )

    rows = []
    for payment in payments:
        rows.append(
            [
                common.btn(
                    f"#{payment.id} · {common.money(payment.amount_irt)} · "
                    f"{METHOD_LABELS.get(payment.method, payment.method.value)}",
                    "payments",
                    "view",
                    payment.id,
                )
            ]
        )
    nav = common.pager("payments", "", page, total_pages)
    if nav:
        rows.append(nav)
    rows.append([common.back()])
    await common.reply(
        target,
        f"<b>💳 پرداخت‌های در انتظار بررسی: {common.money(total)}</b>\n\n"
        "هر مورد را باز کنید، رسید/هش را بررسی و سپس تأیید یا رد کنید.",
        rows,
    )


async def _show_payment(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, payment_id: int
) -> None:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        await common.reply(target, "پرداخت یافت نشد.", [[common.back("payments")]])
        return
    order = await order_service.get_order(session, payment.order_id)
    user_line = "—"
    if order is not None:
        from vpnbot.db.models.users import User

        user = await session.get(User, order.user_id)
        if user is not None:
            handle = f"@{user.username}" if user.username else "بدون یوزرنیم"
            user_line = f"{handle} · <code>{user.telegram_id}</code>"

    details = dict(payment.instructions_snapshot or {})
    lines = [
        f"<b>💳 پرداخت #{payment.id}</b>",
        "",
        f"سفارش: <code>{order.public_id if order else payment.order_id}</code>",
        f"مشتری: {user_line}",
        f"روش: {METHOD_LABELS.get(payment.method, payment.method.value)}",
        f"مبلغ: <b>{common.money(payment.amount_irt)} تومان</b>",
        f"وضعیت: {payment.status.value}",
        f"ارسال رسید: {common.date(payment.submitted_at)}",
    ]
    if payment.method is PaymentMethod.CARD:
        lines.extend(
            [
                f"مقصد: {details.get('card_number', '—')} ({details.get('recipient_name', '—')})",
            ]
        )
    else:
        lines.extend(
            [
                f"شبکه: {details.get('network', '—')} · {details.get('asset', '—')}",
                f"TXID: <code>{common.escape(payment.txid or '—')}</code>",
            ]
        )
    if payment.receipt_file_id:
        lines.append(f"رسید: {payment.receipt_file_type or 'فایل'} پیوست شده")

    rows: list[list] = []
    if payment.status is PaymentStatus.PENDING_REVIEW:
        rows.append(
            [
                common.btn("✅ تأیید و ارسال", "payments", "approve", payment.id),
                common.btn("⛔️ رد پرداخت", "payments", "reject", payment.id),
            ]
        )
    if payment.receipt_file_id and payment.receipt_file_type:
        rows.append([common.btn("👁 نمایش رسید", "payments", "receipt", payment.id)])
    rows.append([common.back("payments")])

    if payment.receipt_file_id and payment.receipt_file_type and isinstance(target, CallbackQuery):
        # The receipt is customer content, not inventory: safe for reviewers.
        pass
    await common.reply(target, "\n".join(lines), rows)


@router.callback_query(AdminCB.filter(F.section == "payments"))
async def payments_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.PAYMENT_REVIEW)
    action = callback_data.action

    if action == "view":
        await _show_payment(callback, session, admin, callback_data.item_id)
        return

    if action == "receipt":
        payment = await session.get(Payment, callback_data.item_id)
        if payment is not None and payment.receipt_file_id:
            message = callback.message
            if isinstance(message, Message):
                # Producers store the type lowercase ("photo"/"document");
                # comparing against "PHOTO" never matched, so photo
                # receipts — the common case — were sent as documents and
                # Telegram rejected them, crashing the tap with no feedback.
                try:
                    if (payment.receipt_file_type or "").upper() == "PHOTO":
                        await callback.bot.send_photo(message.chat.id, payment.receipt_file_id)
                    else:
                        await callback.bot.send_document(message.chat.id, payment.receipt_file_id)
                    await callback.answer("رسید ارسال شد")
                except TelegramBadRequest:
                    await callback.answer("⚠️ ارسال رسید ناموفق بود.", show_alert=True)
            else:
                await callback.answer()
        return

    if action == "approve":
        payment = await session.get(Payment, callback_data.item_id)
        if payment is None:
            await callback.answer("پرداخت یافت نشد", show_alert=True)
            return
        await state.set_state(AdminStates.payment_note)
        await state.update_data(payment_id=payment.id)
        await callback.answer()
        await common.reply(
            callback,
            f"<b>✅ تأیید پرداخت #{payment.id}</b>\n\n"
            "یادداشت تأیید را بفرستید (یا برای بدون یادداشت «-» بفرستید).\n"
            "با تأیید، کانفیگ برای مشتری ارسال می‌شود.",
            [common.cancel_row()],
        )
        return

    if action == "reject":
        payment = await session.get(Payment, callback_data.item_id)
        if payment is None:
            await callback.answer("پرداخت یافت نشد", show_alert=True)
            return
        await state.set_state(AdminStates.rejection_reason)
        await state.update_data(payment_id=payment.id)
        await callback.answer()
        await common.reply(
            callback,
            f"<b>⛔️ رد پرداخت #{payment.id}</b>\n\nدلیل رد را برای ثبت در پرونده بفرستید:",
            [common.cancel_row()],
        )
        return

    await _show_list(callback, session, admin, callback_data.page)


@router.message(AdminStates.payment_note)
async def approve_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    admin = common.guard(admin, Permission.PAYMENT_REVIEW)
    data = await state.get_data()
    payment_id = int(data.get("payment_id") or 0)
    note = (message.text or "").strip()
    note = None if note in {"", "-"} else note[:500]
    try:
        payment = await payment_service.approve_payment(
            session, payment_id=payment_id, admin_id=admin.id, note=note
        )
    except payment_service.PaymentError as exc:
        await state.clear()
        await message.answer(f"⚠️ {exc}")
        await _show_list(message, session, admin, 0)
        return
    await state.clear()
    await message.answer(
        f"✅ پرداخت #{payment.id} تأیید شد.\n"
        "سفارش در صف ارسال قرار گرفت؛ کانفیگ پس از ارسال به مشتری تحویل داده می‌شود."
    )
    await _show_list(message, session, admin, 0)


@router.message(AdminStates.rejection_reason)
async def reject_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    admin = common.guard(admin, Permission.PAYMENT_REVIEW)
    data = await state.get_data()
    payment_id = int(data.get("payment_id") or 0)
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("دلیل رد الزامی است. برای انصراف /cancel را بفرستید.")
        return
    try:
        payment = await payment_service.reject_payment(
            session, payment_id=payment_id, admin_id=admin.id, reason=reason
        )
    except payment_service.PaymentError as exc:
        await state.clear()
        await message.answer(f"⚠️ {exc}")
        await _show_list(message, session, admin, 0)
        return
    await state.clear()
    await message.answer(f"⛔️ پرداخت #{payment.id} رد شد و دلیل برای مشتری ثبت می‌شود.")
    await _show_list(message, session, admin, 0)

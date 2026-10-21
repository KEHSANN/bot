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
from vpnbot.db.models.wallet import WalletTopUp
from vpnbot.services import orders as order_service
from vpnbot.services import payments as payment_service
from vpnbot.services import topups as topup_service

log = logging.getLogger(__name__)
router = Router(name="admin.payments")

METHOD_LABELS = {
    PaymentMethod.CARD: "کارت به کارت",
    PaymentMethod.CRYPTO: "رمزارز",
    PaymentMethod.WALLET: "کیف پول",
}


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
    pending_topups = await topup_service.count_pending_topups(session)
    rows.append(
        [common.btn(f"➕ شارژهای کیف پول در انتظار: {common.money(pending_topups)}", "topups", "")]
    )
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
                f"مقصد: {common.escape(str(details.get('card_number', '—')))} ({common.escape(str(details.get('recipient_name', '—')))})",
            ]
        )
    else:
        lines.extend(
            [
                f"شبکه: {common.escape(str(details.get('network', '—')))} · {common.escape(str(details.get('asset', '—')))}",
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


async def _show_topups(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, page: int
) -> None:
    total = await topup_service.count_pending_topups(session)
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    topups = await topup_service.pending_topups(
        session, offset=page * common.ADMIN_PAGE_SIZE, limit=common.ADMIN_PAGE_SIZE
    )
    rows = []
    for topup in topups:
        rows.append(
            [
                common.btn(
                    f"#{topup.id} · {common.money(topup.amount_irt)} · "
                    f"{METHOD_LABELS.get(topup.method, topup.method.value)}",
                    "topups",
                    "view",
                    topup.id,
                )
            ]
        )
    nav = common.pager("topups", "", page, total_pages)
    if nav:
        rows.append(nav)
    rows.append([common.back("payments")])
    await common.reply(
        target,
        f"<b>➕ شارژهای کیف پول در انتظار بررسی: {common.money(total)}</b>\n\n"
        "با تأیید، مبلغ دقیقاً یک‌بار به کیف پول مشتری اضافه می‌شود.",
        rows,
    )


async def _show_topup(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, topup_id: int
) -> None:
    topup = await session.get(WalletTopUp, topup_id)
    if topup is None:
        await common.reply(target, "درخواست شارژ یافت نشد.", [[common.back("topups")]])
        return
    from vpnbot.db.models.users import User

    user = await session.get(User, topup.user_id)
    user_line = "—"
    if user is not None:
        handle = f"@{user.username}" if user.username else "بدون یوزرنیم"
        user_line = f"{handle} · <code>{user.telegram_id}</code>"
    details = dict(topup.instructions_snapshot or {})
    lines = [
        f"<b>➕ شارژ کیف پول #{topup.id}</b>",
        "",
        f"مشتری: {user_line}",
        f"روش: {METHOD_LABELS.get(topup.method, topup.method.value)}",
        f"مبلغ: <b>{common.money(topup.amount_irt)} تومان</b>",
        f"وضعیت: {topup.status.value}",
        f"ارسال رسید: {common.date(topup.submitted_at)}",
    ]
    if topup.method is PaymentMethod.CARD:
        lines.append(f"مقصد: {common.escape(str(details.get('card_number', '—')))} ({common.escape(str(details.get('recipient_name', '—')))})")
    else:
        lines.extend(
            [
                f"شبکه: {common.escape(str(details.get('network', '—')))} · {common.escape(str(details.get('asset', '—')))}",
                f"TXID: <code>{common.escape(topup.txid or '—')}</code>",
            ]
        )
    if topup.receipt_file_id:
        lines.append(f"رسید: {topup.receipt_file_type or 'فایل'} پیوست شده")

    rows: list[list] = []
    if topup.status is PaymentStatus.PENDING_REVIEW:
        rows.append(
            [
                common.btn("✅ تأیید و شارژ", "topups", "approve", topup.id),
                common.btn("⛔️ رد شارژ", "topups", "reject", topup.id),
            ]
        )
    if topup.receipt_file_id and topup.receipt_file_type:
        rows.append([common.btn("👁 نمایش رسید", "topups", "receipt", topup.id)])
    rows.append([common.back("topups")])
    await common.reply(target, "\n".join(lines), rows)


@router.callback_query(AdminCB.filter(F.section == "topups"))
async def topups_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.PAYMENT_REVIEW)
    action = callback_data.action

    if action == "view":
        await _show_topup(callback, session, admin, callback_data.item_id)
        return

    if action == "receipt":
        topup = await session.get(WalletTopUp, callback_data.item_id)
        if topup is not None and topup.receipt_file_id:
            message = callback.message
            if isinstance(message, Message):
                try:
                    if (topup.receipt_file_type or "").upper() == "PHOTO":
                        await callback.bot.send_photo(message.chat.id, topup.receipt_file_id)
                    else:
                        await callback.bot.send_document(message.chat.id, topup.receipt_file_id)
                    await callback.answer("رسید ارسال شد")
                except TelegramBadRequest:
                    await callback.answer("⚠️ ارسال رسید ناموفق بود.", show_alert=True)
            else:
                await callback.answer()
        return

    if action == "approve":
        topup = await session.get(WalletTopUp, callback_data.item_id)
        if topup is None:
            await callback.answer("درخواست شارژ یافت نشد", show_alert=True)
            return
        await state.set_state(AdminStates.payment_note)
        await state.set_data({'topup_id': topup.id})
        await callback.answer()
        await common.reply(
            callback,
            f"<b>✅ تأیید شارژ #{topup.id}</b>\n\n"
            "یادداشت تأیید را بفرستید (یا برای بدون یادداشت «-» بفرستید).\n"
            f"با تأیید، {common.money(topup.amount_irt)} تومان به کیف پول مشتری اضافه می‌شود.",
            [common.cancel_row()],
        )
        return

    if action == "reject":
        topup = await session.get(WalletTopUp, callback_data.item_id)
        if topup is None:
            await callback.answer("درخواست شارژ یافت نشد", show_alert=True)
            return
        await state.set_state(AdminStates.rejection_reason)
        await state.set_data({'topup_id': topup.id})
        await callback.answer()
        await common.reply(
            callback,
            f"<b>⛔️ رد شارژ #{topup.id}</b>\n\nدلیل رد را بفرستید:",
            [common.cancel_row()],
        )
        return

    await _show_topups(callback, session, admin, callback_data.page)


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
        await state.set_data({'payment_id': payment.id})
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
        await state.set_data({'payment_id': payment.id})
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
    note = (message.text or "").strip()
    note = None if note in {"", "-"} else note[:500]

    topup_id = int(data.get("topup_id") or 0)
    if topup_id:
        try:
            topup = await topup_service.approve_topup(
                session, topup_id=topup_id, admin_id=admin.id, note=note
            )
        except topup_service.TopUpError as exc:
            await state.clear()
            await message.answer(f"⚠️ {exc}")
            await _show_topups(message, session, admin, 0)
            return
        await state.clear()
        await message.answer(
            f"✅ شارژ #{topup.id} تأیید شد و {common.money(topup.amount_irt)} تومان "
            "به کیف پول مشتری اضافه شد."
        )
        await _show_topups(message, session, admin, 0)
        return

    payment_id = int(data.get("payment_id") or 0)
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
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("دلیل رد الزامی است. برای انصراف /cancel را بفرستید.")
        return

    topup_id = int(data.get("topup_id") or 0)
    if topup_id:
        try:
            topup = await topup_service.reject_topup(
                session, topup_id=topup_id, admin_id=admin.id, reason=reason
            )
        except topup_service.TopUpError as exc:
            await state.clear()
            await message.answer(f"⚠️ {exc}")
            await _show_topups(message, session, admin, 0)
            return
        await state.clear()
        await message.answer(f"⛔️ شارژ #{topup.id} رد شد.")
        await _show_topups(message, session, admin, 0)
        return

    payment_id = int(data.get("payment_id") or 0)
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

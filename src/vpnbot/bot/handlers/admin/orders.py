"""Order administration: browse, inspect, cancel, and retry delivery.

Delivery itself is owned by the jobs worker; this screen can only *requeue* a
fulfilment job, never hand a config to the customer directly.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.bot.states import AdminStates
from vpnbot.db.enums import DeliveryStatus, JobKind, JobStatus, OrderStatus, PaymentStatus, Permission
from vpnbot.db.models.orders import Order
from vpnbot.db.models.system import Job
from vpnbot.db.models.users import Admin, User
from vpnbot.services import orders as order_service

log = logging.getLogger(__name__)
router = Router(name="admin.orders")

ORDER_STATUS_FA = {
    OrderStatus.PENDING_PAYMENT: "در انتظار پرداخت",
    OrderStatus.PAID: "پرداخت‌شده",
    OrderStatus.FULFILLMENT_PENDING: "در صف ارسال",
    OrderStatus.FULFILLED: "تحویل‌شده",
    OrderStatus.CANCELLED: "لغو‌شده",
    OrderStatus.REFUNDED: "بازگشت وجه",
}
DELIVERY_STATUS_FA = {
    DeliveryStatus.NOT_STARTED: "شروع نشده",
    DeliveryStatus.PENDING: "در انتظار",
    DeliveryStatus.SENDING: "در حال ارسال",
    DeliveryStatus.DELIVERED: "ارسال‌شده",
    DeliveryStatus.RETRY_REQUIRED: "نیازمند تلاش مجدد",
    DeliveryStatus.MANUAL_REVIEW: "بررسی دستی",
}
FILTERS = {"all": None, "open": "open", "done": OrderStatus.FULFILLED, "cancel": OrderStatus.CANCELLED}
FILTER_LABELS = {"all": "همه", "open": "باز", "done": "تحویل‌شده", "cancel": "لغو‌شده"}


def _conditions(status_key: str) -> list:
    value = FILTERS.get(status_key)
    if value is None:
        return []
    if value == "open":
        return [
            Order.status.in_(
                [OrderStatus.PENDING_PAYMENT, OrderStatus.PAID, OrderStatus.FULFILLMENT_PENDING]
            )
        ]
    return [Order.status == value]


async def _show_list(
    target: CallbackQuery | Message,
    session: AsyncSession,
    admin: Admin,
    status_key: str,
    page: int,
) -> None:
    conditions = _conditions(status_key)
    total = int(
        (
            await session.execute(
                select(func.count(Order.id)).where(*conditions) if conditions else select(func.count(Order.id))
            )
        ).scalar_one()
    )
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    stmt = select(Order).order_by(Order.created_at.desc(), Order.id.desc())
    if conditions:
        stmt = stmt.where(*conditions)
    orders = list(
        (
            await session.execute(
                stmt.offset(common.clamp_page(page) * common.ADMIN_PAGE_SIZE).limit(common.ADMIN_PAGE_SIZE)
            )
        ).scalars()
    )

    rows = [
        [
            common.btn(
                f"{order.public_id} · {order.product_title_snapshot} · "
                f"{common.money(order.total_irt)} · {ORDER_STATUS_FA[order.status]}",
                "orders",
                "view",
                order.id,
            )
        ]
        for order in orders
    ]
    nav = common.pager("orders", f"list:{status_key}", page, total_pages)
    if nav:
        rows.append(nav)
    rows.append(
        [common.btn(label, "orders", f"list:{key}") for key, label in FILTER_LABELS.items()]
    )
    rows.append([common.back()])
    await common.reply(
        target,
        f"<b>🧾 سفارش‌ها</b>\n\nفیلتر: {FILTER_LABELS.get(status_key, 'همه')} · "
        f"تعداد: <b>{common.money(total)}</b>",
        rows,
    session=session)


async def _show_order(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, order_id: int
) -> None:
    order = await order_service.get_order(session, order_id)
    if order is None:
        await common.reply(target, "سفارش یافت نشد.", [[common.back("orders")]], session=session)
        return
    user = await session.get(User, order.user_id)
    user_line = "—"
    if user is not None:
        handle = f"@{user.username}" if user.username else "بدون یوزرنیم"
        user_line = f"{handle} · <code>{user.telegram_id}</code>"

    specs = order.specs_snapshot or {}
    lines = [
        f"<b>🧾 سفارش {order.public_id}</b>",
        "",
        f"مشتری: {user_line}",
        f"محصول: {common.escape(order.product_title_snapshot)}",
        f"پلن: {common.escape(order.plan_title_snapshot or '—')}",
        f"مشخصات: {specs.get('duration_days', '—')} روز · "
        f"{specs.get('traffic_gb', '—')} GB · {specs.get('device_limit', '—')} دستگاه",
        f"مبلغ: <b>{common.money(order.total_irt)} تومان</b>",
        f"وضعیت سفارش: {ORDER_STATUS_FA[order.status]}",
        f"وضعیت ارسال: {DELIVERY_STATUS_FA[order.delivery_status]}",
        f"ثبت: {common.date(order.created_at)}",
    ]
    payment = order.payment
    if payment is not None:
        lines.append(f"پرداخت: {payment.status.value} · {payment.method.value}")
    if order.fulfilled_at:
        lines.append(f"تحویل: {common.date(order.fulfilled_at)}")
    if order.notes:
        lines.append(f"یادداشت: {common.escape(order.notes)}")

    rows: list[list] = []
    if order.status in (OrderStatus.FULFILLMENT_PENDING, OrderStatus.PAID):
        rows.append([common.btn("🔁 تلاش مجدد ارسال", "orders", "retry", order.id)])
    if order.status in (
        OrderStatus.PENDING_PAYMENT,
        OrderStatus.PAID,
        OrderStatus.FULFILLMENT_PENDING,
    ):
        # FULFILLMENT_PENDING included: a paid order stuck in the queue
        # (e.g. out of stock) is exactly the one an admin may need to
        # cancel — the service releases the reserved unit and refunds
        # wallet payments. The button used to be hidden for it.
        rows.append([common.btn("⛔️ لغو سفارش", "orders", "cancel", order.id)])
    if payment is not None and payment.status is PaymentStatus.PENDING_REVIEW:
        rows.append([common.btn("💳 بررسی پرداخت", "payments", "view", payment.id)])
    rows.append([common.back("orders")])
    await common.reply(target, "\n".join(lines), rows, session=session)


async def _requeue(session: AsyncSession, order: Order) -> bool:
    """Insert a fresh fulfilment job unless one is already pending."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    existing = (
        await session.execute(
            select(Job.id).where(
                Job.kind == JobKind.FULFILL_ORDER,
                Job.idempotency_key == f"fulfill:{order.id}",
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
    ).first()
    if existing is not None:
        return False
    # Same fixed idempotency key as everywhere else (the old
    # ``:retry:<timestamp>`` key minted a SECOND live job for the
    # same order whenever a restock requeue raced this button — two
    # executions, one config delivered twice). Upsert: revive the
    # canonical job when it is FAILED/DONE/QUEUED-in-backoff.
    from vpnbot.db.base import utcnow

    await session.execute(
        pg_insert(Job)
        .values(
            kind=JobKind.FULFILL_ORDER,
            payload={"order_id": order.id},
            idempotency_key=f"fulfill:{order.id}",
            status=JobStatus.QUEUED,
        )
        .on_conflict_do_update(
            index_elements=["idempotency_key"],
            set_={
                "status": JobStatus.QUEUED,
                "attempt": 0,
                "scheduled_at": utcnow(),
                "started_at": None,
                "finished_at": None,
                "last_error": None,
            },
            where=Job.status.in_([JobStatus.FAILED, JobStatus.DONE, JobStatus.QUEUED]),
        )
    )
    order.delivery_status = DeliveryStatus.PENDING
    await session.flush()
    return True


@router.callback_query(AdminCB.filter(F.section == "orders"))
async def orders_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.ORDER_VIEW)
    action = callback_data.action
    # Navigating in this section abandons any half-finished wizard
    # (see the note in the payments router): flow entries re-set
    # their state after this clear.
    await state.clear()

    if action == "view":
        await _show_order(callback, session, admin, callback_data.item_id)
        return

    if action == "retry":
        common.guard(admin, Permission.PAYMENT_REVIEW)
        order = await order_service.get_order(session, callback_data.item_id)
        if order is None:
            await common.alert(callback, "سفارش یافت نشد.")
            return
        queued = await _requeue(session, order)
        await common.audit(
            session,
            admin,
            "order.delivery_requeued",
            entity_type="order",
            entity_id=order.id,
            data={"queued": queued},
        )
        await callback.answer(
            "در صف قرار گرفت" if queued else "از قبل در صف است", show_alert=True
        )
        await _show_order(callback, session, admin, order.id)
        return

    if action == "cancel":
        common.guard(admin, Permission.PAYMENT_REVIEW)
        await state.set_state(AdminStates.rejection_reason)
        await state.set_data({"order_id": callback_data.item_id, "cancel_target": "order"})
        await callback.answer()
        await common.reply(
            callback,
            "⛔️ <b>لغو سفارش</b>\n\nدلیل لغو را بنویسید (در پرونده ثبت می‌شود):",
            [common.cancel_row()],
        session=session)
        return

    if action.startswith("list:"):
        await _show_list(callback, session, admin, action.split(":", 1)[1], callback_data.page)
        return

    await _show_list(callback, session, admin, "all", callback_data.page)


@router.message(AdminStates.rejection_reason, F.chat.type == "private")
async def cancel_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    data = await state.get_data()
    if data.get("cancel_target") != "order":
        from vpnbot.bot.handlers.admin.payments import reject_input

        await reject_input(message, session, admin, state)
        return

    admin = common.guard(admin, Permission.PAYMENT_REVIEW)
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("دلیل لغو الزامی است. برای انصراف /cancel را بفرستید.")
        return
    order_id = int(data.get("order_id") or 0)
    try:
        # Admin-initiated cancel IS the manual review: force lets it
        # settle orders whose delivery attempts all FAILED (e.g. the
        # customer blocked the bot) — refund without repooling the
        # pair. SENT/UNCERTAIN attempts still refuse in the service.
        order = await order_service.cancel_order(session, order_id, reason=reason[:500], force=True)
    except order_service.OrderError as exc:
        await state.clear()
        await message.answer(f"⚠️ {exc}")
        return
    await common.audit(
        session,
        admin,
        "order.cancelled",
        entity_type="order",
        entity_id=order.id,
        data={"reason": reason[:500]},
    )
    await state.clear()
    await message.answer(f"⛔️ سفارش {order.public_id} لغو شد.")
    await _show_list(message, session, admin, "all", 0)

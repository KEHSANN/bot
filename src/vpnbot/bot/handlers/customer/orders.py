"""Customer order history and order details.

Configs are never shown here: they are delivered once, in the chat, at the
moment an administrator approves the payment (``services.fulfillment``). This
screen only reports status, so a customer can see where the order stands.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.customer.common import alert, base_context, money, sender_for
from vpnbot.bot.keyboards.callbacks import ORDER_PAGE_SIZE, MenuCB, OrderCB, OrdersPageCB
from vpnbot.db.enums import OrderStatus, PaymentStatus
from vpnbot.db.models.orders import Order, Payment
from vpnbot.db.models.users import User
from vpnbot.jalali import format_jalali
from vpnbot.services import orders as order_service
from vpnbot.services.screens import STAGE_MY_ORDERS, STAGE_ORDER_DETAIL

router = Router(name="customer.orders")

ORDER_LABELS: dict[OrderStatus, str] = {
    OrderStatus.PENDING_PAYMENT: "در انتظار پرداخت",
    OrderStatus.PAID: "پرداخت شده",
    OrderStatus.FULFILLMENT_PENDING: "در حال آماده‌سازی",
    OrderStatus.FULFILLED: "تحویل شده",
    OrderStatus.CANCELLED: "لغو شده",
    OrderStatus.REFUNDED: "بازگشت وجه",
}

PAYMENT_LABELS: dict[PaymentStatus, str] = {
    PaymentStatus.AWAITING_PAYMENT_DETAILS: "در انتظار واریز",
    PaymentStatus.PENDING_REVIEW: "در انتظار بررسی پرداخت",
    PaymentStatus.APPROVED: "تأیید شده",
    PaymentStatus.REJECTED: "پرداخت رد شد",
    PaymentStatus.EXPIRED: "پرداخت منقضی شد",
    PaymentStatus.CANCELLED: "لغو شده",
}

#: While the order is still PENDING_PAYMENT the payment status is the useful one.
_MONEY_STATES = (OrderStatus.PENDING_PAYMENT,)


def status_label(order: Order) -> str:
    payment: Payment | None = order.payment
    if order.status in _MONEY_STATES and payment is not None:
        return PAYMENT_LABELS.get(payment.status, ORDER_LABELS[order.status])
    return ORDER_LABELS.get(order.status, str(order.status.value))


def _order_button(order: Order) -> InlineKeyboardButton:
    title = order.plan_title_snapshot or order.product_title_snapshot
    return InlineKeyboardButton(
        text=f"#{order.public_id} — {title} — {status_label(order)}",
        callback_data=OrderCB(order_id=order.id).pack(),
    )


async def _show_page(
    callback: CallbackQuery, session: AsyncSession, user: User | None, page: int
) -> None:
    if user is None:
        await callback.answer()
        return
    total = await order_service.count_user_orders(session, user.id)
    total_pages = max(1, (total + ORDER_PAGE_SIZE - 1) // ORDER_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)

    orders = await order_service.list_user_orders(
        session, user.id, offset=page * ORDER_PAGE_SIZE, limit=ORDER_PAGE_SIZE
    )
    rows = [[_order_button(order)] for order in orders]
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="◀️ قبلی", callback_data=OrdersPageCB(page=page - 1).pack()
                )
            )
        nav.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data=MenuCB(action="noop").pack(),
            )
        )
        if page + 1 < total_pages:
            nav.append(
                InlineKeyboardButton(
                    text="بعدی ▶️", callback_data=OrdersPageCB(page=page + 1).pack()
                )
            )
        rows.append(nav)

    await sender_for(callback.bot, session).show(
        callback,
        STAGE_MY_ORDERS,
        base_context(user, count=total),
        extra_rows=rows,
    )


@router.callback_query(MenuCB.filter(F.action == "orders"))
async def my_orders(
    callback: CallbackQuery, callback_data: MenuCB, session: AsyncSession, user: User | None
) -> None:
    await _show_page(callback, session, user, callback_data.page)


@router.callback_query(OrdersPageCB.filter())
async def orders_page(
    callback: CallbackQuery, callback_data: OrdersPageCB, session: AsyncSession, user: User | None
) -> None:
    await _show_page(callback, session, user, callback_data.page)


@router.callback_query(OrderCB.filter())
async def order_detail(
    callback: CallbackQuery, callback_data: OrderCB, session: AsyncSession, user: User | None
) -> None:
    if user is None:
        await callback.answer()
        return
    order = await order_service.user_order(session, order_id=callback_data.order_id, user_id=user.id)
    if order is None:
        await alert(callback, "این سفارش یافت نشد")
        return

    await sender_for(callback.bot, session).show(
        callback,
        STAGE_ORDER_DETAIL,
        base_context(
            user,
            order_ref=order.public_id,
            product_title=order.product_title_snapshot,
            plan_title=order.plan_title_snapshot or order.product_title_snapshot,
            price=money(order.total_irt),
            status=status_label(order),
            date=format_jalali(order.created_at, with_time=True),
        ),
    )

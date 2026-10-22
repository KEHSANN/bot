"""Customer order history, order details and config re-delivery.

The pair is delivered in the chat at approval time; the order detail also
carries a "view config / subscription" button so the customer can re-send
it to themselves later — only ever to their own chat, only for their own
fulfilled order, decrypted at tap time.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.customer.common import alert, base_context, money, sender_for
from vpnbot.bot.keyboards.callbacks import (
    ORDER_PAGE_SIZE,
    MenuCB,
    OrderCancelCB,
    OrderCB,
    OrderRevealCB,
    OrdersPageCB,
)
from vpnbot.bot.sender import send_config_pair
from vpnbot.db.enums import InventoryStatus, OrderStatus, PaymentStatus
from vpnbot.db.models.inventory import InventoryItem
from vpnbot.db.models.orders import Order, Payment
from vpnbot.db.models.users import User
from vpnbot.jalali import format_jalali
from vpnbot.services import inventory as inventory_service
from vpnbot.services import orders as order_service
from vpnbot.services.screens import STAGE_MY_ORDERS, STAGE_ORDER_DETAIL

log = logging.getLogger(__name__)
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

    extra_rows = []
    payment = order.payment
    payable = order.status is OrderStatus.PENDING_PAYMENT and (
        payment is None
        or payment.status
        in (PaymentStatus.AWAITING_PAYMENT_DETAILS, PaymentStatus.REJECTED)
    )
    if payable:
        # Unpaid or rejected-payment orders get a direct way back into
        # checkout for THIS order — before this, the customer had to
        # re-buy from the shop and the rejected order just sat there.
        # While a receipt is PENDING_REVIEW the buttons stay hidden:
        # resubmitting or cancelling mid-review is the admin's call.
        from vpnbot.bot.keyboards.callbacks import OrderCancelCB, OrderPayCB

        extra_rows.append(
            [
                InlineKeyboardButton(
                    text="💳 ادامه پرداخت / ارسال فیش",
                    callback_data=OrderPayCB(order_id=order.id).pack(),
                    style="primary",
                )
            ]
        )
        extra_rows.append(
            [
                InlineKeyboardButton(
                    text="❌ لغو سفارش",
                    callback_data=OrderCancelCB(order_id=order.id).pack(),
                    style="danger",
                )
            ]
        )
    if (
        order.status is OrderStatus.PENDING_PAYMENT
        and payment is not None
        and payment.status is PaymentStatus.CANCELLED
    ):
        # Dead order (its payment was cancelled, e.g. by a block): no
        # way to pay it, but the customer can clear it from the cap.
        from vpnbot.bot.keyboards.callbacks import OrderCancelCB

        extra_rows.append(
            [
                InlineKeyboardButton(
                    text="❌ لغو سفارش",
                    callback_data=OrderCancelCB(order_id=order.id).pack(),
                    style="danger",
                )
            ]
        )
    if order.status is OrderStatus.FULFILLED:
        extra_rows.append(
            [
                InlineKeyboardButton(
                    text="📥 مشاهده کانفیگ / لینک اشتراک",
                    callback_data=OrderRevealCB(order_id=order.id).pack(),
                    style="primary",
                )
            ]
        )

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
        extra_rows=extra_rows,
    )


@router.callback_query(OrderCancelCB.filter())
async def cancel_own_order(
    callback: CallbackQuery, callback_data: OrderCancelCB, session: AsyncSession, user: User | None
) -> None:
    if user is None:
        await callback.answer()
        return
    order = await order_service.user_order(session, order_id=callback_data.order_id, user_id=user.id)
    if order is None or order.status is not OrderStatus.PENDING_PAYMENT:
        await alert(callback, "این سفارش قابل لغو نیست")
        return
    payment = order.payment
    if payment is not None and payment.status not in (
        PaymentStatus.AWAITING_PAYMENT_DETAILS,
        PaymentStatus.REJECTED,
        PaymentStatus.CANCELLED,
    ):
        await alert(callback, "پرداخت این سفارش در حال بررسی است و قابل لغو نیست")
        return
    try:
        await order_service.cancel_order(session, order.id, reason="لغو توسط مشتری", by_customer=True)
    except order_service.OrderError as exc:
        await alert(callback, f"⚠️ {exc}")
        return
    await callback.answer("سفارش لغو شد")
    await _show_page(callback, session, user, 0)


@router.callback_query(OrderRevealCB.filter())
async def reveal_config(
    callback: CallbackQuery, callback_data: OrderRevealCB, session: AsyncSession, user: User | None
) -> None:
    """Re-send the delivered pair to the order's owner — nobody else.

    Ownership is enforced by ``user_order`` (id + user_id), the pair is
    decrypted at tap time and sent as a fresh private message in this
    chat; nothing is rendered into the shared screen message.
    """
    if user is None:
        await callback.answer()
        return
    order = await order_service.user_order(session, order_id=callback_data.order_id, user_id=user.id)
    if order is None:
        await alert(callback, "این سفارش یافت نشد")
        return
    if order.status is not OrderStatus.FULFILLED:
        await alert(callback, "این سفارش هنوز تحویل نشده است")
        return
    item = (
        await session.execute(
            select(InventoryItem)
            .where(
                InventoryItem.assigned_order_id == order.id,
                InventoryItem.status == InventoryStatus.DELIVERED,
            )
            .order_by(InventoryItem.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if item is None:
        await alert(callback, "برای این سفارش کانفیگی یافت نشد؛ با پشتیبانی در تماس باشید")
        return

    from vpnbot.config import get_settings
    from vpnbot.security.crypto import CredentialCipher

    cipher = CredentialCipher(get_settings().inventory_encryption_key)
    try:
        pair = await inventory_service.reveal_pair(session, cipher=cipher, item_id=item.id)
    except Exception:  # noqa: BLE001 - decryption/key problems must not crash the tap
        log.exception("customer reveal failed for order %s", order.id)
        await alert(callback, "نمایش کانفیگ ممکن نشد؛ با پشتیبانی در تماس باشید")
        return
    try:
        await send_config_pair(
            callback.bot,
            chat_id=user.telegram_id,
            config=pair.config,
            sub_url=pair.subscription_url,
            product_title=order.product_title_snapshot,
            order_ref=order.public_id,
        )
    except Exception:  # noqa: BLE001
        log.exception("customer reveal send failed for order %s", order.id)
        await alert(callback, "ارسال کانفیگ ناموفق بود؛ دوباره تلاش کنید")
        return
    await callback.answer("📥 کانفیگ در پیام جدیدی برایتان ارسال شد")

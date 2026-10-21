"""Checkout: create the order, show payment instructions, collect evidence.

Order creation happens here rather than in the shop so that browsing never
leaves empty orders behind. Evidence submission only moves a payment to
``PENDING_REVIEW`` — it never delivers anything; delivery is triggered solely
by an administrator's approval (see ``services.fulfillment``).
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.customer.common import alert, base_context, money, sender_for
from vpnbot.bot.keyboards.callbacks import MenuCB, OrderPayCB, ProductCB, WalletBuyCB
from vpnbot.bot.states import CheckoutStates
from vpnbot.db.enums import OrderStatus, PaymentMethod, PaymentStatus
from vpnbot.db.models.catalog import Plan
from vpnbot.db.models.orders import Order
from vpnbot.db.models.users import User
from vpnbot.money import format_irt_plain
from vpnbot.services import catalog as catalog_service
from vpnbot.services import orders as order_service
from vpnbot.services import payments as payment_service
from vpnbot.services import wallet as wallet_service
from vpnbot.services.screens import (
    STAGE_CHECKOUT,
    STAGE_MAIN_MENU,
    STAGE_PAYMENT_CARD,
    STAGE_PAYMENT_CRYPTO,
    STAGE_PAYMENT_SUBMITTED,
)

log = logging.getLogger(__name__)
router = Router(name="customer.checkout")

_METHOD_STAGE = {
    PaymentMethod.CARD: STAGE_PAYMENT_CARD,
    PaymentMethod.CRYPTO: STAGE_PAYMENT_CRYPTO,
}
_EVIDENCE_STATE = {
    PaymentMethod.CARD: CheckoutStates.awaiting_receipt,
    PaymentMethod.CRYPTO: CheckoutStates.awaiting_txid,
}


def _card_holder(details: dict) -> str:
    name = str(details.get("recipient_name") or "").strip()
    bank = str(details.get("bank_name") or "").strip()
    if name and bank:
        return f"{name} — {bank}"
    return name or bank


def _network_label(details: dict) -> str:
    asset = str(details.get("asset") or "").strip()
    network = str(details.get("network") or "").strip()
    if asset and network:
        return f"{asset} / {network}"
    return network or asset


async def _resolve_order(
    session: AsyncSession, *, user: User, plan_id: int, existing_id: int | None
) -> tuple[Order, Plan] | None:
    """Return ``(order, plan)``; reuses the in-flight order when it still matches."""
    plan = await catalog_service.get_plan(session, plan_id)
    if plan is None or not plan.is_on_sale:
        return None
    product = await catalog_service.get_product(session, plan.product_id)
    if product is None or not product.is_on_sale:
        return None

    if existing_id is not None:
        order = await order_service.user_order(session, order_id=existing_id, user_id=user.id)
        if (
            order is not None
            and order.status is OrderStatus.PENDING_PAYMENT
            and order.plan_id == plan.id
            and order.total_irt == plan.price_irt
        ):
            # The price check matters: without it, reusing an open
            # order after an admin price change would sell at the
            # order's stale snapshot price instead of the plan's
            # current one. A drifted order is left to its TTL.
            return order, plan

    try:
        created = await order_service.create_order(
            session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
        )
    except order_service.OrderError as exc:
        raise payment_service.PaymentError(str(exc)) from exc
    order = await order_service.user_order(session, order_id=created.id, user_id=user.id)
    if order is None:  # pragma: no cover - defensive, cannot happen
        raise payment_service.PaymentError("سفارش ایجاد نشد")
    return order, plan


async def _start(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
    method: PaymentMethod,
) -> None:
    if user is None or user.is_blocked:
        await alert(callback, "حساب شما فعال نیست")
        return

    data = await state.get_data()
    plan_id = data.get("plan_id")
    if not plan_id:
        await alert(callback, "لطفاً ابتدا محصول و پلن را انتخاب کنید")
        await sender_for(callback.bot, session).show(
            callback, STAGE_MAIN_MENU, base_context(user)
        )
        return

    try:
        resolved = await _resolve_order(
            session, user=user, plan_id=int(plan_id), existing_id=data.get("order_id")
        )
        if resolved is None:
            await alert(callback, "این محصول در دسترس نیست")
            return
        order, _plan = resolved
        payment = await payment_service.start_payment(session, order=order, method=method)
    except payment_service.PaymentError as exc:
        await alert(callback, str(exc))
        return

    details = dict(payment.instructions_snapshot or {})
    await state.update_data(
        order_id=order.id,
        order_ref=order.public_id,
        payment_id=payment.id,
        plan_id=order.plan_id,
    )
    await state.set_state(_EVIDENCE_STATE[method])

    context = base_context(
        user,
        order_ref=order.public_id,
        price=money(order.total_irt),
        card_number=details.get("card_number", ""),
        card_holder=_card_holder(details),
        crypto_address=details.get("address", ""),
        crypto_network=_network_label(details),
    )
    await sender_for(callback.bot, session).show(callback, _METHOD_STAGE[method], context)


@router.callback_query(MenuCB.filter(F.action == "paycard"))
async def choose_card(
    callback: CallbackQuery, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    await _start(callback, session, user, state, PaymentMethod.CARD)


@router.callback_query(WalletBuyCB.filter())
async def buy_with_wallet(
    callback: CallbackQuery,
    callback_data: WalletBuyCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
) -> None:
    """Buy-with-wallet: first tap asks, second tap charges.

    Charging is one tap away from spending real balance, so the first
    tap only shows a confirmation with the exact price and the current
    balance; the charge itself lives in
    :func:`payments.pay_order_with_wallet` (idempotent — a repeated
    confirm can never debit twice).
    """
    if user is None or user.is_blocked:
        await alert(callback, "حساب شما فعال نیست")
        return
    plan = await catalog_service.get_plan(session, callback_data.plan_id)
    if plan is None or not plan.is_on_sale:
        await alert(callback, "این پلن در دسترس نیست")
        return
    product = await catalog_service.get_product(session, plan.product_id)
    if product is None or not product.is_on_sale:
        await alert(callback, "این محصول در دسترس نیست")
        return
    if await catalog_service.stock_for_plan(session, product.id, plan.id) <= 0:
        await alert(callback, "موجودی این پلن تمام شده است")
        return
    balance = await wallet_service.balance(session, user.id)

    if not callback_data.confirm:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        if balance < plan.price_irt:
            await alert(
                callback,
                f"موجودی کیف پول کافی نیست\nموجودی: {format_irt_plain(balance)} تومان\n"
                f"قیمت: {format_irt_plain(plan.price_irt)} تومان\n"
                "از بخش کیف پول می‌توانید موجودی را افزایش دهید.",
            )
            return
        # Create (or reuse) the pending order NOW and bind the confirm
        # button to it. Previously the order was only created at charge
        # time, so every confirm tap minted a fresh order and charged
        # it — a double-tap meant paying twice.
        from sqlalchemy import select

        from vpnbot.db.models.orders import Order, Payment

        open_order = (
            await session.execute(
                select(Order)
                .outerjoin(Payment, Payment.order_id == Order.id)
                .where(
                    Order.user_id == user.id,
                    Order.plan_id == plan.id,
                    Order.status == OrderStatus.PENDING_PAYMENT,
                    # Orders minted by THIS wallet-ask flow have no
                    # Payment row yet — an inner join here made them
                    # invisible, so every ask minted another order
                    # until the customer hit the open-order cap with
                    # three phantom orders. An order whose receipt is
                    # PENDING_REVIEW belongs to the admin's queue and
                    # is still excluded.
                    (Payment.id.is_(None))
                    | (
                        Payment.status.in_(
                            [PaymentStatus.AWAITING_PAYMENT_DETAILS, PaymentStatus.REJECTED]
                        )
                    ),
                )
                .order_by(Order.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        try:
            resolved = await _resolve_order(
                session, user=user, plan_id=plan.id,
                existing_id=open_order.id if open_order else None,
            )
        except payment_service.PaymentError as exc:
            await alert(callback, f"⚠️ {exc}")
            return
        if resolved is None:
            await alert(callback, "این محصول در دسترس نیست")
            return
        order, _plan = resolved
        # A fresh wallet preview supersedes the customer's other
        # payment-less previews, so browsing prices can never exhaust
        # the open-order cap on its own.
        await order_service.cancel_preview_orders(session, user_id=user.id, keep_order_id=order.id)
        # Bind the order in FSM state too (not only in the confirm
        # button): /cancel and the menu cancel can then cancel this
        # exact order instead of leaving it to the TTL sweep.
        await state.update_data(order_id=order.id, plan_id=plan.id, order_ref=order.public_id)
        await callback.answer()
        message = callback.message
        if isinstance(message, Message):
            await message.answer(
                "👛 <b>خرید با کیف پول</b>\n\n"
                f"محصول: {html.escape(product.title)}\n"
                f"پلن: {html.escape(plan.title)}\n"
                f"مبلغ: <b>{format_irt_plain(plan.price_irt)} تومان</b>\n"
                f"موجودی فعلی: {format_irt_plain(balance)} تومان\n"
                f"موجودی بعد از خرید: {format_irt_plain(balance - plan.price_irt)} تومان\n\n"
                "پرداخت انجام شود؟",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ تأیید و پرداخت",
                                callback_data=WalletBuyCB(
                                    plan_id=plan.id, confirm=1, order_id=order.id
                                ).pack(),
                                style="success",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🔙 بازگشت",
                                callback_data=ProductCB(product_id=product.id).pack(),
                            )
                        ],
                    ]
                ),
            )
        return

    # Confirmed: charge exactly the order bound at the ask step.
    import contextlib

    message = callback.message
    if isinstance(message, Message):
        with contextlib.suppress(Exception):
            await message.edit_reply_markup(reply_markup=None)  # disarm the button
    order = await order_service.user_order(
        session, order_id=callback_data.order_id, user_id=user.id
    )
    if order is None or order.plan_id != plan.id or order.total_irt != plan.price_irt:
        # The price check matters here (not only in _resolve_order):
        # this confirm can be tapped on an OLD message after an admin
        # price change, and the order still carries its snapshot price.
        await alert(callback, "مشخصات یا قیمت این سفارش تغییر کرده است؛ لطفاً دوباره اقدام کنید")
        return
    if order.payment is not None and order.payment.status is PaymentStatus.APPROVED:
        await alert(callback, "این سفارش قبلاً پرداخت شده است")
        return
    try:
        await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    except payment_service.PaymentError as exc:
        await alert(callback, f"⚠️ {exc}")
        return
    await state.clear()
    await callback.answer("✅ پرداخت با کیف پول انجام شد")
    if isinstance(message, Message):
        new_balance = await wallet_service.balance(session, user.id)
        await message.answer(
            "✅ <b>خرید با کیف پول انجام شد</b>\n\n"
            f"سفارش: <code>{order.public_id}</code>\n"
            f"مبلغ پرداختی: {format_irt_plain(order.total_irt)} تومان\n"
            f"موجودی باقی‌مانده: {format_irt_plain(new_balance)} تومان\n\n"
            "کانفیگ شما تا لحظاتی دیگر در همین گفتگو ارسال می‌شود."
        )


@router.callback_query(OrderPayCB.filter())
async def resume_checkout(
    callback: CallbackQuery,
    callback_data: OrderPayCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
) -> None:
    """Re-enter checkout bound to an existing unpaid order.

    Used from the order detail screen after a rejection: the FSM is
    pointed at *this* order, so choosing a payment method reuses it
    (start_payment resets a REJECTED payment) instead of minting a new
    order and orphaning the rejected one.
    """
    if user is None or user.is_blocked:
        await alert(callback, "حساب شما فعال نیست")
        return
    order = await order_service.user_order(session, order_id=callback_data.order_id, user_id=user.id)
    if order is None or order.status is not OrderStatus.PENDING_PAYMENT:
        await alert(callback, "این سفارش قابل پرداخت نیست")
        return
    if order.payment is not None and order.payment.status is PaymentStatus.PENDING_REVIEW:
        await alert(callback, "فیش این سفارش در حال بررسی است؛ منتظر نتیجه بمانید")
        return
    plan = await catalog_service.get_plan(session, order.plan_id)
    if plan is None or not plan.is_on_sale:
        await alert(callback, "این پلن دیگر در دسترس نیست")
        return
    if plan.price_irt != order.total_irt:
        # The checkout screen would show the order's snapshot price
        # while the flow underneath charges the plan's current one.
        # Refuse the stale resume; a fresh buy shows the real price.
        await alert(
            callback,
            "قیمت این پلن تغییر کرده است؛ لطفاً از فروشگاه دوباره خرید کنید",
        )
        return
    await state.clear()
    await state.update_data(order_id=order.id, plan_id=order.plan_id, order_ref=order.public_id)
    await callback.answer()
    await sender_for(callback.bot, session).show(
        callback,
        STAGE_CHECKOUT,
        base_context(
            user,
            product_title=order.product_title_snapshot,
            plan_title=order.plan_title_snapshot or order.product_title_snapshot,
            price=money(order.total_irt),
        ),
    )
    # The resume screen is screen-driven (card/crypto buttons); the
    # wallet option is code-side, so offer it as a follow-up message
    # bound to this plan (the ask flow reuses this very order).
    from vpnbot.bot.handlers.customer.shop import _wallet_buy_row

    wallet_row = await _wallet_buy_row(session, user, plan.id)
    message = callback.message
    if wallet_row is not None and isinstance(message, Message):
        await message.answer(
            "یا همین سفارش را با کیف پول پرداخت کنید:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[wallet_row]),
        )


@router.callback_query(MenuCB.filter(F.action == "paycrypto"))
async def choose_crypto(
    callback: CallbackQuery, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    await _start(callback, session, user, state, PaymentMethod.CRYPTO)


async def _finish(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext, success_text: str
) -> None:
    data = await state.get_data()
    await state.clear()
    await message.answer(success_text)
    if user is None:
        return
    await sender_for(message.bot, session).show(
        message,
        STAGE_PAYMENT_SUBMITTED,
        base_context(user, order_ref=data.get("order_ref", "")),
        log_outcome=False,
    )


@router.message(CheckoutStates.awaiting_receipt, F.photo | F.document)
async def card_receipt(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    if user is None:
        return
    data = await state.get_data()
    payment_id = data.get("payment_id")
    if not payment_id:
        await state.clear()
        return

    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    elif message.document:
        file_id, file_type = message.document.file_id, "document"
    else:  # pragma: no cover - filter guarantees one of the two
        return

    try:
        await payment_service.submit_card_receipt(
            session, payment_id=int(payment_id), user_id=user.id, file_id=file_id, file_type=file_type
        )
    except payment_service.PaymentError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await _finish(message, session, user, state, "📎 رسید دریافت شد.")


@router.message(CheckoutStates.awaiting_receipt)
async def card_receipt_wrong_type(message: Message) -> None:
    await message.answer("⚠️ لطفاً تصویر رسید پرداخت را ارسال کنید (عکس یا فایل).")


@router.message(CheckoutStates.awaiting_txid, F.photo | F.document)
async def crypto_evidence_file(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    txid = (message.caption or "").strip()
    if not txid:
        await message.answer("⚠️ هش تراکنش (TXID) را در توضیح (کپشن) همین فایل بنویسید.")
        return
    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    else:
        file_id, file_type = message.document.file_id, "document"
    await _submit_crypto(message, session, user, state, txid, file_id, file_type)


@router.message(CheckoutStates.awaiting_txid, F.text)
async def crypto_evidence_text(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    await _submit_crypto(message, session, user, state, (message.text or "").strip(), None, None)


async def _submit_crypto(
    message: Message,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
    txid: str,
    file_id: str | None,
    file_type: str | None,
) -> None:
    if user is None:
        return
    data = await state.get_data()
    payment_id = data.get("payment_id")
    if not payment_id:
        await state.clear()
        return
    if not txid:
        await message.answer("⚠️ هش تراکنش را وارد کنید.")
        return

    try:
        await payment_service.submit_crypto_evidence(
            session,
            payment_id=int(payment_id),
            user_id=user.id,
            txid=txid,
            file_id=file_id,
            file_type=file_type,
        )
    except payment_service.PaymentError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await _finish(message, session, user, state, "✅ هش تراکنش ثبت شد.")

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
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.customer.common import alert, base_context, money, sender_for
from vpnbot.bot.keyboards.callbacks import MenuCB, ProductCB, WalletBuyCB
from vpnbot.bot.states import CheckoutStates
from vpnbot.db.enums import OrderStatus, PaymentMethod
from vpnbot.db.models.catalog import Plan
from vpnbot.db.models.orders import Order
from vpnbot.db.models.users import User
from vpnbot.money import format_irt_plain
from vpnbot.services import catalog as catalog_service
from vpnbot.services import orders as order_service
from vpnbot.services import payments as payment_service
from vpnbot.services import wallet as wallet_service
from vpnbot.services.screens import (
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
        if order is not None and order.status is OrderStatus.PENDING_PAYMENT and order.plan_id == plan.id:
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
                                callback_data=WalletBuyCB(plan_id=plan.id, confirm=1).pack(),
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

    # Confirmed: resolve (or create) the pending order and charge it.
    data = await state.get_data()
    try:
        resolved = await _resolve_order(
            session, user=user, plan_id=plan.id, existing_id=data.get("order_id")
        )
        if resolved is None:
            await alert(callback, "این محصول در دسترس نیست")
            return
        order, _plan = resolved
        await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    except payment_service.PaymentError as exc:
        await alert(callback, f"⚠️ {exc}")
        return
    await state.clear()
    await callback.answer("✅ پرداخت با کیف پول انجام شد")
    message = callback.message
    if isinstance(message, Message):
        new_balance = await wallet_service.balance(session, user.id)
        await message.answer(
            "✅ <b>خرید با کیف پول انجام شد</b>\n\n"
            f"سفارش: <code>{order.public_id}</code>\n"
            f"مبلغ پرداختی: {format_irt_plain(order.total_irt)} تومان\n"
            f"موجودی باقی‌مانده: {format_irt_plain(new_balance)} تومان\n\n"
            "کانفیگ شما تا لحظاتی دیگر در همین گفتگو ارسال می‌شود."
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

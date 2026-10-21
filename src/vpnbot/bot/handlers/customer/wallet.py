"""Customer wallet: balance, recent ledger movements and top-up.

Top-up mirrors the order payment flow on purpose — enter an amount
(admin-configured minimum), pick card or crypto, send the receipt/TXID,
and an admin reviews it in the payments panel. Approval credits the
wallet exactly once (ledger idempotency key ``topup:<id>``).
"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.customer.common import alert, base_context, money, sender_for
from vpnbot.bot.keyboards.callbacks import MenuCB
from vpnbot.bot.states import WalletStates
from vpnbot.db.enums import LedgerKind, PaymentMethod
from vpnbot.db.models.users import User
from vpnbot.jalali import format_jalali
from vpnbot.money import MoneyError, format_irt_plain, parse_amount_irt
from vpnbot.services import topups as topup_service
from vpnbot.services import wallet as wallet_service
from vpnbot.services.screens import STAGE_WALLET

router = Router(name="customer.wallet")

KIND_LABELS: dict[LedgerKind, str] = {
    LedgerKind.TOPUP: "شارژ کیف پول",
    LedgerKind.PURCHASE: "خرید",
    LedgerKind.REFUND: "بازگشت وجه",
    LedgerKind.REFERRAL_REWARD: "پاداش دعوت",
    LedgerKind.ADMIN_ADJUSTMENT: "تعدیل توسط پشتیبانی",
}

HISTORY_LIMIT = 5


def _history_block(entries: list) -> str:
    """Plain text block for ``{history}``; the renderer escapes it as a value."""
    if not entries:
        return ""
    lines = ["", "", "آخرین تراکنش‌ها:"]
    for entry in entries:
        label = KIND_LABELS.get(entry.kind, str(entry.kind.value))
        sign = "+" if entry.amount_irt > 0 else "−"
        amount = money(abs(entry.amount_irt))
        lines.append(f"• {format_jalali(entry.created_at)} — {label}: {sign}{amount} تومان")
    return "\n".join(lines)


async def _show_wallet(target: CallbackQuery | Message, session: AsyncSession, user: User) -> None:
    balance = await wallet_service.balance(session, user.id)
    entries = await wallet_service.history(session, user.id, limit=HISTORY_LIMIT)
    rows = []
    if await topup_service.topup_enabled(session):
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ افزایش موجودی",
                    callback_data=MenuCB(action="topup").pack(),
                    style="success",
                )
            ]
        )
    await sender_for(target.bot, session).show(
        target,
        STAGE_WALLET,
        base_context(user, balance=money(balance), history=_history_block(entries)),
        extra_rows=rows,
    )


@router.callback_query(MenuCB.filter(F.action == "wallet"))
async def open_wallet(
    callback: CallbackQuery, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    if user is None:
        await callback.answer()
        return
    # Opening the wallet abandons any half-finished top-up draft.
    await state.clear()
    await _show_wallet(callback, session, user)


# --------------------------------------------------------------------------
# Top-up flow
# --------------------------------------------------------------------------


@router.callback_query(MenuCB.filter(F.action == "topup"))
async def topup_start(
    callback: CallbackQuery, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    if user is None or user.is_blocked:
        await alert(callback, "حساب شما فعال نیست")
        return
    if not await topup_service.topup_enabled(session):
        await alert(callback, "شارژ کیف پول در حال حاضر غیرفعال است")
        return
    minimum = await topup_service.min_topup_irt(session)
    await state.set_state(WalletStates.entering_amount)
    await callback.answer()
    message = callback.message
    if isinstance(message, Message):
        await message.answer(
            "➕ <b>افزایش موجودی کیف پول</b>\n\n"
            f"مبلغ شارژ را به تومان بفرستید (حداقل {format_irt_plain(minimum)} تومان).\n"
            "برای انصراف /start را بفرستید."
        )


@router.message(WalletStates.entering_amount, F.text)
async def topup_amount(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    if user is None:
        return
    try:
        amount = parse_amount_irt(message.text or "")
    except MoneyError:
        await message.answer("⚠️ مبلغ معتبر نیست؛ یک عدد به تومان بفرستید (مثلاً 50000).")
        return
    minimum = await topup_service.min_topup_irt(session)
    if amount < minimum:
        await message.answer(
            f"⚠️ حداقل مبلغ شارژ {format_irt_plain(minimum)} تومان است؛ مبلغ بیشتری بفرستید."
        )
        return
    await state.update_data(amount_irt=amount)
    await state.set_state(WalletStates.choosing_method)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 کارت به کارت", callback_data=MenuCB(action="topup_card").pack()
                ),
                InlineKeyboardButton(
                    text="₿ رمزارز", callback_data=MenuCB(action="topup_crypto").pack()
                ),
            ],
            [InlineKeyboardButton(text="👛 بازگشت", callback_data=MenuCB(action="wallet").pack())],
        ]
    )
    await message.answer(
        f"مبلغ شارژ: <b>{format_irt_plain(amount)} تومان</b>\nروش پرداخت را انتخاب کنید:",
        reply_markup=markup,
    )


@router.callback_query(MenuCB.filter(F.action.in_({"topup_card", "topup_crypto"})))
async def topup_method(
    callback: CallbackQuery,
    callback_data: MenuCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
) -> None:
    if user is None or user.is_blocked:
        await alert(callback, "حساب شما فعال نیست")
        return
    data = await state.get_data()
    amount = int(data.get("amount_irt") or 0)
    if amount <= 0:
        await alert(callback, "مبلغ شارژ مشخص نیست؛ دوباره از کیف پول شروع کنید")
        await state.clear()
        return
    method = PaymentMethod.CARD if callback_data.action == "topup_card" else PaymentMethod.CRYPTO
    try:
        topup = await topup_service.create_topup(
            session, user_id=user.id, amount_irt=amount, method=method
        )
    except topup_service.TopUpError as exc:
        await alert(callback, f"⚠️ {exc}")
        return

    details = topup.instructions_snapshot or {}
    await state.update_data(topup_id=topup.id)
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return
    if method is PaymentMethod.CARD:
        await state.set_state(WalletStates.awaiting_receipt)
        await callback.answer()
        await message.answer(
            "💳 <b>کارت به کارت</b>\n\n"
            f"مبلغ: <b>{format_irt_plain(amount)} تومان</b>\n"
            f"به نام: {html.escape(str(details.get('recipient_name', '—')))}\n"
            f"شماره کارت: <code>{html.escape(str(details.get('card_number', '—')))}</code>\n"
            f"بانک: {html.escape(str(details.get('bank_name', '—')))}\n\n"
            f"{html.escape(str(details.get('instructions', '')))}\n\n"
            "پس از واریز، تصویر یا فایل رسید را همینجا بفرستید."
        )
    else:
        await state.set_state(WalletStates.awaiting_txid)
        await callback.answer()
        await message.answer(
            "₿ <b>پرداخت رمزارز</b>\n\n"
            f"مبلغ شارژ: <b>{format_irt_plain(amount)} تومان</b>\n"
            f"دارایی: {html.escape(str(details.get('asset', 'USDT')))} · شبکه: {html.escape(str(details.get('network', '—')))}\n"
            f"آدرس: <code>{html.escape(str(details.get('address', '—')))}</code>\n\n"
            f"{html.escape(str(details.get('instructions', '')))}\n\n"
            "پس از انتقال، هش تراکنش (TXID) را به‌صورت متن بفرستید."
        )


@router.message(WalletStates.awaiting_receipt, F.photo | F.document)
async def topup_receipt(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    if user is None:
        return
    data = await state.get_data()
    topup_id = int(data.get("topup_id") or 0)
    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    elif message.document is not None:
        file_id, file_type = message.document.file_id, "document"
    else:  # pragma: no cover - the filter guarantees photo or document
        return
    try:
        await topup_service.submit_card_receipt(
            session, topup_id=topup_id, user_id=user.id, file_id=file_id, file_type=file_type
        )
    except topup_service.TopUpError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    await message.answer(
        "✅ رسید شارژ ثبت شد.\n"
        "پس از بررسی و تأیید پشتیبانی، مبلغ به موجودی کیف پول شما اضافه می‌شود."
    )
    await _show_wallet(message, session, user)


@router.message(WalletStates.awaiting_receipt, F.text)
async def topup_receipt_wrong_type(message: Message) -> None:
    await message.answer("لطفاً رسید را به‌صورت عکس یا فایل بفرستید، نه متن.")


@router.message(WalletStates.awaiting_txid, F.photo | F.document)
async def topup_txid_file(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    """Receipt image with the TXID in its caption — the instructions
    tell the customer to send exactly that, but only F.text was
    registered here, so the bot silently ignored the photo (the order
    checkout flow accepted it all along)."""
    txid = (message.caption or "").strip()
    if not txid:
        await message.answer("⚠️ هش تراکنش (TXID) را در توضیح (کپشن) همین فایل بنویسید.")
        return
    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    else:
        file_id, file_type = message.document.file_id, "document"
    await _submit_topup_txid(message, session, user, state, txid, file_id, file_type)


@router.message(WalletStates.awaiting_txid, F.text)
async def topup_txid(
    message: Message, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    await _submit_topup_txid(
        message, session, user, state, (message.text or "").strip(), None, None
    )


async def _submit_topup_txid(
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
    topup_id = int(data.get("topup_id") or 0)
    try:
        await topup_service.submit_crypto_evidence(
            session, topup_id=topup_id, user_id=user.id, txid=txid,
            file_id=file_id, file_type=file_type,
        )
    except topup_service.TopUpError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    await message.answer(
        "✅ هش تراکنش ثبت شد.\n"
        "پس از بررسی و تأیید پشتیبانی، مبلغ به موجودی کیف پول شما اضافه می‌شود."
    )
    await _show_wallet(message, session, user)

"""Wallet administration: look up a customer, inspect the ledger, adjust balance.

Adjustments are ordinary ledger entries (``ADMIN_ADJUSTMENT``) with the admin
recorded as actor — never a direct write to ``wallet_accounts``. That keeps the
append-only history complete: every balance change can be traced to a row.

The flow is deliberately two-step (amount, then reason) so the audit entry
always carries a human explanation.
"""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.bot.states import AdminStates
from vpnbot.db.enums import LedgerKind, Permission
from vpnbot.db.models.users import Admin, User
from vpnbot.db.models.wallet import LedgerEntry, WalletAccount
from vpnbot.money import MoneyError, parse_amount_irt
from vpnbot.services import wallet as wallet_service

router = Router(name="admin.wallet")

HISTORY_LIMIT = 8
KIND_LABELS = {
    LedgerKind.TOPUP: "شارژ",
    LedgerKind.PURCHASE: "خرید",
    LedgerKind.REFUND: "بازگشت وجه",
    LedgerKind.REFERRAL_REWARD: "پاداش دعوت",
    LedgerKind.ADMIN_ADJUSTMENT: "اصلاح مدیر",
}
DIRECTIONS = {"in": "افزایش", "out": "کاهش"}


async def _find_user(session: AsyncSession, raw: str) -> User | None:
    text = raw.strip().lstrip("@")
    if text.isdigit():
        return (
            await session.execute(select(User).where(User.telegram_id == int(text)))
        ).scalar_one_or_none()
    return (
        await session.execute(select(User).where(func.lower(User.username) == text.lower()))
    ).scalar_one_or_none()


async def _show_menu(target: CallbackQuery | Message, session: AsyncSession, page: int) -> None:
    total = int((await session.execute(select(func.count(WalletAccount.id)))).scalar_one())
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    accounts = list(
        (
            await session.execute(
                select(WalletAccount)
                .order_by(WalletAccount.balance_irt.desc(), WalletAccount.id)
                .offset(page * common.ADMIN_PAGE_SIZE)
                .limit(common.ADMIN_PAGE_SIZE)
            )
        ).scalars()
    )
    users = {
        account.id: user
        for account in accounts
        if (user := await session.get(User, account.user_id)) is not None
    }

    rows = []
    for account in accounts:
        user = users.get(account.id)
        label = common.escape(user.display_name[:28]) if user else f"#{account.user_id}"
        rows.append(
            [
                common.btn(
                    f"👛 {label} — {common.money(account.balance_irt)}",
                    "wallet",
                    "user",
                    account.user_id,
                )
            ]
        )
    nav = common.pager("wallet", "", page, total_pages)
    if nav:
        rows.append(nav)
    rows.append([common.btn("🔍 جستجوی کاربر", "wallet", "find")])
    rows.append([common.back()])
    await common.reply(
        target,
        f"<b>👛 کیف پول‌ها</b>\n\n"
        f"تعداد حساب‌ها: {common.money(total)}\n"
        "برای تنظیم موجودی، کاربر را انتخاب کنید یا جستجو کنید.",
        rows,
    )


async def _show_user(
    target: CallbackQuery | Message, session: AsyncSession, user_id: int
) -> None:
    user = await session.get(User, user_id)
    if user is None:
        await common.reply(target, "کاربر یافت نشد.", [[common.back("wallet")]])
        return
    balance = await wallet_service.balance(session, user_id)
    entries = await wallet_service.history(session, user_id, limit=HISTORY_LIMIT)

    lines = [
        f"<b>👛 {common.escape(user.display_name)}</b>",
        "",
        f"شناسه تلگرام: <code>{user.telegram_id}</code>",
        f"نام کاربری: {'@' + user.username if user.username else '—'}",
        f"موجودی: <b>{common.money(balance)} تومان</b>",
        "",
        "<b>آخرین تراکنش‌ها:</b>",
    ]
    if entries:
        for entry in entries:
            sign = "+" if entry.amount_irt > 0 else "−"
            lines.append(
                f"• {common.date(entry.created_at, with_time=False)} · "
                f"{KIND_LABELS.get(entry.kind, entry.kind.value)}: "
                f"{sign}{common.money(abs(entry.amount_irt))}"
                + (f" — {common.escape(entry.reason[:40])}" if entry.reason else "")
            )
    else:
        lines.append("— تراکنشی ثبت نشده است.")

    rows = [
        [
            common.btn("➕ افزایش موجودی", "wallet", "in", user_id),
            common.btn("➖ کاهش موجودی", "wallet", "out", user_id),
        ],
        [common.btn("🔄 بروزرسانی", "wallet", "user", user_id)],
        [common.back("wallet")],
    ]
    await common.reply(target, "\n".join(lines), rows)


@router.callback_query(AdminCB.filter(F.section == "wallet"))
async def wallet_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.WALLET_ADJUST)
    action = callback_data.action

    if action == "find":
        await state.set_state(AdminStates.wallet_adjust)
        await state.update_data(wallet_stage="lookup")
        await callback.answer()
        await common.reply(
            callback,
            "<b>🔍 جستجوی کاربر</b>\n\n"
            "شناسه عددی تلگرام یا نام کاربری (بدون @) را بفرستید.",
            [common.cancel_row()],
        )
        return

    if action == "user":
        await _show_user(callback, session, callback_data.item_id)
        return

    if action in ("in", "out"):
        user_id = callback_data.item_id
        user = await session.get(User, user_id)
        if user is None:
            await common.alert(callback, "کاربر یافت نشد.")
            return
        balance = await wallet_service.balance(session, user_id)
        await state.set_state(AdminStates.wallet_adjust)
        await state.update_data(
            wallet_stage="amount",
            wallet_user_id=user_id,
            wallet_direction=action,
        )
        await callback.answer()
        verb = "افزایش" if action == "in" else "کاهش"
        await common.reply(
            callback,
            f"<b>{'➕' if action == 'in' else '➖'} {verb} موجودی</b>\n\n"
            f"کاربر: {common.escape(user.display_name)}\n"
            f"موجودی فعلی: <b>{common.money(balance)} تومان</b>\n\n"
            "مبلغ به تومان را بفرستید (مثلاً 50000).",
            [common.cancel_row()],
        )
        return

    if action == "adjust":
        await _apply_adjustment(callback, session, admin, state)
        return

    await _show_menu(callback, session, callback_data.page)


async def _apply_adjustment(
    callback: CallbackQuery, session: AsyncSession, admin: Admin, state: FSMContext
) -> None:
    data = await state.get_data()
    user_id = int(data.get("wallet_user_id") or 0)
    direction = str(data.get("wallet_direction") or "in")
    amount = int(data.get("wallet_amount") or 0)
    reason = str(data.get("wallet_reason") or "").strip()

    if not user_id or amount <= 0:
        await state.clear()
        await common.alert(callback, "درخواست منقضی شده است؛ دوباره شروع کنید.")
        return
    user = await session.get(User, user_id)
    if user is None:
        await state.clear()
        await common.alert(callback, "کاربر یافت نشد.")
        return

    signed = amount if direction == "in" else -amount
    key = f"adminadj:{admin.id}:{uuid.uuid4().hex}"
    try:
        result = await wallet_service.apply(
            session,
            user_id=user_id,
            amount_irt=signed,
            kind=LedgerKind.ADMIN_ADJUSTMENT,
            idempotency_key=key,
            reason=reason or None,
            ref_type="admin",
            ref_id=str(admin.id),
            actor_type="ADMIN",
            actor_id=admin.id,
        )
    except wallet_service.WalletError as exc:
        await common.alert(callback, f"⚠️ {exc}")
        await state.clear()
        await _show_user(callback, session, user_id)
        return

    await common.audit(
        session,
        admin,
        "wallet.adjusted",
        entity_type="user",
        entity_id=user_id,
        data={"amount_irt": signed, "reason": reason, "ledger_id": result.entry.id},
    )
    await state.clear()
    await callback.answer("ثبت شد")
    await common.reply(
        callback,
        f"<b>✅ انجام شد</b>\n\n"
        f"کاربر: {common.escape(user.display_name)}\n"
        f"{'افزایش' if signed > 0 else 'کاهش'}: <b>{common.money(abs(signed))} تومان</b>\n"
        f"موجودی جدید: <b>{common.money(result.balance_irt)} تومان</b>",
        [[common.btn("👛 مشاهده کاربر", "wallet", "user", user_id)], [common.back("wallet")]],
    )


@router.message(AdminStates.wallet_adjust, F.chat.type == "private")
async def wallet_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    admin = common.guard(admin, Permission.WALLET_ADJUST)
    data = await state.get_data()
    stage = str(data.get("wallet_stage") or "")
    raw = (message.text or "").strip()

    if stage == "lookup":
        user = await _find_user(session, raw)
        if user is None:
            await message.answer("کاربر یافت نشد. شناسه یا نام کاربری دیگری بفرستید یا /cancel.")
            return
        await state.clear()
        await _show_user(message, session, user.id)
        return

    if stage == "amount":
        try:
            amount = parse_amount_irt(raw)
        except MoneyError as exc:
            await message.answer(f"⚠️ {exc}\n\nدوباره بفرستید یا /cancel را بزنید.")
            return
        if amount <= 0:
            await message.answer("مبلغ باید بزرگ‌تر از صفر باشد. دوباره بفرستید یا /cancel.")
            return
        user_id = int(data.get("wallet_user_id") or 0)
        direction = str(data.get("wallet_direction") or "in")
        if direction == "out" and amount > await wallet_service.balance(session, user_id):
            await message.answer("موجودی کافی نیست. مبلغ کمتری بفرستید یا /cancel.")
            return
        await state.update_data(wallet_stage="reason", wallet_amount=amount)
        rows = [[common.btn("⏭ بدون توضیح", "wallet", "adjust")]]
        from aiogram.types import InlineKeyboardMarkup

        await message.answer(
            f"<b>📝 توضیح تراکنش</b>\n\n"
            f"مبلغ: <b>{common.money(amount)} تومان</b>\n\n"
            "دلیل این تغییر را بنویسید (در گزارش فعالیت ثبت می‌شود) یا دکمه زیر را بزنید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return

    if stage == "reason":
        await state.update_data(wallet_reason=raw[:400])
        user_id = int(data.get("wallet_user_id") or 0)
        direction = str(data.get("wallet_direction") or "in")
        amount = int(data.get("wallet_amount") or 0)
        user = await session.get(User, user_id)
        balance = await wallet_service.balance(session, user_id)
        name = common.escape(user.display_name) if user else "—"
        from aiogram.types import InlineKeyboardMarkup

        await message.answer(
            f"<b>تأیید تغییر موجودی</b>\n\n"
            f"کاربر: {name}\n"
            f"نوع: {DIRECTIONS[direction]}\n"
            f"مبلغ: <b>{common.money(amount)} تومان</b>\n"
            f"موجودی فعلی: {common.money(balance)} تومان\n"
            f"توضیح: {common.escape(raw[:200])}\n\n"
            "ثبت شود؟",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[common.yes_no("wallet", "adjust", user_id)]
            ),
        )
        return

    await state.clear()
    await message.answer("جلسه منقضی شده است. از منوی پنل دوباره شروع کنید.")


__all__ = ["router", "LedgerEntry"]

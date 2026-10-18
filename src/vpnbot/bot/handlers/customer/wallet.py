"""Customer wallet: balance and recent ledger movements."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.customer.common import base_context, money, sender_for
from vpnbot.bot.keyboards.callbacks import MenuCB
from vpnbot.db.enums import LedgerKind
from vpnbot.db.models.users import User
from vpnbot.jalali import format_jalali
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


@router.callback_query(MenuCB.filter(F.action == "wallet"))
async def open_wallet(
    callback: CallbackQuery, session: AsyncSession, user: User | None
) -> None:
    if user is None:
        await callback.answer()
        return
    balance = await wallet_service.balance(session, user.id)
    entries = await wallet_service.history(session, user.id, limit=HISTORY_LIMIT)
    await sender_for(callback.bot, session).show(
        callback,
        STAGE_WALLET,
        base_context(user, balance=money(balance), history=_history_block(entries)),
    )

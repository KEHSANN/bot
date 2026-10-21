"""Callback payloads and inline keyboard construction.

Callback data is compact (Telegram caps it at 64 bytes) and typed via aiogram's
``CallbackData`` factories so a malformed/forged payload cannot reach a handler
with unexpected types.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from vpnbot.services.screens import RenderedScreen


class MenuCB(CallbackData, prefix="m"):
    """Generic navigation action: home, shop, orders, wallet, ..."""

    action: str
    page: int = 0


class CategoryCB(CallbackData, prefix="c"):
    category_id: int
    page: int = 0


class ProductCB(CallbackData, prefix="p"):
    product_id: int
    page: int = 0


class PlanCB(CallbackData, prefix="pl"):
    plan_id: int


class BuyCB(CallbackData, prefix="b"):
    plan_id: int


class PayCB(CallbackData, prefix="py"):
    order_id: int
    method: str  # CARD | CRYPTO


class OrderCB(CallbackData, prefix="o"):
    order_id: int


class OrderRevealCB(CallbackData, prefix="or"):
    """Ask the bot to re-send this order's delivered config / sub link."""

    order_id: int


class OrderPayCB(CallbackData, prefix="opay"):
    """Re-enter checkout for an existing unpaid order (e.g. after the
    payment was rejected) so the receipt can be submitted again."""

    order_id: int


class OrderCancelCB(CallbackData, prefix="ocx"):
    """Customer cancels their own unpaid order from the detail screen."""

    order_id: int


class WalletBuyCB(CallbackData, prefix="wb"):
    """Buy this plan with the wallet balance. ``confirm`` 0 asks first.

    The confirmation carries the concrete ``order_id`` created at the
    ask step, so the charge step pays *that* order — a double-tap on
    «تأیید و پرداخت» hits the same order whose payment is already
    APPROVED and is refused, instead of creating and charging a second
    order (the round-8 double-charge bug).
    """

    plan_id: int
    confirm: int = 0
    order_id: int = 0


class OrdersPageCB(CallbackData, prefix="op"):
    page: int = 0


class AdminCB(CallbackData, prefix="a", sep="|"):
    """Admin panel navigation.

    ``sep="|"`` (not the default ``":"``) because ``action`` routinely embeds a
    sub-key — ``list:open``, ``style:PRIMARY``, ``audience:ALL`` — and aiogram
    refuses to pack a value that contains the separator.
    """

    section: str
    action: str = ""
    item_id: int = 0
    page: int = 0


def button_from_rendered(btn: dict) -> InlineKeyboardButton:
    """Turn one validated rendered button into a Telegram button.

    ``style`` is passed natively only when the Bot API call supports it; the
    sender downgrades gracefully (see ``vpnbot.bot.sender``).
    """
    kwargs: dict[str, object] = {"text": btn["text"]}
    if btn.get("url"):
        kwargs["url"] = btn["url"]
    else:
        action = str(btn.get("action") or "noop")
        kwargs["callback_data"] = MenuCB(action=action.split(":", 1)[0]).pack()
    style = btn.get("style")
    if style:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)  # type: ignore[arg-type]


def build_keyboard(
    screen: RenderedScreen, *, extra_rows: list[list[InlineKeyboardButton]] | None = None
) -> InlineKeyboardMarkup:
    """Group rendered buttons by their ``row`` index, preserving order."""
    rows: dict[int, list[InlineKeyboardButton]] = {}
    for btn in screen.buttons:
        rows.setdefault(int(btn.get("row", 0)), []).append(button_from_rendered(btn))
    keyboard = [rows[key] for key in sorted(rows)]
    if extra_rows:
        keyboard.extend(extra_rows)
    if not keyboard:
        keyboard = [[InlineKeyboardButton(text="🏠 منوی اصلی", callback_data=MenuCB(action="home").pack())]]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def paginator(
    action: str, page: int, total_pages: int, *, with_home: bool = True
) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=MenuCB(action=action, page=page - 1).pack()))
    if total_pages > 1:
        row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data=MenuCB(action="noop").pack()))
    if page + 1 < total_pages:
        row.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=MenuCB(action=action, page=page + 1).pack()))
    if with_home:
        row.append(InlineKeyboardButton(text="🏠", callback_data=MenuCB(action="home").pack()))
    return row


def back_home_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data=MenuCB(action="home").pack())]


PAGE_SIZE = 6
PRODUCT_PAGE_SIZE = 6
ORDER_PAGE_SIZE = 6

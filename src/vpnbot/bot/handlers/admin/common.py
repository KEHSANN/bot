"""Shared plumbing for the Telegram admin panel.

Every admin screen is plain text plus an inline keyboard built from
``AdminCB``; nothing here renders customer screens. Two rules hold everywhere:

* ``guard`` is called inside each handler with the permission that specific
  action needs — being an admin is never enough on its own.
* Sensitive values (configs, sub URLs) are only ever shown through
  ``show_secret``, which re-checks ``inventory.view_secret`` itself.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.db.enums import AuditActor, Permission
from vpnbot.db.models.users import Admin
from vpnbot.jalali import format_jalali, tehran_day_start
from vpnbot.money import format_irt_plain
from vpnbot.services import audit as audit_service
from vpnbot.services import auth as auth_service

log = logging.getLogger(__name__)

ADMIN_PAGE_SIZE = 8
DENIED_TEXT = "⛔️ شما دسترسی لازم برای این بخش را ندارید."
PANEL_CLOSED = "این پنل بسته شده است. با /admin دوباره باز کنید."


class AdminDenied(Exception):
    """Raised by ``guard`` when the admin lacks the required permission."""


class AdminDeliveryFailed(Exception):
    """Telegram refused the message (not a bot-permission problem)."""


def guard(admin: Admin | None, permission: Permission) -> Admin:
    """Return the admin or raise ``AdminDenied``.

    Thin wrapper over ``auth_service.require`` so handlers can catch one type.
    """
    try:
        return auth_service.require(admin, permission)
    except auth_service.NotAuthorized as exc:
        raise AdminDenied(str(exc)) from exc


def is_superuser(admin: Admin) -> bool:
    return admin.role is not None and admin.role.key == "owner"


def money(value: int | None) -> str:
    return format_irt_plain(int(value or 0))


def date(dt, *, with_time: bool = True) -> str:
    return format_jalali(dt, with_time=with_time)


def today_start():
    from vpnbot.db.base import utcnow

    return tehran_day_start(utcnow())


# --------------------------------------------------------------------------
# Keyboard builders
# --------------------------------------------------------------------------
def btn(text: str, section: str, action: str = "", item_id: int = 0, page: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=AdminCB(section=section, action=action, item_id=item_id, page=page).pack(),
    )


def back(section: str = "home", text: str = "⬅️ بازگشت") -> InlineKeyboardButton:
    return btn(text, section)


def cancel_row() -> list[InlineKeyboardButton]:
    return [btn("✖️ انصراف", "home")]


def pager(
    section: str, action: str, page: int, total_pages: int, *, extra: int = 0
) -> list[InlineKeyboardButton] | None:
    if total_pages <= 1:
        return None
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(btn("◀️", section, action, extra, page - 1))
    row.append(btn(f"{page + 1}/{total_pages}", section, "noop"))
    if page + 1 < total_pages:
        row.append(btn("▶️", section, action, extra, page + 1))
    return row


def yes_no(section: str, action: str, item_id: int, *, page: int = 0) -> list[InlineKeyboardButton]:
    return [
        btn("✅ تأیید", section, action, item_id, page),
        btn("✖️ انصراف", section, "noop", item_id, page),
    ]


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------
async def reply(target: CallbackQuery | Message, text: str, rows: Sequence[Sequence[InlineKeyboardButton]] | None = None) -> None:
    """Edit the callback's message, or send a new one for commands."""
    markup = InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows]) if rows else None
    if isinstance(target, CallbackQuery):
        message = target.message
        if isinstance(message, Message):
            try:
                await message.edit_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
                await target.answer()
                return
            except TelegramBadRequest as exc:
                text_lower = str(exc).lower()
                if "message is not modified" in text_lower:
                    await target.answer()
                    return
                if "there is no text in the message to edit" not in text_lower:
                    raise
                with contextlib.suppress(TelegramBadRequest):
                    await message.delete()
            await _send(target.message.chat.id, target.bot, text, markup)
            await target.answer()
            return
        await target.answer()
        return
    await _send(target.chat.id, target.bot, text, markup)


async def _send(chat_id: int, bot: Bot, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        raise AdminDeliveryFailed(str(exc)) from exc


async def alert(target: CallbackQuery, text: str) -> None:
    await target.answer(text, show_alert=True)


async def ask(target: CallbackQuery, text: str) -> None:
    """Prompt for free text without a keyboard (the next message is the input)."""
    await alert(target, text)


# --------------------------------------------------------------------------
# Audit helper
# --------------------------------------------------------------------------
async def audit(
    session: AsyncSession,
    admin: Admin,
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    await audit_service.record(
        session,
        actor_type=AuditActor.ADMIN,
        actor_id=admin.id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        data=data,
    )


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------
def mask_secret(value: str | None, keep: int = 6) -> str:
    if not value:
        return "—"
    if len(value) <= keep:
        return "•" * len(value)
    return f"{value[:keep]}{'•' * 8}"


def mask_link(url: str | None) -> str:
    if not url:
        return "—"
    head = url.split("://", 1)[-1]
    return f"{head[:10]}…" if len(head) > 10 else head


async def show_secret(callback: CallbackQuery, admin: Admin, label: str, value: str) -> None:
    """Reveal one secret; permission is checked here, not only upstream."""
    guard(admin, Permission.INVENTORY_VIEW_SECRET)
    await alert(callback, f"{label}:\n{value}")


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = [
    "ADMIN_PAGE_SIZE",
    "DENIED_TEXT",
    "PANEL_CLOSED",
    "AdminDenied",
    "AdminDeliveryFailed",
    "alert",
    "ask",
    "audit",
    "back",
    "btn",
    "cancel_row",
    "date",
    "escape",
    "guard",
    "is_superuser",
    "mask_link",
    "mask_secret",
    "money",
    "pager",
    "reply",
    "show_secret",
    "today_start",
    "yes_no",
]

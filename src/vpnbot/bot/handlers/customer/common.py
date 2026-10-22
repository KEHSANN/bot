"""Shared helpers for customer handlers."""

from __future__ import annotations

from typing import Any

from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.sender import ScreenSender
from vpnbot.db.models.users import User
from vpnbot.money import format_irt_plain
from vpnbot.services.screens import ScreenService


def sender_for(bot: Any, session: AsyncSession) -> ScreenSender:
    return ScreenSender(bot, ScreenService(session))


RECEIPT_DOCUMENT_MIMES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
RECEIPT_MAX_BYTES = 10 * 1024 * 1024


def receipt_file(message: Message) -> tuple[str, str] | None:
    """Extract (file_id, file_type) from a receipt message, safely.

    Photos are Telegram-generated images — always acceptable. A
    *document* is whatever file the sender picked: without a check,
    an .apk/.exe/.html "receipt" was stored and later forwarded to
    the admin by the trusted bot when they tapped "view receipt".
    Documents are allowlisted to image/PDF mime types and 10 MB.
    Returns None when the attachment must be rejected.
    """
    if message.photo:
        return message.photo[-1].file_id, "photo"
    document = message.document
    if document is None:
        return None
    if (document.mime_type or "") not in RECEIPT_DOCUMENT_MIMES:
        return None
    if document.file_size is not None and document.file_size > RECEIPT_MAX_BYTES:
        return None
    return document.file_id, "document"


RECEIPT_REJECT_TEXT = (
    "⚠️ این فایل قابل قبول نیست. رسید را به‌صورت عکس، یا فایل PDF/JPG/PNG "
    "با حجم کمتر از ۱۰ مگابایت بفرستید."
)


async def edit_or_answer(message: Message, text: str, reply_markup: Any = None) -> None:
    """Edit the current message instead of posting a new one.

    Prompt steps reached by a button tap (wallet-buy confirm,
    top-up start, support start) used to post a FRESH message per
    tap — a handful of accounts tapping fast could eat the bot's
    global ~30 msg/s Telegram budget and starve config deliveries.
    Editing reuses the message already on screen; anything that
    makes the edit impossible (message too old, media message)
    falls back to a normal answer.
    """
    import contextlib

    from aiogram.exceptions import TelegramBadRequest

    with contextlib.suppress(TelegramBadRequest):
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return
    await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")


def base_context(user: User | None, **extra: object) -> dict[str, object]:
    context: dict[str, object] = {
        "first_name": (user.first_name if user else "") or "دوست عزیز",
        "username": (user.username if user else "") or "",
        "user_id": user.telegram_id if user else 0,
    }
    context.update(extra)
    return context


def money(value: int) -> str:
    return format_irt_plain(int(value))


def order_context(user: User | None, **extra: object) -> dict[str, object]:
    context = base_context(user)
    context.update(extra)
    return context


async def alert(callback: CallbackQuery, text: str) -> None:
    await callback.answer(text, show_alert=True)


def is_private(message: Message) -> bool:
    return message.chat.type == "private"


def state_data(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("state_data") or {}


CANCEL_TEXT = "/cancel"

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

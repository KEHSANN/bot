"""Update middlewares: DB session, user/admin resolution, rate limiting.

Order matters (outer first):

    DbSession -> UserLoader -> AdminLoader -> Throttle -> handler

* ``DbSession`` opens one transaction per update and commits only when the
  handler returns without raising.
* ``UserLoader`` upserts the Telegram user and exposes ``data["user"]``.
* ``AdminLoader`` resolves an active admin once and exposes ``data["admin"]``;
  handlers still call ``auth.require`` for each privileged operation, so this is
  a convenience, never the authorization.
* ``Throttle`` protects the bot from callback storms per user.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vpnbot.db.models.users import Admin, User
from vpnbot.services import auth as auth_service

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def _event_user(event: TelegramObject) -> Any:
    if isinstance(event, Update):
        return event.event.from_user if getattr(event, "event", None) is not None else None
    return getattr(event, "from_user", None)


def _event_chat(event: TelegramObject) -> Any:
    if isinstance(event, Update):
        inner = getattr(event, "event", None)
        return getattr(inner, "chat", None) if inner is not None else None
    return getattr(event, "chat", None)


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()
            return result


class UserLoaderMiddleware(BaseMiddleware):
    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = _event_user(event)
        session: AsyncSession = data["session"]
        if tg_user is None or getattr(tg_user, "is_bot", False):
            data["user"] = None
            return await handler(event, data)

        user: User = await auth_service.upsert_user(
            session,
            telegram_id=int(tg_user.id),
            username=getattr(tg_user, "username", None),
            first_name=getattr(tg_user, "first_name", None),
            last_name=getattr(tg_user, "last_name", None),
        )
        data["user"] = user
        return await handler(event, data)


class AdminLoaderMiddleware(BaseMiddleware):
    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        session: AsyncSession = data["session"]
        tg_user = _event_user(event)
        admin: Admin | None = None
        if tg_user is not None:
            admin = await auth_service.get_admin(session, int(tg_user.id))
        data["admin"] = admin
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Simple per-process limiter for noisy updates (callbacks and commands).

    Not a security boundary — admin actions are still authorized server-side —
    it only keeps a single chat from flooding the worker.
    """

    def __init__(self, *, min_interval_seconds: float = 0.35, window: int = 3000) -> None:
        self.min_interval = min_interval_seconds
        self.window = window
        self._last: dict[int, float] = {}
        self._last_gc = time.monotonic()

    def _gc(self, now: float) -> None:
        if now - self._last_gc < 60:
            return
        cutoff = now - 120
        self._last = {uid: ts for uid, ts in self._last.items() if ts >= cutoff}
        self._last_gc = now

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        tg_user = _event_user(event)
        if tg_user is None:
            return await handler(event, data)

        now = time.monotonic()
        self._gc(now)
        uid = int(tg_user.id)
        last = self._last.get(uid, 0.0)
        if now - last < self.min_interval:
            if isinstance(event, CallbackQuery):
                # A failed toast must not turn a throttled tap into an error.
                with contextlib.suppress(Exception):
                    await event.answer("⏳ لطفاً کمی صبر کنید", show_alert=False)
            return None
        self._last[uid] = now
        return await handler(event, data)


__all__ = [
    "AdminLoaderMiddleware",
    "DbSessionMiddleware",
    "ThrottleMiddleware",
    "UserLoaderMiddleware",
]

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

import asyncio
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
    """One session per update, committed in phases.

    The middleware opens the session and commits whatever is still
    open when the handler returns (rolling back on exceptions). The
    long pole used to be the network phase: handlers send screens
    (Telegram calls + configured delays) while this transaction —
    its pooled connection and any FOR UPDATE locks — stayed open.

    Screen sends now end the transaction themselves:
    ``ScreenSender.show`` commits right after rendering, before the
    first Telegram call (see ``ScreenSender._commit_before_send``).
    So an update's DB work runs in short transactions — everything
    up to its first screen send, then any follow-up work — and no
    connection or row lock is ever held across a network send or a
    display delay. Sessions use expire_on_commit=False, so handler
    objects stay readable across the phase boundary.
    """

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


class BlockedUserMiddleware(BaseMiddleware):
    """A blocked customer cannot use the bot at all.

    Runs after the user and admin loaders. Everything a BLOCKED user
    sends is dropped before any handler: messages (including ones aimed
    at an in-progress checkout/support FSM), photos and button taps.
    Two exceptions:

    * ``/start`` still goes through, so the customer sees the blocked
      screen (and, after an unblock, the normal menu again) — but at
      most once per NOTICE_INTERVAL: /start spam from a script would
      otherwise burn the bot's Telegram send quota and grow the
      dispatch log at the throttle's full 90 messages/minute;
    * admins are never gated — an admin whose personal user row is
      blocked keeps full panel access (blocking an admin's user row is
      refused in the service layer anyway).
    """

    NOTICE_INTERVAL_SECONDS = 300.0

    def __init__(self) -> None:
        self._notified_at: dict[int, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)
        if data.get("admin") is not None:
            return await handler(event, data)
        user = data.get("user")
        if user is None or not getattr(user, "is_blocked", False):
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            import time as _time

            telegram_id = getattr(user, "telegram_id", None)
            if telegram_id is None:
                return await handler(event, data)
            now = _time.monotonic()
            last = self._notified_at.get(telegram_id, 0.0)
            if now - last < self.NOTICE_INTERVAL_SECONDS:
                return None  # already told recently; drop silently
            if len(self._notified_at) > 10_000:
                self._notified_at.clear()
            self._notified_at[telegram_id] = now
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            with contextlib.suppress(Exception):
                await event.answer("⛔️ دسترسی شما مسدود شده است", show_alert=True)
        return None


class TokenBucket:
    """Leaky token bucket with an injectable clock (unit-testable).

    A bucket starts full, so normal bursts — tapping through a menu, or
    tapping "pay by card" and immediately sending the receipt photo — pass
    instantly. Sustained flooding is what gets limited.
    """

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill = refill_per_second
        self.tokens = capacity
        self.updated = 0.0

    def _sync(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill)
        self.updated = now

    def allow(self, now: float) -> bool:
        self._sync(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def wait_seconds(self, now: float) -> float:
        """How long until the next token is available."""
        self._sync(now)
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.refill


#: Attachment messages carry user evidence (payment receipts, crypto proof,
#: support files). They are never dropped by the limiter — at worst they
#: wait a moment for a token (see ThrottleMiddleware).
EVIDENCE_CONTENT_TYPES = frozenset(
    {"photo", "document", "video", "animation", "voice", "video_note"}
)


class ThrottleMiddleware(BaseMiddleware):
    """Per-user anti-spam limiter for callbacks and messages.

    Design goals (a flat "one update per 0.35s" gate failed both):

    * **The bot stays up under flood.** Each user gets two token buckets —
      one for button taps, one for messages — so a spammer burns only
      their own budget and the taps/messages are shed cheaply, before any
      database work of substance.
    * **A paying customer never gets stuck.** Taps and messages have
      *separate* budgets, so tapping "pay by card" cannot eat the budget
      the receipt photo needs a second later; buckets start full, so the
      normal checkout burst always passes; and attachment messages
      (receipts!) are never discarded — under flood they are delayed by
      at most ``evidence_max_wait`` seconds instead of dropped.
    * **Feedback, not silence.** A shed tap gets a toast; a shed text
      message gets one notice per ``notice_interval`` per user.
    """

    def __init__(
        self,
        *,
        callback_capacity: float = 10,
        callback_refill: float = 5.0,
        message_capacity: float = 6,
        message_refill: float = 1.5,
        evidence_max_wait: float = 2.5,
        notice_interval: float = 15.0,
        window: int = 3000,
    ) -> None:
        self.callback_bucket_cfg = (callback_capacity, callback_refill)
        self.message_bucket_cfg = (message_capacity, message_refill)
        self.evidence_max_wait = evidence_max_wait
        self.notice_interval = notice_interval
        self.window = window
        self._callbacks: dict[int, TokenBucket] = {}
        self._messages: dict[int, TokenBucket] = {}
        self._noticed: dict[int, float] = {}
        self._last_gc = time.monotonic()

    def _gc(self, now: float) -> None:
        if now - self._last_gc < 60:
            return
        cutoff = now - 120
        self._callbacks = {u: b for u, b in self._callbacks.items() if b.updated >= cutoff}
        self._messages = {u: b for u, b in self._messages.items() if b.updated >= cutoff}
        self._noticed = {u: ts for u, ts in self._noticed.items() if ts >= cutoff}
        # Hard bound: even under a flood of distinct accounts the maps
        # cannot grow without limit — keep only the most recent buckets.
        for store in (self._callbacks, self._messages):
            if len(store) > 20000:
                newest = sorted(store.items(), key=lambda kv: kv[1].updated)[-10000:]
                store.clear()
                store.update(newest)
        self._last_gc = now

    def _bucket(self, store: dict[int, TokenBucket], cfg: tuple[float, float], uid: int) -> TokenBucket:
        bucket = store.get(uid)
        if bucket is None:
            bucket = TokenBucket(*cfg)
            store[uid] = bucket
        return bucket

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        tg_user = _event_user(event)
        if tg_user is None:
            return await handler(event, data)

        now = time.monotonic()
        self._gc(now)
        uid = int(tg_user.id)

        if isinstance(event, CallbackQuery):
            bucket = self._bucket(self._callbacks, self.callback_bucket_cfg, uid)
            if bucket.allow(now):
                return await handler(event, data)
            with contextlib.suppress(Exception):
                await event.answer("⏳ لطفاً کمی آرام‌تر", show_alert=False)
            return None

        # Messages.
        bucket = self._bucket(self._messages, self.message_bucket_cfg, uid)
        if bucket.allow(now):
            return await handler(event, data)

        content_type = getattr(event, "content_type", None)
        content_value = getattr(content_type, "value", content_type)
        if str(content_value) in EVIDENCE_CONTENT_TYPES:
            wait = bucket.wait_seconds(now)
            if wait <= self.evidence_max_wait:
                # Never lose a receipt: hold the update briefly instead
                # of dropping it. Under genuine flood this still caps the
                # rate — the sender just experiences a short delay.
                await asyncio.sleep(wait + 0.05)
                if bucket.allow(time.monotonic()):
                    return await handler(event, data)
            # Even evidence cannot wait forever; fall through to the
            # notice path so the user knows to resend in a moment.
        last_notice = self._noticed.get(uid, 0.0)
        if now - last_notice >= self.notice_interval:
            self._noticed[uid] = now
            with contextlib.suppress(Exception):
                await event.answer("⏳ پیام‌های شما خیلی سریع می‌رسند؛ چند ثانیه صبر کنید و دوباره بفرستید.")
        return None


__all__ = [
    "AdminLoaderMiddleware",
    "BlockedUserMiddleware",
    "DbSessionMiddleware",
    "EVIDENCE_CONTENT_TYPES",
    "ThrottleMiddleware",
    "TokenBucket",
    "UserLoaderMiddleware",
]

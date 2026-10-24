"""Dispatcher assembly: storage, middlewares, routers, error handling.

The bot process owns the update path only; every side effect that must survive
a restart goes through the job queue. That keeps this module small: build the
dispatcher, attach the session/user/admin middlewares in order, include the
root router, and log failures without ever echoing a config or sub URL.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vpnbot.bot.handlers import build_root_router
from vpnbot.bot.middlewares.context import (
    AdminLoaderMiddleware,
    BlockedUserMiddleware,
    DbSessionMiddleware,
    ThrottleMiddleware,
    UserLoaderMiddleware,
)
from vpnbot.bot.sender import ScreenSender, attach_log_factory
from vpnbot.config import Settings

log = logging.getLogger(__name__)

CUSTOMER_COMMANDS = [
    BotCommand(command="start", description="شروع و منوی اصلی"),
    BotCommand(command="menu", description="منوی اصلی"),
    BotCommand(command="cancel", description="لغو عملیات جاری"),
]

ADMIN_COMMANDS = CUSTOMER_COMMANDS + [
    BotCommand(command="admin", description="پنل مدیریت"),
]


def build_storage(settings: Settings) -> BaseStorage | None:
    """Redis-backed FSM storage, or ``None`` to fall back to memory.

    A missing Redis must not stop the bot: admin wizards then live in process
    memory and are lost on restart, which is annoying but not unsafe.

    NOTE: ``RedisStorage.from_url`` is lazy — it raises nothing when
    Redis is down, so construction succeeding proves nothing. Callers
    that can await should pass the result through
    :func:`storage_is_alive` before relying on it.
    """
    if not settings.redis_enabled:
        return None
    try:
        from aiogram.fsm.storage.redis import RedisStorage

        # Bounded state lifetime: without TTLs, every abandoned
        # wizard left its FSM keys in Redis forever. 12h is far
        # longer than any legitimate wizard/checkout pause, and each
        # interaction refreshes the expiry.
        return RedisStorage.from_url(
            settings.redis_url, state_ttl=12 * 3600, data_ttl=12 * 3600
        )
    except Exception:  # noqa: BLE001 - optional dependency or bad URL
        log.warning("Redis storage unavailable; using in-memory FSM", exc_info=True)
        return None


async def storage_is_alive(storage: BaseStorage | None) -> bool:
    """Ping a Redis-backed storage; memory storage counts as alive.

    Without this check, a dead Redis passed build_storage silently
    and the bot then died on the FIRST update's FSM access with a raw
    ConnectionError — up in the process list, dead in practice.
    """
    redis = getattr(storage, "redis", None)
    if redis is None:
        return True
    import asyncio

    try:
        await asyncio.wait_for(redis.ping(), timeout=2.0)
        return True
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        log.warning("Redis ping failed; FSM storage falls back to memory", exc_info=True)
        return False


def build_dispatcher(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    storage: BaseStorage | None = None,
) -> Dispatcher:
    chosen = storage if storage is not None else build_storage(settings)
    fsm_storage = chosen or MemoryStorage()
    dp = Dispatcher(storage=fsm_storage)

    session_mw = DbSessionMiddleware(session_factory)
    user_mw = UserLoaderMiddleware()
    admin_mw = AdminLoaderMiddleware()
    # The blocked gate clears the dropped user's FSM state (see its
    # docstring), so it needs the same storage the throttle reads.
    blocked_mw = BlockedUserMiddleware(storage=fsm_storage)
    # The throttle reads FSM state from the same storage to scope its
    # never-drop media delay to flows that actually expect a file.
    throttle_mw = ThrottleMiddleware(storage=fsm_storage)

    # Outer middleware order. Throttle runs FIRST, before any database
    # work: it keys purely off the Telegram user id on the update, so a
    # flood is shed without opening a session, upserting the user row or
    # querying admins — and its short evidence-delay never holds a pooled
    # connection. Blocked runs after the admin loader so an admin is
    # never gated by their own customer row.
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(throttle_mw)
        observer.outer_middleware(session_mw)
        observer.outer_middleware(user_mw)
        observer.outer_middleware(admin_mw)
        observer.outer_middleware(blocked_mw)
    dp.my_chat_member.outer_middleware(session_mw)
    dp.my_chat_member.outer_middleware(user_mw)

    dp.include_router(build_root_router())

    attach_log_factory(ScreenSender, session_factory)
    dp.errors()(on_error)
    return dp


async def on_error(event) -> bool:
    """Last-resort handler: log once, never leak payload contents.

    One exception gets user feedback instead of silence: ``AdminDenied``.
    Every admin guard raises it, and without this branch a permission-
    mismatched tap looked exactly like a dead button — spinner, rollback,
    no message at all.
    """
    exc = event.exception
    update = event.update
    who = "?"
    try:
        from vpnbot.bot.handlers.admin.common import DENIED_TEXT, AdminDenied

        if isinstance(exc, AdminDenied):
            callback = getattr(update, "callback_query", None) if update is not None else None
            if callback is not None:
                with contextlib.suppress(Exception):
                    await callback.answer(DENIED_TEXT, show_alert=True)
            return True
    except Exception:  # noqa: BLE001 - feedback must not mask the original error path
        log.debug("could not answer AdminDenied callback", exc_info=True)
    try:
        inner = update.event if update is not None else None
        user = getattr(inner, "from_user", None)
        if user is not None:
            who = str(user.id)
    except Exception:  # noqa: BLE001 - diagnostics must not raise
        log.debug("could not read the update sender", exc_info=True)
    log.error("unhandled %s for user=%s", type(exc).__name__, who, exc_info=exc)
    return True


def bot_properties() -> DefaultBotProperties:
    return DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)


async def configure_commands(bot, settings: Settings) -> None:
    """Publish the user menu, and the extra admin command for owners only."""
    from aiogram.types import BotCommandScopeDefault

    await bot.set_my_commands(CUSTOMER_COMMANDS, scope=BotCommandScopeDefault())
    for owner_id in settings.owner_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
        except Exception:  # noqa: BLE001 - owner may not have started the bot yet
            log.info("could not set admin commands for %s", owner_id)


__all__ = ["build_dispatcher", "build_storage", "bot_properties", "configure_commands", "on_error", "storage_is_alive"]

"""Rendering and sending configurable screens.

All customer-visible text goes through here so that:

* the published (or default) screen version is used, not handler literals;
* buttons carry validated actions only;
* native button styles degrade gracefully if the Bot API rejects them;
* every outcome is logged without any sensitive payload (no configs, no URLs).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from vpnbot.bot.keyboards.callbacks import build_keyboard
from vpnbot.db.models.content import MessageDispatchLog
from vpnbot.services.screens import RenderedScreen, ScreenService, ScreenSnapshot, render_screen

log = logging.getLogger(__name__)

#: Bot API may reject ``style`` on older servers; flip to False on first refusal
#: and keep sending (styled buttons are cosmetic, never functional).
_STYLES_SUPPORTED = True


def styles_supported() -> bool:
    return _STYLES_SUPPORTED


def disable_styles() -> None:
    global _STYLES_SUPPORTED
    _STYLES_SUPPORTED = False


def _strip_styles(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    rows = [
        [
            button.model_copy(update={"style": None}) if getattr(button, "style", None) else button
            for button in row
        ]
        for row in markup.inline_keyboard
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _markup(
    screen: RenderedScreen, extra_rows: Sequence[Sequence[InlineKeyboardButton]] | None = None
) -> InlineKeyboardMarkup | None:
    if not screen.buttons and not extra_rows:
        return None
    markup = build_keyboard(screen, extra_rows=list(extra_rows or []))
    if not _STYLES_SUPPORTED:
        return _strip_styles(markup)
    return markup


async def _with_style_fallback(call, markup: InlineKeyboardMarkup | None):
    """Run ``call`` with markup, retrying without button styles if rejected."""
    try:
        return await call(markup)
    except TelegramBadRequest as exc:
        text = str(exc).lower()
        if markup is not None and "style" in text and _STYLES_SUPPORTED:
            log.warning("Bot API rejected button styles; continuing without them")
            disable_styles()
            return await call(_strip_styles(markup))
        raise


class ScreenSender:
    """Sends a screen to a chat, choosing edit-vs-send for callbacks."""

    def __init__(self, bot: Bot, screens: ScreenService) -> None:
        self.bot = bot
        self.screens = screens

    async def render(
        self, stage: str, context: dict[str, object] | None = None
    ) -> tuple[ScreenSnapshot, RenderedScreen]:
        snapshot = await self.screens.snapshot(stage)
        rendered = render_screen(
            snapshot.stage,
            list(snapshot.messages),
            list(snapshot.buttons),
            context or {},
            version=snapshot.version,
        )
        return snapshot, rendered

    async def show(
        self,
        target: Message | CallbackQuery,
        stage: str,
        context: dict[str, object] | None = None,
        *,
        extra_rows: Sequence[Sequence[InlineKeyboardButton]] | None = None,
        log_outcome: bool = True,
    ) -> None:
        snapshot, rendered = await self.render(stage, context)
        if isinstance(target, CallbackQuery):
            ok = await self._edit_or_send(target, rendered, extra_rows)
            if log_outcome:
                await self._log(snapshot, target.from_user.id if target.from_user else 0, ok)
        else:
            ok = await self._send_messages(target.chat.id, rendered, extra_rows=extra_rows)
            if log_outcome:
                await self._log(snapshot, target.chat.id, ok)

    async def _edit_or_send(
        self,
        callback: CallbackQuery,
        screen: RenderedScreen,
        extra_rows: Sequence[Sequence[InlineKeyboardButton]] | None = None,
    ) -> bool:
        message = callback.message
        if not isinstance(message, Message):
            await callback.answer()
            if callback.from_user:
                return await self._send_messages(callback.from_user.id, screen, extra_rows=extra_rows)
            return False

        markup = _markup(screen, extra_rows)
        first = screen.messages[0] if screen.messages else {"type": "TEXT", "text": screen.text}
        try:
            if first["type"] == "TEXT":
                await _with_style_fallback(
                    lambda m: self.bot.edit_message_text(
                        text=first.get("text", ""),
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        reply_markup=m,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    ),
                    markup,
                )
            else:
                await _with_style_fallback(
                    lambda m: self.bot.edit_message_caption(
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        caption=first.get("caption") or "",
                        reply_markup=m,
                        parse_mode="HTML",
                    ),
                    markup,
                )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                pass
            elif "there is no text in the message to edit" in str(exc).lower():
                # Previous message was a media message with no caption: replace it.
                with contextlib.suppress(TelegramBadRequest):
                    await message.delete()
                return await self._send_messages(message.chat.id, screen, extra_rows=extra_rows)
            else:
                raise

        await callback.answer()
        if len(screen.messages) > 1:
            await self._send_messages(message.chat.id, screen, skip_first=True)
        return True

    async def _send_messages(
        self,
        chat_id: int,
        screen: RenderedScreen,
        *,
        skip_first: bool = False,
        extra_rows: Sequence[Sequence[InlineKeyboardButton]] | None = None,
    ) -> bool:
        messages = screen.messages[1:] if skip_first else screen.messages
        slept = 0.0  # cumulative delay budget for this screen send
        try:
            for index, msg in enumerate(messages):
                # Cap at send time too: screens stored before the 3s
                # validation cap may still carry a 10s delay. The cap
                # is also cumulative — ten messages x 3s would hold the
                # caller's DB connection for half a minute otherwise.
                delay = min(int(msg.get("delay_ms", 0) or 0), 3000) / 1000
                if delay > 0 and slept < 6.0:
                    await asyncio.sleep(delay)
                    slept += delay
                last = index == len(messages) - 1
                markup = _markup(screen, extra_rows) if last else None
                # Retry THIS message on a rate limit, not the whole screen:
                # the old code recursed from the first message (duplicating
                # everything already sent) and dropped ``extra_rows``.
                for attempt in range(3):
                    try:
                        if msg["type"] == "TEXT":
                            await _with_style_fallback(
                                lambda m, text=msg["text"]: self.bot.send_message(
                                    chat_id=chat_id,
                                    text=text,
                                    reply_markup=m,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True,
                                ),
                                markup,
                            )
                        else:
                            await _with_style_fallback(
                                lambda m, media=msg: self._send_media(chat_id, media, m),
                                markup,
                            )
                        break
                    except TelegramRetryAfter as exc:
                        # Never sleep a long rate-limit out inside the
                        # request: the caller's DB session (and a pooled
                        # connection) is held for the whole send. A short
                        # wait is retried; a long one abandons this send
                        # instead of stalling the pool.
                        if attempt == 2 or exc.retry_after > 5:
                            log.warning(
                                "rate limited sending screen %s to %s; giving up this send",
                                screen.stage,
                                chat_id,
                            )
                            return False
                        await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            log.info("user blocked the bot: chat_id=%s", chat_id)
            return False
        except TelegramBadRequest:
            log.exception("failed to send screen %s to %s", screen.stage, chat_id)
            return False
        return True

    async def _send_media(self, chat_id: int, msg: dict, markup: InlineKeyboardMarkup | None):
        kwargs = {
            "chat_id": chat_id,
            "caption": msg.get("caption"),
            "reply_markup": markup,
            "parse_mode": "HTML",
        }
        mtype = msg["type"]
        if mtype == "PHOTO":
            return await self.bot.send_photo(photo=msg["file_id"], **kwargs)
        if mtype == "VIDEO":
            return await self.bot.send_video(video=msg["file_id"], **kwargs)
        return await self.bot.send_document(document=msg["file_id"], **kwargs)

    @staticmethod
    async def _log(snapshot: ScreenSnapshot, telegram_id: int, ok: bool) -> None:
        # Best-effort audit log on its OWN session (it must survive a
        # caller rollback) — but bounded: at most a few concurrent log
        # writes, each with a short timeout, so under load the log
        # drops entries instead of piling onto the connection pool
        # the handlers are already holding sessions from.
        import asyncio

        semaphore = getattr(ScreenSender, "_log_semaphore", None)
        if semaphore is None:
            semaphore = asyncio.Semaphore(4)
            ScreenSender._log_semaphore = semaphore  # type: ignore[attr-defined]
        if semaphore.locked():
            return
        try:
            async with semaphore:
                await asyncio.wait_for(
                    ScreenSender._write_log(snapshot, telegram_id, ok), timeout=2.0
                )
        except Exception:  # noqa: BLE001 - logging must never break delivery
            log.debug("dispatch log write skipped/failed", exc_info=True)

    @staticmethod
    async def _write_log(snapshot: ScreenSnapshot, telegram_id: int, ok: bool) -> None:
        session_factory = getattr(ScreenSender, "_log_factory", None)
        if session_factory is None:
            return
        async with session_factory() as session, session.begin():
            session.add(
                MessageDispatchLog(
                    stage=snapshot.stage,
                    user_telegram_id=int(telegram_id),
                    version=snapshot.version,
                    outcome="SENT" if ok else "FAILED",
                )
            )


def attach_log_factory(sender_cls, session_factory) -> None:
    sender_cls._log_factory = session_factory  # type: ignore[attr-defined]


async def send_plain(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=markup, parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except TelegramForbiddenError:
        return False
    except TelegramBadRequest:
        log.exception("plain message failed to %s", chat_id)
        return False


async def send_config_pair(
    bot: Bot,
    chat_id: int,
    config: str,
    sub_url: str | None,
    product_title: str,
    order_ref: str,
) -> None:
    """Private delivery message. Raises on transport failure so the job retries.

    Sent as one message with the pair in ``<pre>`` so it is easy to copy, plus a
    separate subscription link message when present.

    Every interpolated value is HTML-escaped: VPN URIs routinely contain
    ``&`` in their query string, and unescaped the Bot API HTML parser
    rejects the whole delivery.
    """
    import html as _html

    # Subscription stock stores the link in BOTH slots (that is what
    # a subscription product is); sending it twice — once as "config"
    # and once as "subscription link" — is a duplicate, so collapse
    # it to a single link block.
    if sub_url and sub_url == config:
        lines = [
            "🎉 <b>سفارش شما تأیید شد</b>",
            "",
            f"محصول: {_html.escape(product_title)}",
            f"کد سفارش: <code>{_html.escape(order_ref)}</code>",
            "",
            "<b>لینک اشتراک:</b>",
            _html.escape(sub_url),
        ]
        lines.extend(
            [
                "",
                "این لینک را در برنامه VPN خود وارد کنید.",
                "⚠️ این لینک شخصی است؛ آن را با دیگران به اشتراک نگذارید.",
            ]
        )
        await bot.send_message(chat_id, "\n".join(lines))
        return
    lines = [
        "🎉 <b>سفارش شما تأیید شد</b>",
        "",
        f"محصول: {_html.escape(product_title)}",
        f"کد سفارش: <code>{_html.escape(order_ref)}</code>",
        "",
        "<b>کانفیگ:</b>",
        f"<pre>{_html.escape(config)}</pre>",
    ]
    if sub_url:
        lines.extend(["", "<b>لینک اشتراک:</b>", _html.escape(sub_url)])
    lines.extend(
        [
            "",
            "⚠️ این اطلاعات فقط برای شماست. در اختیار دیگران قرار ندهید.",
        ]
    )
    await bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


__all__ = [
    "ScreenSender",
    "attach_log_factory",
    "disable_styles",
    "send_config_pair",
    "send_plain",
    "styles_supported",
]

"""Persistent job worker.

The ``jobs`` table is the outbox for every side effect that must survive a
restart: delivery after payment approval, admin notifications, broadcast
campaigns and payment expiry sweeps. This module claims jobs with
``FOR UPDATE SKIP LOCKED`` so any number of workers can run side by side
without double-processing.

Delivery safety, in order:

* a job is marked ``RUNNING`` in its own short transaction before any work, so
  a crash leaves a row that recovery can requeue;
* the actual send happens outside any transaction;
* ``FULFILL_ORDER`` is idempotent inside :func:`fulfillment.fulfill_order` —
  re-running it re-sends to the same order holder and never re-reserves;
* failures back off exponentially and stop at ``max_attempts``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vpnbot.bot.sender import send_config_pair, send_plain
from vpnbot.config import Settings
from vpnbot.db.base import utcnow
from vpnbot.db.enums import (
    CampaignStatus,
    JobKind,
    JobStatus,
    RecipientStatus,
)
from vpnbot.db.models.broadcast import BroadcastCampaign, BroadcastRecipient
from vpnbot.db.models.system import Job
from vpnbot.security.crypto import CredentialCipher
from vpnbot.services import fulfillment, notify
from vpnbot.services import payments as payment_service
from vpnbot.services import topups as topup_service

log = logging.getLogger(__name__)

CLAIM_BATCH = 10
STALE_AFTER = timedelta(minutes=15)
MAX_BACKOFF_SECONDS = 600
IDLE_SLEEP_SECONDS = 2.0
DISPATCH_PAGE = 500
EXPIRE_INTERVAL_SECONDS = 300


class RateLimiter:
    """Simple token bucket shared by broadcast jobs across coroutines."""

    def __init__(self, per_second: int) -> None:
        self.rate = max(1, per_second)
        self._allowance = float(self.rate)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._allowance = min(
                float(self.rate), self._allowance + (now - self._last) * self.rate
            )
            self._last = now
            if self._allowance < 1:
                await asyncio.sleep((1 - self._allowance) / self.rate)
                self._allowance = 0.0
            else:
                self._allowance -= 1


class Worker:
    def __init__(
        self,
        *,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.settings = settings
        self.cipher = CredentialCipher(settings.inventory_encryption_key)
        self.limiter = RateLimiter(settings.broadcast_rate_per_second)
        self._stopping = asyncio.Event()
        self._handlers: dict[JobKind, Callable[[dict], Awaitable[None]]] = {
            JobKind.FULFILL_ORDER: self._fulfill,
            JobKind.DELIVERY_RETRY: self._fulfill,
            JobKind.NOTIFY_ADMIN: self._notify,
            JobKind.BROADCAST_DISPATCH: self._broadcast_dispatch,
            JobKind.BROADCAST_RECIPIENT: self._broadcast_recipient,
            JobKind.EXPIRE_PAYMENT: self._expire_payments,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        await self.recover()
        sweeper = asyncio.create_task(self._expire_loop(), name="payment-expiry")
        log.info("worker started")
        try:
            while not self._stopping.is_set():
                processed = await self.tick()
                if processed == 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._stopping.wait(), timeout=IDLE_SLEEP_SECONDS
                        )
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper

    def stop(self) -> None:
        self._stopping.set()

    async def recover(self) -> None:
        """Requeue jobs a previous process left ``RUNNING``."""
        cutoff = utcnow() - STALE_AFTER
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                update(Job)
                .where(Job.status == JobStatus.RUNNING, Job.started_at < cutoff)
                .values(status=JobStatus.QUEUED, scheduled_at=utcnow())
            )
        count = result.rowcount or 0
        if count:
            log.warning("requeued %s stale running jobs", count)

    async def _expire_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._expire_payments({})
            except Exception:  # noqa: BLE001 - the sweep must never kill the loop
                log.exception("payment expiry sweep failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=EXPIRE_INTERVAL_SECONDS
                )
            except TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Claiming
    # ------------------------------------------------------------------
    async def tick(self) -> int:
        jobs = await self._claim()
        if not jobs:
            return 0
        await asyncio.gather(*(self._run(job_id, kind, payload) for job_id, kind, payload in jobs))
        return len(jobs)

    async def _claim(self) -> list[tuple[int, JobKind, dict]]:
        now = utcnow()
        async with self.session_factory() as session, session.begin():
            rows = (
                await session.execute(
                    select(Job)
                    .where(Job.status == JobStatus.QUEUED, Job.scheduled_at <= now)
                    .order_by(Job.scheduled_at, Job.id)
                    .limit(CLAIM_BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
            claimed: list[tuple[int, JobKind, dict]] = []
            for job in rows:
                job.status = JobStatus.RUNNING
                job.started_at = now
                job.attempt += 1
                claimed.append((job.id, job.kind, dict(job.payload or {})))
            return claimed

    async def _run(self, job_id: int, kind: JobKind, payload: dict) -> None:
        handler = self._handlers.get(kind)
        if handler is None:
            await self._finish(job_id, JobStatus.FAILED, f"no handler for {kind}")
            return
        try:
            await handler(payload)
        except Exception as exc:  # noqa: BLE001 - every failure is retried or recorded
            await self._fail(job_id, exc)
            return
        await self._finish(job_id, JobStatus.DONE, None)

    async def _finish(self, job_id: int, status: JobStatus, error: str | None) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(status=status, finished_at=utcnow(), last_error=(error or "")[:1000] or None)
            )

    async def _fail(self, job_id: int, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"[:1000]
        async with self.session_factory() as session, session.begin():
            job = (
                await session.execute(select(Job).where(Job.id == job_id).with_for_update())
            ).scalar_one_or_none()
            if job is None:
                return
            if job.attempt >= job.max_attempts:
                job.status = JobStatus.FAILED
                job.finished_at = utcnow()
                job.last_error = message
                log.error("job %s (%s) exhausted retries: %s", job.id, job.kind, message)
                if job.kind is JobKind.BROADCAST_RECIPIENT:
                    # The recipient was deliberately left PENDING while
                    # retries remained (see _broadcast_recipient); now
                    # that they are exhausted it is truly FAILED.
                    payload = job.payload or {}
                    if payload.get("recipient_id") and payload.get("campaign_id"):
                        await self._mark_recipient(
                            int(payload["recipient_id"]),
                            int(payload["campaign_id"]),
                            RecipientStatus.FAILED,
                            message[:200],
                        )
            else:
                delay = min(MAX_BACKOFF_SECONDS, 2 ** min(job.attempt, 9))
                job.status = JobStatus.QUEUED
                job.scheduled_at = utcnow() + timedelta(seconds=delay)
                job.last_error = message
                log.warning("job %s (%s) failed, retry in %ss: %s", job.id, job.kind, delay, message)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------
    async def _fulfill(self, payload: dict) -> None:
        order_id = int(payload["order_id"])

        async def deliver(
            telegram_id: int, config: str, sub_url: str | None, product_title: str, order_ref: str
        ) -> None:
            await send_config_pair(self.bot, telegram_id, config, sub_url, product_title, order_ref)

        result = await fulfillment.fulfill_order(
            self.session_factory, order_id=order_id, cipher=self.cipher, deliver=deliver
        )
        log.info("fulfillment order=%s outcome=%s", order_id, result.outcome.value)
        if result.outcome is fulfillment.FulfillOutcome.RETRY:
            raise RuntimeError(f"delivery failed: {result.detail}")
        if result.outcome is fulfillment.FulfillOutcome.NO_INVENTORY:
            # Paid order waiting for stock: keep it queued, but not hot.
            raise RuntimeError("no inventory available for paid order")

    async def _notify(self, payload: dict) -> None:
        chat_id = int(payload["chat_id"])
        text = str(payload.get("text") or "")
        await send_plain(self.bot, chat_id, text, _markup_from_payload(payload.get("reply_markup")))

    async def _broadcast_dispatch(self, payload: dict) -> None:
        campaign_id = int(payload["campaign_id"])
        async with self.session_factory() as session, session.begin():
            campaign = (
                await session.execute(
                    select(BroadcastCampaign)
                    .where(BroadcastCampaign.id == campaign_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if campaign is None:
                return
            if campaign.status is CampaignStatus.PAUSED:
                # Paused is a state, not a failure: finish quietly so the
                # job does not burn its retry attempts while an admin has
                # it paused. Resuming re-enqueues (and revives) dispatch.
                return
            if campaign.status is not CampaignStatus.RUNNING:
                return

            # Fan out every still-pending recipient into its own job. Keys are
            # deterministic, so a retried dispatch never duplicates a send.
            # Recipients stay PENDING until their job runs, so the page cursor
            # has to advance on id — LIMIT alone would reselect the same rows.
            cursor = 0
            while True:
                recipients = list(
                    (
                        await session.execute(
                            select(BroadcastRecipient)
                            .where(
                                BroadcastRecipient.campaign_id == campaign_id,
                                BroadcastRecipient.status == RecipientStatus.PENDING,
                                BroadcastRecipient.id > cursor,
                            )
                            .order_by(BroadcastRecipient.id)
                            .limit(DISPATCH_PAGE)
                        )
                    ).scalars()
                )
                if not recipients:
                    break
                cursor = recipients[-1].id
                rows = [
                    {
                        "kind": JobKind.BROADCAST_RECIPIENT,
                        "payload": {"campaign_id": campaign_id, "recipient_id": r.id},
                        "idempotency_key": f"bc:{campaign_id}:{r.id}",
                        "status": JobStatus.QUEUED,
                        "scheduled_at": utcnow(),
                    }
                    for r in recipients
                ]
                await session.execute(
                    pg_insert(Job.__table__)
                    .values(rows)
                    # Revive a DONE/FAILED job for this recipient (e.g. one
                    # that finished quietly while the campaign was paused);
                    # a live QUEUED/RUNNING job must not be reset.
                    .on_conflict_do_update(
                        index_elements=["idempotency_key"],
                        set_={
                            "status": JobStatus.QUEUED,
                            "attempt": 0,
                            "scheduled_at": utcnow(),
                            "finished_at": None,
                            "last_error": None,
                        },
                        where=Job.__table__.c.status.in_([JobStatus.DONE, JobStatus.FAILED]),
                    )
                )
                if len(recipients) < DISPATCH_PAGE:
                    break

            pending = int(
                (
                    await session.execute(
                        select(func.count(BroadcastRecipient.id)).where(
                            BroadcastRecipient.campaign_id == campaign_id,
                            BroadcastRecipient.status == RecipientStatus.PENDING,
                        )
                    )
                ).scalar_one()
            )
            if pending == 0:
                campaign.status = CampaignStatus.FINISHED
                campaign.finished_at = utcnow()
                await notify.enqueue(
                    session,
                    chat_id=self._owner_chat(),
                    text=f"📨 کمپین #{campaign.id} پایان یافت.\n"
                    f"✅ {campaign.sent_count} · ⚠️ {campaign.failed_count} · "
                    f"⏭ {campaign.skipped_count}",
                )

    async def _broadcast_recipient(self, payload: dict) -> None:
        campaign_id = int(payload["campaign_id"])
        recipient_id = int(payload["recipient_id"])

        async with self.session_factory() as session:
            recipient = await session.get(BroadcastRecipient, recipient_id)
            campaign = await session.get(BroadcastCampaign, campaign_id)
            if recipient is None or campaign is None:
                return

        if recipient.status is not RecipientStatus.PENDING:
            return
        if campaign.status is CampaignStatus.STOPPED:
            await self._mark_recipient(recipient_id, campaign_id, RecipientStatus.SKIPPED)
            return
        if campaign.status is CampaignStatus.PAUSED:
            # Quiet finish, no retry burn (see _broadcast_dispatch); the
            # recipient stays PENDING and is re-fanned-out on resume.
            return
        if campaign.status is not CampaignStatus.RUNNING:
            return

        await self.limiter.acquire()
        try:
            await self._send_broadcast(recipient.telegram_id, campaign)
        except TelegramForbiddenError:
            await self._mark_recipient(
                recipient_id, campaign_id, RecipientStatus.SKIPPED, "bot blocked"
            )
            return
        except TelegramRetryAfter as exc:
            raise RuntimeError(f"rate limited for {exc.retry_after}s") from exc
        except Exception as exc:  # noqa: BLE001 - recorded per recipient, job retried
            # A transient failure must NOT mark the recipient FAILED:
            # the job will be retried, and the retry used to see a
            # non-PENDING status and silently give up — the message was
            # lost forever. Record the attempt, keep PENDING, and let
            # the job layer decide: _fail marks FAILED only when the
            # job's attempts are exhausted.
            await self._note_recipient_error(recipient_id, str(exc)[:200])
            raise
        await self._mark_recipient(recipient_id, campaign_id, RecipientStatus.SENT)

    async def _note_recipient_error(self, recipient_id: int, error: str) -> None:
        async with self.session_factory() as session, session.begin():
            recipient = await session.get(BroadcastRecipient, recipient_id)
            if recipient is not None and recipient.status is RecipientStatus.PENDING:
                recipient.attempts += 1
                recipient.last_error = error

    async def _send_broadcast(self, telegram_id: int, campaign: BroadcastCampaign) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        rows = []
        for row in campaign.buttons or []:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=str(button.get("text", "—")),
                        url=button.get("url") or None,
                        callback_data=button.get("callback_data") or None,
                    )
                    for button in row
                ]
            )
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        if campaign.media_file_id and campaign.media_type:
            mtype = campaign.media_type.upper()
            if mtype == "PHOTO":
                await self.bot.send_photo(
                    telegram_id, campaign.media_file_id, caption=campaign.message_text,
                    reply_markup=markup, parse_mode="HTML",
                )
                return
            if mtype == "VIDEO":
                await self.bot.send_video(
                    telegram_id, campaign.media_file_id, caption=campaign.message_text,
                    reply_markup=markup, parse_mode="HTML",
                )
                return
            await self.bot.send_document(
                telegram_id, campaign.media_file_id, caption=campaign.message_text,
                reply_markup=markup, parse_mode="HTML",
            )
            return
        await self.bot.send_message(
            telegram_id, campaign.message_text, reply_markup=markup,
            parse_mode="HTML", disable_web_page_preview=True,
        )

    async def _mark_recipient(
        self,
        recipient_id: int,
        campaign_id: int,
        status: RecipientStatus,
        error: str | None = None,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            recipient = (
                await session.execute(
                    select(BroadcastRecipient)
                    .where(BroadcastRecipient.id == recipient_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if recipient is None or recipient.status is not RecipientStatus.PENDING:
                return
            recipient.status = status
            recipient.attempts += 1
            recipient.last_error = error
            if status is RecipientStatus.SENT:
                recipient.sent_at = utcnow()

            delta = {
                RecipientStatus.SENT: "sent_count",
                RecipientStatus.FAILED: "failed_count",
                RecipientStatus.SKIPPED: "skipped_count",
                RecipientStatus.UNREACHABLE: "skipped_count",
            }.get(status)
            if delta:
                # Atomic SQL increment: recipient jobs run in parallel
                # (asyncio.gather) in separate sessions, so a Python-side
                # read-modify-write on the campaign row lost updates.
                column = getattr(BroadcastCampaign, delta)
                await session.execute(
                    update(BroadcastCampaign)
                    .where(BroadcastCampaign.id == campaign_id)
                    .values({delta: column + 1})
                )

            pending = int(
                (
                    await session.execute(
                        select(func.count(BroadcastRecipient.id)).where(
                            BroadcastRecipient.campaign_id == campaign_id,
                            BroadcastRecipient.status == RecipientStatus.PENDING,
                        )
                    )
                ).scalar_one()
            )
            if pending == 0:
                campaign = await session.get(BroadcastCampaign, campaign_id)
                if campaign is not None and campaign.status is CampaignStatus.RUNNING:
                    await session.refresh(campaign)  # fresh counters first
                    campaign.status = CampaignStatus.FINISHED
                    campaign.finished_at = utcnow()
                    # The dispatch path notifies the owner when it
                    # completes a campaign itself; when the LAST
                    # recipient finishes it, nobody did — until now.
                    if self.settings.owner_ids:
                        await notify.enqueue(
                            session,
                            chat_id=self._owner_chat(),
                            text=f"📨 کمپین #{campaign.id} پایان یافت.\n"
                            f"✅ {campaign.sent_count} · ⚠️ {campaign.failed_count} · "
                            f"⏭ {campaign.skipped_count}",
                        )

    async def _expire_payments(self, payload: dict) -> None:
        async with self.session_factory() as session, session.begin():
            expired = await payment_service.expire_stale_payments(session)
            if expired:
                log.info("expired %s stale payments", expired)
            expired_topups = await topup_service.expire_stale_topups(session)
            if expired_topups:
                log.info("expired %s stale wallet top-ups", expired_topups)
            from vpnbot.services import orders as order_service

            cancelled_orders = await order_service.expire_stale_orders(session)
            if cancelled_orders:
                log.info("auto-cancelled %s stale unpaid orders", cancelled_orders)

    def _owner_chat(self) -> int:
        ids = self.settings.owner_ids
        if not ids:
            raise RuntimeError("OWNER_TELEGRAM_IDS is empty; cannot notify an owner")
        return ids[0]


async def sweep_expired_payments_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """One-shot helper used by the CLI and tests."""
    async with session_factory() as session, session.begin():
        return await payment_service.expire_stale_payments(session)


__all__ = ["RateLimiter", "Worker", "sweep_expired_payments_once"]


def _markup_from_payload(raw: object):
    """Rebuild ``InlineKeyboardMarkup`` from a queued JSON payload.

    Accepts a Telegram-style dict (``{"inline_keyboard": [...]}``) or a bare
    list of rows, which is how campaign buttons are stored.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if raw is None:
        return None
    if isinstance(raw, dict):
        rows = raw.get("inline_keyboard") or []
    elif isinstance(raw, list):
        rows = raw
    else:
        return None

    keyboard = []
    for row in rows:
        line = []
        for button in row:
            if not isinstance(button, dict):
                continue
            line.append(
                InlineKeyboardButton(
                    text=str(button.get("text", "—"))[:64],
                    url=button.get("url") or None,
                    callback_data=button.get("callback_data") or None,
                )
            )
        if line:
            keyboard.append(line)
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None

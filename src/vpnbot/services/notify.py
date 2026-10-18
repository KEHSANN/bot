"""Queued outbound Telegram messages.

Everything that a customer or admin should receive asynchronously goes
through the ``jobs`` table (kind ``NOTIFY_ADMIN``) rather than being sent
inline inside a request handler. Two reasons:

* A handler's transaction may roll back after a message was already sent,
  leaving the chat and the database disagreeing.
* A bot restart mid-flow would silently drop the message.

The worker is the only sender, so retries and rate limiting live in one place.
"""

from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.base import utcnow
from vpnbot.db.enums import JobKind, JobStatus
from vpnbot.db.models.system import Job

KIND = JobKind.NOTIFY_ADMIN


def _key(chat_id: int) -> str:
    return f"notify:{chat_id}:{secrets.token_hex(8)}"


async def enqueue(
    session: AsyncSession,
    *,
    chat_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    parse_mode: str = "HTML",
) -> None:
    """Queue one plain message to ``chat_id``.

    ``idempotency_key`` is optional: use it when the same notification must
    never be produced twice (for example "your order was approved").
    """
    if not text or not text.strip():
        raise ValueError("cannot enqueue an empty notification")
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    await session.execute(
        pg_insert(Job)
        .values(
            kind=KIND,
            payload=payload,
            idempotency_key=idempotency_key or _key(chat_id),
            status=JobStatus.QUEUED,
            scheduled_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )


async def enqueue_many(session: AsyncSession, messages: list[tuple[int, str]]) -> None:
    for chat_id, text in messages:
        await enqueue(session, chat_id=chat_id, text=text)


async def pending_count(session: AsyncSession) -> int:
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                select(func.count(Job.id)).where(
                    Job.kind == KIND, Job.status == JobStatus.QUEUED
                )
            )
        ).scalar_one()
    )


__all__ = ["enqueue", "enqueue_many", "pending_count", "KIND"]

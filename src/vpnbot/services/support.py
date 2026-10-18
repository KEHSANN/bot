"""Support tickets: customer conversations with administrators."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.base import utcnow
from vpnbot.db.enums import TicketSender, TicketStatus
from vpnbot.db.models.support import Ticket, TicketMessage
from vpnbot.db.models.users import User

MAX_MESSAGE_LEN = 3000
MAX_SUBJECT_LEN = 200


class SupportError(ValueError):
    pass


def _subject_from(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else "درخواست پشتیبانی"
    return first_line[:MAX_SUBJECT_LEN]


async def create_ticket(
    session: AsyncSession, *, user: User, text: str
) -> Ticket:
    text = (text or "").strip()
    if len(text) < 3:
        raise SupportError("متن پیام خیلی کوتاه است")
    if len(text) > MAX_MESSAGE_LEN:
        raise SupportError(f"متن پیام بیش از {MAX_MESSAGE_LEN} کاراکتر است")

    now = utcnow()
    ticket = Ticket(
        user_id=user.id,
        subject=_subject_from(text),
        status=TicketStatus.OPEN,
        last_message_at=now,
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketMessage(
            ticket_id=ticket.id,
            sender_type=TicketSender.CUSTOMER,
            sender_id=user.id,
            text=text,
        )
    )
    await session.flush()
    return ticket


async def add_message(
    session: AsyncSession,
    *,
    ticket: Ticket,
    text: str,
    sender_type: TicketSender,
    sender_id: int | None,
) -> TicketMessage:
    text = (text or "").strip()
    if not text:
        raise SupportError("متن پیام خالی است")
    if len(text) > MAX_MESSAGE_LEN:
        raise SupportError(f"متن پیام بیش از {MAX_MESSAGE_LEN} کاراکتر است")

    message = TicketMessage(
        ticket_id=ticket.id, sender_type=sender_type, sender_id=sender_id, text=text
    )
    session.add(message)
    ticket.last_message_at = utcnow()
    if sender_type is TicketSender.ADMIN:
        ticket.status = TicketStatus.ANSWERED
    elif ticket.status is TicketStatus.CLOSED:
        ticket.status = TicketStatus.OPEN
    await session.flush()
    return message


async def open_ticket_for_user(session: AsyncSession, user_id: int) -> Ticket | None:
    return (
        await session.execute(
            select(Ticket)
            .where(Ticket.user_id == user_id, Ticket.status != TicketStatus.CLOSED)
            .order_by(Ticket.last_message_at.desc(), Ticket.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_tickets(
    session: AsyncSession,
    *,
    status: TicketStatus | None = None,
    offset: int = 0,
    limit: int = 10,
) -> list[Ticket]:
    stmt = select(Ticket).order_by(Ticket.last_message_at.desc(), Ticket.id.desc())
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    return list((await session.execute(stmt.offset(offset).limit(limit))).scalars())


async def count_tickets(session: AsyncSession, *, status: TicketStatus | None = None) -> int:
    stmt = select(func.count(Ticket.id))
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    return int((await session.execute(stmt)).scalar_one())


async def ticket_messages(session: AsyncSession, ticket_id: int) -> list[TicketMessage]:
    return list(
        (
            await session.execute(
                select(TicketMessage)
                .where(TicketMessage.ticket_id == ticket_id)
                .order_by(TicketMessage.id.asc())
            )
        ).scalars()
    )

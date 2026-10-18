"""Append-only audit logging."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.enums import AuditActor
from vpnbot.db.models.system import AuditEvent


async def record(
    session: AsyncSession,
    *,
    actor_type: AuditActor,
    actor_id: int | None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    data: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        data=data or {},
    )
    session.add(event)
    await session.flush()
    return event

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Select, or_, select

from .models import OutboxMessage


def pending_outbox_query(
    now: datetime, *, limit: int = 100, visibility_timeout: timedelta = timedelta(minutes=5)
) -> Select[tuple[OutboxMessage]]:
    """Claim candidates across workers; abandoned claims become visible later."""
    stale_claim = now - visibility_timeout
    return (
        select(OutboxMessage)
        .where(
            OutboxMessage.delivered_at.is_(None),
            OutboxMessage.dead_at.is_(None),
            OutboxMessage.available_at <= now,
            or_(OutboxMessage.claimed_at.is_(None), OutboxMessage.claimed_at < stale_claim),
        )
        .order_by(OutboxMessage.available_at, OutboxMessage.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )

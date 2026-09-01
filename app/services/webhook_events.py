"""The webhook inbox.

Meta retries aggressively and duplicates happily. Rather than scatter dedupe
logic through the handlers, every delivery is written once to `webhook_events`
with a unique key derived from Meta's own identifiers:

    whatsapp message -> wamid.XXXX
    delivery receipt -> wamid.XXXX:delivered
    lead ad          -> leadgen:1234567890

The unique constraint is the guarantee. Redis in front of it is only an
optimisation, and the system is still correct when Redis is down or empty.

The table doubles as a durable queue: the HTTP handler stores and returns 200
immediately, and a worker does the slow part (OpenAI, Graph API) afterwards.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain import WebhookEventType, WebhookStatus
from app.integrations.meta.schemas import ParsedEvent
from app.models import WebhookEvent
from app.services.base import insert_or_get

logger = get_logger(__name__)


async def record(
    session: AsyncSession, event: ParsedEvent, *, provider: str = "meta"
) -> tuple[WebhookEvent, bool]:
    """Store an event. `created=False` means Meta sent us a duplicate."""
    row, created = await insert_or_get(
        session,
        WebhookEvent,
        defaults={
            "event_type": WebhookEventType(event.kind),
            "payload": event.model_dump(mode="json"),
            "status": WebhookStatus.PENDING,
        },
        provider=provider,
        event_key=event.event_key,
    )
    if not created:
        logger.info(
            "duplicate webhook event ignored",
            extra={"event_key": event.event_key, "status": row.status},
        )
    return row, created


async def claim(session: AsyncSession, event_id: uuid.UUID) -> WebhookEvent | None:
    """Take ownership of an event that still has work left to do.

    Same conditional-update trick as reminders: exactly one worker can move a
    row out of `pending`, so a duplicated job in the Redis queue is harmless.

    The attempt cap is enforced here rather than only in the sweeper, so a
    redelivered job cannot retry a parked event forever.
    """
    result = await session.execute(
        update(WebhookEvent)
        .where(
            WebhookEvent.id == event_id,
            WebhookEvent.status.in_([WebhookStatus.PENDING, WebhookStatus.FAILED]),
            WebhookEvent.processed_at.is_(None),
            WebhookEvent.attempts < get_settings().worker_max_attempts,
        )
        .values(status=WebhookStatus.PROCESSING)
    )
    if not result.rowcount:
        return None

    event = await session.get(WebhookEvent, event_id)
    if event is not None:
        event.attempts += 1
        await session.flush()
    return event


async def mark_processed(session: AsyncSession, event: WebhookEvent) -> None:
    event.status = WebhookStatus.PROCESSED
    event.processed_at = utcnow()
    event.last_error = None
    await session.flush()


async def mark_ignored(session: AsyncSession, event: WebhookEvent, reason: str) -> None:
    event.status = WebhookStatus.IGNORED
    event.processed_at = utcnow()
    event.last_error = reason
    await session.flush()


async def mark_failed(
    session: AsyncSession, event: WebhookEvent, error: str, *, retryable: bool
) -> None:
    """Record a failure.

    A retryable failure goes back to `failed` where the sweeper will pick it up
    again, until `worker_max_attempts` is exhausted. A permanent failure is
    parked immediately - retrying a malformed payload forever helps nobody.
    """
    settings = get_settings()
    will_retry = retryable and event.attempts < settings.worker_max_attempts

    event.status = WebhookStatus.FAILED
    event.last_error = error[:2000]
    if not will_retry:
        # Parked: `processed_at` marks it as finished-with, which is what stops
        # both the sweeper and `claim` from picking it up again.
        event.processed_at = utcnow()
    await session.flush()
    logger.warning(
        "webhook event failed",
        extra={
            "event_id": str(event.id),
            "event_key": event.event_key,
            "attempts": event.attempts,
            "will_retry": will_retry,
        },
    )


async def stale_event_ids(session: AsyncSession, limit: int = 50) -> list[uuid.UUID]:
    """Events that were stored but never finished.

    Covers the two ways the fast path can lose work: Redis was unavailable when
    the API tried to enqueue, or a worker died mid-job. Anything older than the
    sweep threshold and not yet processed is fair game.
    """
    settings = get_settings()
    cutoff = utcnow() - timedelta(seconds=settings.webhook_sweep_after_seconds)
    stmt = (
        select(WebhookEvent.id)
        .where(
            WebhookEvent.status.in_(
                [WebhookStatus.PENDING, WebhookStatus.PROCESSING, WebhookStatus.FAILED]
            ),
            WebhookEvent.received_at <= cutoff,
            WebhookEvent.attempts < settings.worker_max_attempts,
        )
        .order_by(WebhookEvent.received_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def reset_for_retry(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Return a stuck `processing` row to `pending` so it can be claimed again."""
    await session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.id == event_id, WebhookEvent.status == WebhookStatus.PROCESSING)
        .values(status=WebhookStatus.PENDING)
    )

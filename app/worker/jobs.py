"""Job handlers.

One function per job type, each responsible for claiming its own work and
recording the outcome. Claiming is a conditional UPDATE in PostgreSQL, so a
duplicated job (Redis redelivery, or the sweeper racing the queue) simply finds
nothing to claim and returns.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.db import session_scope
from app.core.errors import PermanentError, RetryableError
from app.core.logging import get_logger
from app.core.trace import trace
from app.domain import WebhookEventType
from app.integrations.meta.schemas import (
    InboundMessageEvent,
    LeadgenEvent,
    MessageStatusEvent,
    ParsedEvent,
)
from app.models import WebhookEvent
from app.services import conversation_flow, webhook_events
from app.worker.queue import JOB_REMINDER, JOB_WEBHOOK_EVENT

logger = get_logger(__name__)

_EVENT_MODELS: dict[WebhookEventType, type] = {
    WebhookEventType.WHATSAPP_MESSAGE: InboundMessageEvent,
    WebhookEventType.WHATSAPP_STATUS: MessageStatusEvent,
    WebhookEventType.LEADGEN: LeadgenEvent,
}


async def process_webhook_event(event_id: uuid.UUID) -> None:
    """Claim a stored webhook event and run the flow it belongs to."""
    async with session_scope() as session:
        event = await webhook_events.claim(session, event_id)
        if event is None:
            trace("skip", "event not claimable - already done, taken, or parked", id=str(event_id)[:8])
            logger.debug("webhook event not claimable", extra={"event_id": str(event_id)})
            return
        event_type = event.event_type
        payload = dict(event.payload)
        trace("worker", "claimed event", type=str(event_type), attempt=event.attempts)

    model = _EVENT_MODELS.get(event_type)
    if model is None:
        await _finish(event_id, ignored=f"unhandled event type {event_type}")
        return

    try:
        parsed: ParsedEvent = model.model_validate(payload)
        await _dispatch(parsed)
    except PermanentError as exc:
        trace("guard", "PERMANENT failure - parked, will not retry", error=str(exc))
        logger.error(
            "webhook event permanently failed",
            extra={"event_id": str(event_id), "error": str(exc)},
        )
        await _record_failure(event_id, str(exc), retryable=False)
    except RetryableError as exc:
        trace("worker", "RETRYABLE failure - the sweeper will try again", error=str(exc))
        await _record_failure(event_id, str(exc), retryable=True)
        raise
    except Exception as exc:  # noqa: BLE001 - unknown failures get one more try
        logger.exception("webhook event failed", extra={"event_id": str(event_id)})
        await _record_failure(event_id, repr(exc), retryable=True)
        raise
    else:
        trace("worker", "event processed")
        await _finish(event_id)


async def _dispatch(event: ParsedEvent) -> None:
    if isinstance(event, InboundMessageEvent):
        await conversation_flow.handle_inbound_message(event)
    elif isinstance(event, MessageStatusEvent):
        await conversation_flow.handle_message_status(event)
    elif isinstance(event, LeadgenEvent):
        await conversation_flow.handle_leadgen(event)
    else:  # pragma: no cover - guarded by _EVENT_MODELS
        raise PermanentError(f"no handler for {type(event).__name__}")


async def _finish(event_id: uuid.UUID, ignored: str | None = None) -> None:
    async with session_scope() as session:
        stored = await session.get(WebhookEvent, event_id)
        if stored is None:  # pragma: no cover - defensive
            return
        if ignored:
            await webhook_events.mark_ignored(session, stored, ignored)
        else:
            await webhook_events.mark_processed(session, stored)


async def _record_failure(event_id: uuid.UUID, error: str, *, retryable: bool) -> None:
    async with session_scope() as session:
        stored = await session.get(WebhookEvent, event_id)
        if stored is not None:
            await webhook_events.mark_failed(session, stored, error, retryable=retryable)


async def process_reminder(reminder_id: uuid.UUID) -> None:
    await conversation_flow.send_follow_up(reminder_id)


async def run_job(job: dict[str, Any]) -> None:
    """Route one dequeued job to its handler."""
    job_type = job.get("type")
    payload = job.get("payload") or {}

    if job_type == JOB_WEBHOOK_EVENT:
        await process_webhook_event(uuid.UUID(str(payload["event_id"])))
    elif job_type == JOB_REMINDER:
        await process_reminder(uuid.UUID(str(payload["reminder_id"])))
    else:
        logger.error("unknown job type", extra={"job_type": job_type})

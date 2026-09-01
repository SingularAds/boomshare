"""Meta webhook endpoints.

The handler does the least work that is still correct:

    verify signature -> parse -> store -> enqueue -> 200

Everything slow (OpenAI, the Graph API, sending messages) happens in the worker.
Meta expects a fast 200 and retries anything else, so the endpoint must not
block on an external service.

Failure semantics are deliberate:
  * bad signature      -> 403, and nothing is stored
  * cannot store       -> 500, so Meta retries (we would rather be retried than
                          silently drop a customer's message)
  * stored but not     -> 200, the scheduler sweep will pick the event up
    enqueued
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import settings_dep
from app.core import redis as redis_helper
from app.core.config import Settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.core.trace import banner, dump, trace
from app.integrations.meta.parser import parse_webhook
from app.integrations.meta.signature import verify_signature, verify_subscription_token
from app.services import webhook_events
from app.worker import queue

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: Meta's own retry window is long; a day of dedupe keys is plenty and cheap.
_IDEMPOTENCY_TTL_SECONDS = 24 * 3600


@router.get("/meta")
async def verify_subscription(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    settings: Settings = Depends(settings_dep),
) -> Response:
    """Subscription handshake. Meta calls this once when the webhook is saved."""
    expected = settings.meta_verify_token.get_secret_value()
    if hub_mode != "subscribe" or not verify_subscription_token(expected, hub_verify_token):
        logger.warning("webhook verification rejected", extra={"mode": hub_mode})
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="verification failed")

    logger.info("webhook subscription verified")
    return Response(content=hub_challenge or "", media_type="text/plain")


@router.post("/meta")
async def receive(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    settings: Settings = Depends(settings_dep),
    session: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    # The signature covers the exact bytes Meta sent. Re-serialising the parsed
    # JSON would change key order and whitespace, and the HMAC would not match.
    raw_body = await request.body()

    banner("POST /webhooks/meta")
    trace("webhook", "received delivery", bytes=len(raw_body), signed=bool(x_hub_signature_256))

    if not verify_signature(settings.meta_app_secret.get_secret_value(), raw_body, x_hub_signature_256):
        trace("guard", "REFUSED - signature does not match X-Hub-Signature-256")
        logger.warning("rejected webhook with invalid signature")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        # Signed but unparseable: retrying will not help, so accept and drop.
        logger.error("webhook body was not valid json")
        return {"received": 0, "accepted": 0}

    trace("webhook", "signature verified")
    dump("raw webhook payload", payload)

    events = parse_webhook(payload)
    trace("webhook", "parsed", events=[type(e).__name__ for e in events] or "none")
    if not events:
        return {"received": 0, "accepted": 0}

    accepted = 0
    to_enqueue: list[str] = []

    for event in events:
        # Fast path: skip a repeat delivery without touching the database. The
        # unique constraint below is still the real guarantee.
        if not await redis_helper.idempotency_guard(
            f"meta:{event.event_key}", _IDEMPOTENCY_TTL_SECONDS
        ):
            trace("skip", "duplicate suppressed by redis cache", key=event.event_key)
            logger.info("webhook duplicate skipped by cache", extra={"event_key": event.event_key})
            continue

        row, created = await webhook_events.record(session, event)
        if created:
            trace("db", "webhook_events row inserted", key=event.event_key, id=str(row.id)[:8])
            accepted += 1
            to_enqueue.append(str(row.id))
        else:
            trace("skip", "duplicate suppressed by unique constraint", key=event.event_key)

    # Commit before enqueuing: the worker must never look up a row that the API
    # has not written yet.
    await session.commit()

    for event_id in to_enqueue:
        queued = await queue.enqueue(queue.JOB_WEBHOOK_EVENT, {"event_id": event_id})
        trace(
            "webhook",
            "queued for the worker" if queued else "queue unavailable - the sweeper will pick it up",
            id=event_id[:8],
        )

    logger.info("webhook accepted", extra={"received": len(events), "accepted": accepted})
    return {"received": len(events), "accepted": accepted}

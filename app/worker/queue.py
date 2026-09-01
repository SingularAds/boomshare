"""A small Redis job queue.

A Redis list plus `BRPOP` is all the MVP needs, and it is genuinely simple:
no broker to operate, no result backend, no task registry. Two job types exist
(process a webhook event, send a reminder) and both carry nothing but an id.

The queue is intentionally *not* the source of truth. Every job refers to a row
in PostgreSQL that already exists, so a lost job costs a delay, not data - the
scheduler's sweep re-enqueues anything the queue dropped.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

JOB_WEBHOOK_EVENT = "webhook_event"
JOB_REMINDER = "reminder"


def _queue_name() -> str:
    return get_settings().worker_queue_name


async def enqueue(job_type: str, payload: dict[str, Any]) -> bool:
    """Push a job. Returns False if Redis is unreachable.

    A False here is not an error the caller needs to handle: the durable row is
    already written and the sweeper will pick it up.
    """
    body = json.dumps({"type": job_type, "payload": payload})
    try:
        await get_redis().lpush(_queue_name(), body)
        return True
    except Exception as exc:  # noqa: BLE001 - queue is best-effort
        logger.warning("could not enqueue job", extra={"job_type": job_type, "error": str(exc)})
        return False


async def dequeue(timeout_seconds: int = 5) -> dict[str, Any] | None:
    """Block until a job arrives or the timeout expires."""
    try:
        result = await get_redis().brpop(_queue_name(), timeout=timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("queue read failed", extra={"error": str(exc)})
        return None

    if not result:
        return None

    _, body = result
    try:
        job = json.loads(body)
    except json.JSONDecodeError:
        logger.error("discarding malformed job", extra={"body": str(body)[:200]})
        return None

    return job if isinstance(job, dict) and job.get("type") else None


async def depth() -> int:
    try:
        return int(await get_redis().llen(_queue_name()))
    except Exception:  # noqa: BLE001
        return -1

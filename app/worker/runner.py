"""The background worker process.

Two coroutines run side by side:

  * **consumer** - blocks on the Redis queue and runs jobs as they arrive. This
    is the fast path: a webhook stored by the API is usually processed within
    milliseconds.
  * **scheduler** - wakes on a timer and enqueues (a) reminders that have come
    due and (b) webhook events that were stored but never finished, because
    Redis was down when the API tried to enqueue them or a worker died
    mid-job.

The scheduler is what makes the queue disposable. If Redis were flushed right
now, nothing would be lost - only delayed by one sweep interval.

Run with:  python -m app.worker.runner
"""

from __future__ import annotations

import asyncio
import signal

from app.core.config import get_settings
from app.core.db import dispose_engine, session_scope
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis
from app.core.trace import guard_production as guard_trace_in_production
from app.services import reminders as reminder_service, webhook_events
from app.worker import queue
from app.worker.jobs import run_job

logger = get_logger(__name__)


async def consume(stop: asyncio.Event) -> None:
    settings = get_settings()
    while not stop.is_set():
        job = await queue.dequeue(timeout_seconds=int(settings.worker_poll_interval_seconds) or 1)
        if job is None:
            continue
        try:
            await run_job(job)
        except Exception:  # noqa: BLE001 - one bad job must not kill the worker
            logger.exception("job failed", extra={"job_type": job.get("type")})


async def sweep_due_reminders() -> int:
    async with session_scope() as session:
        ids = await reminder_service.due_reminder_ids(session, limit=get_settings().worker_batch_size)
    for reminder_id in ids:
        await queue.enqueue(queue.JOB_REMINDER, {"reminder_id": str(reminder_id)})
    return len(ids)


async def sweep_stuck_webhooks() -> int:
    """Re-enqueue events that never reached a terminal state."""
    async with session_scope() as session:
        ids = await webhook_events.stale_event_ids(session, limit=get_settings().worker_batch_size)
        for event_id in ids:
            await webhook_events.reset_for_retry(session, event_id)
    for event_id in ids:
        await queue.enqueue(queue.JOB_WEBHOOK_EVENT, {"event_id": str(event_id)})
    return len(ids)


async def schedule(stop: asyncio.Event) -> None:
    interval = get_settings().scheduler_interval_seconds
    while not stop.is_set():
        try:
            reminders = await sweep_due_reminders()
            stuck = await sweep_stuck_webhooks()
            if reminders or stuck:
                logger.info("sweep complete", extra={"reminders": reminders, "webhooks": stuck})
        except Exception:  # noqa: BLE001
            logger.exception("scheduler sweep failed")

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    guard_trace_in_production()
    logger.info("worker starting", extra={"queue": settings.worker_queue_name})

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows does not support add_signal_handler; KeyboardInterrupt
            # still unwinds the loop below.
            signal.signal(sig, lambda *_: stop.set())

    try:
        await asyncio.gather(consume(stop), schedule(stop))
    finally:
        await close_redis()
        await dispose_engine()
        logger.info("worker stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        pass

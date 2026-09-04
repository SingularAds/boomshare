"""The background worker process.

Three coroutines run side by side:

  * **consumer** - blocks on the Redis queue and runs jobs as they arrive. This
    is the fast path: a webhook stored by the API is usually processed within
    milliseconds.
  * **scheduler** - wakes on a timer and enqueues (a) reminders that have come
    due and (b) webhook events that were stored but never finished, because
    Redis was down when the API tried to enqueue them or a worker died
    mid-job.
  * **health listener** - answers the orchestrator's probe on ``$PORT``. The
    worker serves no API, but Cloud Run and friends need every container to
    listen somewhere. See ``app/worker/health.py``.

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
from app.worker.health import serve_health
from app.worker.jobs import run_job

logger = get_logger(__name__)


async def _run_one(job: dict) -> None:
    """Run a single job, swallowing its failure.

    A job that raises has already recorded its own outcome on the row it was
    working on (see `app.worker.jobs`), so there is nothing left for the loop to
    do about it except keep going.
    """
    try:
        await run_job(job)
    except Exception:  # noqa: BLE001 - one bad job must not kill the worker
        logger.exception("job failed", extra={"job_type": job.get("type")})


async def consume(stop: asyncio.Event) -> None:
    """Drain the queue, up to `worker_concurrency` jobs at a time.

    A turn is almost entirely waiting - on OpenAI, on the Graph API, on the
    database - so running jobs one at a time left every other customer queued
    behind whichever model call happened to be in flight. Overlapping them costs
    nothing but connections, and the reply path no longer holds one across the
    model call.

    Two messages in the *same* conversation still serialise: they contend on the
    Redis conversation lock, which is now a short wait rather than a failure.
    """
    settings = get_settings()
    poll = int(settings.worker_poll_interval_seconds) or 1
    limit = max(settings.worker_concurrency, 1)
    running: set[asyncio.Task[None]] = set()

    try:
        while not stop.is_set():
            if len(running) >= limit:
                # Every slot is busy. Wait for one to free up rather than
                # pulling a job off the queue we cannot start - a job held in
                # memory is a job the sweeper cannot see.
                await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                continue

            job = await queue.dequeue(timeout_seconds=poll)
            if job is None:
                # A healthy BRPOP already blocked for `poll` seconds. When Redis
                # is unreachable, dequeue returns immediately - so pause here, or
                # the loop spins and floods the log for the length of the
                # outage. The scheduler sweep still recovers the work.
                await asyncio.sleep(poll)
                continue

            task = asyncio.create_task(_run_one(job))
            running.add(task)
            # Keeping a strong reference until completion is not optional: the
            # event loop only holds a weak one, so an unreferenced task can be
            # garbage collected mid-flight.
            task.add_done_callback(running.discard)
    finally:
        if running:
            # Finish what was started before reporting the loop as stopped, so a
            # shutdown does not abandon a half-sent reply.
            await asyncio.gather(*running, return_exceptions=True)


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
        # serve_health answers the platform's probe on $PORT; without it a
        # Cloud Run worker revision never goes live.
        await asyncio.gather(consume(stop), schedule(stop), serve_health(stop))
    finally:
        await close_redis()
        await dispose_engine()
        logger.info("worker stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        pass

"""Test helpers for driving the background worker in-process."""

from __future__ import annotations

from app.worker import jobs, queue


async def drain_queue(max_jobs: int = 50) -> int:
    """Run every queued job, the way the worker process would.

    Tests use this instead of calling handlers directly, so the queue,
    the claim logic and the job routing are all exercised for real.
    """
    processed = 0
    while processed < max_jobs:
        if await queue.depth() <= 0:
            break
        job = await queue.dequeue(timeout_seconds=1)
        if job is None:
            break
        await jobs.run_job(job)
        processed += 1
    return processed

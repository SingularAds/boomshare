"""Redis access.

Redis is a *helper*, never the source of truth. Everything written here has a
TTL because the MVP runs on a 30MB free tier. Three concrete uses:

  * `idempotency_guard` - cheap duplicate suppression in front of the database
  * `lock`              - stops two workers replying to the same conversation
  * `rate_limit`        - per-sender inbound throttle

The job queue lives in `app.worker.queue` and also uses this client.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
    return _client


def set_redis(client: Redis | None) -> None:
    """Inject a client (the test suite passes a fakeredis instance)."""
    global _client
    _client = client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def ping() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001 - health check must not raise
        logger.warning("redis ping failed", extra={"error": str(exc)})
        return False


async def idempotency_guard(key: str, ttl_seconds: int = 900) -> bool:
    """Return True the first time `key` is seen, False for repeats.

    Best-effort only. If Redis is unavailable we return True and let the
    database unique constraints do the real work.
    """
    try:
        return bool(await get_redis().set(f"idem:{key}", "1", nx=True, ex=ttl_seconds))
    except Exception as exc:  # noqa: BLE001
        logger.warning("idempotency guard unavailable", extra={"error": str(exc)})
        return True


@asynccontextmanager
async def lock(
    name: str,
    ttl_seconds: int = 30,
    *,
    wait_seconds: float = 0.0,
    poll_seconds: float = 0.25,
) -> AsyncIterator[bool]:
    """Best-effort mutual exclusion. Yields False when the lock is already held.

    Deliberately not a correctness mechanism. It exists so two workers do not
    reply to the same conversation at once; the things that must be exactly-once
    (messages, leads, reminders) are protected by database constraints instead.

    `wait_seconds` turns "is it free right now?" into "is it free within the next
    N seconds?". The default of 0 keeps the single-attempt behaviour every
    existing caller relies on. A caller that passes a budget is saying it would
    rather queue behind the current holder than fail - which for a customer's
    second message is the difference between waiting a few seconds and waiting
    for a sweep.

    Release checks the token before deleting so a lock that already expired is
    not stolen from its next holder. The check and the delete are two round
    trips rather than one Lua script, which leaves a race window the length of
    one command - acceptable for a lock whose worst failure is a duplicated
    reply that the message-level idempotency then catches.
    """
    token = uuid.uuid4().hex
    key = f"lock:{name}"
    acquired = False
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    try:
        while True:
            acquired = bool(await get_redis().set(key, token, nx=True, ex=ttl_seconds))
            if acquired or time.monotonic() >= deadline:
                break
            # Sleep no longer than the budget that is left, so a caller never
            # waits meaningfully past the deadline it asked for.
            await asyncio.sleep(min(poll_seconds, max(deadline - time.monotonic(), 0.0)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("lock unavailable, proceeding without", extra={"error": str(exc)})
        acquired = True
        key = ""
    try:
        yield acquired
    finally:
        if acquired and key:
            try:
                client = get_redis()
                if await client.get(key) == token:
                    await client.delete(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("lock release failed", extra={"error": str(exc)})


async def rate_limit(key: str, limit: int, window_seconds: int = 60) -> bool:
    """Fixed-window counter. Returns True when the call is allowed."""
    try:
        client = get_redis()
        redis_key = f"rl:{key}"
        count = await client.incr(redis_key)
        if count == 1:
            await client.expire(redis_key, window_seconds)
        return count <= limit
    except Exception as exc:  # noqa: BLE001
        logger.warning("rate limiter unavailable", extra={"error": str(exc)})
        return True


async def cache_get(key: str) -> str | None:
    try:
        return await get_redis().get(f"cache:{key}")
    except Exception:  # noqa: BLE001
        return None


async def cache_set(key: str, value: str, ttl_seconds: int | None = None) -> None:
    ttl = ttl_seconds or get_settings().redis_default_ttl_seconds
    try:
        await get_redis().set(f"cache:{key}", value, ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache write failed", extra={"error": str(exc)})

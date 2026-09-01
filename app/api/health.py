"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core import redis as redis_helper
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.worker import queue

logger = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: is the process up? No dependencies checked on purpose."""
    return {"status": "ok", "environment": get_settings().environment}


@router.get("/health/ready")
async def ready(response: Response) -> dict[str, object]:
    """Readiness: can we actually serve traffic?

    PostgreSQL is required - without it we cannot store a webhook and would
    lose messages, so a failure here is a 503. Redis is degraded-but-usable:
    dedupe falls back to the database constraints and the scheduler sweep picks
    up work the queue could not carry.
    """
    checks: dict[str, object] = {}

    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.error("database health check failed", extra={"error": str(exc)})
        checks["database"] = "error"

    redis_ok = await redis_helper.ping()
    checks["redis"] = "ok" if redis_ok else "degraded"
    checks["queue_depth"] = await queue.depth() if redis_ok else -1

    if checks["database"] != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        checks["status"] = "unavailable"
    else:
        checks["status"] = "ok" if redis_ok else "degraded"

    return checks

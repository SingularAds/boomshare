"""A minimal health listener for the worker process.

The worker drains a queue; it does not serve an API. But Cloud Run - and most
orchestrators - require every container to answer a probe on ``$PORT`` or the
revision never goes live. This is the smallest thing that satisfies that: a
raw asyncio HTTP responder, no framework, that answers

    GET /health         -> 200   the process is up (liveness)
    GET /health/ready    -> 200 / 503   PostgreSQL reachable? (readiness)
    anything else        -> 404

Kept deliberately independent of ``app.api`` so importing it pulls in nothing
the worker would not already load.
"""

from __future__ import annotations

import asyncio
import json
import os

from app.core.logging import get_logger

logger = get_logger(__name__)

_READ_TIMEOUT = 5.0
_REASON = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}


async def _readiness() -> tuple[int, dict[str, str]]:
    """PostgreSQL is required for the worker to do anything; Redis is not."""
    from sqlalchemy import text

    from app.core import redis as redis_helper
    from app.core.db import session_scope

    checks: dict[str, str] = {"role": "worker"}
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        logger.error("worker readiness: database check failed", extra={"error": str(exc)})
        checks["database"] = "error"

    checks["redis"] = "ok" if await redis_helper.ping() else "degraded"

    if checks["database"] != "ok":
        checks["status"] = "unavailable"
        return 503, checks
    checks["status"] = "ok" if checks["redis"] == "ok" else "degraded"
    return 200, checks


async def _respond(writer: asyncio.StreamWriter, code: int, body: dict[str, str]) -> None:
    payload = json.dumps(body).encode()
    head = (
        f"HTTP/1.1 {code} {_REASON.get(code, 'OK')}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(head.encode("latin-1") + payload)
    await writer.drain()


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), _READ_TIMEOUT)
        while True:  # consume headers up to the blank line
            line = await asyncio.wait_for(reader.readline(), _READ_TIMEOUT)
            if line in (b"\r\n", b"\n", b""):
                break

        parts = request_line.decode("latin-1", "replace").split()
        path = parts[1] if len(parts) >= 2 else "/"

        if path.startswith("/health/ready"):
            code, body = await _readiness()
        elif path.startswith("/health"):
            code, body = 200, {"status": "ok", "role": "worker"}
        else:
            code, body = 404, {"detail": "not found"}

        await _respond(writer, code, body)
    except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
        pass
    except Exception:  # noqa: BLE001 - the listener must outlive one bad request
        logger.exception("worker health request failed")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def serve_health(stop: asyncio.Event, port: int | None = None) -> None:
    """Run until `stop` is set. Binds `$PORT` (default 8000)."""
    port = port if port is not None else int(os.getenv("PORT", "8000"))
    server = await asyncio.start_server(_handle, "0.0.0.0", port)
    logger.info("worker health listener up", extra={"port": port})
    try:
        async with server:
            await stop.wait()
    finally:
        server.close()

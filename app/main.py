"""FastAPI application factory.

A modular monolith: one process, one deployment, clear internal seams.

    app/api          HTTP surface - thin, no business logic
    app/services     the business - customers, conversations, sales, follow-ups
    app/ai           prompt assembly and output validation
    app/integrations Meta and OpenAI, the only modules that speak to them
    app/worker       background processing
    app/core         config, database, redis, logging, errors
    app/domain.py    the sales vocabulary and state machine
    app/models.py    persistence
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import admin, health, internal, webhooks
from app.core.config import get_settings
from app.core.db import dispose_engine
from app.core.errors import BoomshareError, MessagingPolicyError, PermanentError
from app.core.logging import configure_logging, get_logger, request_id_var
from app.core.redis import close_redis
from app.core.trace import guard_production as guard_trace_in_production
from app.integrations.meta.client import get_meta_client

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    guard_trace_in_production()
    logger.info("api starting", extra={"environment": settings.environment})

    stop_event = asyncio.Event()
    worker_tasks: list[asyncio.Task[None]] = []

    if settings.run_embedded_worker and not settings.is_test:
        from app.worker.runner import consume, schedule

        logger.info("starting embedded worker tasks", extra={"queue": settings.worker_queue_name})
        worker_tasks = [
            asyncio.create_task(consume(stop_event)),
            asyncio.create_task(schedule(stop_event)),
        ]

    try:
        yield
    finally:
        if worker_tasks:
            logger.info("stopping embedded worker tasks")
            stop_event.set()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*worker_tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except TimeoutError:
                for task in worker_tasks:
                    task.cancel()
        await get_meta_client().aclose()
        await close_redis()
        await dispose_engine()
        logger.info("api stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Boomshare AI Backend",
        version="1.0.0",
        description="WhatsApp AI sales platform: Meta webhooks, OpenAI sales agent, follow-ups.",
        lifespan=lifespan,
        # The API is server-to-server; there is no reason to publish the schema
        # in production.
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Attach a request id and log completion, without logging bodies.

        Webhook bodies contain customer phone numbers and message content, so
        they are never written to the request log.
        """
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    @app.exception_handler(MessagingPolicyError)
    async def messaging_policy_handler(_: Request, exc: MessagingPolicyError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PermanentError)
    async def permanent_error_handler(_: Request, exc: PermanentError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(BoomshareError)
    async def application_error_handler(_: Request, exc: BoomshareError) -> JSONResponse:
        logger.error("unhandled application error", extra={"error": str(exc)})
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(internal.router)
    app.include_router(admin.router)

    return app


app = create_app()

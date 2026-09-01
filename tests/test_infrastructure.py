"""Infrastructure: health, logging hygiene, Redis degradation, worker, OpenAI client."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from app.ai.schemas import AiDecision
from app.core import redis as redis_helper
from app.core.errors import AiUnavailableError
from app.core.logging import JsonFormatter, RedactionFilter, redact, request_id_var
from app.integrations.openai.client import OpenAiSalesModel
from app.worker import queue


class TestHealth:
    async def test_liveness_needs_no_dependencies(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["environment"] == "test"

    async def test_readiness_reports_both_datastores(self, client, redis):
        response = await client.get("/health/ready")
        body = response.json()

        assert response.status_code == 200
        assert body["database"] == "ok"
        assert body["redis"] == "ok"
        assert body["status"] == "ok"

    async def test_readiness_is_degraded_not_down_without_redis(self, client, broken_redis):
        """Losing Redis costs speed, not correctness - it must not fail readiness."""
        response = await client.get("/health/ready")
        body = response.json()

        assert response.status_code == 200
        assert body["database"] == "ok"
        assert body["redis"] == "degraded"
        assert body["status"] == "degraded"

    async def test_request_id_is_echoed(self, client):
        response = await client.get("/health", headers={"X-Request-ID": "abc123"})
        assert response.headers["X-Request-ID"] == "abc123"

    async def test_a_request_id_is_generated_when_absent(self, client):
        response = await client.get("/health")
        assert response.headers["X-Request-ID"]


class TestSecretRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "token=EAAGm0PX4ZCpsBO1234567890abcdefghij",
            "Authorization: Bearer EAAGm0PX4ZCpsBO1234567890abcdefghij",
            'api_key="sk-proj-abcdefghijklmnopqrstuvwxyz123"',
            "password=hunter2supersecretvalue",
        ],
    )
    def test_credentials_are_scrubbed(self, text):
        cleaned = redact(text)
        assert "EAAGm0PX4ZCpsBO1234567890abcdefghij" not in cleaned
        assert "sk-proj-abcdefghijklmnopqrstuvwxyz123" not in cleaned
        assert "hunter2supersecretvalue" not in cleaned

    def test_ordinary_text_is_untouched(self):
        text = "conversation 123 moved to stage engaged"
        assert redact(text) == text

    def test_the_filter_scrubs_the_message(self):
        record = logging.LogRecord(
            "t", logging.INFO, "f", 1, "leaked sk-proj-abcdefghijklmnopqrstuvwxyz123", None, None
        )
        RedactionFilter().filter(record)
        assert "sk-proj-abcdefghijklmnopqrstuvwxyz123" not in record.msg

    def test_json_formatter_emits_structured_output(self):
        record = logging.LogRecord("app.test", logging.INFO, "f", 1, "hello", None, None)
        record.conversation_id = "abc"

        payload = json.loads(JsonFormatter().format(record))

        assert payload["level"] == "INFO"
        assert payload["logger"] == "app.test"
        assert payload["message"] == "hello"
        assert payload["conversation_id"] == "abc"

    def test_json_formatter_includes_the_request_id(self):
        token = request_id_var.set("req-1")
        try:
            record = logging.LogRecord("app.test", logging.INFO, "f", 1, "hello", None, None)
            assert json.loads(JsonFormatter().format(record))["request_id"] == "req-1"
        finally:
            request_id_var.reset(token)

    def test_json_formatter_redacts_too(self):
        record = logging.LogRecord("app.test", logging.INFO, "f", 1, "x", None, None)
        record.detail = "api_key=sk-proj-abcdefghijklmnopqrstuvwxyz123"
        assert "sk-proj-abcdefghijklmnopqrstuvwxyz123" not in JsonFormatter().format(record)

    def test_settings_never_print_secrets(self, settings):
        rendered = repr(settings)
        assert "test-app-secret" not in rendered
        assert "test-access-token" not in rendered
        assert "SecretStr" in rendered


class TestRedisDegradation:
    """Every Redis helper has to survive Redis being unavailable."""

    async def test_idempotency_guard_allows_through(self, broken_redis):
        assert await redis_helper.idempotency_guard("k") is True

    async def test_rate_limiter_allows_through(self, broken_redis):
        assert await redis_helper.rate_limit("k", 1) is True

    async def test_lock_is_granted(self, broken_redis):
        async with redis_helper.lock("k") as acquired:
            assert acquired is True

    async def test_cache_reads_return_none(self, broken_redis):
        assert await redis_helper.cache_get("k") is None

    async def test_cache_writes_do_not_raise(self, broken_redis):
        await redis_helper.cache_set("k", "v")

    async def test_ping_reports_false(self, broken_redis):
        assert await redis_helper.ping() is False

    async def test_enqueue_reports_failure(self, broken_redis):
        assert await queue.enqueue("job", {}) is False

    async def test_queue_depth_is_unknown(self, broken_redis):
        assert await queue.depth() == -1


class TestRedisHelpers:
    async def test_idempotency_guard_is_true_once(self, redis):
        assert await redis_helper.idempotency_guard("dup") is True
        assert await redis_helper.idempotency_guard("dup") is False

    async def test_rate_limit_blocks_past_the_ceiling(self, redis):
        assert await redis_helper.rate_limit("caller", 2) is True
        assert await redis_helper.rate_limit("caller", 2) is True
        assert await redis_helper.rate_limit("caller", 2) is False

    async def test_rate_limits_are_per_key(self, redis):
        assert await redis_helper.rate_limit("a", 1) is True
        assert await redis_helper.rate_limit("b", 1) is True

    async def test_a_held_lock_is_refused(self, redis):
        async with redis_helper.lock("shared") as first:
            assert first is True
            async with redis_helper.lock("shared") as second:
                assert second is False

    async def test_a_lock_is_released_afterwards(self, redis):
        async with redis_helper.lock("shared"):
            pass
        async with redis_helper.lock("shared") as acquired:
            assert acquired is True

    async def test_cache_round_trips(self, redis):
        await redis_helper.cache_set("k", "v")
        assert await redis_helper.cache_get("k") == "v"


class TestQueue:
    async def test_round_trip(self, redis):
        assert await queue.enqueue("webhook_event", {"event_id": "1"}) is True
        assert await queue.depth() == 1

        job = await queue.dequeue(timeout_seconds=1)
        assert job == {"type": "webhook_event", "payload": {"event_id": "1"}}
        assert await queue.depth() == 0

    async def test_empty_queue_returns_none(self, redis):
        assert await queue.dequeue(timeout_seconds=1) is None

    async def test_malformed_job_is_discarded(self, redis):
        await redis.lpush("boomshare:jobs", "not json")
        assert await queue.dequeue(timeout_seconds=1) is None

    async def test_job_without_a_type_is_discarded(self, redis):
        await redis.lpush("boomshare:jobs", json.dumps({"payload": {}}))
        assert await queue.dequeue(timeout_seconds=1) is None

    async def test_jobs_are_processed_oldest_first(self, redis):
        await queue.enqueue("a", {"n": 1})
        await queue.enqueue("b", {"n": 2})
        assert (await queue.dequeue(1))["type"] == "a"
        assert (await queue.dequeue(1))["type"] == "b"


class TestWorkerLoop:
    async def test_consumer_runs_a_queued_job(self, client, db, ai, meta, redis):
        from sqlalchemy import select

        from app.models import Message, WebhookEvent
        from app.worker.runner import consume
        from tests.factories import signed, whatsapp_message_payload

        body, headers = signed(whatsapp_message_payload(text="hello"))
        await client.post("/webhooks/meta", content=body, headers=headers)

        stop = asyncio.Event()
        task = asyncio.create_task(consume(stop))
        for _ in range(50):
            await asyncio.sleep(0.05)
            if await queue.depth() == 0:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        event = (await db.execute(select(WebhookEvent))).scalar_one()
        assert event.status == "processed"
        assert (await db.execute(select(Message))).scalars().all()
        assert len(meta.texts) == 1

    async def test_a_failing_job_does_not_kill_the_consumer(self, client, ai, meta, redis):
        from app.worker.runner import consume

        await queue.enqueue("webhook_event", {"event_id": "not-a-uuid"})
        await queue.enqueue("unknown_job_type", {})

        stop = asyncio.Event()
        task = asyncio.create_task(consume(stop))
        for _ in range(50):
            await asyncio.sleep(0.05)
            if await queue.depth() == 0:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        assert task.done() and task.exception() is None

    async def test_scheduler_stops_when_asked(self, redis, session_factory):
        from app.worker.runner import schedule

        stop = asyncio.Event()
        task = asyncio.create_task(schedule(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert task.done()

    async def test_sweeps_are_no_ops_when_there_is_nothing_to_do(self, redis, session_factory):
        from app.worker.runner import sweep_due_reminders, sweep_stuck_webhooks

        assert await sweep_due_reminders() == 0
        assert await sweep_stuck_webhooks() == 0


class TestWorkerHealthListener:
    """The worker serves no API, but Cloud Run needs every container to answer a
    probe on $PORT or the revision never goes live."""

    async def test_liveness_readiness_and_unknown_paths(self, redis, session_factory):
        import httpx

        from app.worker import health

        server = await asyncio.start_server(health._handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                live = await http.get("/health")
                assert live.status_code == 200
                assert live.json()["role"] == "worker"

                ready = await http.get("/health/ready")
                assert ready.status_code == 200
                assert ready.json()["database"] == "ok"

                missing = await http.get("/something-else")
                assert missing.status_code == 404
        finally:
            server.close()
            await server.wait_closed()

    async def test_readiness_is_503_when_the_database_is_gone(self, redis, monkeypatch):
        import httpx

        from app.worker import health

        def boom():
            raise RuntimeError("no database")

        monkeypatch.setattr("app.core.db.session_scope", boom)

        server = await asyncio.start_server(health._handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                ready = await http.get("/health/ready")
            assert ready.status_code == 503
            assert ready.json()["status"] == "unavailable"
        finally:
            server.close()
            await server.wait_closed()

    async def test_serve_health_starts_and_stops_cleanly(self):
        from app.worker.health import serve_health

        stop = asyncio.Event()
        task = asyncio.create_task(serve_health(stop, port=0))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert task.done() and task.exception() is None


class FakeOpenAiResponse:
    def __init__(self, content: str):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
        self.model = "gpt-4o-mini"
        self.usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30)


class FakeOpenAi:
    """Minimal stand-in for the OpenAI SDK client."""

    def __init__(self, content: str = "{}", error: Exception | None = None):
        self._content = content
        self._error = error
        self.captured: dict = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.captured = kwargs
        if self._error is not None:
            raise self._error
        return FakeOpenAiResponse(self._content)


class TestOpenAiClient:
    async def test_a_decision_is_returned_with_usage_metadata(self, settings):
        payload = json.dumps(
            {
                "reply_text": "What do you record most often?",
                "intent": "information_request",
                "suggested_stage": "engaged",
                "actions": [],
                "follow_up_minutes": None,
                "handoff_reason": None,
                "confidence": 0.8,
                "customer_notes": {"role": "support"},
            }
        )
        model = OpenAiSalesModel(settings, client=FakeOpenAi(payload))
        result = await model.decide([{"role": "user", "content": "hi"}])

        assert result.decision.reply_text == "What do you record most often?"
        assert result.decision.customer_notes == {"role": "support"}
        assert result.model == "gpt-4o-mini"
        assert result.prompt_tokens == 120
        assert result.completion_tokens == 30
        assert result.latency_ms is not None

    async def test_the_json_schema_is_requested(self, settings):
        fake = FakeOpenAi('{"reply_text": "hi"}')
        await OpenAiSalesModel(settings, client=fake).decide([{"role": "user", "content": "hi"}])

        assert fake.captured["response_format"]["type"] == "json_schema"
        assert fake.captured["model"] == settings.openai_model
        assert fake.captured["max_tokens"] == settings.openai_max_output_tokens

    async def test_an_empty_response_is_an_outage(self, settings):
        model = OpenAiSalesModel(settings, client=FakeOpenAi(""))
        with pytest.raises(AiUnavailableError):
            await model.decide([{"role": "user", "content": "hi"}])

    async def test_a_timeout_is_an_outage(self, settings):
        from openai import APITimeoutError

        error = APITimeoutError(request=SimpleNamespace(url="x"))
        model = OpenAiSalesModel(settings, client=FakeOpenAi(error=error))
        with pytest.raises(AiUnavailableError):
            await model.decide([{"role": "user", "content": "hi"}])

    async def test_a_rate_limit_is_an_outage(self, settings):
        import httpx
        from openai import RateLimitError

        error = RateLimitError(
            "slow down",
            response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
            body=None,
        )
        model = OpenAiSalesModel(settings, client=FakeOpenAi(error=error))
        with pytest.raises(AiUnavailableError):
            await model.decide([{"role": "user", "content": "hi"}])


class TestPromptFiles:
    def test_the_sales_prompt_is_loaded_from_disk(self):
        from app.ai.knowledge import sales_behaviour

        text = sales_behaviour()
        assert "sales executive" in text
        assert "Never invent product facts" in text

    def test_product_knowledge_includes_objections(self):
        from app.ai.knowledge import product_knowledge

        text = product_knowledge()
        assert "Boomshare" in text
        assert "Common Questions and Objections" in text

    def test_a_missing_document_is_empty_not_an_exception(self):
        from app.ai.knowledge import load_knowledge

        assert load_knowledge("does-not-exist") == ""

    async def test_prompts_can_be_reloaded_without_a_restart(self, client):
        response = await client.post(
            "/admin/prompts/reload", headers={"X-Internal-Token": "test-internal-token"}
        )
        assert response.status_code == 202

    def test_the_context_builder_composes_every_block(self):
        from app.ai.context import build_context
        from app.domain import SalesStage
        from app.models import Conversation, Customer

        conversation = Conversation(sales_stage=SalesStage.ENGAGED, context_notes={"role": "lead"})
        customer = Customer(phone="1", full_name="Sam")

        messages = build_context(conversation, customer, [], directive="Follow up.").to_messages()
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")

        assert "sales executive" in system
        assert "Product knowledge" in system
        assert "Sam" in system
        assert "Sales stage: engaged" in system
        assert "Follow up." in system

    def test_a_decision_can_be_serialised_for_the_log(self):
        decision = AiDecision(reply_text="hi", intent="greeting")
        assert decision.model_dump(mode="json")["intent"] == "greeting"


class TestTraceProductionGuard:
    """Trace output prints phone numbers and message bodies; it must never run
    in production, whatever `TRACE` was set to."""

    def test_trace_is_forced_off_in_production(self, monkeypatch):
        from app.core import trace
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "environment", "production")
        monkeypatch.setattr(trace, "_enabled", True)

        trace.guard_production()

        assert trace.enabled() is False

    def test_trace_is_left_alone_outside_production(self, monkeypatch):
        from app.core import trace
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "environment", "staging")
        monkeypatch.setattr(trace, "_enabled", True)

        trace.guard_production()

        assert trace.enabled() is True

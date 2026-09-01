"""What happens when Meta or OpenAI misbehaves."""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.ai.schemas import AiDecision
from app.core.errors import (
    AiUnavailableError,
    MetaPermanentError,
    MetaRetryableError,
)
from app.domain import WebhookStatus
from app.integrations.meta.client import MetaClient
from app.integrations.openai.client import _parse_decision
from app.models import Message, WebhookEvent
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue

GRAPH = "https://graph.facebook.com/v26.0"


async def post(client, **kwargs):
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


class TestMetaErrorClassification:
    @respx.mock
    async def test_successful_send_returns_the_message_id(self, settings):
        respx.post(f"{GRAPH}/111222333/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.NEW"}]})
        )
        result = await MetaClient(settings).send_text("919876543210", "hello")
        assert result.provider_message_id == "wamid.NEW"

    @respx.mock
    async def test_bad_request_is_permanent(self, settings):
        respx.post(f"{GRAPH}/111222333/messages").mock(
            return_value=httpx.Response(
                400, json={"error": {"code": 100, "message": "Invalid parameter"}}
            )
        )
        with pytest.raises(MetaPermanentError) as exc:
            await MetaClient(settings).send_text("919876543210", "hello")
        assert exc.value.status_code == 400

    @respx.mock
    async def test_permanent_errors_are_not_retried(self, settings):
        route = respx.post(f"{GRAPH}/111222333/messages").mock(
            return_value=httpx.Response(400, json={"error": {"code": 100}})
        )
        with pytest.raises(MetaPermanentError):
            await MetaClient(settings).send_text("919876543210", "hello")
        assert route.call_count == 1

    @respx.mock
    async def test_server_error_is_retryable(self, settings):
        respx.post(f"{GRAPH}/111222333/messages").mock(return_value=httpx.Response(503))
        with pytest.raises(MetaRetryableError):
            await MetaClient(settings).send_text("919876543210", "hello", phone_number_id="111222333")

    @respx.mock
    async def test_rate_limit_code_is_retryable(self, settings):
        respx.post(f"{GRAPH}/111222333/messages").mock(
            return_value=httpx.Response(
                400, json={"error": {"code": 4, "message": "Application request limit reached"}}
            )
        )
        with pytest.raises(MetaRetryableError):
            await MetaClient(settings).send_text("919876543210", "hello")

    @respx.mock
    async def test_a_transient_failure_is_retried_then_succeeds(self, settings):
        route = respx.post(f"{GRAPH}/111222333/messages").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"messages": [{"id": "wamid.RETRY"}]}),
            ]
        )
        result = await MetaClient(settings).send_text("919876543210", "hello")
        assert result.provider_message_id == "wamid.RETRY"
        assert route.call_count == 2

    @respx.mock
    async def test_connection_errors_are_retryable(self, settings):
        respx.post(f"{GRAPH}/111222333/messages").mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        with pytest.raises(MetaRetryableError):
            await MetaClient(settings).send_text("919876543210", "hello")

    @respx.mock
    async def test_read_receipt_failure_is_swallowed(self, settings):
        respx.post(f"{GRAPH}/111222333/messages").mock(return_value=httpx.Response(500))
        # Must not raise: a missing read receipt is not worth failing a reply over.
        await MetaClient(settings).mark_read("wamid.X")

    @respx.mock
    async def test_lead_fetch_flattens_field_data(self, settings):
        respx.get(f"{GRAPH}/LEAD-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "LEAD-1",
                    "campaign_id": "CAMP-1",
                    "field_data": [
                        {"name": "full_name", "values": ["Alex"]},
                        {"name": "phone_number", "values": ["+44 7700 900123"]},
                    ],
                },
            )
        )
        details = await MetaClient(settings).fetch_lead("LEAD-1")
        assert details.fields() == {"full_name": "Alex", "phone_number": "+44 7700 900123"}

    @respx.mock
    async def test_the_access_token_is_sent_as_a_bearer_header(self, settings):
        route = respx.post(f"{GRAPH}/111222333/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "x"}]})
        )
        await MetaClient(settings).send_text("919876543210", "hello")
        assert route.calls[0].request.headers["Authorization"] == "Bearer test-access-token"


class TestOpenAiResponseHandling:
    def test_valid_json_is_parsed(self):
        decision = _parse_decision(
            '{"reply_text": "hi", "intent": "greeting", "confidence": 0.9, "actions": []}'
        )
        assert decision.reply_text == "hi"
        assert decision.intent == "greeting"

    def test_invalid_json_raises(self):
        with pytest.raises(AiUnavailableError):
            _parse_decision("this is not json")

    def test_json_that_is_not_an_object_raises(self):
        with pytest.raises(AiUnavailableError):
            _parse_decision('["a", "list"]')

    def test_unknown_enum_values_degrade_rather_than_lose_the_reply(self):
        """A good sentence with a bad label is still worth sending."""
        decision = _parse_decision(
            '{"reply_text": "Sounds good", "intent": "vibing", "suggested_stage": "nonsense"}'
        )
        assert decision.reply_text == "Sounds good"
        assert decision.intent == "unclear"
        assert decision.suggested_stage is None

    def test_missing_fields_fall_back_to_defaults(self):
        decision = _parse_decision('{"reply_text": "hello"}')
        assert decision.actions == []
        assert decision.confidence == 0.5

    def test_extra_fields_are_ignored(self):
        decision = _parse_decision('{"reply_text": "hi", "made_up_field": 42}')
        assert decision.reply_text == "hi"

    def test_one_bad_field_does_not_discard_the_others(self):
        """The production bug: `"actions": "none"` wiped intent and stage too.

        The model was told (wrongly) that `none` was an action, so it returned a
        bare string where a list belongs. Recovery then cleared every enum-ish
        field, and a quarter of all decisions lost their intent, stage and
        confidence along with it.
        """
        decision = _parse_decision(
            '{"reply_text": "Sure thing", "intent": "buying_intent",'
            ' "suggested_stage": "download_suggested", "actions": "none",'
            ' "confidence": 0.91}'
        )
        assert decision.actions == []  # the only field actually at fault
        assert decision.intent == "buying_intent"
        assert decision.suggested_stage == "download_suggested"
        assert decision.confidence == 0.91

    def test_an_unrecoverable_field_is_not_silently_defaulted(self):
        """A decision with no usable reply must fail loudly, not send "".

        `reply_text` is deliberately outside the recoverable set: defaulting it
        would turn a malformed response into a silent non-answer.
        """
        with pytest.raises(AiUnavailableError):
            _parse_decision('{"reply_text": {"unexpected": "object"}}')

    def test_notes_arrive_as_pairs_and_become_a_mapping(self):
        """Strict structured outputs forbid free-form objects, so we ask for pairs."""
        decision = _parse_decision(
            '{"reply_text": "ok", "customer_notes":'
            ' [{"key": "role", "value": "support lead"},'
            '  {"key": "platform", "value": "Mac"}]}'
        )
        assert decision.customer_notes == {"role": "support lead", "platform": "Mac"}


class TestAiOutageBehaviour:
    async def test_no_reply_is_invented_when_the_model_is_down(self, client, db, ai, meta):
        ai.raise_with = AiUnavailableError("openai down")

        await post(client, text="hello")
        with pytest.raises(Exception):
            await drain_queue()

        assert meta.sent == []
        # The customer's message is still safely stored.
        assert (await db.execute(select(func.count()).select_from(Message))).scalar() == 1

    async def test_the_event_is_left_retryable(self, client, db, ai, meta):
        ai.raise_with = AiUnavailableError("openai down")

        await post(client, text="hello")
        with pytest.raises(Exception):
            await drain_queue()

        event = (await db.execute(select(WebhookEvent))).scalar_one()
        assert event.status == WebhookStatus.FAILED
        assert event.attempts == 1

    async def test_the_sweeper_retries_it_and_it_succeeds(self, client, db, ai, meta):
        from app.worker.runner import sweep_stuck_webhooks

        ai.raise_with = AiUnavailableError("openai down")
        await post(client, text="hello")
        with pytest.raises(Exception):
            await drain_queue()

        ai.raise_with = None

        async def _age(session):
            event = (await session.execute(select(WebhookEvent))).scalar_one()
            from datetime import timedelta

            from app.core.clock import utcnow

            event.received_at = utcnow() - timedelta(minutes=5)

        await db.write(_age)

        assert await sweep_stuck_webhooks() == 1
        await drain_queue()

        event = (await db.execute(select(WebhookEvent))).scalar_one()
        assert event.status == WebhookStatus.PROCESSED
        assert len(meta.texts) == 1

    async def test_attempts_stop_at_the_configured_maximum(self, client, db, ai, meta, settings):
        """A permanently broken event must not be retried forever."""
        from app.worker.jobs import process_webhook_event

        ai.raise_with = AiUnavailableError("openai down")
        await post(client, text="hello")
        event_id = (await db.execute(select(WebhookEvent))).scalar_one().id

        for _ in range(settings.worker_max_attempts + 3):
            try:
                await process_webhook_event(event_id)
            except Exception:
                pass

        event = await db.get(WebhookEvent, event_id)
        assert event.attempts == settings.worker_max_attempts
        assert event.processed_at is not None  # parked, no longer claimable
        assert event.status == WebhookStatus.FAILED

    async def test_a_parked_event_is_not_swept_up_again(self, client, db, ai, meta, settings):
        from datetime import timedelta

        from app.core.clock import utcnow
        from app.worker.jobs import process_webhook_event
        from app.worker.runner import sweep_stuck_webhooks

        ai.raise_with = AiUnavailableError("openai down")
        await post(client, text="hello")
        event_id = (await db.execute(select(WebhookEvent))).scalar_one().id
        for _ in range(settings.worker_max_attempts):
            try:
                await process_webhook_event(event_id)
            except Exception:
                pass

        async def _age(session):
            stored = await session.get(WebhookEvent, event_id)
            stored.received_at = utcnow() - timedelta(hours=1)

        await db.write(_age)
        assert await sweep_stuck_webhooks() == 0


class TestRedisOutage:
    async def test_webhooks_are_still_accepted_without_redis(self, client, db, redis, ai, meta):
        """Redis is an accelerator. PostgreSQL is the guarantee."""
        await redis.connection_pool.disconnect()
        await redis.aclose()

        response = await client.post(
            "/webhooks/meta", **_signed_kwargs(whatsapp_message_payload(text="hi"))
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 1

        event = (await db.execute(select(WebhookEvent))).scalar_one()
        assert event.status == WebhookStatus.PENDING

    async def test_the_sweeper_picks_up_work_the_queue_never_received(
        self, client, db, redis, ai, meta
    ):
        from datetime import timedelta

        from app.core.clock import utcnow
        from app.worker.runner import sweep_stuck_webhooks

        await client.post("/webhooks/meta", **_signed_kwargs(whatsapp_message_payload(text="hi")))
        await redis.flushall()  # the queued job is gone

        async def _age(session):
            event = (await session.execute(select(WebhookEvent))).scalar_one()
            event.received_at = utcnow() - timedelta(minutes=5)

        await db.write(_age)

        assert await sweep_stuck_webhooks() == 1
        await drain_queue()

        assert len(meta.texts) == 1


def _signed_kwargs(payload):
    body, headers = signed(payload)
    return {"content": body, "headers": headers}


class TestAiDecisionOutcomes:
    async def test_an_empty_reply_sends_nothing(self, client, db, ai, meta):
        ai.queue_decision(AiDecision(reply_text="", intent="unclear"))
        await post(client, text="???")
        await drain_queue()
        assert meta.sent == []

    async def test_a_valid_reply_after_a_failure_still_works(self, client, db, ai, meta):
        ai.raise_with = AiUnavailableError("down")
        await post(client, text="first")
        with pytest.raises(Exception):
            await drain_queue()

        ai.raise_with = None
        await post(client, text="second")
        await drain_queue()

        assert len(meta.texts) == 1

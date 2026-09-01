"""Meta retries. Nothing may happen twice."""

from __future__ import annotations

from sqlalchemy import func, select

from app.domain import MessageDirection, WebhookStatus
from app.models import (
    AiDecisionLog,
    Conversation,
    Customer,
    DownloadLink,
    Lead,
    Message,
    Reminder,
    WebhookEvent,
)
from tests.factories import (
    ctwa_referral,
    lead_details,
    leadgen_payload,
    signed,
    status_payload,
    whatsapp_message_payload,
)
from tests.helpers import drain_queue


async def post(client, payload):
    body, headers = signed(payload)
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200
    return response.json()


class TestDuplicateWebhookDeliveries:
    async def test_identical_delivery_is_accepted_once(self, client, db, ai, meta):
        payload = whatsapp_message_payload(text="hello", message_id="wamid.DUPE")

        first = await post(client, payload)
        second = await post(client, payload)

        assert first == {"received": 1, "accepted": 1}
        assert second == {"received": 1, "accepted": 0}
        assert (await db.execute(select(func.count()).select_from(WebhookEvent))).scalar() == 1

    async def test_duplicate_produces_no_second_reply(self, client, db, ai, meta):
        payload = whatsapp_message_payload(text="hello", message_id="wamid.DUPE")

        await post(client, payload)
        await drain_queue()
        await post(client, payload)
        await drain_queue()

        assert len(ai.calls) == 1
        assert len(meta.sent) == 1
        assert (await db.execute(select(func.count()).select_from(Message))).scalar() == 2

    async def test_dedupe_survives_a_cold_cache(self, client, db, redis, ai, meta):
        """Redis is an optimisation. The database constraint is the guarantee."""
        payload = whatsapp_message_payload(text="hello", message_id="wamid.DUPE")

        await post(client, payload)
        await drain_queue()

        await redis.flushall()

        second = await post(client, payload)
        assert second["accepted"] == 0
        assert (await db.execute(select(func.count()).select_from(WebhookEvent))).scalar() == 1
        assert len(meta.sent) == 1

    async def test_duplicate_creates_no_second_customer_or_conversation(
        self, client, db, ai, meta
    ):
        payload = whatsapp_message_payload(
            text="hi", message_id="wamid.DUPE", referral=ctwa_referral()
        )
        for _ in range(3):
            await post(client, payload)
            await drain_queue()

        assert (await db.execute(select(func.count()).select_from(Customer))).scalar() == 1
        assert (await db.execute(select(func.count()).select_from(Conversation))).scalar() == 1
        assert (await db.execute(select(func.count()).select_from(Lead))).scalar() == 1


class TestJobRedelivery:
    async def test_reprocessing_a_finished_event_is_a_no_op(self, client, db, ai, meta):
        """The queue is at-least-once; the claim makes processing at-most-once."""
        from app.worker.jobs import process_webhook_event

        await post(client, whatsapp_message_payload(text="hello"))
        await drain_queue()

        event = (await db.execute(select(WebhookEvent))).scalar_one()
        await process_webhook_event(event.id)

        assert len(ai.calls) == 1
        assert len(meta.sent) == 1

    async def test_retry_after_a_failed_reply_does_not_double_send(self, client, db, ai, meta):
        """The ingest transaction is committed before the AI call, so a retry
        must recognise that this message was already answered."""
        from app.core.errors import AiUnavailableError
        from app.worker.jobs import process_webhook_event

        await post(client, whatsapp_message_payload(text="hello"))

        ai.raise_with = AiUnavailableError("openai down")
        try:
            await drain_queue()
        except Exception:
            pass

        assert meta.sent == []
        event = (await db.execute(select(WebhookEvent))).scalar_one()
        assert event.status == WebhookStatus.FAILED

        ai.raise_with = None
        await process_webhook_event(event.id)
        assert len(meta.sent) == 1

        # And a third attempt still sends nothing new.
        await process_webhook_event(event.id)
        assert len(meta.sent) == 1

        # One inbound + one outbound, no duplicated inbound row.
        messages = list((await db.execute(select(Message))).scalars())
        assert sum(m.direction == MessageDirection.INBOUND for m in messages) == 1
        assert sum(m.direction == MessageDirection.OUTBOUND for m in messages) == 1


class TestLeadIdempotency:
    async def test_duplicate_leadgen_sends_one_template(self, client, db, ai, meta):
        meta.register_lead(lead_details("LEAD-500"))

        for _ in range(3):
            await post(client, leadgen_payload("LEAD-500"))
            await drain_queue()

        assert (await db.execute(select(func.count()).select_from(Lead))).scalar() == 1
        assert len(meta.templates) == 1
        assert (await db.execute(select(func.count()).select_from(Reminder))).scalar() == 1

    async def test_two_different_leads_are_both_processed(self, client, db, ai, meta):
        meta.register_lead(lead_details("LEAD-1", phone="+91 90000 00001"))
        meta.register_lead(lead_details("LEAD-2", phone="+91 90000 00002"))

        await post(client, leadgen_payload("LEAD-1"))
        await post(client, leadgen_payload("LEAD-2"))
        await drain_queue()

        assert (await db.execute(select(func.count()).select_from(Lead))).scalar() == 2
        assert len(meta.templates) == 2


class TestStatusIdempotency:
    async def test_status_updates_apply_once_each(self, client, db, ai, meta):
        await post(client, whatsapp_message_payload(text="hi"))
        await drain_queue()

        outbound = (
            await db.execute(
                select(Message).where(Message.direction == MessageDirection.OUTBOUND)
            )
        ).scalar_one()

        await post(client, status_payload(outbound.provider_message_id, "delivered"))
        await post(client, status_payload(outbound.provider_message_id, "delivered"))
        await drain_queue()

        events = list((await db.execute(select(WebhookEvent))).scalars())
        assert sum(e.event_type == "whatsapp_status" for e in events) == 1

    async def test_sent_delivered_read_are_three_distinct_events(self, client, db, ai, meta):
        await post(client, whatsapp_message_payload(text="hi"))
        await drain_queue()

        outbound = (
            await db.execute(
                select(Message).where(Message.direction == MessageDirection.OUTBOUND)
            )
        ).scalar_one()

        for state in ("sent", "delivered", "read"):
            await post(client, status_payload(outbound.provider_message_id, state))
        await drain_queue()

        updated = await db.get(Message, outbound.id)
        assert updated.status == "read"
        assert updated.delivered_at is not None
        assert updated.read_at is not None


class TestDownloadEventIdempotency:
    async def test_repeat_download_reports_change_nothing(self, client, db, ai, meta):
        from app.ai.schemas import AiDecision

        ai.queue_decision(
            AiDecision(
                reply_text="Here you go.",
                intent="download_request",
                actions=["send_download_link"],
            )
        )
        await post(client, whatsapp_message_payload(text="send me the link"))
        await drain_queue()

        link = (await db.execute(select(DownloadLink))).scalar_one()
        headers = {"X-Internal-Token": "test-internal-token"}

        first = await client.post(
            "/internal/events/download", json={"token": link.token}, headers=headers
        )
        second = await client.post(
            "/internal/events/download", json={"token": link.token}, headers=headers
        )

        assert first.json()["applied"] is True
        assert second.json()["applied"] is False

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is not None


class TestDecisionLogIdempotency:
    async def test_one_decision_row_per_answered_message(self, client, db, ai, meta):
        payload = whatsapp_message_payload(text="hello", message_id="wamid.ONCE")
        for _ in range(3):
            await post(client, payload)
            await drain_queue()

        assert (await db.execute(select(func.count()).select_from(AiDecisionLog))).scalar() == 1

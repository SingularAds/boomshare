"""Meta's messaging rules, enforced in one place for AI and humans alike."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.ai.schemas import AiDecision
from app.core.clock import utcnow
from app.core.errors import MessagingPolicyError
from app.domain import MessageStatus, MessageType
from app.models import Conversation, Customer, Message
from app.services import conversations as conversation_service, messaging
from tests.factories import signed, whatsapp_message_payload
from tests.fakes import meta_bad_request, meta_rate_limited
from tests.helpers import drain_queue

AUTH = {"X-Internal-Token": "test-internal-token"}
ADMIN_AUTH = {"X-Admin-Token": "test-admin-token"}


async def post(client, **kwargs):
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


class TestServiceWindow:
    def test_no_inbound_message_means_no_free_form_reply(self):
        conversation = Conversation(last_inbound_at=None)
        assert not conversation_service.within_service_window(conversation)

    def test_recent_inbound_message_opens_the_window(self):
        conversation = Conversation(last_inbound_at=utcnow() - timedelta(hours=1))
        assert conversation_service.within_service_window(conversation)

    def test_just_inside_twenty_four_hours(self):
        conversation = Conversation(last_inbound_at=utcnow() - timedelta(hours=23, minutes=59))
        assert conversation_service.within_service_window(conversation)

    def test_just_outside_twenty_four_hours(self):
        conversation = Conversation(last_inbound_at=utcnow() - timedelta(hours=24, minutes=1))
        assert not conversation_service.within_service_window(conversation)

    async def test_free_form_send_is_refused_outside_the_window(
        self, client, db, ai, meta, session_factory
    ):
        await post(client, text="hi")
        await drain_queue()

        async with session_factory() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
            customer = (await session.execute(select(Customer))).scalar_one()
            conversation.last_inbound_at = utcnow() - timedelta(hours=30)
            await session.commit()

            with pytest.raises(MessagingPolicyError):
                await messaging.send_text(session, conversation, customer, "hello again")

    async def test_send_reply_falls_back_to_a_template(
        self, client, db, ai, meta, session_factory
    ):
        await post(client, text="hi")
        await drain_queue()

        async with session_factory() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
            customer = (await session.execute(select(Customer))).scalar_one()
            conversation.last_inbound_at = utcnow() - timedelta(hours=30)
            await session.commit()

            outcome = await messaging.send_reply(session, conversation, customer, "still there?")
            await session.commit()

        assert outcome.sent
        assert meta.templates[-1].template == "boomshare_followup"
        # What is persisted is the template's approved body, not the reply that
        # was replaced on its way out. "still there?" was never delivered, and
        # storing it would have the model answer for words it never sent.
        assert outcome.message.content == (
            "Hi Priya, just checking in about Boomshare. "
            "Still interested? Happy to help whenever suits."
        )
        assert outcome.message.message_type == MessageType.TEMPLATE

    async def test_the_window_reopens_when_the_customer_writes_again(
        self, client, db, ai, meta, session_factory
    ):
        await post(client, text="hi")
        await drain_queue()

        async with session_factory() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.last_inbound_at = utcnow() - timedelta(hours=30)
            await session.commit()

        await post(client, text="sorry, was away")
        await drain_queue()

        assert len(meta.texts) == 2  # free-form again, no template needed
        assert meta.templates == []


class TestOptOut:
    async def test_nothing_is_sent_to_an_opted_out_customer(
        self, client, db, ai, meta, session_factory
    ):
        await post(client, text="hi")
        await drain_queue()

        async with session_factory() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
            customer = (await session.execute(select(Customer))).scalar_one()
            customer.opted_out_at = utcnow()
            await session.commit()

            outcome = await messaging.send_text(session, conversation, customer, "one more thing")

        assert not outcome.sent
        assert outcome.reason == "customer opted out"
        assert len(meta.texts) == 1

    async def test_opt_out_blocks_templates_too(self, client, db, ai, meta, session_factory):
        await post(client, text="hi")
        await drain_queue()

        async with session_factory() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
            customer = (await session.execute(select(Customer))).scalar_one()
            customer.opted_out_at = utcnow()
            await session.commit()

            outcome = await messaging.send_template(
                session, conversation, customer, template_name="boomshare_followup"
            )

        assert not outcome.sent
        assert meta.templates == []

    async def test_a_new_inbound_message_re_opens_the_door(self, client, db, ai, meta):
        """If they write to us, they have re-initiated contact themselves."""
        ai.queue_decision(
            AiDecision(reply_text="Understood.", intent="opt_out", actions=["opt_out"])
        )
        await post(client, text="stop messaging me")
        await drain_queue()

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.opted_out_at is not None

        await post(client, text="actually, one question")
        await drain_queue()

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.opted_out_at is None
        assert len(meta.texts) == 2


class TestFailedSends:
    async def test_a_failed_send_is_still_recorded(
        self, client, db, ai, meta, session_factory
    ):
        """A message that vanished without a trace is the worst outcome."""
        await post(client, text="hi")
        await drain_queue()

        meta.fail_text_with = meta_bad_request()

        async with session_factory() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
            customer = (await session.execute(select(Customer))).scalar_one()
            outcome = await messaging.send_text(session, conversation, customer, "will fail")
            await session.commit()

        assert not outcome.sent
        assert outcome.message.status == MessageStatus.FAILED
        assert outcome.message.error["status_code"] == 400
        assert outcome.message.content == "will fail"

    async def test_a_failed_send_does_not_advance_last_outbound_at(
        self, client, db, ai, meta, session_factory
    ):
        await post(client, text="hi")
        await drain_queue()

        conversation_before = (await db.execute(select(Conversation))).scalar_one()
        before = conversation_before.last_outbound_at

        meta.fail_text_with = meta_rate_limited()
        async with session_factory() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
            customer = (await session.execute(select(Customer))).scalar_one()
            await messaging.send_text(session, conversation, customer, "will fail")
            await session.commit()

        after = (await db.execute(select(Conversation))).scalar_one().last_outbound_at
        assert after == before

    async def test_an_ai_reply_that_cannot_be_sent_is_logged_as_such(
        self, client, db, ai, meta
    ):
        meta.fail_text_with = meta_bad_request()
        ai.queue_decision(AiDecision(reply_text="Hello there", intent="greeting"))

        await post(client, text="hi")
        await drain_queue()

        from app.models import AiDecisionLog

        decision = (await db.execute(select(AiDecisionLog))).scalar_one()
        assert "send_failed" in (decision.rejected_reasons or {})

        outbound = list(
            (await db.execute(select(Message).where(Message.status == MessageStatus.FAILED)))
            .scalars()
        )
        assert len(outbound) == 1


class TestHumanMessagesFollowTheSameRules:
    async def test_operator_message_uses_a_template_outside_the_window(
        self, client, db, ai, meta, session_factory
    ):
        await post(client, text="hi")
        await drain_queue()
        conversation = (await db.execute(select(Conversation))).scalar_one()

        async with session_factory() as session:
            stored = await session.get(Conversation, conversation.id)
            stored.last_inbound_at = utcnow() - timedelta(hours=30)
            await session.commit()

        response = await client.post(
            f"/admin/conversations/{conversation.id}/messages",
            json={"body": "Following up personally", "agent": "sam"},
            headers=ADMIN_AUTH,
        )

        assert response.status_code == 200
        assert meta.templates[-1].template == "boomshare_followup"

    async def test_operator_cannot_message_an_opted_out_customer(
        self, client, db, ai, meta, session_factory
    ):
        await post(client, text="hi")
        await drain_queue()
        conversation = (await db.execute(select(Conversation))).scalar_one()

        async with session_factory() as session:
            customer = (await session.execute(select(Customer))).scalar_one()
            customer.opted_out_at = utcnow()
            await session.commit()

        response = await client.post(
            f"/admin/conversations/{conversation.id}/messages",
            json={"body": "just one more thing"},
            headers=ADMIN_AUTH,
        )
        assert response.status_code == 502
        assert "opted out" in response.json()["detail"]

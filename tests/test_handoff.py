"""Human takeover: the AI must stop, and a person must be able to step in."""

from __future__ import annotations

from sqlalchemy import select

from app.ai.schemas import AiDecision
from app.domain import HandlingMode, MessageDirection, SalesStage
from app.models import Conversation, Message
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue

AUTH = {"X-Internal-Token": "test-internal-token"}


async def post(client, **kwargs):
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


class TestAiRequestedHandoff:
    async def test_ai_can_hand_over(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="Let me bring in a colleague who can help with that.",
                intent="human_request",
                actions=["request_human_handoff"],
                handoff_reason="customer asked to speak to a person",
            )
        )
        await post(client, text="I want to talk to a real person")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.handling_mode == HandlingMode.HUMAN
        assert conversation.sales_stage == SalesStage.HUMAN_HANDOFF
        assert conversation.handoff_reason == "customer asked to speak to a person"
        assert conversation.handoff_at is not None

    async def test_the_acknowledgement_is_sent_before_handing_over(self, client, db, ai, meta):
        """The customer should not be left with silence while a human is found."""
        ai.queue_decision(
            AiDecision(
                reply_text="Of course - one moment.",
                intent="human_request",
                actions=["request_human_handoff"],
            )
        )
        await post(client, text="get me a human")
        await drain_queue()

        assert len(meta.texts) == 1
        assert meta.texts[0].body == "Of course - one moment."

    async def test_ai_does_not_answer_once_a_human_owns_it(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="One moment.", intent="human_request", actions=["request_human_handoff"]
            )
        )
        await post(client, text="get me a human")
        await drain_queue()

        calls_before = len(ai.calls)
        sent_before = len(meta.sent)

        await post(client, text="hello? are you there?")
        await drain_queue()

        assert len(ai.calls) == calls_before
        assert len(meta.sent) == sent_before

    async def test_the_customer_message_is_still_stored(self, client, db, ai, meta):
        """A human needs to see what was said while they were being fetched."""
        ai.queue_decision(
            AiDecision(
                reply_text="One moment.", intent="human_request", actions=["request_human_handoff"]
            )
        )
        await post(client, text="get me a human")
        await drain_queue()

        await post(client, text="it is urgent")
        await drain_queue()

        inbound = list(
            (
                await db.execute(
                    select(Message).where(Message.direction == MessageDirection.INBOUND)
                )
            ).scalars()
        )
        assert [m.content for m in inbound] == ["get me a human", "it is urgent"]


class TestOperatorHandoff:
    async def _conversation_id(self, db):
        conversation = (await db.execute(select(Conversation))).scalar_one()
        return conversation.id

    async def test_an_operator_can_take_over(self, client, db, ai, meta):
        await post(client, text="hi")
        await drain_queue()
        conversation_id = await self._conversation_id(db)

        response = await client.post(
            f"/admin/conversations/{conversation_id}/handoff",
            json={"reason": "high value account", "agent": "sam@boomshare.ai"},
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["handling_mode"] == "human"
        assert response.json()["assigned_agent"] == "sam@boomshare.ai"

        await post(client, text="are you there?")
        await drain_queue()
        assert len(ai.calls) == 1  # only the very first message was answered

    async def test_an_operator_can_send_a_message(self, client, db, ai, meta):
        await post(client, text="hi")
        await drain_queue()
        conversation_id = await self._conversation_id(db)

        await client.post(
            f"/admin/conversations/{conversation_id}/handoff",
            json={"reason": "taking over"},
            headers=AUTH,
        )
        response = await client.post(
            f"/admin/conversations/{conversation_id}/messages",
            json={"body": "Hi, Sam here from Boomshare.", "agent": "sam@boomshare.ai"},
            headers=AUTH,
        )

        assert response.status_code == 200
        assert response.json()["ai_generated"] is False
        assert response.json()["sent_by"] == "sam@boomshare.ai"
        assert meta.texts[-1].body == "Hi, Sam here from Boomshare."

    async def test_an_operator_can_hand_back_to_the_ai(self, client, db, ai, meta):
        await post(client, text="hi")
        await drain_queue()
        conversation_id = await self._conversation_id(db)

        await client.post(
            f"/admin/conversations/{conversation_id}/handoff",
            json={"reason": "checking"},
            headers=AUTH,
        )
        response = await client.post(
            f"/admin/conversations/{conversation_id}/release", json={}, headers=AUTH
        )
        assert response.status_code == 200
        assert response.json()["handling_mode"] == "ai"
        assert response.json()["sales_stage"] == SalesStage.ENGAGED

        await post(client, text="still interested")
        await drain_queue()
        assert len(ai.calls) == 2

    async def test_handoff_is_idempotent(self, client, db, ai, meta):
        await post(client, text="hi")
        await drain_queue()
        conversation_id = await self._conversation_id(db)

        for _ in range(3):
            response = await client.post(
                f"/admin/conversations/{conversation_id}/handoff",
                json={"reason": "again"},
                headers=AUTH,
            )
            assert response.status_code == 200

        conversation = await db.get(Conversation, conversation_id)
        assert conversation.handling_mode == HandlingMode.HUMAN

    async def test_conversation_detail_shows_the_transcript(self, client, db, ai, meta):
        await post(client, text="hi there")
        await drain_queue()
        conversation_id = await self._conversation_id(db)

        response = await client.get(f"/admin/conversations/{conversation_id}", headers=AUTH)
        body = response.json()

        assert response.status_code == 200
        assert body["customer_phone"] == "919876543210"
        assert [m["content"] for m in body["messages"]][0] == "hi there"
        assert len(body["messages"]) == 2

    async def test_conversations_can_be_filtered_by_handling_mode(self, client, db, ai, meta):
        await post(client, text="hi")
        await drain_queue()
        conversation_id = await self._conversation_id(db)
        await client.post(
            f"/admin/conversations/{conversation_id}/handoff",
            json={"reason": "x"},
            headers=AUTH,
        )

        human = await client.get("/admin/conversations?handling_mode=human", headers=AUTH)
        ai_handled = await client.get("/admin/conversations?handling_mode=ai", headers=AUTH)

        assert len(human.json()) == 1
        assert ai_handled.json() == []


class TestAdminAuth:
    async def test_admin_requires_a_token(self, client):
        assert (await client.get("/admin/conversations")).status_code == 401

    async def test_wrong_token_is_rejected(self, client):
        response = await client.get(
            "/admin/conversations", headers={"X-Internal-Token": "wrong"}
        )
        assert response.status_code == 401

    async def test_unknown_conversation_is_404(self, client):
        response = await client.get(
            "/admin/conversations/11111111-1111-1111-1111-111111111111", headers=AUTH
        )
        assert response.status_code == 404

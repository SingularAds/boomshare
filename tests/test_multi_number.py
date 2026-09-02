"""Two WhatsApp numbers, one webhook.

The deployment answers on more than one number - a Brazil number and a US one -
and Meta delivers both to the same endpoint, signed with the same app secret.
Nothing in the payload tells the application which identity to answer as except
`metadata.phone_number_id`, and by the time a follow-up goes out that payload is
long gone.

So there is really one thing to prove, in every direction: **a message always
leaves from the number the customer wrote to.** Getting that wrong is not a
cosmetic bug - the reply lands in a thread the customer never opened, their
original message sits unanswered, and the 24-hour window we are tracking belongs
to a conversation that never happened.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.ai.schemas import AiDecision
from app.core.clock import as_utc, utcnow
from app.core.config import Settings
from app.models import Conversation, Customer, Message, Reminder
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue

#: Matches `WHATSAPP_PHONE_NUMBER_IDS` in conftest: primary first.
BRAZIL = "111222333"
USA = "444555666"


async def post(client, *, phone_number_id: str, **kwargs):
    body, headers = signed(whatsapp_message_payload(phone_number_id=phone_number_id, **kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


class TestConfiguration:
    def test_the_first_number_is_the_default_sender(self, settings):
        assert settings.whatsapp_phone_number_ids == (BRAZIL, USA)
        assert settings.default_phone_number_id == BRAZIL

    def test_only_configured_numbers_are_ours(self, settings):
        assert settings.knows_phone_number(BRAZIL)
        assert settings.knows_phone_number(USA)
        assert not settings.knows_phone_number("999999999")
        assert not settings.knows_phone_number(None)
        assert not settings.knows_phone_number("")

    def test_a_blank_entry_is_refused_at_startup(self):
        """`["123", ""]` would send to `/v26.0//messages` - a 404 per message."""
        with pytest.raises(ValueError, match="blank entry"):
            Settings(whatsapp_phone_number_ids=("123", ""))

    def test_a_duplicate_entry_is_refused_at_startup(self):
        with pytest.raises(ValueError, match="duplicates"):
            Settings(whatsapp_phone_number_ids=("123", "123"))

    def test_whitespace_is_trimmed(self):
        """A copy-paste from Business Manager brings spaces with it."""
        assert Settings(whatsapp_phone_number_ids=(" 123 ",)).whatsapp_phone_number_ids == ("123",)


class TestBothNumbersReachTheSameWebhook:
    async def test_a_message_to_the_new_number_is_answered(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(reply_text="Hey! What would you record?", intent="greeting")
        )
        await post(client, phone_number_id=USA, text="hi", wa_id="15551234567")
        await drain_queue()

        assert len(meta.texts) == 1
        assert meta.texts[0].body == "Hey! What would you record?"

    async def test_the_reply_leaves_from_the_number_they_wrote_to(self, client, db, ai, meta):
        await post(client, phone_number_id=USA, text="hi", wa_id="15551234567")
        await drain_queue()

        assert meta.texts[0].phone_number_id == USA

    async def test_the_brazil_number_still_answers_as_itself(self, client, db, ai, meta):
        await post(client, phone_number_id=BRAZIL, text="oi", wa_id="5511999999999")
        await drain_queue()

        assert meta.texts[0].phone_number_id == BRAZIL

    async def test_the_number_is_recorded_on_the_conversation(self, client, db, ai, meta):
        await post(client, phone_number_id=USA, text="hi", wa_id="15551234567")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.phone_number_id == USA

    async def test_the_read_receipt_goes_to_the_right_number(self, client, db, ai, meta):
        """`mark_read` hits `/{phone_number_id}/messages` like a send does."""
        await post(client, phone_number_id=USA, text="hi", wa_id="15551234567")
        await drain_queue()

        assert meta.read_receipts, "no read receipt was sent"


class TestTwoNumbersInParallel:
    """Two customers, two numbers, one webhook - neither leaks into the other."""

    async def _two_customers(self, client, ai, meta):
        ai.queue_decision(AiDecision(reply_text="Oi! Como posso ajudar?", intent="greeting"))
        await post(client, phone_number_id=BRAZIL, text="oi", wa_id="5511999999999")
        await drain_queue()

        ai.queue_decision(AiDecision(reply_text="Hey! How can I help?", intent="greeting"))
        await post(client, phone_number_id=USA, text="hello", wa_id="15551234567")
        await drain_queue()

    async def test_each_reply_goes_out_from_its_own_number(self, client, db, ai, meta):
        await self._two_customers(client, ai, meta)

        routed = {m.body: m.phone_number_id for m in meta.texts}
        assert routed == {"Oi! Como posso ajudar?": BRAZIL, "Hey! How can I help?": USA}

    async def test_they_are_separate_conversations(self, client, db, ai, meta):
        await self._two_customers(client, ai, meta)

        conversations = list((await db.execute(select(Conversation))).scalars())
        assert {c.phone_number_id for c in conversations} == {BRAZIL, USA}
        assert len({c.customer_id for c in conversations}) == 2

    async def test_neither_history_leaks_into_the_other_prompt(self, client, db, ai, meta):
        """A shared conversation would put one customer's words in the other's prompt."""
        await self._two_customers(client, ai, meta)

        history = [m["content"] for m in ai.last_prompt if m["role"] == "user"]
        assert history == ["hello"]


class TestTheSameCustomerOnBothNumbers:
    """Rare, but it must not corrupt either thread.

    On the customer's phone these are two separate chats. Sharing one
    conversation row would leave whichever thread the row forgot unanswered.
    """

    async def _both(self, client, ai, meta):
        await post(client, phone_number_id=BRAZIL, text="oi", wa_id="5511999999999")
        await drain_queue()
        await post(client, phone_number_id=USA, text="hello", wa_id="5511999999999")
        await drain_queue()

    async def test_one_customer_two_open_conversations(self, client, db, ai, meta):
        await self._both(client, ai, meta)

        assert (await db.execute(select(func.count()).select_from(Customer))).scalar() == 1
        conversations = list((await db.execute(select(Conversation))).scalars())
        assert len(conversations) == 2
        assert {c.phone_number_id for c in conversations} == {BRAZIL, USA}
        assert all(c.status == "open" for c in conversations)

    async def test_each_thread_is_answered_on_its_own_number(self, client, db, ai, meta):
        await self._both(client, ai, meta)

        assert [m.phone_number_id for m in meta.texts] == [BRAZIL, USA]

    async def test_messages_are_filed_under_the_right_thread(self, client, db, ai, meta):
        await self._both(client, ai, meta)

        by_number = {}
        for conversation in (await db.execute(select(Conversation))).scalars():
            rows = (
                await db.execute(
                    select(Message.content).where(Message.conversation_id == conversation.id)
                )
            ).scalars()
            by_number[conversation.phone_number_id] = [r for r in rows if r]

        assert "oi" in by_number[BRAZIL]
        assert "hello" in by_number[USA]
        assert "hello" not in by_number[BRAZIL]

    async def test_an_install_stops_selling_on_both_threads(self, client, db, ai, meta):
        """Installing is a fact about the person, not about one thread."""
        ai.queue_decision(
            AiDecision(
                reply_text="Here you go.",
                intent="download_request",
                actions=["send_download_link"],
            )
        )
        await self._both(client, ai, meta)

        from app.models import DownloadLink

        link = (await db.execute(select(DownloadLink))).scalars().first()
        response = await client.post(
            "/internal/events/download",
            json={"token": link.token},
            headers={"X-Internal-Token": "test-internal-token"},
        )
        assert response.json()["applied"] is True

        stages = {
            c.phone_number_id: c.sales_stage
            for c in (await db.execute(select(Conversation))).scalars()
        }
        assert set(stages.values()) == {"downloaded"}


class TestFollowUpsRememberTheNumber:
    """The case with no webhook to fall back on.

    A follow-up fires hours or days later, in the worker, with nothing but the
    conversation row to route by. This is why the number is stored rather than
    read off the inbound event each time.
    """

    async def test_a_follow_up_leaves_from_the_original_number(self, client, db, ai, meta):
        ai.queue_decision(AiDecision(reply_text="Hey! What would you record?", intent="greeting"))
        await post(client, phone_number_id=USA, text="hello", wa_id="15551234567")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()

        async def _due(session):
            (await session.get(Reminder, reminder.id)).due_at = utcnow() - timedelta(minutes=1)

        await db.write(_due)

        ai.queue_decision(AiDecision(reply_text="Still keen to try it?", intent="small_talk"))
        from app.services.conversation_flow import send_follow_up

        await send_follow_up(reminder.id)

        assert meta.texts[-1].body == "Still keen to try it?"
        assert meta.texts[-1].phone_number_id == USA

    async def test_a_late_template_follow_up_uses_the_original_number(
        self, client, db, ai, meta
    ):
        """Outside 24h it becomes a template - and templates route too."""
        ai.queue_decision(AiDecision(reply_text="Hey there.", intent="greeting"))
        await post(client, phone_number_id=USA, text="hello", wa_id="15551234567")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()

        async def _age(session):
            (await session.get(Reminder, reminder.id)).due_at = utcnow() - timedelta(minutes=1)
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.last_inbound_at = utcnow() - timedelta(hours=30)

        await db.write(_age)

        from app.services.conversation_flow import send_follow_up

        await send_follow_up(reminder.id)

        assert len(meta.templates) == 1
        assert meta.templates[0].phone_number_id == USA


class TestAnUnknownNumber:
    """Meta and this deployment disagreeing about what we own.

    In practice: a number added in Business Manager and subscribed to the
    webhook, but not added to `WHATSAPP_PHONE_NUMBER_IDS`. Answering it from
    whichever number happened to be first would open a thread the customer
    never started.
    """

    async def test_the_message_is_still_stored(self, client, db, ai, meta):
        await post(client, phone_number_id="999999999", text="hello", wa_id="15551234567")
        await drain_queue()

        message = (await db.execute(select(Message))).scalar_one()
        assert message.content == "hello"

    async def test_nothing_is_sent_back(self, client, db, ai, meta):
        await post(client, phone_number_id="999999999", text="hello", wa_id="15551234567")
        await drain_queue()

        assert meta.sent == []

    async def test_the_conversation_records_the_number_it_arrived_on(self, client, db, ai, meta):
        """So it can be answered as soon as the configuration catches up."""
        await post(client, phone_number_id="999999999", text="hello", wa_id="15551234567")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.phone_number_id == "999999999"

    async def test_adding_the_number_makes_the_conversation_answerable(
        self, client, db, ai, meta, monkeypatch
    ):
        """The stored row is already correct, so nothing has to be repaired."""
        await post(client, phone_number_id="999999999", text="hello", wa_id="15551234567")
        await drain_queue()

        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(
            settings, "whatsapp_phone_number_ids", (BRAZIL, USA, "999999999"), raising=False
        )

        await post(client, phone_number_id="999999999", text="anyone there?", wa_id="15551234567")
        await drain_queue()

        assert len(meta.texts) == 1
        assert meta.texts[0].phone_number_id == "999999999"


class TestLeadAdsPickTheDefault:
    """A lead-ad submission has no number in it - the customer has not written."""

    async def test_outreach_goes_out_from_the_first_configured_number(
        self, client, db, ai, meta
    ):
        from tests.factories import lead_details, leadgen_payload

        meta.register_lead(lead_details(leadgen_id="LEAD-MULTI-1"))
        body, headers = signed(leadgen_payload(leadgen_id="LEAD-MULTI-1"))
        assert (await client.post("/webhooks/meta", content=body, headers=headers)).status_code == 200
        await drain_queue()

        assert len(meta.templates) == 1
        assert meta.templates[0].phone_number_id == BRAZIL

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.phone_number_id == BRAZIL

    async def test_the_reply_stays_on_that_number(self, client, db, ai, meta):
        """Once outreach lands, that thread is the one they answer into."""
        from tests.factories import lead_details, leadgen_payload

        meta.register_lead(lead_details(leadgen_id="LEAD-MULTI-2", phone="15551234567"))
        body, headers = signed(leadgen_payload(leadgen_id="LEAD-MULTI-2"))
        await client.post("/webhooks/meta", content=body, headers=headers)
        await drain_queue()

        await post(client, phone_number_id=BRAZIL, text="tell me more", wa_id="15551234567")
        await drain_queue()

        assert meta.texts[-1].phone_number_id == BRAZIL
        assert as_utc((await db.execute(select(Conversation))).scalar_one().last_inbound_at)

"""Flow 1 end to end: a Meta ad click becomes an AI sales conversation."""

from __future__ import annotations

from sqlalchemy import func, select

from app.ai.schemas import AiDecision
from app.domain import (
    HandlingMode,
    LeadSource,
    MessageDirection,
    MessageStatus,
    SalesStage,
    WebhookStatus,
)
from app.models import (
    Ad,
    AiDecisionLog,
    Conversation,
    Customer,
    DownloadLink,
    Lead,
    Message,
    WebhookEvent,
)
from tests.factories import ctwa_referral, signed, whatsapp_message_payload
from tests.helpers import drain_queue


async def post_message(client, **kwargs):
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200
    return response.json()


class TestClickToWhatsApp:
    async def test_full_pipeline(self, client, db, ai, meta):
        """Ad click -> webhook -> customer -> conversation -> AI -> WhatsApp reply."""
        ai.queue_decision(
            AiDecision(
                reply_text="Hey Priya! What are you hoping to use Boomshare for?",
                intent="information_request",
                suggested_stage=SalesStage.ENGAGED,
                confidence=0.9,
            )
        )

        await post_message(
            client,
            text="I want to know more",
            wa_id="919876543210",
            referral=ctwa_referral(ad_id="AD-777"),
        )
        assert await drain_queue() == 1

        customer = (
            await db.execute(select(Customer).where(Customer.phone == "919876543210"))
        ).scalar_one()
        assert customer.full_name == "Priya"
        assert customer.wa_id == "919876543210"

        conversation = (
            await db.execute(select(Conversation).where(Conversation.customer_id == customer.id))
        ).scalar_one()
        assert conversation.handling_mode == HandlingMode.AI
        assert conversation.sales_stage == SalesStage.ENGAGED

        messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at)
                )
            ).scalars()
        )
        assert [m.direction for m in messages] == [
            MessageDirection.INBOUND,
            MessageDirection.OUTBOUND,
        ]
        assert messages[0].content == "I want to know more"
        assert messages[1].ai_generated is True
        assert messages[1].status == MessageStatus.SENT

        assert len(meta.texts) == 1
        assert meta.texts[0].to == "919876543210"
        assert "Priya" in meta.texts[0].body

    async def test_ad_attribution_is_preserved(self, client, db, ai, meta):
        """A lead is never just a phone number."""
        await post_message(client, referral=ctwa_referral(ad_id="AD-777", headline="Record once"))
        await drain_queue()

        lead = (await db.execute(select(Lead))).scalar_one()
        assert lead.source == LeadSource.CLICK_TO_WHATSAPP
        assert lead.ctwa_clid == "CLID-abc123"
        assert lead.raw_payload["headline"] == "Record once"

        ad = await db.get(Ad, lead.ad_id)
        assert ad.meta_ad_id == "AD-777"

    async def test_message_without_referral_creates_no_lead(self, client, db, ai, meta):
        await post_message(client, text="hello")
        await drain_queue()
        assert (await db.execute(select(func.count()).select_from(Lead))).scalar() == 0

    async def test_second_message_reuses_the_same_conversation(self, client, db, ai, meta):
        await post_message(client, text="hi", wa_id="919876543210")
        await drain_queue()
        await post_message(client, text="tell me about pricing", wa_id="919876543210")
        await drain_queue()

        assert (await db.execute(select(func.count()).select_from(Conversation))).scalar() == 1
        assert (await db.execute(select(func.count()).select_from(Customer))).scalar() == 1
        assert (await db.execute(select(func.count()).select_from(Message))).scalar() == 4
        assert len(ai.calls) == 2

    async def test_only_one_lead_per_customer_from_repeat_referrals(self, client, db, ai, meta):
        for _ in range(3):
            await post_message(client, referral=ctwa_referral(ad_id="AD-777"))
            await drain_queue()
        assert (await db.execute(select(func.count()).select_from(Lead))).scalar() == 1

    async def test_read_receipt_is_sent(self, client, ai, meta):
        await post_message(client, text="hi")
        await drain_queue()
        assert len(meta.read_receipts) == 1

    async def test_the_read_receipt_does_not_block_the_reply(self, client, ai, meta):
        """The blue tick runs *during* the reply, not in front of it.

        Measured against the live stack, awaiting it first cost 340-1000ms
        before any work on the answer began - a Meta round trip plus the TLS
        handshake, because idle connections do not survive the gap between two
        customer messages.

        This is deadlock-shaped rather than timing-shaped on purpose: the fake
        receipt refuses to finish until the reply has gone out, so if the
        receipt were ever awaited first the test would time out instead of
        going green on a fast machine and red on a slow one.
        """
        import asyncio

        replied = asyncio.Event()
        real_send = meta.send_text

        async def send_text(*args, **kwargs):
            result = await real_send(*args, **kwargs)
            replied.set()
            return result

        async def mark_read(provider_message_id, phone_number_id=None):
            await asyncio.wait_for(replied.wait(), timeout=5)
            meta.read_receipts.append(provider_message_id)

        meta.send_text = send_text
        meta.mark_read = mark_read

        await post_message(client, text="hi")
        await drain_queue()

        assert meta.texts, "the reply never went out"
        assert len(meta.read_receipts) == 1, "the receipt did not finish"

    async def test_a_failing_read_receipt_does_not_lose_the_reply(self, client, ai, meta):
        """It is a courtesy. Moving it off the critical path must not make a
        failure there able to take the customer's answer down with it."""

        async def mark_read(provider_message_id, phone_number_id=None):
            raise RuntimeError("meta said no")

        meta.mark_read = mark_read

        await post_message(client, text="hi")
        await drain_queue()

        assert len(meta.texts) == 1

    async def test_webhook_event_is_marked_processed(self, client, db, ai, meta):
        await post_message(client, text="hi")
        await drain_queue()

        event = (await db.execute(select(WebhookEvent))).scalar_one()
        assert event.status == WebhookStatus.PROCESSED
        assert event.processed_at is not None
        assert event.attempts == 1


class TestConversationContext:
    async def test_history_is_passed_to_the_model(self, client, db, ai, meta):
        await post_message(client, text="first question")
        await drain_queue()
        await post_message(client, text="second question")
        await drain_queue()

        history = [m for m in ai.last_prompt if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in history][0] == "first question"
        assert history[-1]["content"] == "second question"

    async def test_prompt_carries_product_knowledge_and_state(self, client, ai, meta):
        await post_message(client, text="what is this")
        await drain_queue()

        system = ai.system_text()
        assert "Boomshare" in system
        assert "Sales stage:" in system
        assert "No download link has been sent yet." in system

    async def test_customer_notes_are_remembered_across_turns(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="Got it - how big is the team?",
                intent="information_request",
                customer_notes={"role": "support lead", "tool": "loom"},
            )
        )
        await post_message(client, text="I run support")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.context_notes == {"role": "support lead", "tool": "loom"}

        await post_message(client, text="about 20 people")
        await drain_queue()
        assert "support lead" in ai.system_text()


class TestDownloadLinkHandling:
    async def test_the_link_goes_out_on_the_turn_it_is_asked_for(self, client, db, ai, meta):
        """The download is never held back to qualify the customer first.

        It used to wait a turn whenever we did not know the build yet, so the
        one thing they came for cost an extra round trip. The build question
        now travels with the link instead of in front of it.
        """
        ai.queue_decision(
            AiDecision(
                reply_text="Here you go - are you on Windows or Mac?",
                intent="buying_intent",
                actions=["send_download_link"],
                suggested_stage=SalesStage.DOWNLOAD_SUGGESTED,
            )
        )
        await post_message(client, text="yes sounds useful")
        await drain_queue()

        link = (await db.execute(select(DownloadLink))).scalar_one()
        assert link.url in meta.texts[0].body
        assert link.sent_at is not None

        decision = (await db.execute(select(AiDecisionLog))).scalar_one()
        assert "send_download_link" in (decision.executed_actions or {})["actions"]

    async def test_a_known_platform_picks_the_installer(self, client, db, ai, meta):
        """Knowing the build still shapes the URL - it just never gates it."""
        ai.queue_decision(
            AiDecision(
                reply_text="Here you go.",
                intent="buying_intent",
                actions=["send_download_link"],
                customer_notes={"platform": "Windows"},
            )
        )
        await post_message(client, text="send me the link, I'm on windows")
        await drain_queue()

        link = (await db.execute(select(DownloadLink))).scalar_one()
        assert link.platform == "windows"
        assert link.url in meta.texts[0].body

    async def test_an_outright_request_is_never_held(self, client, db, ai, meta):
        """"Send me the link" must not be answered with a question."""
        ai.queue_decision(
            AiDecision(
                reply_text="Here you go.",
                intent="download_request",
                actions=["send_download_link"],
            )
        )
        await post_message(client, text="send me the link")
        await drain_queue()

        assert "http" in meta.texts[0].body

    async def test_link_is_generated_by_the_backend_not_the_model(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="Sounds like a great fit - here you go.",
                intent="download_request",
                actions=["send_download_link"],
                suggested_stage=SalesStage.DOWNLOAD_SUGGESTED,
            )
        )
        await post_message(client, text="send me the link")
        await drain_queue()

        link = (await db.execute(select(DownloadLink))).scalar_one()
        assert link.sent_at is not None
        assert link.token in meta.texts[0].body
        assert link.url in meta.texts[0].body

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage == SalesStage.LINK_SENT

    async def test_link_sent_does_not_mean_downloaded(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(reply_text="Here you go.", intent="download_request", actions=["send_download_link"])
        )
        await post_message(client, text="send me the link")
        await drain_queue()

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is None
        assert customer.activated_at is None

    async def test_link_is_not_re_sent_unprompted(self, client, db, ai, meta):
        """The brief is explicit: do not keep pushing the download link."""
        ai.queue_decision(
            AiDecision(reply_text="Here you go.", intent="download_request", actions=["send_download_link"])
        )
        await post_message(client, text="send me the link")
        await drain_queue()

        ai.queue_decision(
            AiDecision(
                reply_text="Let me know how you get on.",
                intent="small_talk",
                actions=["send_download_link"],
            )
        )
        await post_message(client, text="ok thanks")
        await drain_queue()

        assert meta.texts[1].body.count("http") == 0
        assert (await db.execute(select(func.count()).select_from(DownloadLink))).scalar() == 1

        decisions = list((await db.execute(select(AiDecisionLog))).scalars())
        assert any(
            "download_link_suppressed" in (d.rejected_reasons or {}) for d in decisions
        )

    async def test_link_is_re_sent_when_explicitly_requested_again(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(reply_text="Here you go.", intent="download_request", actions=["send_download_link"])
        )
        await post_message(client, text="send me the link")
        await drain_queue()

        ai.queue_decision(
            AiDecision(
                reply_text="Of course, here it is again.",
                intent="download_request",
                actions=["send_download_link"],
            )
        )
        await post_message(client, text="I lost it, resend please")
        await drain_queue()

        assert "http" in meta.texts[1].body
        # Same tracked token both times, so attribution is not split.
        assert (await db.execute(select(func.count()).select_from(DownloadLink))).scalar() == 1


class TestNonTextMessages:
    async def test_media_without_caption_is_stored_but_not_answered(self, client, db, ai, meta):
        await post_message(client, message_type="image")
        await drain_queue()

        message = (await db.execute(select(Message))).scalar_one()
        assert message.direction == MessageDirection.INBOUND
        assert message.message_type == "image"
        assert ai.calls == []
        assert meta.sent == []


class TestDecisionLogging:
    async def test_every_decision_is_recorded(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="What do you record most?",
                intent="information_request",
                suggested_stage=SalesStage.ENGAGED,
                confidence=0.77,
            )
        )
        await post_message(client, text="hi")
        await drain_queue()

        decision = (await db.execute(select(AiDecisionLog))).scalar_one()
        assert decision.model == "fake-model"
        assert decision.intent == "information_request"
        assert decision.suggested_stage == SalesStage.ENGAGED
        assert decision.applied_stage == SalesStage.ENGAGED
        assert decision.confidence == 0.77
        assert decision.executed_actions == {"actions": ["send_reply", "auto_follow_up"]}
        assert decision.inbound_message_id is not None
        assert decision.outbound_message_id is not None
        assert decision.prompt_tokens == 100

    async def test_rejected_stage_is_recorded_but_not_applied(self, client, db, ai, meta):
        """The model cannot claim an install; the attempt is logged."""
        ai.queue_decision(
            AiDecision(
                reply_text="Great, you are all set up.",
                intent="buying_intent",
                suggested_stage=SalesStage.DOWNLOADED,
            )
        )
        await post_message(client, text="I installed it")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage != SalesStage.DOWNLOADED

        decision = (await db.execute(select(AiDecisionLog))).scalar_one()
        assert "stage_system_only" in (decision.rejected_reasons or {})

    async def test_unusable_reply_sends_nothing(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(reply_text="I've sent you the link already", intent="download_request")
        )
        await post_message(client, text="where is it")
        await drain_queue()

        assert meta.sent == []
        decision = (await db.execute(select(AiDecisionLog))).scalar_one()
        assert "false_action_claim" in (decision.rejected_reasons or {})
        assert decision.outbound_message_id is None

"""Flow 2 end to end: a Meta lead ad becomes a WhatsApp conversation."""

from __future__ import annotations

from sqlalchemy import func, select

from app.ai.schemas import AiDecision
from app.domain import (
    LeadSource,
    LeadStatus,
    MessageType,
    ReminderKind,
    ReminderStatus,
    SalesStage,
)
from app.models import Ad, Campaign, Conversation, Customer, Lead, Message, Reminder
from tests.factories import (
    lead_details,
    leadgen_payload,
    signed,
    whatsapp_message_payload,
)
from tests.helpers import drain_queue


async def post_leadgen(client, leadgen_id="LEAD-500", **kwargs):
    body, headers = signed(leadgen_payload(leadgen_id, **kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200
    return response.json()


class TestLeadAdIntake:
    async def test_full_pipeline(self, client, db, ai, meta):
        meta.register_lead(lead_details("LEAD-500"))

        await post_leadgen(client, "LEAD-500")
        assert await drain_queue() == 1

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.phone == "919876543210"  # normalised from "+91 98765 43210"
        assert customer.full_name == "Priya Sharma"
        assert customer.email == "priya@example.com"

        lead = (await db.execute(select(Lead))).scalar_one()
        assert lead.source == LeadSource.LEAD_AD
        assert lead.meta_leadgen_id == "LEAD-500"
        assert lead.status == LeadStatus.CONTACTED
        assert lead.field_data["company_size"] == "11-50"

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage == SalesStage.CONTACTED
        assert conversation.lead_id == lead.id

    async def test_first_contact_uses_a_template_not_free_text(self, client, db, ai, meta):
        """We have never received a message from this person, so free-form is
        not permitted by Meta - it must be an approved template."""
        meta.register_lead(lead_details("LEAD-500"))
        await post_leadgen(client, "LEAD-500")
        await drain_queue()

        assert len(meta.templates) == 1
        assert meta.texts == []
        assert meta.templates[0].template == "boomshare_lead_intro"
        assert meta.templates[0].parameters == ["Priya"]
        assert meta.templates[0].to == "919876543210"

        message = (await db.execute(select(Message))).scalar_one()
        assert message.message_type == MessageType.TEMPLATE

    async def test_no_ai_call_before_the_customer_replies(self, client, ai, meta):
        meta.register_lead(lead_details("LEAD-500"))
        await post_leadgen(client, "LEAD-500")
        await drain_queue()
        assert ai.calls == []

    async def test_campaign_attribution_is_recorded(self, client, db, ai, meta):
        meta.register_lead(lead_details("LEAD-500", campaign_id="CAMP-7", ad_id="AD-200"))
        await post_leadgen(client, "LEAD-500")
        await drain_queue()

        campaign = (await db.execute(select(Campaign))).scalar_one()
        assert campaign.meta_campaign_id == "CAMP-7"
        assert campaign.name == "Boomshare Q1 Leads"

        ad = (await db.execute(select(Ad))).scalar_one()
        assert ad.meta_ad_id == "AD-200"
        assert ad.campaign_id == campaign.id
        assert ad.meta_adset_id == "ADSET-3"

        lead = (await db.execute(select(Lead))).scalar_one()
        assert lead.campaign_id == campaign.id
        assert lead.ad_id == ad.id

    async def test_a_no_reply_nudge_is_queued(self, client, db, ai, meta):
        meta.register_lead(lead_details("LEAD-500"))
        await post_leadgen(client, "LEAD-500")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()
        assert reminder.kind == ReminderKind.NO_REPLY_NUDGE
        assert reminder.status == ReminderStatus.PENDING


class TestLeadReply:
    async def test_reply_starts_the_ai_conversation(self, client, db, ai, meta):
        meta.register_lead(lead_details("LEAD-500"))
        await post_leadgen(client, "LEAD-500")
        await drain_queue()

        ai.queue_decision(
            AiDecision(
                reply_text="Great to hear from you - what do you record most often?",
                intent="information_request",
                suggested_stage=SalesStage.ENGAGED,
            )
        )
        body, headers = signed(
            whatsapp_message_payload(text="Yes, tell me more", wa_id="919876543210")
        )
        await client.post("/webhooks/meta", content=body, headers=headers)
        await drain_queue()

        assert len(ai.calls) == 1
        assert len(meta.texts) == 1

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage == SalesStage.ENGAGED

        lead = (await db.execute(select(Lead))).scalar_one()
        assert lead.status == LeadStatus.RESPONDED

    async def test_reply_cancels_the_no_reply_nudge(self, client, db, ai, meta):
        meta.register_lead(lead_details("LEAD-500"))
        await post_leadgen(client, "LEAD-500")
        await drain_queue()

        body, headers = signed(whatsapp_message_payload(text="hi", wa_id="919876543210"))
        await client.post("/webhooks/meta", content=body, headers=headers)
        await drain_queue()

        reminders = list((await db.execute(select(Reminder))).scalars())
        cancelled = [r for r in reminders if r.status == ReminderStatus.CANCELLED]
        assert [r.resolution for r in cancelled] == ["customer replied"]
        assert sum(r.status == ReminderStatus.PENDING for r in reminders) == 1

    async def test_lead_form_context_reaches_the_prompt(self, client, ai, meta):
        meta.register_lead(lead_details("LEAD-500"))
        await post_leadgen(client, "LEAD-500")
        await drain_queue()

        body, headers = signed(whatsapp_message_payload(text="hi", wa_id="919876543210"))
        await client.post("/webhooks/meta", content=body, headers=headers)
        await drain_queue()

        system = ai.system_text()
        assert "a lead form" in system
        assert "11-50" in system


class TestLeadIdentityMerging:
    async def test_lead_form_and_whatsapp_resolve_to_one_customer(self, client, db, ai, meta):
        """Same person, two channels, one customer record."""
        body, headers = signed(whatsapp_message_payload(text="hello", wa_id="919876543210"))
        await client.post("/webhooks/meta", content=body, headers=headers)
        await drain_queue()

        meta.register_lead(lead_details("LEAD-500", phone="+91 98765 43210"))
        await post_leadgen(client, "LEAD-500")
        await drain_queue()

        assert (await db.execute(select(func.count()).select_from(Customer))).scalar() == 1
        assert (await db.execute(select(func.count()).select_from(Conversation))).scalar() == 1

    async def test_lead_form_fills_in_details_whatsapp_did_not_have(self, client, db, ai, meta):
        body, headers = signed(
            whatsapp_message_payload(text="hello", wa_id="919876543210", profile_name=None)
        )
        await client.post("/webhooks/meta", content=body, headers=headers)
        await drain_queue()

        meta.register_lead(lead_details("LEAD-500"))
        await post_leadgen(client, "LEAD-500")
        await drain_queue()

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.full_name == "Priya Sharma"
        assert customer.email == "priya@example.com"


class TestLeadFailures:
    async def test_lead_without_a_phone_number_is_not_retried(self, client, db, ai, meta):
        details = lead_details("LEAD-NOPHONE")
        details.field_data = [{"name": "full_name", "values": ["Nameless"]}]
        meta.register_lead(details)

        await post_leadgen(client, "LEAD-NOPHONE")
        await drain_queue()

        assert (await db.execute(select(func.count()).select_from(Lead))).scalar() == 0
        assert meta.sent == []

    async def test_unknown_lead_id_is_recorded_as_failed(self, client, db, ai, meta):
        from app.domain import WebhookStatus
        from app.models import WebhookEvent

        await post_leadgen(client, "LEAD-MISSING")
        await drain_queue()

        event = (await db.execute(select(WebhookEvent))).scalar_one()
        assert event.status == WebhookStatus.FAILED
        assert "unknown lead" in (event.last_error or "")

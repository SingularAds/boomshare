"""Download link vs. download vs. activation - three different facts."""

from __future__ import annotations

from sqlalchemy import select

from app.ai.schemas import AiDecision
from app.domain import SalesStage
from app.models import Conversation, Customer, DownloadLink
from app.services.downloads import normalise_platform
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue

AUTH = {"X-Internal-Token": "test-internal-token"}


async def post(client, **kwargs):
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


async def send_link(client, ai):
    ai.queue_decision(
        AiDecision(
            reply_text="Here you go - takes about a minute to install.",
            intent="download_request",
            actions=["send_download_link"],
        )
    )
    await post(client, text="send me the link")
    await drain_queue()


class TestLinkGeneration:
    async def test_link_carries_a_token_for_attribution(self, client, db, ai, meta):
        await send_link(client, ai)

        link = (await db.execute(select(DownloadLink))).scalar_one()
        assert link.token
        assert link.token in link.url
        assert link.url.startswith("https://boomshare.test/download")

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert link.conversation_id == conversation.id

    async def test_sending_a_link_does_not_mark_a_download(self, client, db, ai, meta):
        await send_link(client, ai)

        link = (await db.execute(select(DownloadLink))).scalar_one()
        assert link.sent_at is not None
        assert link.downloaded_at is None
        assert link.activated_at is None

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is None

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage == SalesStage.LINK_SENT


class TestConfirmedInstall:
    async def test_download_event_updates_customer_and_stage(self, client, db, ai, meta):
        await send_link(client, ai)
        link = (await db.execute(select(DownloadLink))).scalar_one()

        response = await client.post(
            "/internal/events/download",
            json={"token": link.token, "platform": "windows", "app_version": "1.4.0"},
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["applied"] is True

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is not None
        assert customer.activated_at is None

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage == SalesStage.DOWNLOADED

        updated_link = await db.get(DownloadLink, link.id)
        assert updated_link.downloaded_at is not None
        assert updated_link.details["platform"] == "windows"

    async def test_activation_implies_download(self, client, db, ai, meta):
        await send_link(client, ai)
        link = (await db.execute(select(DownloadLink))).scalar_one()

        response = await client.post(
            "/internal/events/activation", json={"token": link.token}, headers=AUTH
        )
        assert response.json()["applied"] is True

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is not None
        assert customer.activated_at is not None

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage == SalesStage.ACTIVATED

    async def test_events_can_be_matched_by_phone_when_no_token_is_available(
        self, client, db, ai, meta
    ):
        await post(client, text="hi")
        await drain_queue()

        response = await client.post(
            "/internal/events/download", json={"phone": "+91 98765 43210"}, headers=AUTH
        )
        assert response.status_code == 200
        assert response.json()["applied"] is True

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is not None

    async def test_click_is_tracked_separately_from_install(self, client, db, ai, meta):
        await send_link(client, ai)
        link = (await db.execute(select(DownloadLink))).scalar_one()

        await client.post("/internal/events/click", json={"token": link.token}, headers=AUTH)

        updated = await db.get(DownloadLink, link.id)
        assert updated.clicked_at is not None
        assert updated.downloaded_at is None

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is None


class TestAiCannotFakeAnInstall:
    async def test_ai_claiming_an_install_changes_nothing(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="Brilliant, glad it is working.",
                intent="buying_intent",
                suggested_stage=SalesStage.ACTIVATED,
            )
        )
        await post(client, text="all set up now")
        await drain_queue()

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is None
        assert customer.activated_at is None

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage != SalesStage.ACTIVATED


class TestInternalAuth:
    async def test_token_is_required(self, client):
        response = await client.post("/internal/events/download", json={"phone": "123"})
        assert response.status_code == 401

    async def test_wrong_token_is_rejected(self, client):
        response = await client.post(
            "/internal/events/download",
            json={"phone": "123"},
            headers={"X-Internal-Token": "nope"},
        )
        assert response.status_code == 401

    async def test_an_identifier_is_required(self, client):
        response = await client.post("/internal/events/download", json={}, headers=AUTH)
        assert response.status_code == 422

    async def test_unknown_customer_is_404(self, client):
        response = await client.post(
            "/internal/events/download", json={"phone": "440000000000"}, headers=AUTH
        )
        assert response.status_code == 404

    async def test_unknown_token_is_404(self, client):
        response = await client.post(
            "/internal/events/click", json={"token": "does-not-exist"}, headers=AUTH
        )
        assert response.status_code == 404


class TestFunnelReport:
    async def test_report_counts_the_funnel_by_campaign(self, client, db, ai, meta):
        from tests.factories import lead_details, leadgen_payload

        meta.register_lead(lead_details("LEAD-500", campaign_id="CAMP-7"))
        body, headers = signed(leadgen_payload("LEAD-500"))
        await client.post("/webhooks/meta", content=body, headers=headers)
        await drain_queue()

        await send_link(client, ai)
        link = (await db.execute(select(DownloadLink))).scalar_one()
        await client.post(
            "/internal/events/activation", json={"token": link.token}, headers=AUTH
        )

        response = await client.get("/admin/reports/funnel", headers=AUTH)
        assert response.status_code == 200

        row = response.json()[0]
        assert row["campaign_id"] == "CAMP-7"
        assert row["campaign_name"] == "Boomshare Q1 Leads"
        assert row["leads"] == 1
        assert row["conversations"] == 1
        assert row["links_sent"] == 1
        assert row["downloads"] == 1
        assert row["activations"] == 1


class TestPlatformOnTheLink:
    """The platform slot is an install detail, so it has to survive to the link."""

    def test_free_text_is_mapped_onto_a_real_build(self):
        assert normalise_platform("Windows") == "windows"
        assert normalise_platform("windows 11 laptop") == "windows"
        assert normalise_platform("Mac") == "macos"
        assert normalise_platform("my macbook at work") == "macos"

    def test_anything_unrecognised_becomes_none(self):
        """It reaches a URL and a 32-char column, so it cannot be free text."""
        assert normalise_platform("a phone") is None
        assert normalise_platform("") is None
        assert normalise_platform(None) is None
        assert normalise_platform("linux") is None

    async def test_a_known_platform_is_recorded_on_the_link(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="What are you on?",
                intent="buying_intent",
                customer_notes={"platform": "Windows"},
            )
        )
        await post(client, text="I want it")
        await drain_queue()

        ai.queue_decision(
            AiDecision(
                reply_text="Here you go.",
                intent="download_request",
                actions=["send_download_link"],
            )
        )
        await post(client, text="send it over")
        await drain_queue()

        link = (await db.execute(select(DownloadLink))).scalar_one()
        assert link.platform == "windows"
        assert "platform=windows" in link.url


class TestAgentEffectivenessReport:
    """The metric that makes the next prompt change measurable rather than a guess."""

    async def test_it_measures_passivity_and_progress(self, client, db, ai, meta):
        # One purely conversational turn - the failure mode being measured.
        ai.queue_decision(AiDecision(reply_text="Happy to explain.", intent="small_talk"))
        await post(client, text="what is this")
        await drain_queue()

        # One turn that actually advances the sale.
        await send_link(client, ai)

        response = await client.get("/admin/reports/agent", headers=AUTH)
        assert response.status_code == 200
        report = response.json()

        assert report["conversations"] == 1
        assert report["ai_turns"] == 2
        assert report["turns_without_action"] == 1
        assert report["passivity_rate"] == 0.5
        assert report["reached_link_sent"] == 1
        assert report["median_turns_to_link"] == 2

    async def test_it_reports_zero_rather_than_dividing_by_nothing(self, client):
        response = await client.get("/admin/reports/agent", headers=AUTH)
        assert response.status_code == 200
        report = response.json()
        assert report["ai_turns"] == 0
        assert report["passivity_rate"] == 0.0
        assert report["median_turns_to_link"] is None

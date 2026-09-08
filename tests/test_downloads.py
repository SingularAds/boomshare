"""Download link vs. download vs. activation - three different facts."""

from __future__ import annotations

from sqlalchemy import func, select

from app.ai.schemas import AiDecision
from app.domain import SalesStage
from app.models import AiDecisionLog, Conversation, Customer, DownloadLink
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue

AUTH = {"X-Internal-Token": "test-internal-token"}
ADMIN_AUTH = {"X-Admin-Token": "test-admin-token"}


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

        response = await client.get("/admin/reports/funnel", headers=ADMIN_AUTH)
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
    """The reported platform has to survive as far as the installer URL."""

    async def test_a_reported_platform_is_recorded_on_the_link(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="What are you on?",
                intent="buying_intent",
                customer_platform="windows",
            )
        )
        await post(client, text="I want it")
        await drain_queue()

        ai.queue_decision(
            AiDecision(
                reply_text="Here you go.",
                intent="download_request",
                actions=["send_download_link"],
                customer_platform="windows",
            )
        )
        await post(client, text="send it over")
        await drain_queue()

        link = (await db.execute(select(DownloadLink))).scalar_one()
        assert link.platform == "windows"
        assert "platform=windows" in link.url

    async def test_an_unknown_platform_leaves_the_url_generic(self, client, db, ai, meta):
        """The download page works without knowing the build, so not knowing is
        never a reason to hold the link back."""
        ai.queue_decision(
            AiDecision(
                reply_text="Here you go - Windows or Mac?",
                intent="download_request",
                actions=["send_download_link"],
            )
        )
        await post(client, text="send it over")
        await drain_queue()

        link = (await db.execute(select(DownloadLink))).scalar_one()
        assert link.platform is None
        assert "platform=" not in link.url


class TestAgentEffectivenessReport:
    """The metric that makes the next prompt change measurable rather than a guess."""

    async def test_it_measures_passivity_and_progress(self, client, db, ai, meta):
        # One purely conversational turn - the failure mode being measured.
        ai.queue_decision(AiDecision(reply_text="Happy to explain.", intent="small_talk"))
        await post(client, text="what is this")
        await drain_queue()

        # One turn that actually advances the sale.
        await send_link(client, ai)

        response = await client.get("/admin/reports/agent", headers=ADMIN_AUTH)
        assert response.status_code == 200
        report = response.json()

        assert report["conversations"] == 1
        assert report["ai_turns"] == 2
        assert report["turns_without_action"] == 1
        assert report["passivity_rate"] == 0.5
        assert report["reached_link_sent"] == 1
        assert report["median_turns_to_link"] == 2

    async def test_it_reports_zero_rather_than_dividing_by_nothing(self, client):
        response = await client.get("/admin/reports/agent", headers=ADMIN_AUTH)
        assert response.status_code == 200
        report = response.json()
        assert report["ai_turns"] == 0
        assert report["passivity_rate"] == 0.0
        assert report["median_turns_to_link"] is None


class TestAnswerThenOfferThenSend:
    """The sales order a person would use, driven end to end.

    From a live transcript: the customer asked about pricing and got the price,
    an offer of the link, and the link itself all in one message - so "would you
    like me to send it?" arrived with the answer already attached.

    The fix is that offering and delivering are two different actions, so the
    model says which message it is writing. The backend no longer infers that
    from the wording; it just does what it was asked.
    """

    async def test_the_offer_turn_carries_no_link(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text=(
                    "There's a free plan with unlimited 5-minute recordings. Would you "
                    "like me to send you the download so you can try it?"
                ),
                intent="pricing_question",
                suggested_stage=SalesStage.PRODUCT_EXPLAINED,
                actions=["offer_download_link"],
            )
        )
        await post(client, text="can you tell me about price")
        await drain_queue()

        assert "http" not in meta.texts[-1].body
        assert (await db.execute(select(func.count()).select_from(DownloadLink))).scalar() == 0

        decision = (await db.execute(select(AiDecisionLog))).scalar_one()
        assert "offer_download_link" in decision.executed_actions["actions"]

    async def test_saying_yes_gets_the_link(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="Want me to send the download?",
                intent="pricing_question",
                actions=["offer_download_link"],
            )
        )
        await post(client, text="can you tell me about price")
        await drain_queue()

        ai.queue_decision(
            AiDecision(
                reply_text="Here you go - the installer takes about a minute.",
                intent="download_request",
                actions=["send_download_link"],
            )
        )
        await post(client, text="yes please")
        await drain_queue()

        link = (await db.execute(select(DownloadLink))).scalar_one()
        assert link.url in meta.texts[-1].body
        assert link.sent_at is not None

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage == SalesStage.LINK_SENT

    async def test_a_send_is_not_downgraded_because_of_how_it_reads(
        self, client, db, ai, meta
    ):
        """The wording used to be pattern-matched, which only ever worked in
        English. If the model asked to send, it meant send."""
        ai.queue_decision(
            AiDecision(
                reply_text="Quer que eu envie o link para download? Aqui esta.",
                intent="information_request",
                actions=["send_download_link"],
            )
        )
        await post(client, text="me manda o link")
        await drain_queue()

        assert "http" in meta.texts[-1].body

    async def test_an_outright_request_still_skips_the_offer(self, client, db, ai, meta):
        """"Send me the link" must never be answered with "shall I send it?"."""
        await send_link(client, ai)

        link = (await db.execute(select(DownloadLink))).scalar_one()
        assert link.url in meta.texts[0].body


class TestPhoneResolution:
    """A signup form takes whatever the user types, so the number rarely comes
    back in the `wa_id` form WhatsApp gave us."""

    async def test_a_number_without_its_country_code_still_resolves(
        self, client, db, ai, meta
    ):
        await post(client, text="hi")
        await drain_queue()

        response = await client.post(
            "/internal/events/activation", json={"phone": "9876543210"}, headers=AUTH
        )
        assert response.status_code == 200
        assert response.json()["applied"] is True

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.activated_at is not None

    async def test_a_number_in_national_form_still_resolves(self, client, db, ai, meta):
        await post(client, text="hi")
        await drain_queue()

        response = await client.post(
            "/internal/events/download", json={"phone": "0 98765 43210"}, headers=AUTH
        )
        assert response.status_code == 200

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is not None

    async def test_an_ambiguous_suffix_is_refused_rather_than_guessed(
        self, client, db, ai, meta
    ):
        """Two people sharing a tail is exactly when a guess picks the wrong one."""
        await post(client, text="hi")
        await drain_queue()

        async def add_lookalike(session):
            session.add(Customer(phone="449876543210", wa_id="449876543210"))

        await db.write(add_lookalike)

        response = await client.post(
            "/internal/events/download", json={"phone": "9876543210"}, headers=AUTH
        )
        assert response.status_code == 404

        for customer in (await db.execute(select(Customer))).scalars():
            assert customer.downloaded_at is None

    async def test_a_suffix_too_short_to_identify_anyone_is_refused(
        self, client, db, ai, meta
    ):
        await post(client, text="hi")
        await drain_queue()

        response = await client.post(
            "/internal/events/download", json={"phone": "543210"}, headers=AUTH
        )
        assert response.status_code == 404

    async def test_an_unmatched_install_writes_nothing(self, client, db, ai, meta):
        """Most installs are somebody else's - they must not leave a trace."""
        await post(client, text="hi")
        await drain_queue()

        response = await client.post(
            "/internal/events/download", json={"phone": "+1 555 010 9999"}, headers=AUTH
        )
        assert response.status_code == 404

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is None


class TestEventDetails:
    async def test_absent_fields_are_not_written_as_nulls(self, client, db, ai, meta):
        """A null would erase a platform an earlier event had already recorded."""
        await send_link(client, ai)
        link = (await db.execute(select(DownloadLink))).scalar_one()

        await client.post(
            "/internal/events/download",
            json={"token": link.token, "platform": "windows", "app_version": "1.4.2"},
            headers=AUTH,
        )
        await client.post(
            "/internal/events/activation", json={"token": link.token}, headers=AUTH
        )

        stored = await db.get(DownloadLink, link.id)
        assert stored.details["platform"] == "windows"
        assert stored.details["app_version"] == "1.4.2"
        assert None not in stored.details.values()


class TestClickReporting:
    async def test_only_the_first_click_is_applied(self, client, db, ai, meta):
        """The page may report on every load; the moment they followed the link
        happened once."""
        await send_link(client, ai)
        link = (await db.execute(select(DownloadLink))).scalar_one()

        first = await client.post(
            "/internal/events/click", json={"token": link.token}, headers=AUTH
        )
        assert first.status_code == 200
        assert first.json()["applied"] is True

        clicked_at = (await db.get(DownloadLink, link.id)).clicked_at
        assert clicked_at is not None

        second = await client.post(
            "/internal/events/click", json={"token": link.token}, headers=AUTH
        )
        assert second.status_code == 200
        assert second.json()["applied"] is False
        assert second.json()["detail"] == "already recorded"

        assert (await db.get(DownloadLink, link.id)).clicked_at == clicked_at

    async def test_an_unknown_token_is_rejected(self, client, db, ai, meta):
        response = await client.post(
            "/internal/events/click", json={"token": "never-issued"}, headers=AUTH
        )
        assert response.status_code == 404

    async def test_a_longer_number_that_merely_ends_the_same_is_not_matched(
        self, client, db, ai, meta
    ):
        """Customers span Portugal, the US and Brazil. Only a country code may
        sit in front of the national number - anything longer is a coincidence."""

        async def add_unrelated(session):
            # Ends with the same ten digits, but with five digits in front of
            # them rather than a country code.
            session.add(Customer(phone="123459876543210", wa_id="123459876543210"))

        await db.write(add_unrelated)

        response = await client.post(
            "/internal/events/download", json={"phone": "9876543210"}, headers=AUTH
        )
        assert response.status_code == 404

    async def test_the_same_national_number_in_another_country_is_ambiguous(
        self, client, db, ai, meta
    ):
        """+91 98765 43210 and +55 98765 43210 are two people, not one."""
        await post(client, text="hi")
        await drain_queue()

        async def add_brazilian(session):
            session.add(Customer(phone="559876543210", wa_id="559876543210"))

        await db.write(add_brazilian)

        response = await client.post(
            "/internal/events/download", json={"phone": "9876543210"}, headers=AUTH
        )
        assert response.status_code == 404

        for customer in (await db.execute(select(Customer))).scalars():
            assert customer.downloaded_at is None


class TestTokenSeparation:
    """The internal secret is held by a partner. The admin console is not."""

    async def test_the_partner_secret_cannot_reach_the_admin_api(self, client):
        response = await client.get("/admin/reports/funnel", headers=AUTH)
        assert response.status_code == 401

    async def test_the_partner_secret_cannot_delete_data(self, client, db, ai, meta):
        await post(client, text="hi")
        await drain_queue()

        response = await client.post("/admin/reset", headers=AUTH)
        assert response.status_code == 401

        assert (await db.execute(select(func.count()).select_from(Customer))).scalar_one() == 1

    async def test_the_admin_secret_cannot_report_installs(self, client, db, ai, meta):
        await post(client, text="hi")
        await drain_queue()

        response = await client.post(
            "/internal/events/download",
            json={"phone": "+91 98765 43210"},
            headers=ADMIN_AUTH,
        )
        assert response.status_code == 401

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.downloaded_at is None

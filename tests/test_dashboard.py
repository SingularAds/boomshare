"""The dashboard reports what is in the database, and nothing else.

The point of these tests is the second half of that sentence: a dashboard that
rounds, estimates or quietly substitutes a plausible number is worse than no
dashboard, because the operator cannot tell which figures to trust.
"""

from __future__ import annotations

from sqlalchemy import select

from app.ai.schemas import AiDecision
from app.models import Customer, DownloadLink
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue

AUTH = {"X-Internal-Token": "test-internal-token"}
# The dedicated read-only dashboard token.
DASHBOARD_AUTH = {"X-Admin-Token": "test-dashboard-token"}
# The master admin token — still works on dashboard routes, required for
# destructive operations (reset, handoff, prompt reload, agent messages).
ADMIN_AUTH = {"X-Admin-Token": "test-admin-token"}


async def post(client, **kwargs):
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


async def send_link(client, ai, wa_id="919876543210"):
    ai.queue_decision(
        AiDecision(
            reply_text="Here you go - takes about a minute to install.",
            intent="download_request",
            actions=["send_download_link"],
        )
    )
    await post(client, text="send me the link", wa_id=wa_id)
    await drain_queue()


async def link_for(db, phone):
    return (
        await db.execute(
            select(DownloadLink).join(Customer).where(Customer.phone == phone)
        )
    ).scalar_one()


async def click(client, link):
    response = await client.post(
        "/internal/events/click", json={"token": link.token}, headers=AUTH
    )
    assert response.status_code == 200


class TestDashboardAuth:
    async def test_data_requires_a_token(self, client):
        """No token at all → 401."""
        for path in (
            "/admin/dashboard/overview",
            "/admin/dashboard/customers",
        ):
            assert (await client.get(path)).status_code == 401

    async def test_the_partner_token_cannot_read_the_dashboard(self, client):
        response = await client.get("/admin/dashboard/overview", headers=AUTH)
        assert response.status_code == 401

    async def test_the_shell_needs_no_token(self, client):
        """It carries no data - the token is what the page then asks for."""
        response = await client.get("/dashboard")
        assert response.status_code in (200, 503)

    async def test_dashboard_token_grants_overview_access(self, client):
        """The lightweight read-only dashboard token works on dashboard routes."""
        response = await client.get("/admin/dashboard/overview", headers=DASHBOARD_AUTH)
        assert response.status_code == 200

    async def test_dashboard_token_grants_customer_list_access(self, client):
        response = await client.get("/admin/dashboard/customers", headers=DASHBOARD_AUTH)
        assert response.status_code == 200

    async def test_master_token_still_works_on_dashboard_routes(self, client):
        """The master admin token is accepted as a fallback on dashboard routes,
        so the team never needs two tokens to use their own dashboard."""
        response = await client.get("/admin/dashboard/overview", headers=ADMIN_AUTH)
        assert response.status_code == 200

    async def test_dashboard_token_cannot_call_destructive_admin_reset(self, client):
        """The dashboard token must NOT unlock /admin/reset (wipes the database)."""
        response = await client.post("/admin/reset", headers=DASHBOARD_AUTH)
        assert response.status_code == 401

    async def test_dashboard_token_cannot_access_admin_conversations(self, client):
        """The dashboard token must NOT unlock /admin/conversations."""
        response = await client.get("/admin/conversations", headers=DASHBOARD_AUTH)
        assert response.status_code == 401


class TestOverview:
    async def test_an_empty_database_reports_zeroes_not_gaps(self, client):
        """Zero is an answer. A missing field would look like a broken query."""
        body = (await client.get("/admin/dashboard/overview", headers=ADMIN_AUTH)).json()

        assert body["customers"] == 0
        assert body["customers_from_ads"] == 0
        assert body["customers_direct"] == 0
        assert body["links_sent"] == 0
        assert body["customers_clicked"] == 0
        assert body["conversations"] == 0
        assert body["messages"] == 0
        assert body["leads_by_source"] == []

    async def test_counts_match_what_actually_happened(self, client, db, ai, meta):
        await send_link(client, ai)
        link = (await db.execute(select(DownloadLink))).scalar_one()
        await client.post("/internal/events/click", json={"token": link.token}, headers=AUTH)
        await client.post(
            "/internal/events/activation", json={"token": link.token}, headers=AUTH
        )

        body = (await client.get("/admin/dashboard/overview", headers=ADMIN_AUTH)).json()

        assert body["customers"] == 1
        assert body["links_sent"] == 1
        assert body["links_clicked"] == 1
        assert body["customers_with_link"] == 1
        assert body["customers_downloaded"] == 1
        assert body["customers_activated"] == 1
        assert body["conversations"] == 1
        assert body["messages"] > 0
        assert body["messages_inbound"] + body["messages_outbound"] == body["messages"]

    async def test_clicks_count_people_not_repeat_clicks(self, client, db, ai, meta):
        """The funnel step is people who opened their link, so a second click
        from the same person - or a download page that reports every load -
        must not move it, and someone who never opened theirs is not in it."""
        await send_link(client, ai, wa_id="919876543210")
        await send_link(client, ai, wa_id="447700900123")
        opened = await link_for(db, "919876543210")

        await click(client, opened)
        await click(client, opened)

        body = (await client.get("/admin/dashboard/overview", headers=ADMIN_AUTH)).json()

        assert body["customers_with_link"] == 2
        assert body["customers_clicked"] == 1
        # The existing per-link count is unchanged.
        assert body["links_clicked"] == 1

    async def test_a_customer_with_no_lead_counts_as_direct(self, client, ai, meta):
        """Nobody arrived from an ad, and the dashboard says so rather than
        leaving the customer uncounted."""
        await post(client, text="hi")
        await drain_queue()

        body = (await client.get("/admin/dashboard/overview", headers=ADMIN_AUTH)).json()

        assert body["customers"] == 1
        assert body["customers_from_ads"] == 0
        assert body["customers_direct"] == 1
        assert body["campaigns"] == 0

    async def test_an_ad_lead_is_counted_as_acquisition(self, client, ai, meta):
        from tests.factories import lead_details, leadgen_payload

        meta.register_lead(lead_details("LEAD-900", campaign_id="CAMP-9"))
        body, headers = signed(leadgen_payload("LEAD-900"))
        await client.post("/webhooks/meta", content=body, headers=headers)
        await drain_queue()

        overview = (await client.get("/admin/dashboard/overview", headers=ADMIN_AUTH)).json()

        assert overview["customers_from_ads"] == 1
        assert overview["customers_direct"] == 0
        assert overview["campaigns"] == 1
        assert [row["key"] for row in overview["leads_by_source"]] == ["lead_ad"]


class TestCustomerList:
    async def test_rows_carry_the_derived_facts_the_table_shows(self, client, ai, meta):
        await send_link(client, ai)

        page = (await client.get("/admin/dashboard/customers", headers=ADMIN_AUTH)).json()

        assert page["total"] == 1
        row = page["rows"][0]
        assert row["phone"] == "919876543210"
        assert row["source"] == "direct"
        assert row["campaign_name"] is None
        assert row["numbers"] == ["15550001111"]
        assert row["stage"] == "link_sent"
        assert row["messages"] > 0
        assert row["last_activity_at"] is not None

    async def test_a_row_says_when_the_link_was_first_clicked(self, client, db, ai, meta):
        await send_link(client, ai)

        before = (await client.get("/admin/dashboard/customers", headers=ADMIN_AUTH)).json()
        assert before["rows"][0]["clicked_at"] is None

        link = await link_for(db, "919876543210")
        await click(client, link)

        after = (await client.get("/admin/dashboard/customers", headers=ADMIN_AUTH)).json()
        assert after["rows"][0]["clicked_at"] is not None
        clicked = (await db.execute(select(DownloadLink))).scalar_one().clicked_at
        assert after["rows"][0]["clicked_at"].startswith(clicked.isoformat()[:19])

    async def test_the_clicked_filter_lists_only_people_who_opened_their_link(
        self, client, db, ai, meta
    ):
        await send_link(client, ai, wa_id="919876543210")
        await send_link(client, ai, wa_id="447700900123")
        await click(client, await link_for(db, "919876543210"))

        clicked = (
            await client.get("/admin/dashboard/customers?outcome=clicked", headers=ADMIN_AUTH)
        ).json()
        assert clicked["total"] == 1
        assert [row["phone"] for row in clicked["rows"]] == ["919876543210"]

        # The filters that already existed keep their meaning: clicking is not
        # downloading, so both people are still in the pipeline.
        pipeline = (
            await client.get(
                "/admin/dashboard/customers?outcome=not_downloaded", headers=ADMIN_AUTH
            )
        ).json()
        assert pipeline["total"] == 2

    async def test_search_matches_phone_and_name(self, client, ai, meta):
        await post(client, text="hi")
        await drain_queue()

        hit = (
            await client.get("/admin/dashboard/customers?search=9876", headers=ADMIN_AUTH)
        ).json()
        assert hit["total"] == 1

        miss = (
            await client.get("/admin/dashboard/customers?search=zzzz", headers=ADMIN_AUTH)
        ).json()
        assert miss["total"] == 0
        assert miss["rows"] == []

    async def test_outcome_filter_narrows_to_real_rows(self, client, db, ai, meta):
        await send_link(client, ai)
        link = (await db.execute(select(DownloadLink))).scalar_one()

        before = (
            await client.get(
                "/admin/dashboard/customers?outcome=downloaded", headers=ADMIN_AUTH
            )
        ).json()
        assert before["total"] == 0

        await client.post(
            "/internal/events/download", json={"token": link.token}, headers=AUTH
        )

        after = (
            await client.get(
                "/admin/dashboard/customers?outcome=downloaded", headers=ADMIN_AUTH
            )
        ).json()
        assert after["total"] == 1

    async def test_paging_reports_a_stable_total(self, client, ai, meta):
        for i in range(3):
            await post(client, text=f"hello {i}", wa_id=f"91987654321{i}")
        await drain_queue()

        page = (
            await client.get("/admin/dashboard/customers?limit=2&offset=0", headers=ADMIN_AUTH)
        ).json()
        assert page["total"] == 3
        assert len(page["rows"]) == 2

        rest = (
            await client.get("/admin/dashboard/customers?limit=2&offset=2", headers=ADMIN_AUTH)
        ).json()
        assert rest["total"] == 3
        assert len(rest["rows"]) == 1

    async def test_a_customer_with_two_threads_reports_the_furthest_stage(
        self, client, db, ai, meta
    ):
        """Two of our numbers, one person - the table shows their best stage."""
        await send_link(client, ai)
        await post(client, text="hi there", phone_number_id="444555666")
        await drain_queue()

        page = (await client.get("/admin/dashboard/customers", headers=ADMIN_AUTH)).json()
        row = page["rows"][0]
        # Both of our numbers, in the order they first wrote to them.
        assert row["numbers"] == ["15550001111", "442079460002"]
        assert row["stage"] == "link_sent"


class TestCustomerDetail:
    async def test_detail_returns_the_thread_for_the_modal(self, client, db, ai, meta):
        await send_link(client, ai)
        customer = (await db.execute(select(Customer))).scalar_one()

        body = (
            await client.get(
                f"/admin/dashboard/customers/{customer.id}", headers=ADMIN_AUTH
            )
        ).json()

        assert body["phone"] == "919876543210"
        assert len(body["conversations"]) == 1

        thread = body["conversations"][0]
        assert thread["truncated"] is False
        assert [m["direction"] for m in thread["messages"]] == ["inbound", "outbound"]
        assert thread["messages"][0]["content"] == "send me the link"
        assert thread["messages"][1]["ai_generated"] is True

    async def test_messages_are_oldest_first(self, client, db, ai, meta):
        """The modal renders them top to bottom without re-sorting."""
        await send_link(client, ai)
        customer = (await db.execute(select(Customer))).scalar_one()

        body = (
            await client.get(
                f"/admin/dashboard/customers/{customer.id}", headers=ADMIN_AUTH
            )
        ).json()
        stamps = [m["created_at"] for m in body["conversations"][0]["messages"]]
        assert stamps == sorted(stamps)

    async def test_the_link_is_exposed_for_the_facts_panel(self, client, db, ai, meta):
        await send_link(client, ai)
        link = (await db.execute(select(DownloadLink))).scalar_one()
        await client.post("/internal/events/click", json={"token": link.token}, headers=AUTH)

        customer = (await db.execute(select(Customer))).scalar_one()
        body = (
            await client.get(
                f"/admin/dashboard/customers/{customer.id}", headers=ADMIN_AUTH
            )
        ).json()

        assert len(body["links"]) == 1
        assert body["links"][0]["clicked_at"] is not None

    async def test_unknown_customer_is_404(self, client):
        response = await client.get(
            "/admin/dashboard/customers/11111111-1111-1111-1111-111111111111",
            headers=ADMIN_AUTH,
        )
        assert response.status_code == 404


class TestWhichNumberTheyCameThrough:
    """Two numbers answer the same webhook, so the dashboard has to say which."""

    async def test_a_row_names_the_number_they_wrote_to(self, client, ai, meta):
        await post(client, text="hi", phone_number_id="444555666")
        await drain_queue()

        page = (await client.get("/admin/dashboard/customers", headers=ADMIN_AUTH)).json()
        assert page["rows"][0]["numbers"] == ["442079460002"]

    async def test_someone_who_wrote_to_both_shows_both(self, client, ai, meta):
        await post(client, text="first", phone_number_id="111222333")
        await drain_queue()
        await post(client, text="second", phone_number_id="444555666")
        await drain_queue()

        page = (await client.get("/admin/dashboard/customers", headers=ADMIN_AUTH)).json()
        assert page["rows"][0]["numbers"] == ["15550001111", "442079460002"]

    async def test_the_profile_and_each_thread_name_the_number(self, client, db, ai, meta):
        from app.models import Customer

        await post(client, text="hi", phone_number_id="444555666")
        await drain_queue()
        customer = (await db.execute(select(Customer))).scalar_one()

        body = (
            await client.get(
                f"/admin/dashboard/customers/{customer.id}", headers=ADMIN_AUTH
            )
        ).json()
        assert body["numbers"] == ["442079460002"]
        assert body["conversations"][0]["number_label"] == "442079460002"
        assert body["conversations"][0]["phone_number_id"] == "444555666"

    async def test_what_meta_reported_beats_hand_written_config(
        self, client, ai, meta, monkeypatch
    ):
        """The number on the webhook is the number. Config is only a fallback.

        A label map has to be edited whenever a number is added, and the moment
        someone forgets, the dashboard is wrong. What Meta sent with the message
        cannot go stale, so it wins.
        """
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setitem(settings.whatsapp_number_labels, "444555666", "STALE CONFIG")
        try:
            await post(client, text="hi", phone_number_id="444555666")
            await drain_queue()

            page = (
                await client.get("/admin/dashboard/customers", headers=ADMIN_AUTH)
            ).json()
            assert page["rows"][0]["numbers"] == ["442079460002"]
        finally:
            settings.whatsapp_number_labels.pop("444555666", None)

    async def test_config_still_names_a_thread_that_predates_the_column(
        self, client, db, ai, meta, monkeypatch
    ):
        """Old threads never saw that webhook, so the fallback still matters."""
        from app.core.config import get_settings
        from app.models import Conversation

        await post(client, text="hi", phone_number_id="444555666")
        await drain_queue()

        async def forget(session):
            row = (
                await session.execute(
                    select(Conversation).where(Conversation.phone_number_id == "444555666")
                )
            ).scalar_one()
            row.display_phone_number = None

        await db.write(forget)

        settings = get_settings()
        monkeypatch.setitem(settings.whatsapp_number_labels, "444555666", "+44 7700 900123")
        try:
            page = (
                await client.get("/admin/dashboard/customers", headers=ADMIN_AUTH)
            ).json()
            assert page["rows"][0]["numbers"] == ["+44 7700 900123"]
        finally:
            settings.whatsapp_number_labels.pop("444555666", None)


class TestHidingTheTeamsOwnNumbers:
    """We test the pipeline with our own phones. Those are not customers."""

    async def _seed_two(self, client, ai, meta):
        await post(client, text="a real prospect", wa_id="919876543210")
        await drain_queue()
        await post(client, text="us testing", wa_id="910000000001")
        await drain_queue()

    async def test_both_are_visible_when_nothing_is_configured(self, client, ai, meta):
        """No configured test numbers means nothing is hidden."""
        await self._seed_two(client, ai, meta)

        page = (await client.get("/admin/dashboard/customers", headers=ADMIN_AUTH)).json()
        assert page["total"] == 2

    async def test_a_configured_test_number_is_hidden_by_default(
        self, client, ai, meta, settings, monkeypatch
    ):
        await self._seed_two(client, ai, meta)
        monkeypatch.setattr(settings, "test_phone_numbers", ("910000000001",))

        page = (await client.get("/admin/dashboard/customers", headers=ADMIN_AUTH)).json()
        assert page["total"] == 1
        assert page["rows"][0]["phone"] == "919876543210"

    async def test_unticking_the_box_shows_them(self, client, ai, meta, settings, monkeypatch):
        await self._seed_two(client, ai, meta)
        monkeypatch.setattr(settings, "test_phone_numbers", ("910000000001",))

        page = (
            await client.get(
                "/admin/dashboard/customers?include_test=true", headers=ADMIN_AUTH
            )
        ).json()
        assert page["total"] == 2

    async def test_every_headline_number_agrees_with_the_filter(
        self, client, ai, meta, settings, monkeypatch
    ):
        """A count that forgot the filter would contradict the table."""
        await self._seed_two(client, ai, meta)
        monkeypatch.setattr(settings, "test_phone_numbers", ("910000000001",))

        real = (await client.get("/admin/dashboard/overview", headers=ADMIN_AUTH)).json()
        everything = (
            await client.get(
                "/admin/dashboard/overview?include_test=true", headers=ADMIN_AUTH
            )
        ).json()

        assert real["customers"] == 1
        assert everything["customers"] == 2
        assert real["conversations"] == 1
        assert everything["conversations"] == 2
        # Messages reach a customer only through a conversation, so this is the
        # aggregate most likely to be left unfiltered.
        assert real["messages"] < everything["messages"]
        assert real["messages"] > 0


    async def test_clicks_agree_with_the_filter(
        self, client, db, ai, meta, settings, monkeypatch
    ):
        await send_link(client, ai, wa_id="919876543210")
        await send_link(client, ai, wa_id="910000000001")
        await click(client, await link_for(db, "919876543210"))
        await click(client, await link_for(db, "910000000001"))
        monkeypatch.setattr(settings, "test_phone_numbers", ("910000000001",))

        real = (await client.get("/admin/dashboard/overview", headers=ADMIN_AUTH)).json()
        everything = (
            await client.get(
                "/admin/dashboard/overview?include_test=true", headers=ADMIN_AUTH
            )
        ).json()
        assert real["customers_clicked"] == 1
        assert everything["customers_clicked"] == 2

        page = (
            await client.get("/admin/dashboard/customers?outcome=clicked", headers=ADMIN_AUTH)
        ).json()
        assert page["total"] == 1


class TestUnansweredNumbers:
    """A message on a number we do not answer on is stored and left unanswered.

    That is the right behaviour - replying from another number would open a
    thread the customer never started - but it is invisible unless the dashboard
    says so, which is how one of these sat unnoticed in production.
    """

    async def test_the_healthy_state_is_an_empty_list(self, client, ai, meta):
        await post(client, text="hi")
        await drain_queue()

        body = (await client.get("/admin/dashboard/overview", headers=ADMIN_AUTH)).json()
        assert body["unanswerable_by_number"] == []

    async def test_a_thread_on_an_unconfigured_number_is_reported(
        self, client, db, ai, meta
    ):
        from app.models import Conversation

        await post(client, text="hello", phone_number_id="999888777")
        await drain_queue()

        body = (await client.get("/admin/dashboard/overview", headers=ADMIN_AUTH)).json()
        # Named by what Meta reported, not by the opaque id.
        assert body["unanswerable_by_number"] == [{"key": "1555888777", "count": 1}]

        # The message is kept, and we stayed quiet rather than answering from
        # a number the customer never wrote to.
        conversation = (
            await db.execute(
                select(Conversation).where(Conversation.phone_number_id == "999888777")
            )
        ).scalar_one()
        assert conversation.last_outbound_at is None

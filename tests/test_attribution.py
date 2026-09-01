"""Campaign attribution, including the click-to-WhatsApp ad lookup."""

from __future__ import annotations

import httpx
import respx
from sqlalchemy import func, select

from app.integrations.meta.client import MetaClient
from app.integrations.meta.schemas import AdDetails
from app.models import Ad, Campaign, Lead
from app.services import attribution
from tests.factories import ctwa_referral, signed, whatsapp_message_payload
from tests.helpers import drain_queue

GRAPH = "https://graph.facebook.com/v26.0"

AD_RESPONSE = {
    "id": "AD-777",
    "name": "CTWA - Record once",
    "adset_id": "ADSET-9",
    "adset": {"name": "India / Support"},
    "campaign_id": "CAMP-CTWA",
    "campaign": {"name": "Q1 Click-to-WhatsApp"},
}


async def post(client, **kwargs):
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


class TestAdLookup:
    @respx.mock
    async def test_fetch_ad_reads_the_campaign(self, settings):
        respx.get(f"{GRAPH}/AD-777").mock(return_value=httpx.Response(200, json=AD_RESPONSE))

        details = await MetaClient(settings).fetch_ad("AD-777")

        assert details.campaign_id == "CAMP-CTWA"
        assert details.campaign_name == "Q1 Click-to-WhatsApp"
        assert details.adset_name == "India / Support"


class FakeAdClient:
    def __init__(self, details=None, error=None):
        self.details = details
        self.error = error
        self.calls: list[str] = []

    async def fetch_ad(self, ad_id: str) -> AdDetails:
        self.calls.append(ad_id)
        if self.error is not None:
            raise self.error
        return self.details


class TestClickToWhatsAppAttribution:
    """A referral carries only an ad id, so the campaign has to be looked up."""

    async def test_the_campaign_is_resolved_and_backfilled(self, client, db, ai, meta):
        meta.fetch_ad = FakeAdClient(AdDetails.model_validate(AD_RESPONSE)).fetch_ad

        await post(client, referral=ctwa_referral(ad_id="AD-777"))
        await drain_queue()

        campaign = (await db.execute(select(Campaign))).scalar_one()
        assert campaign.meta_campaign_id == "CAMP-CTWA"
        assert campaign.name == "Q1 Click-to-WhatsApp"

        ad = (await db.execute(select(Ad))).scalar_one()
        assert ad.campaign_id == campaign.id
        assert ad.meta_adset_id == "ADSET-9"

        lead = (await db.execute(select(Lead))).scalar_one()
        assert lead.campaign_id == campaign.id

    async def test_the_lookup_happens_once_per_ad_not_once_per_lead(
        self, client, db, ai, meta
    ):
        fake = FakeAdClient(AdDetails.model_validate(AD_RESPONSE))
        meta.fetch_ad = fake.fetch_ad

        for wa_id in ("919000000001", "919000000002", "919000000003"):
            await post(client, wa_id=wa_id, referral=ctwa_referral(ad_id="AD-777"))
            await drain_queue()

        assert (await db.execute(select(func.count()).select_from(Lead))).scalar() == 3
        assert fake.calls == ["AD-777"]

    async def test_a_failed_lookup_never_breaks_the_conversation(self, client, db, ai, meta):
        """Attribution is valuable; a reply to a customer is more valuable."""
        meta.fetch_ad = FakeAdClient(error=httpx.ConnectError("graph unreachable")).fetch_ad

        await post(client, referral=ctwa_referral(ad_id="AD-777"))
        await drain_queue()

        assert len(meta.texts) == 1
        lead = (await db.execute(select(Lead))).scalar_one()
        assert lead.campaign_id is None
        assert lead.ad_id is not None

    async def test_no_lookup_without_a_referral(self, client, db, ai, meta):
        fake = FakeAdClient(AdDetails.model_validate(AD_RESPONSE))
        meta.fetch_ad = fake.fetch_ad

        await post(client, text="hello")
        await drain_queue()

        assert fake.calls == []


class TestEnrichmentService:
    async def test_an_unknown_ad_is_a_no_op(self, db, session_factory):
        async with session_factory() as session:
            result = await attribution.enrich_ad_from_meta(session, "MISSING", FakeAdClient())
        assert result is False

    async def test_an_already_attributed_ad_is_left_alone(self, db, session_factory):
        fake = FakeAdClient(AdDetails.model_validate(AD_RESPONSE))
        async with session_factory() as session:
            campaign = await attribution.get_or_create_campaign(session, "CAMP-EXISTING")
            await attribution.get_or_create_ad(session, "AD-777", campaign=campaign)
            await session.commit()

            result = await attribution.enrich_ad_from_meta(session, "AD-777", fake)

        assert result is False
        assert fake.calls == []

    async def test_an_ad_without_a_campaign_in_meta_is_handled(self, db, session_factory):
        details = AdDetails.model_validate({"id": "AD-777", "name": "Orphan ad"})
        async with session_factory() as session:
            await attribution.get_or_create_ad(session, "AD-777")
            await session.commit()

            result = await attribution.enrich_ad_from_meta(session, "AD-777", FakeAdClient(details))
            await session.commit()

        assert result is False
        ad = (await db.execute(select(Ad))).scalar_one()
        assert ad.name == "Orphan ad"

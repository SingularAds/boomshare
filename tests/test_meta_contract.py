"""Contract tests: our real MetaClient against Meta's real rules.

Every other test replaces the Meta client with a fake that says yes. These
tests do the opposite - they run the **real** `MetaClient` against
`scripts/fake_meta.py`, which enforces the same constraints the Cloud API does
and returns the same error codes.

That makes this the test that answers "will this still work once we connect a
real WhatsApp Business Account?". If our request shape is wrong, it fails here,
in CI, rather than on the first live message.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.errors import MetaPermanentError
from app.integrations.meta.client import MetaClient
from scripts import fake_meta

ALLOWED = "919876543210"
BLOCKED = "440000000000"


@pytest.fixture
def meta_server():
    """A fresh fake Meta, reachable over ASGI (no socket needed)."""
    fake_meta._reset_defaults()
    yield fake_meta
    fake_meta._reset_defaults()


@pytest.fixture
def client(settings, meta_server) -> MetaClient:
    """The real MetaClient, pointed at the fake Graph API."""
    transport = httpx.ASGITransport(app=fake_meta.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://graph.local")

    patched = settings.model_copy(update={"meta_graph_base_url": "http://graph.local"})
    return MetaClient(patched, client=http)


def window_open(wa_id: str = ALLOWED) -> None:
    """Simulate the customer having just messaged us."""
    import time

    fake_meta.LAST_INBOUND[wa_id] = time.time()


def window_closed(wa_id: str = ALLOWED, hours: float = 30) -> None:
    import time

    fake_meta.LAST_INBOUND[wa_id] = time.time() - hours * 3600


class TestOurRequestsAreAccepted:
    """The happy paths. If these break, live messaging breaks."""

    async def test_a_text_message_is_accepted(self, client):
        window_open()
        result = await client.send_text(ALLOWED, "Hello from Boomshare")

        assert result.provider_message_id.startswith("wamid.")
        sent = fake_meta.OUTBOX[-1]
        assert sent["body"]["messaging_product"] == "whatsapp"
        assert sent["body"]["recipient_type"] == "individual"
        assert sent["body"]["type"] == "text"
        assert sent["body"]["text"]["body"] == "Hello from Boomshare"

    async def test_the_lead_intro_template_is_accepted(self, client, settings):
        """The exact call `handle_leadgen` makes."""
        result = await client.send_template(
            ALLOWED,
            settings.whatsapp_lead_template_name,
            language_code=settings.whatsapp_lead_template_language,
            body_parameters=["Priya"],
        )
        assert result.provider_message_id is not None

        body = fake_meta.OUTBOX[-1]["body"]
        assert body["type"] == "template"
        assert body["template"]["name"] == "boomshare_lead_intro"
        assert body["template"]["language"] == {"code": "en"}
        assert body["template"]["components"][0]["parameters"] == [
            {"type": "text", "text": "Priya"}
        ]

    async def test_the_followup_template_is_accepted(self, client, settings):
        """The exact call `send_follow_up` makes outside the 24h window."""
        window_closed()
        result = await client.send_template(
            ALLOWED,
            settings.whatsapp_followup_template_name,
            language_code=settings.whatsapp_followup_template_language,
            body_parameters=["Priya"],
        )
        assert result.provider_message_id is not None

    async def test_a_read_receipt_is_accepted(self, client):
        await client.mark_read("wamid.INBOUND1")
        assert fake_meta.REQUEST_LOG[-1] == {
            **fake_meta.REQUEST_LOG[-1],
            "kind": "read",
            "ok": True,
        }

    async def test_lead_retrieval_is_accepted(self, client):
        details = await client.fetch_lead("LEAD-SANDBOX-1")
        assert details.campaign_id == "CAMP-Q1"
        assert details.fields()["phone_number"] == "+91 90000 11111"

    async def test_ad_lookup_is_accepted(self, client):
        details = await client.fetch_ad("AD-CTWA-42")
        assert details.campaign_id == "CAMP-CTWA"
        assert details.campaign_name == "Boomshare Q1 Click-to-WhatsApp"

    async def test_the_bearer_token_is_sent(self, client):
        window_open()
        await client.send_text(ALLOWED, "hi")
        # Reaching the outbox at all means auth passed - the fake refuses
        # anything without a well-formed Bearer header.
        assert fake_meta.OUTBOX


class TestMetasRulesAreEnforced:
    """The failures. Each maps to a real Meta error code."""

    async def test_free_form_outside_the_window_is_refused(self, client):
        """131047 - the most common live failure there is."""
        window_closed()
        with pytest.raises(MetaPermanentError) as exc:
            await client.send_text(ALLOWED, "still there?")
        assert "131047" in str(exc.value) or exc.value.status_code == 400
        assert exc.value.payload["error"]["code"] == 131047

    async def test_a_number_outside_the_allowed_list_is_refused(self, client):
        """131030 - what a Meta *test* number does for unverified recipients."""
        window_open(BLOCKED)
        with pytest.raises(MetaPermanentError) as exc:
            await client.send_text(BLOCKED, "hello")
        assert exc.value.payload["error"]["code"] == 131030

    async def test_an_unknown_template_is_refused(self, client):
        with pytest.raises(MetaPermanentError) as exc:
            await client.send_template(ALLOWED, "no_such_template", body_parameters=["x"])
        assert exc.value.payload["error"]["code"] == 132001

    async def test_an_unapproved_template_is_refused(self, client):
        """132015 - the template exists but is still PENDING review."""
        fake_meta.TEMPLATES["boomshare_followup"].status = "PENDING"
        with pytest.raises(MetaPermanentError) as exc:
            await client.send_template(ALLOWED, "boomshare_followup", body_parameters=["Priya"])
        assert exc.value.payload["error"]["code"] == 132015

    async def test_the_wrong_parameter_count_is_refused(self, client):
        """132000 - the template wants one parameter, we sent two."""
        with pytest.raises(MetaPermanentError) as exc:
            await client.send_template(
                ALLOWED, "boomshare_lead_intro", body_parameters=["Priya", "extra"]
            )
        assert exc.value.payload["error"]["code"] == 132000

    async def test_a_missing_parameter_is_refused(self, client):
        with pytest.raises(MetaPermanentError) as exc:
            await client.send_template(ALLOWED, "boomshare_lead_intro", body_parameters=None)
        assert exc.value.payload["error"]["code"] == 132000

    async def test_the_wrong_language_is_refused(self, client):
        with pytest.raises(MetaPermanentError) as exc:
            await client.send_template(
                ALLOWED, "boomshare_lead_intro", language_code="fr", body_parameters=["Priya"]
            )
        assert exc.value.payload["error"]["code"] == 132001

    async def test_an_oversized_body_is_refused(self, client):
        window_open()
        with pytest.raises(MetaPermanentError) as exc:
            await client.send_text(ALLOWED, "x" * 5000)
        assert exc.value.payload["error"]["code"] == 131009

    async def test_an_unknown_phone_number_id_is_refused(self, client, settings):
        window_open()
        with pytest.raises(MetaPermanentError):
            await client.send_text(ALLOWED, "hi", phone_number_id="999999999")

    async def test_an_unknown_lead_is_refused(self, client):
        with pytest.raises(MetaPermanentError):
            await client.fetch_lead("LEAD-DOES-NOT-EXIST")

    async def test_a_missing_token_is_refused(self, settings, meta_server):
        """190 - what an expired or unset META_ACCESS_TOKEN produces."""
        transport = httpx.ASGITransport(app=fake_meta.app)
        http = httpx.AsyncClient(transport=transport, base_url="http://graph.local")
        blank = settings.model_copy(
            update={"meta_graph_base_url": "http://graph.local", "meta_access_token": _blank()}
        )
        window_open()
        with pytest.raises(MetaPermanentError) as exc:
            await MetaClient(blank, client=http).send_text(ALLOWED, "hi")
        assert exc.value.payload["error"]["code"] == 190


def _blank():
    from pydantic import SecretStr

    return SecretStr("")


class TestOurGuardsMatchMetas:
    """Our own 24h check must agree with Meta's, or we send doomed requests."""

    async def test_we_refuse_before_meta_does(self, client, session_factory):
        """`messaging.send_text` should stop us calling Meta at all."""
        from datetime import timedelta

        from app.core.clock import utcnow
        from app.core.errors import MessagingPolicyError
        from app.models import Conversation, Customer
        from app.services import messaging

        window_closed()
        async with session_factory() as session:
            customer = Customer(phone=ALLOWED, wa_id=ALLOWED, full_name="Priya")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                customer_id=customer.id, last_inbound_at=utcnow() - timedelta(hours=30)
            )
            session.add(conversation)
            await session.flush()

            with pytest.raises(MessagingPolicyError):
                await messaging.send_text(session, conversation, customer, "hi", client=client)

        # We never reached the network, so Meta saw nothing.
        assert fake_meta.OUTBOX == []

    async def test_inside_the_window_both_agree(self, client, session_factory):
        from app.core.clock import utcnow
        from app.models import Conversation, Customer
        from app.services import messaging

        window_open()
        async with session_factory() as session:
            customer = Customer(phone=ALLOWED, wa_id=ALLOWED, full_name="Priya")
            session.add(customer)
            await session.flush()
            conversation = Conversation(customer_id=customer.id, last_inbound_at=utcnow())
            session.add(conversation)
            await session.flush()

            outcome = await messaging.send_text(
                session, conversation, customer, "hello", client=client
            )

        assert outcome.sent
        assert len(fake_meta.OUTBOX) == 1

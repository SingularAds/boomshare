"""Scoped reset has to remove a customer that actually has history.

The endpoint existed for a while in a state where it only worked on customers
with no conversations - the ORM tried to orphan the children instead of
deleting them, and a NOT NULL column rejected it. These tests pin the
behaviour the operator console depends on.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.ai.schemas import AiDecision
from app.models import Conversation, Customer, DownloadLink, Message
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue

ADMIN_AUTH = {"X-Admin-Token": "test-admin-token"}


async def _post(client, **kwargs):
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


async def _seed_customer_with_history(client, ai):
    ai.queue_decision(
        AiDecision(
            reply_text="Here you go.",
            intent="download_request",
            actions=["send_download_link"],
        )
    )
    await _post(client, text="send me the link")
    await drain_queue()


async def _count(db, model):
    return (await db.execute(select(func.count(model.id)))).scalar_one()


class TestScopedReset:
    async def test_removes_a_customer_with_conversations_and_messages(
        self, client, db, ai, meta
    ):
        await _seed_customer_with_history(client, ai)
        customer = (await db.execute(select(Customer))).scalar_one()
        assert await _count(db, Conversation) > 0
        assert await _count(db, Message) > 0
        assert await _count(db, DownloadLink) > 0

        response = await client.post(
            f"/admin/reset?phone={customer.phone}", headers=ADMIN_AUTH
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "reset"

        assert await _count(db, Customer) == 0

    async def test_children_go_with_the_parent(self, client, db, ai, meta):
        await _seed_customer_with_history(client, ai)
        customer = (await db.execute(select(Customer))).scalar_one()

        await client.post(f"/admin/reset?phone={customer.phone}", headers=ADMIN_AUTH)

        # Orphans are the failure this endpoint used to produce silently.
        assert await _count(db, Conversation) == 0
        assert await _count(db, Message) == 0
        assert await _count(db, DownloadLink) == 0

    async def test_a_leading_plus_is_accepted(self, client, db, ai, meta):
        await _seed_customer_with_history(client, ai)
        customer = (await db.execute(select(Customer))).scalar_one()

        response = await client.post(
            f"/admin/reset?phone=%2B{customer.phone}", headers=ADMIN_AUTH
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "reset"
        assert await _count(db, Customer) == 0

    async def test_unknown_phone_is_not_an_error(self, client, ai, meta):
        response = await client.post(
            "/admin/reset?phone=910000000000", headers=ADMIN_AUTH
        )
        assert response.status_code == 200
        assert response.json()["status"] == "not_found"

    async def test_reset_needs_the_admin_token(self, client, ai, meta):
        response = await client.post("/admin/reset?phone=910000000000")
        assert response.status_code == 401

"""Webhook authentication: the front door of the whole system."""

from __future__ import annotations

import json

import pytest

from app.integrations.meta.signature import (
    compute_signature,
    verify_signature,
    verify_subscription_token,
)
from tests.factories import signed, whatsapp_message_payload


class TestSubscriptionHandshake:
    async def test_valid_token_echoes_challenge(self, client):
        response = await client.get(
            "/webhooks/meta",
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "1158201444",
                "hub.verify_token": "test-verify-token",
            },
        )
        assert response.status_code == 200
        assert response.text == "1158201444"

    async def test_wrong_token_is_rejected(self, client):
        response = await client.get(
            "/webhooks/meta",
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "1158201444",
                "hub.verify_token": "not-the-token",
            },
        )
        assert response.status_code == 403

    async def test_missing_token_is_rejected(self, client):
        response = await client.get(
            "/webhooks/meta", params={"hub.mode": "subscribe", "hub.challenge": "x"}
        )
        assert response.status_code == 403

    async def test_wrong_mode_is_rejected(self, client):
        response = await client.get(
            "/webhooks/meta",
            params={
                "hub.mode": "unsubscribe",
                "hub.challenge": "x",
                "hub.verify_token": "test-verify-token",
            },
        )
        assert response.status_code == 403


class TestPayloadSignature:
    def test_signature_round_trips(self):
        body = b'{"hello":"world"}'
        assert verify_signature("secret", body, compute_signature("secret", body))

    def test_wrong_secret_fails(self):
        body = b'{"hello":"world"}'
        assert not verify_signature("other", body, compute_signature("secret", body))

    @pytest.mark.parametrize(
        "header", [None, "", "sha1=abc", "abcdef", "sha256=", "sha256=deadbeef"]
    )
    def test_malformed_headers_fail(self, header):
        assert not verify_signature("secret", b"{}", header)

    def test_empty_app_secret_never_passes(self):
        """An unconfigured secret must not silently accept everything."""
        body = b"{}"
        assert not verify_signature("", body, compute_signature("", body))

    def test_signature_covers_exact_bytes(self):
        """Re-serialising the JSON changes the signature - hence the raw body."""
        payload = {"b": 1, "a": 2}
        original = json.dumps(payload).encode()
        reordered = json.dumps(payload, sort_keys=True).encode()
        assert compute_signature("secret", original) != compute_signature("secret", reordered)

    def test_verify_subscription_token_requires_both_sides(self):
        assert verify_subscription_token("abc", "abc")
        assert not verify_subscription_token("abc", "abd")
        assert not verify_subscription_token("", "")
        assert not verify_subscription_token("abc", None)


class TestWebhookEndpointAuth:
    async def test_valid_signature_is_accepted(self, client, db):
        body, headers = signed(whatsapp_message_payload())
        response = await client.post("/webhooks/meta", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"received": 1, "accepted": 1}

    async def test_bad_signature_is_rejected_and_stores_nothing(self, client, db):
        from sqlalchemy import func, select

        from app.models import WebhookEvent

        body, _ = signed(whatsapp_message_payload())
        response = await client.post(
            "/webhooks/meta",
            content=body,
            headers={"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"},
        )
        assert response.status_code == 403

        count = (await db.execute(select(func.count()).select_from(WebhookEvent))).scalar()
        assert count == 0

    async def test_missing_signature_header_is_rejected(self, client):
        body, _ = signed(whatsapp_message_payload())
        response = await client.post(
            "/webhooks/meta", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 403

    async def test_signed_but_unparseable_body_is_accepted_and_dropped(self, client):
        """Retrying broken JSON would never help, so we take it off Meta's queue."""
        body = b"not json at all"
        response = await client.post(
            "/webhooks/meta",
            content=body,
            headers={
                "X-Hub-Signature-256": compute_signature("test-app-secret", body),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 0

    async def test_unknown_field_is_ignored(self, client):
        payload = {
            "object": "page",
            "entry": [{"id": "1", "changes": [{"field": "feed", "value": {"item": "comment"}}]}],
        }
        body, headers = signed(payload)
        response = await client.post("/webhooks/meta", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"received": 0, "accepted": 0}

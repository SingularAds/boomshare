"""Webhook parsing: turning Meta's shapes into our events."""

from __future__ import annotations

from app.domain import WebhookEventType
from app.integrations.meta.parser import extract_text, parse_webhook
from app.integrations.meta.schemas import InboundMessageEvent, LeadgenEvent, MessageStatusEvent
from tests.factories import (
    ctwa_referral,
    leadgen_payload,
    status_payload,
    whatsapp_message_payload,
)


class TestInboundMessages:
    def test_text_message(self):
        events = parse_webhook(whatsapp_message_payload(text="Hi there", wa_id="447700900123"))
        assert len(events) == 1

        event = events[0]
        assert isinstance(event, InboundMessageEvent)
        assert event.kind == WebhookEventType.WHATSAPP_MESSAGE
        assert event.text == "Hi there"
        assert event.wa_id == "447700900123"
        assert event.profile_name == "Priya"
        assert event.phone_number_id == "111222333"
        assert event.event_key == event.provider_message_id

    def test_referral_is_captured(self):
        payload = whatsapp_message_payload(referral=ctwa_referral(ad_id="AD-777"))
        event = parse_webhook(payload)[0]

        assert event.referral is not None
        assert event.referral.source_id == "AD-777"
        assert event.referral.ctwa_clid == "CLID-abc123"
        assert event.referral.is_ad

    def test_message_without_referral_has_none(self):
        assert parse_webhook(whatsapp_message_payload())[0].referral is None

    def test_media_message_has_no_text(self):
        event = parse_webhook(whatsapp_message_payload(message_type="image"))[0]
        assert event.message_type == "image"
        assert event.text is None

    def test_missing_profile_name_is_tolerated(self):
        event = parse_webhook(whatsapp_message_payload(profile_name=None))[0]
        assert event.profile_name is None

    def test_message_without_id_is_skipped(self):
        payload = whatsapp_message_payload()
        del payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
        assert parse_webhook(payload) == []


class TestTextExtraction:
    def test_button_reply(self):
        assert extract_text({"type": "button", "button": {"text": "Yes please"}}) == "Yes please"

    def test_interactive_button_reply(self):
        message = {
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "1", "title": "Send it"}},
        }
        assert extract_text(message) == "Send it"

    def test_interactive_list_reply(self):
        message = {
            "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": "2", "title": "Windows"}},
        }
        assert extract_text(message) == "Windows"

    def test_image_caption(self):
        assert extract_text({"type": "image", "image": {"caption": "look"}}) == "look"

    def test_unknown_type_returns_none(self):
        assert extract_text({"type": "location", "location": {"latitude": 1}}) is None

    def test_malformed_block_returns_none(self):
        assert extract_text({"type": "text", "text": "not-a-dict"}) is None


class TestDeliveryStatuses:
    def test_status_event(self):
        events = parse_webhook(status_payload("wamid.OUT1", "delivered"))
        assert len(events) == 1

        event = events[0]
        assert isinstance(event, MessageStatusEvent)
        assert event.status == "delivered"
        assert event.provider_message_id == "wamid.OUT1"

    def test_event_key_includes_status(self):
        """sent/delivered/read all share one message id - the key must not."""
        sent = parse_webhook(status_payload("wamid.OUT1", "sent"))[0]
        read = parse_webhook(status_payload("wamid.OUT1", "read"))[0]
        assert sent.event_key != read.event_key

    def test_failed_status_carries_errors(self):
        payload = status_payload("wamid.OUT1", "failed")
        payload["entry"][0]["changes"][0]["value"]["statuses"][0]["errors"] = [
            {"code": 131047, "title": "Re-engagement message"}
        ]
        event = parse_webhook(payload)[0]
        assert event.errors[0]["code"] == 131047


class TestLeadgen:
    def test_leadgen_event(self):
        events = parse_webhook(leadgen_payload("LEAD-42"))
        assert len(events) == 1

        event = events[0]
        assert isinstance(event, LeadgenEvent)
        assert event.leadgen_id == "LEAD-42"
        assert event.event_key == "leadgen:LEAD-42"
        assert event.form_id == "FORM-9"
        assert event.ad_id == "AD-200"
        assert event.page_id == "PAGE-1"

    def test_leadgen_without_id_is_skipped(self):
        payload = leadgen_payload()
        del payload["entry"][0]["changes"][0]["value"]["leadgen_id"]
        assert parse_webhook(payload) == []


class TestRobustness:
    def test_empty_payload(self):
        assert parse_webhook({}) == []

    def test_non_dict_payload(self):
        assert parse_webhook([]) == []  # type: ignore[arg-type]

    def test_entry_without_changes(self):
        assert parse_webhook({"entry": [{"id": "1"}]}) == []

    def test_change_with_non_dict_value(self):
        assert parse_webhook({"entry": [{"changes": [{"field": "messages", "value": None}]}]}) == []

    def test_batched_payload_yields_every_event(self):
        """Meta batches - one delivery can carry several messages and statuses."""
        payload = whatsapp_message_payload(text="one")
        value = payload["entry"][0]["changes"][0]["value"]
        value["messages"].append(
            {"from": "919000000000", "id": "wamid.SECOND", "type": "text", "text": {"body": "two"}}
        )
        value["statuses"] = [
            {"id": "wamid.OUT9", "status": "read", "timestamp": "1", "recipient_id": "91900"}
        ]

        events = parse_webhook(payload)
        assert len(events) == 3
        assert sum(isinstance(e, InboundMessageEvent) for e in events) == 2
        assert sum(isinstance(e, MessageStatusEvent) for e in events) == 1

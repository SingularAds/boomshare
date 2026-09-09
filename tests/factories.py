"""Builders for Meta webhook payloads and test data.

Shapes match what Meta actually posts, so the parser is exercised against
realistic input rather than a convenient simplification.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

from app.integrations.meta.schemas import LeadDetails
from app.integrations.meta.signature import compute_signature

_wamid = itertools.count(1)


def wamid() -> str:
    return f"wamid.HBgMTEST{next(_wamid):06d}"


#: The readable number Meta reports for each id the tests use.
_DISPLAY_NUMBERS = {
    "111222333": "15550001111",
    "444555666": "442079460002",
}


def _display_number(phone_number_id: str) -> str:
    """What Meta would report alongside this id.

    Unknown ids get a distinct number of their own rather than sharing one, so
    a test cannot pass by coincidence when two numbers should differ.
    """
    return _DISPLAY_NUMBERS.get(phone_number_id, f"1555{phone_number_id[-6:]}")


def whatsapp_message_payload(
    *,
    text: str = "I want to know more",
    wa_id: str = "919876543210",
    profile_name: str | None = "Priya",
    message_id: str | None = None,
    message_type: str = "text",
    referral: dict[str, Any] | None = None,
    phone_number_id: str = "111222333",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "from": wa_id,
        "id": message_id or wamid(),
        "timestamp": "1735689600",
        "type": message_type,
    }
    if message_type == "text":
        message["text"] = {"body": text}
    elif message_type == "image":
        message["image"] = {"id": "media-1", "mime_type": "image/jpeg"}
    if referral is not None:
        message["referral"] = referral

    contacts = []
    if profile_name:
        contacts.append({"profile": {"name": profile_name}, "wa_id": wa_id})

    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                # Meta sends the readable number beside the id.
                                # Derived from the id so two of our numbers are
                                # distinguishable, the way they are in reality.
                                "display_phone_number": _display_number(phone_number_id),
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": contacts,
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def ctwa_referral(
    ad_id: str = "AD-100", headline: str = "Record once, share everywhere"
) -> dict[str, Any]:
    return {
        "source_url": "https://fb.me/boomshare",
        "source_id": ad_id,
        "source_type": "ad",
        "headline": headline,
        "body": "Try Boomshare free",
        "media_type": "image",
        "ctwa_clid": "CLID-abc123",
    }


def status_payload(
    provider_message_id: str, status: str = "delivered", recipient: str = "919876543210"
) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "111222333"},
                            "statuses": [
                                {
                                    "id": provider_message_id,
                                    "status": status,
                                    "timestamp": "1735689700",
                                    "recipient_id": recipient,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def leadgen_payload(
    leadgen_id: str = "LEAD-500",
    *,
    form_id: str = "FORM-9",
    ad_id: str = "AD-200",
    page_id: str = "PAGE-1",
) -> dict[str, Any]:
    return {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": 1735689600,
                "changes": [
                    {
                        "field": "leadgen",
                        "value": {
                            "created_time": 1735689600,
                            "leadgen_id": leadgen_id,
                            "page_id": page_id,
                            "form_id": form_id,
                            "adgroup_id": "ADSET-3",
                            "ad_id": ad_id,
                        },
                    }
                ],
            }
        ],
    }


def lead_details(
    leadgen_id: str = "LEAD-500",
    *,
    phone: str = "+91 98765 43210",
    name: str = "Priya Sharma",
    email: str = "priya@example.com",
    campaign_id: str = "CAMP-7",
    ad_id: str = "AD-200",
) -> LeadDetails:
    return LeadDetails(
        id=leadgen_id,
        created_time="2025-01-01T00:00:00+0000",
        ad_id=ad_id,
        ad_name="Boomshare Lead Ad v2",
        adset_id="ADSET-3",
        adset_name="India / Managers",
        campaign_id=campaign_id,
        campaign_name="Boomshare Q1 Leads",
        form_id="FORM-9",
        platform="fb",
        field_data=[
            {"name": "full_name", "values": [name]},
            {"name": "phone_number", "values": [phone]},
            {"name": "email", "values": [email]},
            {"name": "company_size", "values": ["11-50"]},
        ],
    )


def signed(payload: dict[str, Any], secret: str = "test-app-secret") -> tuple[bytes, dict[str, str]]:
    """Serialise a payload and produce the header Meta would send with it."""
    body = json.dumps(payload).encode("utf-8")
    return body, {
        "X-Hub-Signature-256": compute_signature(secret, body),
        "Content-Type": "application/json",
    }

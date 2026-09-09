"""Normalised representations of the Meta webhook payloads we care about.

The rest of the application only ever sees these objects, never Meta's raw
JSON shape. When Meta changes its payload format, only `parser.py` changes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain import WebhookEventType


class Referral(BaseModel):
    """Click-to-WhatsApp attribution attached to the customer's first message."""

    model_config = ConfigDict(extra="ignore")

    source_id: str | None = None  # the Meta ad id
    source_type: str | None = None  # "ad" | "post"
    source_url: str | None = None
    headline: str | None = None
    body: str | None = None
    media_type: str | None = None
    ctwa_clid: str | None = None

    @property
    def is_ad(self) -> bool:
        return bool(self.source_id) and (self.source_type or "ad") == "ad"


class InboundMessageEvent(BaseModel):
    """A message a customer sent us on WhatsApp."""

    kind: Literal[WebhookEventType.WHATSAPP_MESSAGE] = WebhookEventType.WHATSAPP_MESSAGE

    event_key: str
    provider_message_id: str
    wa_id: str
    phone_number_id: str | None = None
    #: The same number as a person would dial it. Meta sends it beside the
    #: id on every webhook, and it is the only place we ever learn it -
    #: the Graph API is not consulted just to name a number we own.
    display_phone_number: str | None = None
    profile_name: str | None = None
    message_type: str = "text"
    text: str | None = None
    timestamp: int | None = None
    referral: Referral | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class MessageStatusEvent(BaseModel):
    """A delivery receipt for a message we sent."""

    kind: Literal[WebhookEventType.WHATSAPP_STATUS] = WebhookEventType.WHATSAPP_STATUS

    event_key: str
    provider_message_id: str
    status: str
    recipient_id: str | None = None
    timestamp: int | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class LeadgenEvent(BaseModel):
    """A lead-ad form submission notification."""

    kind: Literal[WebhookEventType.LEADGEN] = WebhookEventType.LEADGEN

    event_key: str
    leadgen_id: str
    form_id: str | None = None
    ad_id: str | None = None
    adgroup_id: str | None = None
    page_id: str | None = None
    created_time: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


ParsedEvent = InboundMessageEvent | MessageStatusEvent | LeadgenEvent


class LeadDetails(BaseModel):
    """A lead as returned by the Graph API `/{leadgen_id}` endpoint."""

    model_config = ConfigDict(extra="ignore")

    id: str
    created_time: str | None = None
    ad_id: str | None = None
    ad_name: str | None = None
    adset_id: str | None = None
    adset_name: str | None = None
    campaign_id: str | None = None
    campaign_name: str | None = None
    form_id: str | None = None
    platform: str | None = None
    field_data: list[dict[str, Any]] = Field(default_factory=list)

    def fields(self) -> dict[str, str]:
        """Flatten Meta's `[{name, values: [...]}]` shape into a plain dict."""
        out: dict[str, str] = {}
        for item in self.field_data:
            name = item.get("name")
            values = item.get("values") or []
            if name and values:
                out[str(name)] = str(values[0])
        return out


class AdDetails(BaseModel):
    """An ad as returned by the Graph API `/{ad_id}` endpoint.

    Click-to-WhatsApp referrals only carry an ad id, so the campaign has to be
    looked up to answer "which campaign produced this customer?".
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str | None = None
    adset_id: str | None = None
    campaign_id: str | None = None
    campaign: dict[str, Any] = Field(default_factory=dict)
    adset: dict[str, Any] = Field(default_factory=dict)

    @property
    def campaign_name(self) -> str | None:
        return self.campaign.get("name")

    @property
    def adset_name(self) -> str | None:
        return self.adset.get("name")


class SendResult(BaseModel):
    """Outcome of an outbound WhatsApp send."""

    provider_message_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

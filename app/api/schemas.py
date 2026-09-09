"""Request/response models for the HTTP API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain import HandlingMode, SalesStage


# --------------------------------------------------------------------------- #
# Internal events (from the Boomshare desktop backend)
# --------------------------------------------------------------------------- #
class InstallEvent(BaseModel):
    """Reported by Boomshare itself once an install or activation is real.

    Either identifier works: `token` is the value from the tracked download URL
    (and gives exact attribution), `phone` is the fallback when the install came
    through some other route.
    """

    token: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    platform: str | None = Field(default=None, max_length=32)
    app_version: str | None = Field(default=None, max_length=32)
    details: dict[str, Any] | None = None

    def has_identifier(self) -> bool:
        return bool(self.token or self.phone)


class EventAck(BaseModel):
    ok: bool = True
    applied: bool
    customer_id: uuid.UUID | None = None
    detail: str | None = None


# --------------------------------------------------------------------------- #
# Admin / agent console
# --------------------------------------------------------------------------- #
class HandoffRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    agent: str | None = Field(default=None, max_length=255)


class ReleaseRequest(BaseModel):
    stage: SalesStage | None = None


class AgentMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    agent: str | None = Field(default=None, max_length=255)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: str
    message_type: str
    status: str
    content: str | None
    ai_generated: bool
    sent_by: str | None
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    status: str
    handling_mode: HandlingMode
    sales_stage: SalesStage
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None
    handoff_reason: str | None
    assigned_agent: str | None
    created_at: datetime


class ConversationDetail(ConversationOut):
    customer_phone: str | None = None
    customer_name: str | None = None
    downloaded_at: datetime | None = None
    activated_at: datetime | None = None
    messages: list[MessageOut] = Field(default_factory=list)


class FunnelRow(BaseModel):
    campaign_id: str | None
    campaign_name: str | None
    leads: int
    conversations: int
    links_sent: int
    downloads: int
    activations: int


class AgentEffectivenessOut(BaseModel):
    """How well the AI is selling, as opposed to how well it is chatting.

    `passivity_rate` is the headline: the share of AI turns that requested no
    business action at all. A conversational agent scores near 1.0; a selling
    one does not.
    """

    conversations: int
    reached_link_sent: int
    links_clicked: int
    downloads: int
    ai_turns: int
    turns_without_action: int
    passivity_rate: float
    adjusted_decisions: int
    median_turns_to_link: float | None


# --------------------------------------------------------------------------- #
# Dashboard
#
# Every field here is read from a column that exists. Nothing is estimated,
# inferred or filled in with a plausible-looking default: a number the operator
# cannot trace back to a row is worse than no number at all.
# --------------------------------------------------------------------------- #
class CountByKey(BaseModel):
    key: str
    count: int


class DashboardOverview(BaseModel):
    """The counts behind the headline cards, all from one round of aggregates."""

    customers: int
    customers_downloaded: int
    customers_activated: int
    customers_opted_out: int

    #: Customers reached by each acquisition route. `direct` is the ones with no
    #: lead row at all - they messaged a number without an ad behind it.
    customers_from_ads: int
    customers_direct: int
    leads_by_source: list[CountByKey]

    links_sent: int
    links_clicked: int
    customers_with_link: int

    conversations: int
    conversations_open: int

    messages: int
    messages_inbound: int
    messages_outbound: int
    messages_ai_generated: int

    campaigns: int
    #: Threads that arrived on a number this deployment is not configured to
    #: answer on, keyed by Meta's phone number id. Each one is a customer who
    #: messaged us and got silence: the message is stored, but replying from a
    #: different number would open a thread they never started. Empty is the
    #: healthy state.
    unanswerable_by_number: list[CountByKey]
    generated_at: datetime


class CustomerRow(BaseModel):
    """One line in the customer table."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phone: str
    full_name: str | None
    created_at: datetime

    downloaded_at: datetime | None
    activated_at: datetime | None
    opted_out_at: datetime | None

    #: `lead_ad`, `click_to_whatsapp`, `manual`, or `direct` when no lead exists.
    source: str
    campaign_name: str | None

    #: Which of our WhatsApp numbers they have written to, first contact
    #: first. A person who wrote to both appears with both - that is a fact
    #: about them, not a thread count.
    numbers: list[str]
    #: Furthest stage reached across this customer's conversations.
    stage: SalesStage | None
    last_activity_at: datetime | None
    messages: int


class CustomerPage(BaseModel):
    rows: list[CustomerRow]
    total: int
    limit: int
    offset: int


class DashboardMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: str
    message_type: str
    status: str
    content: str | None
    ai_generated: bool
    sent_by: str | None
    created_at: datetime
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None


class DashboardConversation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    handling_mode: HandlingMode
    sales_stage: SalesStage
    phone_number_id: str
    #: `phone_number_id` as a person would read it.
    number_label: str
    created_at: datetime
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None
    messages: list[DashboardMessage]
    #: True when older messages were trimmed off the top of `messages`.
    truncated: bool


class DashboardLink(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    token: str
    url: str
    platform: str | None
    sent_at: datetime | None
    clicked_at: datetime | None
    downloaded_at: datetime | None
    activated_at: datetime | None
    details: dict[str, Any] | None


class CustomerDetail(BaseModel):
    """Everything the modal shows, in one request."""

    id: uuid.UUID
    phone: str
    full_name: str | None
    email: str | None
    locale: str | None
    created_at: datetime
    downloaded_at: datetime | None
    activated_at: datetime | None
    opted_out_at: datetime | None
    source: str
    campaign_name: str | None
    #: Our numbers this person has written to, first contact first.
    numbers: list[str]
    conversations: list[DashboardConversation]
    links: list[DashboardLink]

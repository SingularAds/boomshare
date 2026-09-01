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

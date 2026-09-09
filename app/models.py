"""SQLAlchemy models.

Schema shape follows the reporting question we know is coming:

    campaign -> ad -> lead -> customer -> conversation -> message
                                      -> download_link -> activation

Durable lifecycle facts (downloaded / activated / opted out) live on the
customer. Sales *progression* lives on the conversation, because that is where
it happens and where it can legitimately restart.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utcnow
from app.core.db import Base, JsonB
from app.domain import (
    ConversationStatus,
    CustomerIntent,
    HandlingMode,
    LeadSource,
    LeadStatus,
    MessageDirection,
    MessageStatus,
    MessageType,
    ReminderKind,
    ReminderStatus,
    SalesStage,
    WebhookEventType,
    WebhookStatus,
)


def _enum(enum_cls: type, name: str) -> Enum:
    """Store enums as VARCHAR + CHECK constraint.

    Native PostgreSQL enum types make migrations painful for a vocabulary we
    expect to extend often (new sales stages, new intents), and they do not
    exist on SQLite, which the test suite uses.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        values_callable=lambda e: [m.value for m in e],
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    # Timestamps are generated in Python, not by the database. `now()` in
    # PostgreSQL returns the *transaction* start time, so several rows written
    # in one transaction would share a timestamp and message ordering would
    # become ambiguous. A microsecond-resolution client clock keeps the
    # conversation in the order it actually happened. The server default is
    # kept as a backstop for rows inserted outside the ORM.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


# Relationships are declared `lazy="raise_on_sql"`: in an async session a
# lazy load raises deep inside the greenlet machinery, far from the line that
# caused it. Forcing an explicit load (session.get / selectinload) keeps every
# query visible at the call site.


# --------------------------------------------------------------------------- #
# Advertising attribution
# --------------------------------------------------------------------------- #
class Campaign(Base, TimestampMixin):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = _uuid_pk()
    meta_campaign_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    objective: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict | None] = mapped_column(JsonB)

    ads: Mapped[list[Ad]] = relationship(back_populates="campaign", lazy="raise_on_sql")


class Ad(Base, TimestampMixin):
    __tablename__ = "ads"

    id: Mapped[uuid.UUID] = _uuid_pk()
    meta_ad_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    meta_adset_id: Mapped[str | None] = mapped_column(String(64), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    name: Mapped[str | None] = mapped_column(String(255))
    # Lead-ad form this creative points at, when applicable.
    meta_form_id: Mapped[str | None] = mapped_column(String(64), index=True)
    details: Mapped[dict | None] = mapped_column(JsonB)

    campaign: Mapped[Campaign | None] = relationship(back_populates="ads", lazy="raise_on_sql")


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #
class Customer(Base, TimestampMixin):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # E.164 digits without the leading '+' - this is what WhatsApp calls wa_id.
    phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    wa_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    locale: Mapped[str | None] = mapped_column(String(16))
    timezone: Mapped[str | None] = mapped_column(String(64))

    # Durable lifecycle facts. Only ever set from verified backend signals.
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attributes: Mapped[dict | None] = mapped_column(JsonB)

    leads: Mapped[list[Lead]] = relationship(back_populates="customer", lazy="raise_on_sql")
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="customer", lazy="raise_on_sql"
    )

    @property
    def is_opted_out(self) -> bool:
        return self.opted_out_at is not None


class Lead(Base, TimestampMixin):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_campaign_created", "campaign_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[LeadSource] = mapped_column(_enum(LeadSource, "lead_source"), nullable=False)
    status: Mapped[LeadStatus] = mapped_column(
        _enum(LeadStatus, "lead_status"), nullable=False, default=LeadStatus.NEW
    )

    # Meta identifiers. `meta_leadgen_id` is our idempotency key for lead ads.
    meta_leadgen_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    meta_form_id: Mapped[str | None] = mapped_column(String(64), index=True)
    meta_page_id: Mapped[str | None] = mapped_column(String(64))
    # Click-to-WhatsApp click id, present on WhatsApp referral payloads.
    ctwa_clid: Mapped[str | None] = mapped_column(String(128), index=True)

    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    ad_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ads.id", ondelete="SET NULL"), index=True
    )

    # Whatever Meta actually sent us, kept verbatim for later analysis.
    raw_payload: Mapped[dict | None] = mapped_column(JsonB)
    field_data: Mapped[dict | None] = mapped_column(JsonB)

    customer: Mapped[Customer] = relationship(back_populates="leads", lazy="raise_on_sql")
    campaign: Mapped[Campaign | None] = relationship(lazy="raise_on_sql")
    ad: Mapped[Ad | None] = relationship(lazy="raise_on_sql")


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #
class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        # At most one open conversation per customer, per channel, per business
        # number. The number belongs in the key because two of our numbers are
        # two different threads on the customer's phone, each with its own 24h
        # service window - collapsing them would answer one thread on the other.
        Index(
            "uq_conversations_open_per_customer",
            "customer_id",
            "channel",
            "phone_number_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
        Index("ix_conversations_stage_status", "sales_stage", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))

    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="whatsapp")
    # Which of our WhatsApp numbers this thread is on. Deliberately not
    # nullable and with no default: a follow-up sent days later has only this
    # row to route by, so a conversation that does not know its own number
    # would be answered from whichever number the config happened to list
    # first. Better to fail at the one place conversations are created.
    phone_number_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # The same number as a person would dial it, learned from the webhook that
    # opened this thread. Meta's id identifies a number; it does not name one,
    # and an operator reading a transcript needs the name. Nullable because a
    # thread opened before this column existed never saw that webhook, and
    # because Meta is not obliged to send it.
    display_phone_number: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[ConversationStatus] = mapped_column(
        _enum(ConversationStatus, "conversation_status"),
        nullable=False,
        default=ConversationStatus.OPEN,
    )
    handling_mode: Mapped[HandlingMode] = mapped_column(
        _enum(HandlingMode, "handling_mode"), nullable=False, default=HandlingMode.AI
    )
    sales_stage: Mapped[SalesStage] = mapped_column(
        _enum(SalesStage, "sales_stage"), nullable=False, default=SalesStage.NEW
    )
    stage_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_intent: Mapped[CustomerIntent | None] = mapped_column(
        _enum(CustomerIntent, "customer_intent")
    )

    handoff_reason: Mapped[str | None] = mapped_column(Text)
    handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_agent: Mapped[str | None] = mapped_column(String(255))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Running notes the AI keeps about the customer (needs, team size, tools).
    context_notes: Mapped[dict | None] = mapped_column(JsonB)

    customer: Mapped[Customer] = relationship(
        back_populates="conversations", lazy="raise_on_sql"
    )
    lead: Mapped[Lead | None] = relationship(lazy="raise_on_sql")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", order_by="Message.created_at", lazy="raise_on_sql"
    )

    @property
    def is_ai_handled(self) -> bool:
        return self.handling_mode == HandlingMode.AI and self.status == ConversationStatus.OPEN


class Message(Base, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        # Meta retries webhooks; the wamid is our durable dedupe key.
        UniqueConstraint("provider_message_id", name="uq_messages_provider_message_id"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[MessageDirection] = mapped_column(
        _enum(MessageDirection, "message_direction"), nullable=False
    )
    message_type: Mapped[MessageType] = mapped_column(
        _enum(MessageType, "message_type"), nullable=False, default=MessageType.TEXT
    )
    status: Mapped[MessageStatus] = mapped_column(
        _enum(MessageStatus, "message_status"), nullable=False, default=MessageStatus.RECEIVED
    )

    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    content: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JsonB)
    error: Mapped[dict | None] = mapped_column(JsonB)

    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_by: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[Conversation] = relationship(
        back_populates="messages", lazy="raise_on_sql"
    )


class AiDecisionLog(Base, TimestampMixin):
    """Every model call we act on, kept so we can tune the sales behaviour."""

    __tablename__ = "ai_decisions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )

    model: Mapped[str | None] = mapped_column(String(64))
    intent: Mapped[CustomerIntent | None] = mapped_column(_enum(CustomerIntent, "customer_intent"))
    suggested_stage: Mapped[SalesStage | None] = mapped_column(_enum(SalesStage, "sales_stage"))
    applied_stage: Mapped[SalesStage | None] = mapped_column(_enum(SalesStage, "sales_stage"))
    requested_actions: Mapped[dict | None] = mapped_column(JsonB)
    executed_actions: Mapped[dict | None] = mapped_column(JsonB)
    rejected_reasons: Mapped[dict | None] = mapped_column(JsonB)
    confidence: Mapped[float | None] = mapped_column(Float)
    raw_response: Mapped[dict | None] = mapped_column(JsonB)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


# --------------------------------------------------------------------------- #
# Follow-ups
# --------------------------------------------------------------------------- #
class Reminder(Base, TimestampMixin):
    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_due", "status", "due_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ReminderKind] = mapped_column(
        _enum(ReminderKind, "reminder_kind"), nullable=False, default=ReminderKind.FOLLOW_UP
    )
    status: Mapped[ReminderStatus] = mapped_column(
        _enum(ReminderStatus, "reminder_status"), nullable=False, default=ReminderStatus.PENDING
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict | None] = mapped_column(JsonB)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why a reminder ended the way it did ("customer replied", "already downloaded").
    resolution: Mapped[str | None] = mapped_column(String(255))


# --------------------------------------------------------------------------- #
# Download / activation
# --------------------------------------------------------------------------- #
class DownloadLink(Base, TimestampMixin):
    """A tokenised download URL, so an install can be attributed to a customer.

    `sent_at` records that *we sent a link*. `downloaded_at` / `activated_at`
    are only written when the Boomshare desktop backend confirms the event.
    """

    __tablename__ = "download_links"

    id: Mapped[uuid.UUID] = _uuid_pk()
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    platform: Mapped[str | None] = mapped_column(String(32))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict | None] = mapped_column(JsonB)


# --------------------------------------------------------------------------- #
# Webhook inbox
# --------------------------------------------------------------------------- #
class WebhookEvent(Base):
    """Durable inbox + idempotency ledger for everything Meta sends us.

    The HTTP handler only verifies, parses and inserts. A worker does the real
    work, so a Meta retry can never produce a second reply or a second lead.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_key", name="uq_webhook_events_provider_event_key"),
        Index("ix_webhook_events_status_received", "status", "received_at"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="meta")
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[WebhookEventType] = mapped_column(
        _enum(WebhookEventType, "webhook_event_type"), nullable=False
    )
    status: Mapped[WebhookStatus] = mapped_column(
        _enum(WebhookStatus, "webhook_status"), nullable=False, default=WebhookStatus.PENDING
    )
    payload: Mapped[dict] = mapped_column(JsonB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

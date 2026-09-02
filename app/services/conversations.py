"""Conversation lifecycle, message persistence and sales-state transitions.

The sales stage is owned here, not by the model. `set_stage` is the only way it
changes, and it refuses illegal transitions - so a confused AI response cannot
corrupt the funnel data that reporting depends on.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import as_utc, utcnow
from app.core.config import get_settings
from app.core.errors import InvalidStateTransition
from app.core.logging import get_logger
from app.domain import (
    SYSTEM_ONLY_STAGES,
    ConversationStatus,
    CustomerIntent,
    HandlingMode,
    MessageDirection,
    MessageStatus,
    MessageType,
    SalesStage,
    can_transition,
)
from app.models import Conversation, Customer, Message
from app.services.base import insert_or_get, merge_json

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Lookup / creation
# --------------------------------------------------------------------------- #
async def get_open_conversation(
    session: AsyncSession,
    customer_id: uuid.UUID,
    phone_number_id: str,
    channel: str = "whatsapp",
) -> Conversation | None:
    stmt = (
        select(Conversation)
        .where(
            Conversation.customer_id == customer_id,
            Conversation.channel == channel,
            Conversation.phone_number_id == phone_number_id,
            Conversation.status == ConversationStatus.OPEN,
        )
        .order_by(desc(Conversation.created_at))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def open_conversations(
    session: AsyncSession, customer_id: uuid.UUID, channel: str = "whatsapp"
) -> list[Conversation]:
    """Every open thread this customer has with us, newest first.

    There is one per business number they have written to. Facts about the
    *person* rather than the thread - they installed, they activated - apply to
    all of them, so those callers ask for the list rather than picking one.
    """
    stmt = (
        select(Conversation)
        .where(
            Conversation.customer_id == customer_id,
            Conversation.channel == channel,
            Conversation.status == ConversationStatus.OPEN,
        )
        .order_by(desc(Conversation.created_at))
    )
    return list((await session.execute(stmt)).scalars())


async def get_or_create_open_conversation(
    session: AsyncSession,
    customer: Customer,
    *,
    phone_number_id: str,
    channel: str = "whatsapp",
    lead_id: uuid.UUID | None = None,
) -> tuple[Conversation, bool]:
    """One open conversation per customer, per channel, per business number.

    A partial unique index enforces this in PostgreSQL, so a concurrent retry
    loses the insert race and re-reads the winner's row.

    The number is part of the key, not incidental data on the row. Someone who
    writes to our Brazil number and later to our US one is holding two separate
    WhatsApp threads, each with its own 24-hour service window; sharing one
    conversation between them would answer whichever thread the row happened to
    remember and leave the other silent.
    """
    existing = await get_open_conversation(session, customer.id, phone_number_id, channel)
    if existing is not None:
        if lead_id and existing.lead_id is None:
            existing.lead_id = lead_id
        return existing, False

    conversation, created = await insert_or_get(
        session,
        Conversation,
        defaults={
            "lead_id": lead_id,
            "sales_stage": SalesStage.NEW,
            "stage_updated_at": utcnow(),
            "handling_mode": HandlingMode.AI,
        },
        customer_id=customer.id,
        channel=channel,
        phone_number_id=phone_number_id,
        status=ConversationStatus.OPEN,
    )
    if created:
        logger.info(
            "conversation opened",
            extra={
                "conversation_id": str(conversation.id),
                "customer_id": str(customer.id),
                "phone_number_id": phone_number_id,
            },
        )
    return conversation, created


async def load_history(
    session: AsyncSession, conversation_id: uuid.UUID, limit: int | None = None
) -> list[Message]:
    """Most recent `limit` messages, returned oldest-first for the prompt."""
    limit = limit or get_settings().history_message_limit
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(desc(Message.created_at))
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars())
    return list(reversed(rows))


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
async def record_inbound_message(
    session: AsyncSession,
    conversation: Conversation,
    *,
    provider_message_id: str,
    content: str | None,
    message_type: str = "text",
    payload: dict | None = None,
) -> tuple[Message, bool]:
    """Persist a customer message. The provider id makes this idempotent."""
    message, created = await insert_or_get(
        session,
        Message,
        defaults={
            "conversation_id": conversation.id,
            "direction": MessageDirection.INBOUND,
            "message_type": _coerce_message_type(message_type),
            "status": MessageStatus.RECEIVED,
            "content": content,
            "payload": payload,
        },
        provider_message_id=provider_message_id,
    )

    if created:
        conversation.last_inbound_at = utcnow()
        await session.flush()

    return message, created


async def record_outbound_message(
    session: AsyncSession,
    conversation: Conversation,
    *,
    content: str,
    message_type: MessageType = MessageType.TEXT,
    provider_message_id: str | None = None,
    status: MessageStatus = MessageStatus.SENT,
    ai_generated: bool = False,
    sent_by: str | None = None,
    payload: dict | None = None,
    error: dict | None = None,
) -> Message:
    now = utcnow()
    message = Message(
        conversation_id=conversation.id,
        direction=MessageDirection.OUTBOUND,
        message_type=message_type,
        status=status,
        provider_message_id=provider_message_id,
        content=content,
        payload=payload,
        error=error,
        ai_generated=ai_generated,
        sent_by=sent_by,
        sent_at=now if status != MessageStatus.FAILED else None,
    )
    session.add(message)

    if status != MessageStatus.FAILED:
        conversation.last_outbound_at = now

    await session.flush()
    return message


async def apply_delivery_status(
    session: AsyncSession,
    provider_message_id: str,
    status: str,
    errors: list[dict] | None = None,
) -> Message | None:
    """Update a sent message from a WhatsApp delivery receipt."""
    message = (
        await session.execute(
            select(Message).where(Message.provider_message_id == provider_message_id)
        )
    ).scalar_one_or_none()
    if message is None:
        return None

    now = utcnow()
    match status:
        case "sent":
            message.status = MessageStatus.SENT
        case "delivered":
            message.status = MessageStatus.DELIVERED
            message.delivered_at = message.delivered_at or now
        case "read":
            message.status = MessageStatus.READ
            message.read_at = message.read_at or now
        case "failed":
            message.status = MessageStatus.FAILED
            message.error = {"errors": errors or []}
        case _:
            return message

    await session.flush()
    return message


def _coerce_message_type(value: str) -> MessageType:
    try:
        return MessageType(value)
    except ValueError:
        return MessageType.UNSUPPORTED


# --------------------------------------------------------------------------- #
# Sales state
# --------------------------------------------------------------------------- #
async def set_stage(
    session: AsyncSession,
    conversation: Conversation,
    target: SalesStage,
    *,
    system: bool = False,
    reason: str | None = None,
) -> bool:
    """Move the conversation to `target`.

    `system=True` marks a transition backed by verified data (a confirmed
    install, an operator action) and is the only way to reach a system-only
    stage. Everything else - including anything the AI suggested - must be a
    legal funnel transition.
    """
    current = conversation.sales_stage
    if current == target:
        return False

    if target in SYSTEM_ONLY_STAGES and not system:
        raise InvalidStateTransition(current.value, target.value)

    if not can_transition(current, target):
        raise InvalidStateTransition(current.value, target.value)

    conversation.sales_stage = target
    conversation.stage_updated_at = utcnow()
    await session.flush()

    logger.info(
        "sales stage changed",
        extra={
            "conversation_id": str(conversation.id),
            "from": current.value,
            "to": target.value,
            "system": system,
            "reason": reason,
        },
    )
    return True


#: Stages a customer's own message moves back to `engaged`. The first two are
#: someone replying for the first time. `not_interested` is someone coming back
#: after we wrote them off - which the AI does readily, and sometimes wrongly,
#: from a "not right now".
_REVIVED_BY_INBOUND: frozenset[SalesStage] = frozenset(
    {SalesStage.NEW, SalesStage.CONTACTED, SalesStage.NOT_INTERESTED}
)


async def advance_on_inbound(session: AsyncSession, conversation: Conversation) -> None:
    """Move the conversation to `engaged` when the customer's message warrants it.

    Deliberately conservative: everything past `engaged` is the AI's call, based
    on what the customer actually said.

    Reviving `not_interested` is the exception worth spelling out. That stage
    stops every follow-up, and the AI reaches for it whenever someone defers
    ("not right now"). A customer who then writes to us has re-engaged - that is
    a verified event, not a judgement call - so the backend reopens the funnel
    itself rather than waiting for the model to suggest it.
    """
    if conversation.sales_stage in _REVIVED_BY_INBOUND:
        await set_stage(session, conversation, SalesStage.ENGAGED, reason="customer replied")


async def record_intent(
    session: AsyncSession, conversation: Conversation, intent: CustomerIntent | None
) -> None:
    if intent is not None:
        conversation.last_intent = intent
        await session.flush()


async def merge_context_notes(
    session: AsyncSession, conversation: Conversation, notes: dict[str, str]
) -> None:
    if notes:
        conversation.context_notes = merge_json(conversation.context_notes, notes)
        await session.flush()


# --------------------------------------------------------------------------- #
# Human handoff
# --------------------------------------------------------------------------- #
async def hand_off_to_human(
    session: AsyncSession,
    conversation: Conversation,
    *,
    reason: str,
    assigned_agent: str | None = None,
) -> bool:
    """Stop AI replies and flag the conversation for a person.

    Idempotent: a second request only records the agent assignment.
    """
    already = conversation.handling_mode == HandlingMode.HUMAN
    conversation.handling_mode = HandlingMode.HUMAN
    conversation.handoff_reason = reason
    conversation.handoff_at = conversation.handoff_at or utcnow()
    if assigned_agent:
        conversation.assigned_agent = assigned_agent

    if not already and can_transition(conversation.sales_stage, SalesStage.HUMAN_HANDOFF):
        await set_stage(session, conversation, SalesStage.HUMAN_HANDOFF, reason=reason)

    await session.flush()
    if not already:
        logger.info(
            "conversation handed to human",
            extra={"conversation_id": str(conversation.id), "reason": reason},
        )
    return not already


async def return_to_ai(
    session: AsyncSession, conversation: Conversation, *, stage: SalesStage | None = None
) -> None:
    """Give a conversation back to the AI after a human is done with it."""
    conversation.handling_mode = HandlingMode.AI
    conversation.assigned_agent = None
    conversation.handoff_reason = None
    conversation.handoff_at = None
    if stage is not None:
        await set_stage(session, conversation, stage, system=True, reason="returned to ai")
    elif conversation.sales_stage == SalesStage.HUMAN_HANDOFF:
        await set_stage(session, conversation, SalesStage.ENGAGED, reason="returned to ai")
    await session.flush()


async def close_conversation(
    session: AsyncSession, conversation: Conversation, reason: str | None = None
) -> None:
    conversation.status = ConversationStatus.CLOSED
    conversation.closed_at = utcnow()
    if can_transition(conversation.sales_stage, SalesStage.CLOSED):
        await set_stage(session, conversation, SalesStage.CLOSED, system=True, reason=reason)
    await session.flush()


# --------------------------------------------------------------------------- #
# Meta messaging policy
# --------------------------------------------------------------------------- #
def within_service_window(conversation: Conversation, hours: int | None = None) -> bool:
    """Is a free-form reply still allowed?

    Meta only permits free-form messages for 24 hours after the customer's last
    inbound message. Outside that window a pre-approved template is required.
    """
    hours = hours if hours is not None else get_settings().service_window_hours
    last_inbound = as_utc(conversation.last_inbound_at)
    if last_inbound is None:
        return False
    return utcnow() - last_inbound < timedelta(hours=hours)

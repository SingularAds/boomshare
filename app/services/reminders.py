"""Follow-ups.

Reminders live in PostgreSQL, not Redis: a follow-up that vanishes because a
30MB cache evicted it is a lost sale, and the scheduling horizon (hours to
weeks) is far longer than anything a cache should hold.

The important rule is that a reminder is re-checked against *current* state at
send time, not at schedule time. Between "follow up in 2 days" and two days
later, the customer may have replied, installed the app, asked for a human, or
asked us to stop - and in all of those cases the follow-up must not go out.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import as_utc, utcnow
from app.core.logging import get_logger
from app.domain import (
    STOP_FOLLOW_UP_STAGES,
    ConversationStatus,
    HandlingMode,
    ReminderKind,
    ReminderStatus,
)
from app.models import Conversation, Customer, Reminder

logger = get_logger(__name__)

#: Never queue more than this many live follow-ups for one conversation.
MAX_PENDING_PER_CONVERSATION = 3


async def schedule(
    session: AsyncSession,
    conversation: Conversation,
    customer: Customer,
    *,
    delay: timedelta,
    kind: ReminderKind = ReminderKind.FOLLOW_UP,
    reason: str | None = None,
    payload: dict | None = None,
    replace_pending: bool = True,
) -> Reminder | None:
    """Queue a follow-up `delay` from now.

    The delay is a `timedelta` rather than a number so a caller can never mean
    minutes and be read as hours. Five minutes and five days are both ordinary
    requests here, and the difference between them is a promise kept or broken.

    By default this replaces any pending follow-up of the same kind, so a
    customer who says "later" three times ends up with one reminder, not three.
    """
    if customer.is_opted_out or conversation.sales_stage in STOP_FOLLOW_UP_STAGES:
        logger.info(
            "follow-up not scheduled",
            extra={"conversation_id": str(conversation.id), "stage": conversation.sales_stage},
        )
        return None

    if replace_pending:
        await cancel_pending(session, conversation.id, kind=kind, resolution="superseded")
    elif await _pending_count(session, conversation.id) >= MAX_PENDING_PER_CONVERSATION:
        logger.info("follow-up cap reached", extra={"conversation_id": str(conversation.id)})
        return None

    reminder = Reminder(
        conversation_id=conversation.id,
        customer_id=customer.id,
        kind=kind,
        status=ReminderStatus.PENDING,
        due_at=utcnow() + delay,
        reason=reason,
        payload=payload,
    )
    session.add(reminder)
    await session.flush()

    logger.info(
        "follow-up scheduled",
        extra={
            "reminder_id": str(reminder.id),
            "conversation_id": str(conversation.id),
            "due_at": reminder.due_at.isoformat(),
        },
    )
    return reminder


async def _pending_count(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    rows = (
        await session.execute(
            select(Reminder.id).where(
                Reminder.conversation_id == conversation_id,
                Reminder.status == ReminderStatus.PENDING,
            )
        )
    ).scalars()
    return len(list(rows))


async def cancel_pending(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    kind: ReminderKind | None = None,
    resolution: str = "cancelled",
) -> int:
    """Cancel outstanding follow-ups, e.g. because the customer replied."""
    stmt = (
        update(Reminder)
        .where(
            Reminder.conversation_id == conversation_id,
            Reminder.status == ReminderStatus.PENDING,
        )
        .values(status=ReminderStatus.CANCELLED, resolution=resolution)
    )
    if kind is not None:
        stmt = stmt.where(Reminder.kind == kind)

    result = await session.execute(stmt)
    count = result.rowcount or 0
    if count:
        logger.info(
            "follow-ups cancelled",
            extra={"conversation_id": str(conversation_id), "count": count, "reason": resolution},
        )
    return count


async def due_reminder_ids(session: AsyncSession, limit: int = 50) -> list[uuid.UUID]:
    stmt = (
        select(Reminder.id)
        .where(Reminder.status == ReminderStatus.PENDING, Reminder.due_at <= utcnow())
        .order_by(Reminder.due_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def claim(session: AsyncSession, reminder_id: uuid.UUID) -> Reminder | None:
    """Take exclusive ownership of a due reminder.

    A conditional UPDATE is the whole lock: whichever worker changes the row
    from `pending` gets it, everyone else sees rowcount 0 and moves on. This
    works identically on PostgreSQL and SQLite, so the tests exercise the real
    code path.
    """
    result = await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id, Reminder.status == ReminderStatus.PENDING)
        .values(status=ReminderStatus.PROCESSING)
    )
    if not result.rowcount:
        return None

    reminder = await session.get(Reminder, reminder_id)
    if reminder is not None:
        reminder.attempts += 1
        await session.flush()
    return reminder


def relevance_block(
    reminder: Reminder, conversation: Conversation, customer: Customer
) -> str | None:
    """Why this follow-up should NOT be sent now, or None if it should."""
    if customer.is_opted_out:
        return "customer opted out"
    if customer.activated_at is not None:
        return "customer already activated"
    if customer.downloaded_at is not None:
        return "customer already downloaded"
    if conversation.status != ConversationStatus.OPEN:
        return "conversation closed"
    if conversation.handling_mode != HandlingMode.AI:
        return "a human is handling this conversation"
    if conversation.sales_stage in STOP_FOLLOW_UP_STAGES:
        return f"stage '{conversation.sales_stage}' does not take follow-ups"

    created = as_utc(reminder.created_at)
    last_inbound = as_utc(conversation.last_inbound_at)
    if created and last_inbound and last_inbound > created:
        return "customer already replied"

    return None


async def resolve(
    session: AsyncSession,
    reminder: Reminder,
    status: ReminderStatus,
    resolution: str | None = None,
) -> None:
    reminder.status = status
    reminder.resolution = resolution
    if status == ReminderStatus.SENT:
        reminder.sent_at = utcnow()
    await session.flush()

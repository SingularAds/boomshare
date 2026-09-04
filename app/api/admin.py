"""Operator endpoints.

Deliberately thin - just enough for a human to see what the AI is doing, take
over a conversation, and answer the attribution questions from the brief. This
is the surface a proper agent console would be built on later.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import knowledge
from app.api.deps import require_internal_token
from app.api.schemas import (
    AgentEffectivenessOut,
    AgentMessageRequest,
    ConversationDetail,
    ConversationOut,
    FunnelRow,
    HandoffRequest,
    MessageOut,
    ReleaseRequest,
)
from app.core.db import get_db
from app.core.logging import get_logger
from app.domain import ConversationStatus, HandlingMode, SalesStage
from app.models import (
    AiDecisionLog,
    Campaign,
    Conversation,
    Customer,
    DownloadLink,
    Lead,
    Message,
)
from app.services import conversations as conversation_service, messaging

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_internal_token)])


#: Stages that mean the customer was actually handed a link.
_AT_OR_PAST_LINK_SENT = (SalesStage.LINK_SENT, SalesStage.DOWNLOADED, SalesStage.ACTIVATED)


async def _median_turns_to_link(session: AsyncSession) -> float | None:
    """Median AI turns taken to get the download link out, counting the turn it went on.

    Read off the decision log rather than the link's `sent_at`: the log row for
    the sending turn is written just after the link is stamped, so a timestamp
    comparison silently drops the very turn being measured.

    Both the search and the median run in Python. Neither JSON containment nor
    `percentile_cont` behaves the same on the SQLite used in tests and the
    PostgreSQL used in production, and this is an operator report, not a hot path.
    """
    rows = (
        await session.execute(
            select(AiDecisionLog.conversation_id, AiDecisionLog.executed_actions)
            .order_by(AiDecisionLog.conversation_id, AiDecisionLog.created_at)
        )
    ).all()

    turns: dict[uuid.UUID, int] = {}
    counts: list[int] = []
    for conversation_id, executed in rows:
        if conversation_id in turns and turns[conversation_id] == 0:
            continue  # link already found for this conversation
        turns[conversation_id] = turns.get(conversation_id, 0) + 1
        if "send_download_link" in (executed or {}).get("actions", []):
            counts.append(turns[conversation_id])
            turns[conversation_id] = 0  # stop counting this conversation

    if not counts:
        return None

    counts.sort()
    middle = len(counts) // 2
    if len(counts) % 2:
        return float(counts[middle])
    return (counts[middle - 1] + counts[middle]) / 2


async def _get_conversation(session: AsyncSession, conversation_id: uuid.UUID) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")
    return conversation


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    stage: SalesStage | None = None,
    handling_mode: HandlingMode | None = None,
    conversation_status: ConversationStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> list[Conversation]:
    stmt = select(Conversation).order_by(desc(Conversation.updated_at)).limit(limit).offset(offset)
    if stage is not None:
        stmt = stmt.where(Conversation.sales_stage == stage)
    if handling_mode is not None:
        stmt = stmt.where(Conversation.handling_mode == handling_mode)
    if conversation_status is not None:
        stmt = stmt.where(Conversation.status == conversation_status)
    return list((await session.execute(stmt)).scalars())


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, session: AsyncSession = Depends(get_db)
) -> ConversationDetail:
    conversation = await _get_conversation(session, conversation_id)
    customer = await session.get(Customer, conversation.customer_id)
    messages = list(
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
            )
        ).scalars()
    )

    # Built from the base model rather than validating the ORM object directly:
    # relationships are explicit-load-only, and `from_attributes` would try to
    # read `conversation.messages` off the instance.
    return ConversationDetail(
        **ConversationOut.model_validate(conversation).model_dump(),
        customer_phone=customer.phone if customer else None,
        customer_name=customer.full_name if customer else None,
        downloaded_at=customer.downloaded_at if customer else None,
        activated_at=customer.activated_at if customer else None,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.post("/conversations/{conversation_id}/handoff", response_model=ConversationOut)
async def take_over(
    conversation_id: uuid.UUID,
    request: HandoffRequest,
    session: AsyncSession = Depends(get_db),
) -> Conversation:
    """Stop the AI and assign the conversation to a person."""
    conversation = await _get_conversation(session, conversation_id)
    await conversation_service.hand_off_to_human(
        session, conversation, reason=request.reason, assigned_agent=request.agent
    )
    return conversation


@router.post("/conversations/{conversation_id}/release", response_model=ConversationOut)
async def release(
    conversation_id: uuid.UUID,
    request: ReleaseRequest,
    session: AsyncSession = Depends(get_db),
) -> Conversation:
    """Hand the conversation back to the AI."""
    conversation = await _get_conversation(session, conversation_id)
    await conversation_service.return_to_ai(session, conversation, stage=request.stage)
    return conversation


@router.post("/conversations/{conversation_id}/messages", response_model=MessageOut)
async def send_agent_message(
    conversation_id: uuid.UUID,
    request: AgentMessageRequest,
    session: AsyncSession = Depends(get_db),
) -> Message:
    """Send a message as a human agent.

    Goes through the same messaging service as the AI, so the 24h window rule
    and opt-out checks apply to people too.
    """
    conversation = await _get_conversation(session, conversation_id)
    customer = await session.get(Customer, conversation.customer_id)
    if customer is None:  # pragma: no cover - FK guarantees this
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="customer not found")

    outcome = await messaging.send_reply(
        session, conversation, customer, request.body, ai_generated=False
    )
    if not outcome.sent or outcome.message is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=outcome.reason or "send failed",
        )
    outcome.message.sent_by = request.agent or "agent"
    await session.flush()
    return outcome.message


@router.post("/prompts/reload", status_code=status.HTTP_202_ACCEPTED)
async def reload_prompts() -> dict[str, str]:
    """Re-read the prompt and product-knowledge files without a restart."""
    knowledge.reload()
    return {"status": "reloaded"}


@router.get("/reports/funnel", response_model=list[FunnelRow])
async def funnel_report(session: AsyncSession = Depends(get_db)) -> list[FunnelRow]:
    """Leads to activations, by campaign.

    The reason the brief asked for PostgreSQL: this is one join across
    campaign -> lead -> customer -> download_link, not a data export.
    """
    stmt = (
        select(
            Campaign.meta_campaign_id,
            Campaign.name,
            func.count(func.distinct(Lead.id)).label("leads"),
            func.count(func.distinct(Conversation.id)).label("conversations"),
            func.count(func.distinct(DownloadLink.id))
            .filter(DownloadLink.sent_at.is_not(None))
            .label("links_sent"),
            func.count(func.distinct(Customer.id))
            .filter(Customer.downloaded_at.is_not(None))
            .label("downloads"),
            func.count(func.distinct(Customer.id))
            .filter(Customer.activated_at.is_not(None))
            .label("activations"),
        )
        .select_from(Lead)
        .outerjoin(Campaign, Lead.campaign_id == Campaign.id)
        .outerjoin(Customer, Lead.customer_id == Customer.id)
        .outerjoin(Conversation, Conversation.customer_id == Customer.id)
        .outerjoin(DownloadLink, DownloadLink.customer_id == Customer.id)
        .group_by(Campaign.meta_campaign_id, Campaign.name)
        .order_by(func.count(func.distinct(Lead.id)).desc())
    )

    rows = (await session.execute(stmt)).all()
    return [
        FunnelRow(
            campaign_id=row[0],
            campaign_name=row[1],
            leads=row[2],
            conversations=row[3],
            links_sent=row[4],
            downloads=row[5],
            activations=row[6],
        )
        for row in rows
    ]


@router.get("/reports/agent", response_model=AgentEffectivenessOut)
async def agent_effectiveness_report(
    session: AsyncSession = Depends(get_db),
) -> AgentEffectivenessOut:
    """Whether the AI is advancing conversations or just holding them.

    Campaign attribution is answered by `/reports/funnel`; this answers the
    different question of how the agent itself is performing, from data already
    written on every turn. Without it, a prompt change is a guess.
    """
    conversations = (
        await session.execute(select(func.count()).select_from(Conversation))
    ).scalar_one()
    reached_link_sent = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.sales_stage.in_(_AT_OR_PAST_LINK_SENT))
        )
    ).scalar_one()
    links_clicked = (
        await session.execute(
            select(func.count())
            .select_from(DownloadLink)
            .where(DownloadLink.clicked_at.is_not(None))
        )
    ).scalar_one()
    downloads = (
        await session.execute(
            select(func.count()).select_from(Customer).where(Customer.downloaded_at.is_not(None))
        )
    ).scalar_one()

    ai_turns = (await session.execute(select(func.count()).select_from(AiDecisionLog))).scalar_one()
    # `requested_actions` is stored as {"actions": [...]}, so an empty list is
    # the fingerprint of a turn that asked the backend for nothing.
    turns_without_action = (
        await session.execute(
            select(func.count())
            .select_from(AiDecisionLog)
            .where(AiDecisionLog.requested_actions == {"actions": []})
        )
    ).scalar_one()
    adjusted_decisions = (
        await session.execute(
            select(func.count())
            .select_from(AiDecisionLog)
            .where(AiDecisionLog.rejected_reasons.is_not(None))
        )
    ).scalar_one()

    return AgentEffectivenessOut(
        conversations=conversations,
        reached_link_sent=reached_link_sent,
        links_clicked=links_clicked,
        downloads=downloads,
        ai_turns=ai_turns,
        turns_without_action=turns_without_action,
        passivity_rate=round(turns_without_action / ai_turns, 3) if ai_turns else 0.0,
        adjusted_decisions=adjusted_decisions,
        median_turns_to_link=await _median_turns_to_link(session),
    )


@router.post("/reset")
async def reset_data(
    phone: str | None = Query(default=None, description="Optional phone number to reset specifically"),
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Reset data across tables and Redis cache for re-testing.

    If `phone` is given, deletes that specific customer and their cascaded records.
    If no `phone` is given, clears all customers, leads, conversations, messages,
    decision logs, reminders, download links, and webhook events.
    """
    from sqlalchemy import delete

    from app.core import redis as redis_helper
    from app.models import Ad, Campaign, Customer, WebhookEvent

    if phone:
        phone_clean = phone.lstrip("+").strip()
        customer = (
            await session.execute(
                select(Customer).where((Customer.phone == phone_clean) | (Customer.wa_id == phone_clean))
            )
        ).scalar_one_or_none()
        if customer is None:
            return {"status": "not_found", "phone": phone}
        await session.delete(customer)
        await session.commit()
        try:
            r = redis_helper.get_redis()
            keys = await r.keys(f"*{phone_clean}*")
            if keys:
                await r.delete(*keys)
        except Exception:
            pass
        logger.info("admin reset completed for customer", extra={"phone": phone_clean})
        return {"status": "reset", "phone": phone_clean}

    # Wipe all customer data & webhooks
    await session.execute(delete(Customer))
    await session.execute(delete(WebhookEvent))
    await session.execute(delete(Ad))
    await session.execute(delete(Campaign))
    await session.commit()

    try:
        await redis_helper.get_redis().flushdb()
    except Exception:
        pass

    logger.info("admin reset completed for all test data")
    return {"status": "reset_all", "message": "all test data cleared"}


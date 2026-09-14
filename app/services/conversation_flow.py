"""The sales pipeline - where a Meta event becomes a conversation.

This module is the one place that reads like the product does:

    inbound message -> identify -> persist -> ask the AI -> validate -> act

Processing an inbound message deliberately runs in **two transactions**:

  1. *ingest* - identify the customer, record attribution, persist the message.
     Committed immediately, because losing a customer's message because OpenAI
     was slow would be inexcusable.
  2. *reply*  - generate and send the answer. If this fails the job is retried,
     and the retry is safe because the inbound message is already stored and an
     `ai_decisions` row records whether we already answered it.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import agent, guardrails, objectives
from app.ai.schemas import AiDecision
from app.core import redis as redis_helper
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import InvalidStateTransition, RetryableError
from app.core.logging import get_logger
from app.core.trace import trace
from app.domain import (
    AiAction,
    HandlingMode,
    LeadStatus,
    MessageDirection,
    ReminderKind,
    ReminderStatus,
    SalesStage,
    installer_for,
    read_platform,
)
from app.integrations.meta.client import MetaClient, get_meta_client
from app.integrations.meta.schemas import InboundMessageEvent, LeadgenEvent, MessageStatusEvent
from app.models import AiDecisionLog, Conversation, Customer, Lead, Message
from app.services import (
    attribution,
    conversations as conversation_service,
    customers as customer_service,
    downloads as download_service,
    leads as lead_service,
    messaging,
    reminders as reminder_service,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class InboundContext:
    conversation_id: uuid.UUID
    customer_id: uuid.UUID
    message_id: uuid.UUID
    phone_number_id: str | None = None
    provider_message_id: str | None = None
    should_reply: bool = True
    skip_reason: str | None = None
    # Set when a click-to-WhatsApp ad has not been resolved to a campaign yet.
    ad_needing_campaign: str | None = None


# --------------------------------------------------------------------------- #
# Flow 1: click-to-WhatsApp / ongoing conversation
# --------------------------------------------------------------------------- #
async def ingest_inbound(session: AsyncSession, event: InboundMessageEvent) -> InboundContext:
    """Transaction 1 - identify, attribute and persist. No external calls."""
    settings = get_settings()
    # Which of our numbers they wrote to. Every later message in this thread is
    # routed by it, so it is resolved once, here, and stored on the row - the
    # webhook that carried it is long gone by the time a follow-up goes out.
    inbound_number = event.phone_number_id or settings.default_phone_number_id

    customer, _ = await customer_service.get_or_create(
        session,
        event.wa_id,
        wa_id=event.wa_id,
        full_name=event.profile_name,
    )

    lead = None
    if event.referral is not None:
        lead = await lead_service.create_from_referral(session, customer, event.referral)

    conversation, _ = await conversation_service.get_or_create_open_conversation(
        session,
        customer,
        phone_number_id=inbound_number,
        display_phone_number=event.display_phone_number,
        lead_id=lead.id if lead else None,
    )

    trace("db", "customer resolved", phone=customer.phone, name=customer.full_name)
    trace("db", "arrived on our number", phone_number_id=inbound_number)
    if lead is not None:
        trace("db", "lead attributed", source=str(lead.source), ad=str(lead.ad_id)[:8])
    trace("db", "conversation", id=str(conversation.id)[:8], stage=str(conversation.sales_stage))

    message, created = await conversation_service.record_inbound_message(
        session,
        conversation,
        provider_message_id=event.provider_message_id,
        content=event.text,
        message_type=event.message_type,
        payload=event.raw,
    )
    trace(
        "db" if created else "skip",
        "inbound message stored" if created else "message already stored - Meta redelivered it",
        text=event.text,
    )

    if created:
        # A reply makes every queued follow-up obsolete.
        await reminder_service.cancel_pending(
            session, conversation.id, resolution="customer replied"
        )
        await conversation_service.advance_on_inbound(session, conversation)
        if conversation.lead_id is not None:
            await lead_service.set_status(
                session, await session.get(Lead, conversation.lead_id), LeadStatus.RESPONDED
            )

        # A customer who writes to us has re-opened the door themselves.
        if customer.is_opted_out:
            customer.opted_out_at = None
            logger.info("opt-out cleared by inbound message", extra={"customer_id": str(customer.id)})

    context = InboundContext(
        conversation_id=conversation.id,
        customer_id=customer.id,
        message_id=message.id,
        phone_number_id=inbound_number,
        provider_message_id=event.provider_message_id,
    )

    if lead is not None and lead.campaign_id is None and event.referral is not None:
        # Resolved after this transaction commits - see handle_inbound_message.
        context.ad_needing_campaign = event.referral.source_id

    if not settings.knows_phone_number(inbound_number):
        # Meta and this deployment disagree about which numbers we own - almost
        # always a number added in Business Manager but not in the environment.
        # The message is stored, so nothing is lost and it can be answered once
        # the configuration catches up; what we will not do is reply from some
        # other number, which would open a thread the customer never started.
        context.should_reply = False
        context.skip_reason = f"'{inbound_number}' is not a configured whatsapp number"
        logger.error(
            "inbound message on an unconfigured whatsapp number",
            extra={"phone_number_id": inbound_number, "wa_id": event.wa_id},
        )
    elif conversation.handling_mode != HandlingMode.AI:
        context.should_reply = False
        context.skip_reason = f"conversation handled by {conversation.handling_mode}"
    elif not event.text:
        # Media with no caption: stored for the record, but there is nothing to
        # answer and guessing would be worse than staying quiet.
        context.should_reply = False
        context.skip_reason = f"no text in {event.message_type} message"

    return context


async def already_answered(session: AsyncSession, inbound_message_id: uuid.UUID) -> bool:
    """Did a previous attempt already reply to this message?

    Guards the retry path: the ingest transaction is committed, so a retry must
    not produce a second reply to the same customer message.
    """
    row = (
        await session.execute(
            select(AiDecisionLog.id).where(
                AiDecisionLog.inbound_message_id == inbound_message_id,
                AiDecisionLog.outbound_message_id.is_not(None),
            )
        )
    ).first()
    return row is not None


@dataclass(slots=True)
class ReplyInputs:
    """Everything the model reasons over, read in one transaction.

    The rows here are detached once that transaction closes. That is safe to
    read - the session factory sets `expire_on_commit=False`, so loaded
    attributes survive the commit - and it is deliberately *not* safe to write:
    anything the decision mutates is re-read in `apply_reply` against a live
    session. Keeping the two apart is what lets the model call happen with no
    database connection checked out.
    """

    conversation: Conversation
    customer: Customer
    history: list[Message]
    lead: Lead | None
    link_sent: bool
    within_window: bool
    objective: str | None
    previous_reply: str | None


async def load_reply_inputs(
    session: AsyncSession, context: InboundContext
) -> ReplyInputs | None:
    """Transaction 2a - read the state the model needs. No external calls.

    Returns `None` when there is nothing to answer, which is not a failure: the
    caller simply stops.
    """
    conversation = await session.get(Conversation, context.conversation_id)
    customer = await session.get(Customer, context.customer_id)
    if conversation is None or customer is None:  # pragma: no cover - defensive
        return None

    if await already_answered(session, context.message_id):
        trace("skip", "this message was already answered - not replying twice")
        logger.info("inbound message already answered", extra={"message_id": str(context.message_id)})
        return None

    history = await conversation_service.load_history(session, conversation.id)
    link_sent = await download_service.has_link_been_sent(session, customer.id)
    in_window = conversation_service.within_service_window(conversation)
    lead = await session.get(Lead, conversation.lead_id) if conversation.lead_id else None

    replies_sent = sum(1 for m in history if m.direction == MessageDirection.OUTBOUND)
    objective = objectives.next_objective(
        conversation.sales_stage,
        conversation.context_notes,
        download_link_sent=link_sent,
        replies_sent=replies_sent,
    )

    trace(
        "ai",
        "building the prompt",
        history=len(history),
        stage=str(conversation.sales_stage),
        link_sent=link_sent,
        in_24h_window=in_window,
        replies_sent=replies_sent,
    )

    return ReplyInputs(
        conversation=conversation,
        customer=customer,
        history=history,
        lead=lead,
        link_sent=link_sent,
        within_window=in_window,
        objective=objective,
        # Answering a question is worth doing even with a repeated sentence, so
        # this only records the evidence here. On an unprompted check-in the
        # same signal suppresses the send instead - see `_apply_decision`.
        previous_reply=_last_outbound_text(history),
    )


async def apply_reply(
    session: AsyncSession,
    context: InboundContext,
    inputs: ReplyInputs,
    result: agent.AgentResult,
) -> None:
    """Transaction 2b - act on the decision and send.

    The rows are re-read here rather than reused from `inputs`: those are
    detached, and everything below writes to them.
    """
    settings = get_settings()

    conversation = await session.get(Conversation, context.conversation_id)
    customer = await session.get(Customer, context.customer_id)
    if conversation is None or customer is None:  # pragma: no cover - defensive
        return

    # Re-checked against live state. The model call happens between the two
    # transactions, so this is the window in which another worker could have
    # answered the same message - narrow, and already covered by the
    # conversation lock, but the check costs one indexed read and being wrong
    # costs the customer the same answer twice.
    if await already_answered(session, context.message_id):
        trace("skip", "answered while the model was thinking - not replying twice")
        logger.info(
            "inbound message answered concurrently",
            extra={"message_id": str(context.message_id)},
        )
        return

    await _apply_decision(
        session,
        conversation,
        customer,
        result,
        inbound_message_id=context.message_id,
        link_already_sent=inputs.link_sent,
        max_reply_characters=settings.max_reply_characters,
        previous_reply=inputs.previous_reply,
    )


async def reply_to_inbound(context: InboundContext) -> None:
    """Ask the model, then act on what it said.

    Deliberately three steps, with the model call in the middle and *no database
    connection held across it*. The pool is small (`db_pool_size`), a model call
    is seconds long, and the previous single-transaction version meant every
    in-flight reply occupied a connection for its whole duration - so a handful
    of simultaneous customers could exhaust the pool and leave the webhook
    endpoint itself blocking on checkout.
    """
    async with session_scope() as session:
        inputs = await load_reply_inputs(session, context)

    if inputs is None:
        return

    result = await agent.generate_reply(
        inputs.conversation,
        inputs.customer,
        inputs.history,
        download_link_sent=inputs.link_sent,
        within_service_window=inputs.within_window,
        directive=inputs.objective,
        lead=inputs.lead,
    )

    if result is None:
        trace("guard", "the model gave nothing usable - no reply invented, will retry")
        # The model is down or gave us nothing usable. Fail loudly so the job is
        # retried rather than inventing a reply.
        raise RetryableError("ai did not return a usable decision")

    async with session_scope() as session:
        await apply_reply(session, context, inputs, result)


def _start_read_receipt(context: InboundContext) -> asyncio.Task | None:
    """Begin marking the customer's message read, without waiting for it.

    The blue tick is a courtesy and it is the slowest thing on this path that
    nobody is waiting for: a Meta round trip, and - because idle connections do
    not survive the gap between two customer messages - a TLS handshake with it.
    Measured against the live stack it was 340-1000ms spent before any work on
    the actual answer began.

    Starting it here lets it run during the model call instead. The caller still
    awaits the task before returning, so ordering is unchanged from the outside
    and nothing is left running after the job finishes. `mark_read` swallows its
    own failures, so awaiting it can never mask the real error.
    """
    if not context.provider_message_id:
        return None
    return asyncio.create_task(
        get_meta_client().mark_read(context.provider_message_id, context.phone_number_id)
    )


async def handle_inbound_message(event: InboundMessageEvent) -> None:
    """Full pipeline for one customer message."""
    async with session_scope() as session:
        context = await ingest_inbound(session, event)

    receipt = _start_read_receipt(context)
    try:
        if context.ad_needing_campaign:
            # Deliberately outside the ingest transaction: this is an HTTP call, and
            # holding a database transaction open across one is how connection pools
            # get exhausted. Runs once per ad, and never blocks the reply.
            async with session_scope() as session:
                await attribution.enrich_ad_from_meta(
                    session, context.ad_needing_campaign, get_meta_client()
                )

        if not context.should_reply:
            logger.info(
                "not replying",
                extra={
                    "conversation_id": str(context.conversation_id),
                    "reason": context.skip_reason,
                },
            )
            return

        settings = get_settings()
        allowed = await redis_helper.rate_limit(
            f"inbound:{event.wa_id}", settings.inbound_rate_limit_per_minute
        )
        if not allowed:
            logger.warning("inbound rate limit hit", extra={"wa_id": event.wa_id})
            return

        # Two messages arriving together must not produce two overlapping
        # replies. The second one now queues behind the first rather than
        # failing: giving up here parks the job, burns one of
        # `worker_max_attempts`, and leaves the customer waiting for a sweep to
        # answer a message we could have answered seconds later.
        async with redis_helper.lock(
            f"conversation:{context.conversation_id}",
            settings.ai_reply_lock_seconds,
            wait_seconds=settings.ai_reply_lock_wait_seconds,
        ) as acquired:
            if not acquired:
                raise RetryableError("conversation is already being answered")
            await reply_to_inbound(context)
    finally:
        if receipt is not None:
            try:
                await receipt
            except Exception as exc:  # noqa: BLE001 - a courtesy cannot fail a turn
                # This runs in a `finally`, so an exception escaping here would
                # replace whatever the reply path was reporting - including a
                # successful one - and send the whole job back for a retry over
                # a blue tick. `mark_read` contains its own failures; this makes
                # the guarantee hold whatever client is behind it.
                logger.info("could not mark message read", extra={"error": str(exc)})


# --------------------------------------------------------------------------- #
# Acting on a validated AI decision
# --------------------------------------------------------------------------- #
def _last_outbound_text(history: list) -> str | None:
    """The last thing we said, so a follow-up cannot say it again."""
    for message in reversed(history):
        if message.direction == MessageDirection.OUTBOUND and message.content:
            return message.content
    return None


async def _apply_decision(
    session: AsyncSession,
    conversation: Conversation,
    customer: Customer,
    result: agent.AgentResult,
    *,
    inbound_message_id: uuid.UUID | None,
    link_already_sent: bool,
    max_reply_characters: int,
    previous_reply: str | None = None,
) -> bool:
    """Act on a validated decision. Returns whether a message reached them."""
    decision: AiDecision = result.decision
    executed: list[str] = []
    rejected = dict(result.outcome.rejected)

    raw = result.call.decision
    trace(
        "ai",
        "model returned",
        intent=str(raw.intent),
        stage=str(raw.suggested_stage),
        actions=[a.value for a in raw.actions] or "none",
        confidence=raw.confidence,
    )
    for reason, detail in rejected.items():
        trace("guard", f"REJECTED {reason}", detail=detail)

    await conversation_service.record_intent(session, conversation, decision.intent)
    await conversation_service.merge_context_notes(session, conversation, decision.customer_notes)
    if inbound_message_id is not None and result.usable and decision.preferred_language:
        customer.locale = decision.preferred_language
        await session.flush()

    applied_stage = await _apply_stage(session, conversation, decision, rejected)

    reply_text = decision.reply_text
    outbound_message = None

    # Saying the same thing twice is the one failure a follow-up cannot
    # recover from: the customer already read it, and reading it again says the
    # conversation has lost its place. On an unprompted check-in silence is the
    # better message, so it is suppressed. When they asked us something we
    # answer anyway - a repeat is poor, but leaving a question hanging is worse
    # - and the log carries the evidence either way.
    repeats = guardrails.is_repeat(reply_text, previous_reply)
    unprompted = inbound_message_id is None
    if repeats:
        rejected["duplicate_reply"] = "identical to the previous message"

    if not result.usable or (repeats and unprompted):
        # The guardrails rejected the text itself. Nothing is sent; the decision
        # is still logged so the prompt can be fixed.
        trace("guard", "reply SUPPRESSED - nothing sent to the customer")
        rejected.setdefault("reply_suppressed", "reply failed validation")
    else:
        reply_text, link = await _attach_download_link(
            session,
            conversation,
            customer,
            decision,
            link_already_sent=link_already_sent,
            reply_text=reply_text,
            executed=executed,
            rejected=rejected,
            max_reply_characters=max_reply_characters,
        )

        outcome = await messaging.send_reply(
            session, conversation, customer, reply_text, ai_generated=True
        )
        outbound_message = outcome.message

        if outcome.sent:
            executed.append("send_reply")
            if link is not None:
                await download_service.mark_link_sent(session, link)
                await _try_stage(session, conversation, SalesStage.LINK_SENT, rejected, "link sent")
        else:
            rejected["send_failed"] = outcome.reason or "unknown send failure"

    await _apply_actions(
        session,
        conversation,
        customer,
        decision,
        executed,
        rejected,
        reply_sent="send_reply" in executed,
    )
    await _ensure_follow_up(session, conversation, customer, executed)

    session.add(
        AiDecisionLog(
            conversation_id=conversation.id,
            inbound_message_id=inbound_message_id,
            outbound_message_id=outbound_message.id if outbound_message else None,
            model=result.call.model,
            intent=decision.intent,
            # What the model asked for, before validation - that is the version
            # worth keeping when tuning prompts.
            suggested_stage=result.call.decision.suggested_stage,
            applied_stage=applied_stage or conversation.sales_stage,
            # As above: what the model asked for, before validation. The
            # guardrails rewrite actions as well as stages - a delivery becomes
            # an offer - and logging the rewritten version would hide the very
            # behaviour the prompt needs tuning for.
            requested_actions={"actions": [a.value for a in result.call.decision.actions]},
            executed_actions={"actions": executed},
            rejected_reasons=rejected or None,
            confidence=decision.confidence,
            raw_response=result.call.raw_response,
            prompt_tokens=result.call.prompt_tokens,
            completion_tokens=result.call.completion_tokens,
            latency_ms=result.call.latency_ms,
        )
    )
    await session.flush()
    return "send_reply" in executed


async def _apply_stage(
    session: AsyncSession,
    conversation: Conversation,
    decision: AiDecision,
    rejected: dict[str, str],
) -> SalesStage | None:
    if decision.suggested_stage is None:
        return None
    if await _try_stage(
        session, conversation, decision.suggested_stage, rejected, "suggested by ai"
    ):
        return decision.suggested_stage
    return None


async def _try_stage(
    session: AsyncSession,
    conversation: Conversation,
    stage: SalesStage,
    rejected: dict[str, str],
    reason: str,
) -> bool:
    try:
        return await conversation_service.set_stage(session, conversation, stage, reason=reason)
    except InvalidStateTransition as exc:
        rejected["stage_rejected"] = str(exc)
        return False


async def _attach_download_link(
    session: AsyncSession,
    conversation: Conversation,
    customer: Customer,
    decision: AiDecision,
    *,
    link_already_sent: bool,
    reply_text: str,
    executed: list[str],
    rejected: dict[str, str],
    max_reply_characters: int,
):
    """Append the real, tracked download URL when the AI asked for it.

    The model never writes a URL (the guardrails strip them) - the backend owns
    the link, its token and its attribution.
    """
    if not decision.wants(AiAction.SEND_DOWNLOAD_LINK):
        return reply_text, None

    if customer.is_opted_out:
        rejected["download_link_blocked"] = "customer opted out"
        return reply_text, None

    explicit_request = decision.intent in {"download_request", "buying_intent"}
    if link_already_sent and not explicit_request:
        # Re-sending an ignored link is the behaviour we were explicitly asked
        # to avoid. Let the AI's words stand without another URL.
        rejected["download_link_suppressed"] = "link already sent and not re-requested"
        return reply_text, None

    # The link is not withheld to qualify anyone. It used to be held back for a
    # turn whenever we did not know the platform yet, which cost every customer
    # an extra round trip before the one thing they came for. The download page
    # works without knowing the build; when we do know it, it picks the right
    # installer.
    link = await download_service.create_link(
        session,
        customer,
        conversation,
        platform=installer_for(read_platform((conversation.context_notes or {}).get("platform"))),
    )
    suffix = f"\n\n{link.url}"
    body = reply_text.rstrip()
    if len(body) + len(suffix) > max_reply_characters:
        body = body[: max_reply_characters - len(suffix)].rstrip()

    executed.append("send_download_link")
    return f"{body}{suffix}", link


async def _apply_actions(
    session: AsyncSession,
    conversation: Conversation,
    customer: Customer,
    decision: AiDecision,
    executed: list[str],
    rejected: dict[str, str],
    *,
    reply_sent: bool,
) -> None:
    """Run the non-messaging actions, after the reply has gone out.

    Order matters: handoff and opt-out both stop the AI from sending, so they
    are applied once the customer has already received the acknowledgement.

    `reply_sent` gates only the callback. Handing off, opting out and marking
    someone uninterested are facts about the customer and stand whether or not
    our message reached them; a promise to come back is not, because it exists
    only inside a message that never went.
    """
    if decision.wants(AiAction.OFFER_DOWNLOAD_LINK):
        # Nothing to send: the offer is the reply itself. What matters is that
        # it is recorded, because `download_suggested` is what tells the next
        # turn to hand the link over instead of offering it a second time.
        executed.append("offer_download_link")

    if decision.wants(AiAction.REQUEST_HUMAN_HANDOFF):
        await conversation_service.hand_off_to_human(
            session, conversation, reason=decision.handoff_reason or "requested by ai"
        )
        await reminder_service.cancel_pending(
            session, conversation.id, resolution="human took over"
        )
        executed.append("request_human_handoff")

    if decision.wants(AiAction.MARK_NOT_INTERESTED):
        if await _try_stage(
            session, conversation, SalesStage.NOT_INTERESTED, rejected, "customer not interested"
        ):
            executed.append("mark_not_interested")

    if decision.wants(AiAction.OPT_OUT):
        await customer_service.opt_out(session, customer, reason="requested on whatsapp")
        await reminder_service.cancel_pending(session, conversation.id, resolution="opted out")
        await _try_stage(
            session, conversation, SalesStage.NOT_INTERESTED, rejected, "customer opted out"
        )
        executed.append("opt_out")

    if decision.wants(AiAction.SCHEDULE_FOLLOW_UP) and decision.follow_up_minutes:
        # `follow_up_reason` is what the model said it was checking back about.
        # It used to read `handoff_reason` - a different field, about a human
        # taking over - so every callback was stored as the same constant and
        # arrived with nothing to say. The promise is kept on the payload as
        # well as the reason: the reason is for whoever reads the row, the
        # payload is read back into the prompt when the reminder fires.
        promise = decision.follow_up_reason or "customer asked to be contacted later"
        already_sent = await reminder_service.promised_follow_ups_sent(session, conversation.id)

        if not reply_sent:
            # A callback is a promise made *inside* a reply. This turn sent
            # nothing, so no promise was made and there is nothing to come back
            # to. Running it anyway is what turned one suppressed follow-up into
            # a chain of them: the reply was withheld as a repeat, the action
            # still scheduled the next callback, and that callback produced the
            # same repeat again - twelve reminders in forty-five minutes, one of
            # which ever reached the customer.
            rejected["follow_up_without_a_reply"] = "no message went out to check back on"
            reminder = None
        elif already_sent >= reminder_service.MAX_PROMISED_FOLLOW_UPS:
            rejected["promised_follow_ups_exhausted"] = (
                f"{already_sent} promised callbacks already sent"
            )
            reminder = None
        else:
            reminder = await reminder_service.schedule(
                session,
                conversation,
                customer,
                delay=timedelta(minutes=decision.follow_up_minutes),
                reason=promise,
                # Always recorded, so a callback the customer asked for is
                # always distinguishable from an automatic check-in. That
                # distinction is what the two-callback cap counts.
                payload={"promise": promise},
            )
        if reminder is not None:
            executed.append("schedule_follow_up")
        else:
            rejected["follow_up_not_scheduled"] = "stage or opt-out prevents follow-ups"


async def _ensure_follow_up(
    session: AsyncSession,
    conversation: Conversation,
    customer: Customer,
    executed: list[str],
) -> None:
    """Guarantee a live conversation always has a way back - but not forever.

    The model asks for a follow-up far less often than it should - warm leads
    were being lost to silence because nobody scheduled anything. Rather than
    prompting harder, the backend queues a check-in whenever a turn ends with
    nothing pending.

    The rung is chosen by how many messages this customer has already had
    without answering, so the gaps widen and the sequence ends: the previous
    fixed delay meant a customer who went quiet was messaged on the same cycle
    indefinitely. `reminder_service.schedule` refuses opted-out customers,
    terminal stages and an exhausted ladder, and replaces any pending
    follow-up, so this cannot stack up either.
    """
    if "schedule_follow_up" in executed:
        return
    if "send_reply" not in executed:
        # Nothing reached the customer, so there is nothing to follow up on.
        return

    if await _schedule_next_check_in(session, conversation, customer):
        executed.append("auto_follow_up")


async def _schedule_next_check_in(
    session: AsyncSession,
    conversation: Conversation,
    customer: Customer,
) -> bool:
    """Queue the rung after the one this customer has just been sent.

    Shared by the two ways a check-in goes out - an AI message inside the
    service window and an approved template outside it - because to the
    customer they are the same unanswered message, and the ladder counts them
    the same way.
    """
    spent = await reminder_service.follow_ups_since_reply(session, conversation)
    delay = reminder_service.follow_up_delay(spent)
    if delay is None:
        logger.info(
            "no further check-ins - ladder exhausted",
            extra={"conversation_id": str(conversation.id), "unanswered": spent},
        )
        return False

    reminder = await reminder_service.schedule(
        session,
        conversation,
        customer,
        delay=delay,
        reason=f"automatic check-in {spent + 1} of {len(reminder_service.follow_up_ladder())}",
    )
    return reminder is not None


# --------------------------------------------------------------------------- #
# Flow 2: lead ads
# --------------------------------------------------------------------------- #
async def handle_leadgen(event: LeadgenEvent, client: MetaClient | None = None) -> None:
    """Fetch a lead-ad submission and open the conversation with a template.

    We cannot send a free-form message to someone who has never messaged us -
    that is what Meta's template requirement is for - so the first contact is
    always an approved template, and the AI conversation starts at their reply.
    """
    client = client or get_meta_client()
    settings = get_settings()

    details = await client.fetch_lead(event.leadgen_id)

    async with session_scope() as session:
        lead, customer, created = await lead_service.create_from_lead_ad(
            session, details, page_id=event.page_id
        )
        if not created:
            logger.info("leadgen already processed", extra={"leadgen_id": event.leadgen_id})
            return

        # A lead-ad submission carries no phone number: the customer has not
        # messaged us, so there is no thread to answer on and we choose one.
        # The first configured number is that choice, and once the template
        # goes out it is the thread they will reply into.
        conversation, _ = await conversation_service.get_or_create_open_conversation(
            session,
            customer,
            phone_number_id=settings.default_phone_number_id,
            lead_id=lead.id,
        )
        conversation_id = conversation.id
        customer_id = customer.id
        lead_id = lead.id
        first_name = (customer.full_name or "there").split()[0]

    async with session_scope() as session:
        conversation = await session.get(Conversation, conversation_id)
        customer = await session.get(Customer, customer_id)
        lead = await session.get(Lead, lead_id)
        if conversation is None or customer is None:  # pragma: no cover - defensive
            return

        outcome = await messaging.send_template(
            session,
            conversation,
            customer,
            template_name=settings.whatsapp_lead_template_name,
            language=settings.whatsapp_lead_template_language,
            body_parameters=[first_name],
            client=client,
        )

        if not outcome.sent:
            await lead_service.set_status(session, lead, LeadStatus.UNREACHABLE)
            raise RetryableError(f"lead template send failed: {outcome.reason}")

        await conversation_service.set_stage(
            session, conversation, SalesStage.CONTACTED, reason="lead ad template sent"
        )
        await lead_service.set_status(session, lead, LeadStatus.CONTACTED)

        # If they never reply, try once more rather than losing the lead.
        await reminder_service.schedule(
            session,
            conversation,
            customer,
            delay=timedelta(hours=24),
            kind=ReminderKind.NO_REPLY_NUDGE,
            reason="no reply to lead ad outreach",
        )


# --------------------------------------------------------------------------- #
# Delivery receipts
# --------------------------------------------------------------------------- #
async def handle_message_status(event: MessageStatusEvent) -> None:
    async with session_scope() as session:
        await conversation_service.apply_delivery_status(
            session, event.provider_message_id, event.status, event.errors
        )


# --------------------------------------------------------------------------- #
# Follow-ups
# --------------------------------------------------------------------------- #
async def send_follow_up(reminder_id: uuid.UUID, client: MetaClient | None = None) -> None:
    """Send a due follow-up, re-checking that it is still appropriate.

    The relevance check happens here, at send time, against live state - not at
    schedule time.
    """
    settings = get_settings()

    async with session_scope() as session:
        reminder = await reminder_service.claim(session, reminder_id)
        if reminder is None:
            logger.info("reminder already claimed", extra={"reminder_id": str(reminder_id)})
            return

        conversation = await session.get(Conversation, reminder.conversation_id)
        customer = await session.get(Customer, reminder.customer_id)
        if conversation is None or customer is None:  # pragma: no cover - defensive
            await reminder_service.resolve(
                session, reminder, ReminderStatus.CANCELLED, "conversation missing"
            )
            return

        block = await reminder_service.relevance_block(
            session, reminder, conversation, customer
        )
        if block is not None:
            logger.info(
                "follow-up skipped",
                extra={"reminder_id": str(reminder_id), "reason": block},
            )
            await reminder_service.resolve(session, reminder, ReminderStatus.CANCELLED, block)
            return

        in_window = conversation_service.within_service_window(conversation)
        history = await conversation_service.load_history(session, conversation.id)
        link_sent = await download_service.has_link_been_sent(session, customer.id)

        if in_window:
            # Still inside 24h: let the AI write something that fits the thread.
            # Which check-in this is decides what it has to say - the second
            # nudge repeating the first is how a sequence becomes noise.
            attempt = await reminder_service.follow_ups_since_reply(session, conversation)
            result = await agent.generate_reply(
                conversation,
                customer,
                history,
                download_link_sent=link_sent,
                within_service_window=True,
                directive=objectives.follow_up_objective(
                    attempt,
                    download_link_sent=link_sent,
                    link_offered=conversation.sales_stage == SalesStage.DOWNLOAD_SUGGESTED,
                    promised=(reminder.payload or {}).get("promise"),
                ),
            )
            if result is None or not result.usable:
                raise RetryableError("ai could not produce a follow-up")

            sent = await _apply_decision(
                session,
                conversation,
                customer,
                result,
                inbound_message_id=None,
                link_already_sent=link_sent,
                max_reply_characters=settings.max_reply_characters,
                previous_reply=_last_outbound_text(history),
            )
            await reminder_service.resolve(
                session,
                reminder,
                ReminderStatus.SENT if sent else ReminderStatus.CANCELLED,
                "ai follow-up" if sent else "would have repeated the last message",
            )
            return

        first_name = (customer.full_name or "there").split()[0]
        outcome = await messaging.send_template(
            session,
            conversation,
            customer,
            template_name=settings.whatsapp_followup_template_name,
            language=settings.whatsapp_followup_template_language,
            body_parameters=[first_name],
            client=client,
        )
        if not outcome.sent:
            # The reminder is now terminal - `claim` only takes pending rows, so
            # nothing will retry it. Raising here would produce a worker
            # traceback for work that is already finished with, and would hide
            # the real cause (usually an unapproved template) behind a stack.
            # The failure is on the reminder row and on the message row.
            await reminder_service.resolve(
                session, reminder, ReminderStatus.FAILED, outcome.reason or "send failed"
            )
            logger.error(
                "follow-up could not be sent",
                extra={
                    "reminder_id": str(reminder_id),
                    "conversation_id": str(conversation.id),
                    "reason": outcome.reason,
                },
            )
            return

        await reminder_service.resolve(session, reminder, ReminderStatus.SENT, "template follow-up")
        # A template is still a check-in this customer did not answer, so the
        # ladder continues from here exactly as it does after an AI follow-up.
        # Without this it stopped at whichever rung first fell outside the
        # service window, and every rung past that one was never scheduled.
        await _schedule_next_check_in(session, conversation, customer)

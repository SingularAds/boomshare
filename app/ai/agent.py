"""The AI entry point used by the conversation logic.

    state -> context -> model -> guardrails -> validated decision

One function, so there is exactly one path from application state to something
the business logic may act on. If the model is unavailable or its answer is
unusable, this returns `None` and the caller decides what to do (it schedules a
retry rather than guessing at a reply).
"""

from __future__ import annotations

from app.ai import context as context_builder
from app.ai.guardrails import ValidationOutcome, validate_decision
from app.ai.schemas import AiCallResult
from app.core.config import get_settings
from app.core.errors import AiUnavailableError
from app.core.logging import get_logger
from app.integrations.openai.client import SalesModel, get_sales_model
from app.models import Conversation, Customer, Lead, Message

logger = get_logger(__name__)


class AgentResult:
    def __init__(self, call: AiCallResult, outcome: ValidationOutcome):
        self.call = call
        self.outcome = outcome

    @property
    def decision(self):
        return self.outcome.decision

    @property
    def usable(self) -> bool:
        return self.outcome.usable


async def generate_reply(
    conversation: Conversation,
    customer: Customer,
    messages: list[Message],
    *,
    download_link_sent: bool = False,
    within_service_window: bool = True,
    directive: str | None = None,
    lead: Lead | None = None,
    model: SalesModel | None = None,
) -> AgentResult | None:
    """Produce a validated sales decision, or None when the model cannot help."""
    settings = get_settings()
    model = model or get_sales_model()

    prompt_context = context_builder.build_context(
        conversation,
        customer,
        messages,
        history_limit=settings.history_message_limit,
        download_link_sent=download_link_sent,
        within_service_window=within_service_window,
        directive=directive,
        lead=lead,
    )

    try:
        call = await model.decide(prompt_context.to_messages())
    except AiUnavailableError as exc:
        logger.warning(
            "ai call failed",
            extra={"conversation_id": str(conversation.id), "error": str(exc)},
        )
        return None

    outcome = validate_decision(
        call.decision,
        conversation.sales_stage,
        settings.max_reply_characters,
        # What we already knew about their machine. The model reports it again
        # on every turn, so this only fills in when this turn left it unknown.
        known_platform=(conversation.context_notes or {}).get("platform"),
    )
    return AgentResult(call, outcome)

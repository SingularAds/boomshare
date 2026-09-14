"""Assembles the model input from application state.

The prompt is built from five clearly separated pieces:

    sales behaviour + product knowledge + customer facts + sales state + history

Each piece comes from exactly one place, so changing sales tone (a markdown
file), product facts (a markdown file) or what state the model can see (this
file) are three independent edits.

Context strategy for the MVP: the last N messages plus a compact state block.
That is enough for a WhatsApp sales chat and keeps cost predictable. When
conversations get long enough to matter, `recent_history` is the one function
that grows a summarisation step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.knowledge import product_knowledge, sales_behaviour
from app.core.clock import as_utc, utcnow
from app.domain import MessageDirection, SalesStage
from app.localization import resolve_language
from app.models import Conversation, Customer, Lead, Message

#: Hard cap per historical message so one pasted wall of text cannot blow up the
#: context window.
MAX_MESSAGE_CHARS = 1500


@dataclass(slots=True)
class PromptContext:
    """Everything the model is allowed to see, in one inspectable object."""

    system_blocks: list[str] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)

    def to_messages(self) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": block} for block in self.system_blocks if block]
        messages.extend(self.history)
        return messages


def _truncate(text: str | None, limit: int = MAX_MESSAGE_CHARS) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _describe_age(value: Any) -> str:
    dt = as_utc(value)
    if dt is None:
        return "never"
    minutes = int((utcnow() - dt).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hours ago"
    return f"{hours // 24} days ago"


def customer_block(
    customer: Customer, conversation: Conversation, lead: Lead | None = None
) -> str:
    """Facts about who we are talking to. Only what changes the reply.

    The lead is passed in rather than read off the conversation: relationships
    are explicit-load-only, and the prompt builder should not be issuing queries.
    """
    lines = ["# Who you are talking to"]
    lines.append(f"- Name: {customer.full_name or 'unknown'}")

    if lead is not None:
        source = "a click-to-WhatsApp ad" if lead.source == "click_to_whatsapp" else "a lead form"
        lines.append(f"- Reached us through: {source}")
        headline = (lead.raw_payload or {}).get("headline") if lead.raw_payload else None
        if headline:
            lines.append(f'- Ad they responded to: "{headline}"')
        for key, value in (lead.field_data or {}).items():
            if key not in {"phone_number", "full_name"} and value:
                lines.append(f"- {key.replace('_', ' ').title()}: {value}")

    notes = conversation.context_notes or {}
    if notes:
        lines.append("- What you have already learned about them:")
        for key, value in notes.items():
            lines.append(f"    - {key.replace('_', ' ')}: {value}")

    return "\n".join(lines)


def state_block(
    conversation: Conversation,
    customer: Customer,
    *,
    download_link_sent: bool = False,
    within_service_window: bool = True,
) -> str:
    """The authoritative business state. The model reports on it, never sets it."""
    lines = [
        "# Current state (set by the backend - treat as fact)",
        f"- Sales stage: {conversation.sales_stage}",
        f"- Stage last changed: {_describe_age(conversation.stage_updated_at)}",
        f"- Customer last messaged: {_describe_age(conversation.last_inbound_at)}",
        f"- We last messaged: {_describe_age(conversation.last_outbound_at)}",
    ]

    if download_link_sent:
        lines.append(
            "- A download link has ALREADY been sent to this customer. Do not ask the "
            "backend to send it again unless they explicitly ask for it. Ask what is "
            "holding them back instead."
        )
    else:
        lines.append("- No download link has been sent yet.")

    if customer.downloaded_at is not None:
        lines.append("- CONFIRMED: they have installed Boomshare. Stop selling; help them get value.")
    if customer.activated_at is not None:
        lines.append("- CONFIRMED: they have activated Boomshare and recorded something.")
    if customer.is_opted_out:
        lines.append("- They have opted out of contact. Do not pitch.")
    if not within_service_window:
        lines.append(
            "- We are outside the 24h messaging window, so a free-form reply may not be "
            "deliverable. Keep it short and expect the backend to convert it to a template."
        )
    if conversation.sales_stage == SalesStage.HUMAN_HANDOFF:
        lines.append("- A human colleague is handling this conversation.")

    return "\n".join(lines)


def language_block(customer: Customer) -> str:
    language = resolve_language(customer.phone, customer.locale)
    country = (
        f"{language.country_name} ({language.country_code})"
        if language.country_code else "unknown"
    )
    return "\n".join([
        "# Conversation language (backend policy)",
        f"- Phone-number country: {country}",
        f"- Reply language: {language.language_name} ({language.language_code})",
        f"- Language source: {language.source}",
        "Resolve the language for THIS reply in this order: (1) a language explicitly "
        "requested in the latest user message, (2) the reply language listed above. "
        "The listed language is a default, NEVER a restriction on changing languages. "
        "An explicit request overrides BOTH the saved preference and phone country. "
        "For example, if the default is Portuguese and they say 'Please speak English', "
        "reply in English immediately and set preferred_language to en. Never refuse "
        "a language change or say you can only help in the previous language.",
        "When changing language, report the requested language/locale in preferred_language. "
        "Otherwise return null. Do not infer a request from a name, ad, quoted text, "
        "or ordinary greetings in another language. Keep JSON keys and enums unchanged.",
        "Use the resolved language for all customer-facing text, including greetings, "
        "download instructions, handoffs, opt-out acknowledgements and follow-ups. "
        "English examples and internal sales objectives never override this policy. "
        "Respect regional usage: pt-PT uses European Portuguese (ecrã, ficheiro, partilhar); "
        "pt-BR uses Brazilian Portuguese (tela, arquivo, compartilhar).",
        "The phone-number country is a default, not proof of location, nationality or "
        "the customer's own language. Do not claim those facts about them.",
    ])


def recent_history(messages: list[Message], limit: int) -> list[dict[str, str]]:
    """The last `limit` messages as chat turns, oldest first.

    Only messages with text are useful to the model; media without a caption is
    represented by a short placeholder so the model knows something arrived.
    """
    turns: list[dict[str, str]] = []
    for message in messages[-limit:]:
        content = _truncate(message.content)
        if not content:
            content = f"[{message.message_type} message with no text]"
        role = "user" if message.direction == MessageDirection.INBOUND else "assistant"
        turns.append({"role": role, "content": content})
    return turns


def build_context(
    conversation: Conversation,
    customer: Customer,
    messages: list[Message],
    *,
    history_limit: int = 20,
    download_link_sent: bool = False,
    within_service_window: bool = True,
    directive: str | None = None,
    lead: Lead | None = None,
) -> PromptContext:
    blocks = [
        sales_behaviour(),
        "# Product knowledge (the ONLY facts you may state)\n\n" + product_knowledge(),
        customer_block(customer, conversation, lead),
        state_block(
            conversation,
            customer,
            download_link_sent=download_link_sent,
            within_service_window=within_service_window,
        ),
    ]
    if directive:
        # Used for turns the customer did not trigger, e.g. a due follow-up.
        blocks.append(
            "# What to do right now\n\n" + directive + "\n\n"
            "This sales objective comes from earlier state. An explicit request in the "
            "latest customer message takes priority: if they ask for the download, use "
            "send_download_link and hand it over instead of offering it again (provided "
            "their platform is supported or unknown). Honor opt-outs and human requests."
        )
    blocks.append(language_block(customer))

    return PromptContext(
        system_blocks=blocks,
        history=recent_history(messages, history_limit),
    )

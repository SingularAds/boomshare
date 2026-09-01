"""Domain vocabulary: enums and the sales state machine.

This module holds the business rules that the rest of the application enforces.
It has no database or network imports on purpose - it is pure and cheap to test.
"""

from __future__ import annotations

from enum import StrEnum

from app.core.errors import InvalidStateTransition


class SalesStage(StrEnum):
    """Where a conversation sits in the sales process."""

    NEW = "new"
    CONTACTED = "contacted"
    ENGAGED = "engaged"
    QUALIFIED = "qualified"
    PRODUCT_EXPLAINED = "product_explained"
    OBJECTION_HANDLING = "objection_handling"
    DOWNLOAD_SUGGESTED = "download_suggested"
    LINK_SENT = "link_sent"
    DOWNLOADED = "downloaded"
    ACTIVATED = "activated"
    NOT_INTERESTED = "not_interested"
    HUMAN_HANDOFF = "human_handoff"
    CLOSED = "closed"


#: Stages only the application may set, and only from verified signals:
#: a confirmed install event, an operator action, or a closed conversation.
#: The AI can never move a conversation into one of these.
SYSTEM_ONLY_STAGES: frozenset[SalesStage] = frozenset(
    {SalesStage.DOWNLOADED, SalesStage.ACTIVATED, SalesStage.CLOSED}
)

#: Terminal-ish stages that stop automated follow-up.
STOP_FOLLOW_UP_STAGES: frozenset[SalesStage] = frozenset(
    {
        SalesStage.DOWNLOADED,
        SalesStage.ACTIVATED,
        SalesStage.NOT_INTERESTED,
        SalesStage.HUMAN_HANDOFF,
        SalesStage.CLOSED,
    }
)

#: The conversion path, in order. `OBJECTION_HANDLING` is deliberately not part
#: of it: an objection is an excursion off the path, not a rung on the ladder.
_CONVERSATION_STAGES = [
    SalesStage.NEW,
    SalesStage.CONTACTED,
    SalesStage.ENGAGED,
    SalesStage.QUALIFIED,
    SalesStage.PRODUCT_EXPLAINED,
    SalesStage.DOWNLOAD_SUGGESTED,
    SalesStage.LINK_SENT,
]

#: Where an objection may return to. Never below `QUALIFIED`: by the time
#: someone is objecting they have told you enough to be qualified, and dropping
#: them back to `ENGAGED` is how a conversation starts going round in circles.
_OBJECTION_RETURNS_TO = [
    SalesStage.QUALIFIED,
    SalesStage.PRODUCT_EXPLAINED,
    SalesStage.DOWNLOAD_SUGGESTED,
    SalesStage.LINK_SENT,
]

#: Exits reachable from anywhere in the funnel.
_UNIVERSAL_EXITS = {
    SalesStage.NOT_INTERESTED,
    SalesStage.HUMAN_HANDOFF,
    SalesStage.DOWNLOADED,
    SalesStage.ACTIVATED,
    SalesStage.CLOSED,
}


def _build_transitions() -> dict[SalesStage, frozenset[SalesStage]]:
    """Forward-only movement through the funnel, with objections as an excursion.

    We deliberately allow skipping ahead (a customer who says "just send me the
    link" should not have to walk every stage) but never backwards. A funnel that
    can go down as well as up is not a funnel: the previous behaviour let a
    conversation slide from `link_sent` back to `download_suggested` and re-offer
    a link the customer already had.

    An objection is the one detour. Any live stage may enter
    `OBJECTION_HANDLING`, and it returns to the path at `QUALIFIED` or later -
    so handling an objection never costs the progress that earned it.
    """
    transitions: dict[SalesStage, set[SalesStage]] = {}
    for index, stage in enumerate(_CONVERSATION_STAGES):
        forward = set(_CONVERSATION_STAGES[index + 1 :])
        transitions[stage] = forward | {SalesStage.OBJECTION_HANDLING} | set(_UNIVERSAL_EXITS)

    transitions[SalesStage.OBJECTION_HANDLING] = set(_OBJECTION_RETURNS_TO) | set(_UNIVERSAL_EXITS)

    transitions[SalesStage.NOT_INTERESTED] = {
        SalesStage.ENGAGED,
        SalesStage.HUMAN_HANDOFF,
        SalesStage.CLOSED,
        SalesStage.DOWNLOADED,
        SalesStage.ACTIVATED,
    }
    transitions[SalesStage.HUMAN_HANDOFF] = {
        SalesStage.ENGAGED,
        SalesStage.DOWNLOAD_SUGGESTED,
        SalesStage.LINK_SENT,
        SalesStage.DOWNLOADED,
        SalesStage.ACTIVATED,
        SalesStage.NOT_INTERESTED,
        SalesStage.CLOSED,
    }
    transitions[SalesStage.DOWNLOADED] = {SalesStage.ACTIVATED, SalesStage.HUMAN_HANDOFF, SalesStage.CLOSED}
    transitions[SalesStage.ACTIVATED] = {SalesStage.HUMAN_HANDOFF, SalesStage.CLOSED}
    transitions[SalesStage.CLOSED] = {SalesStage.ENGAGED, SalesStage.HUMAN_HANDOFF}

    return {stage: frozenset(targets) for stage, targets in transitions.items()}


ALLOWED_TRANSITIONS: dict[SalesStage, frozenset[SalesStage]] = _build_transitions()


def can_transition(current: SalesStage, target: SalesStage) -> bool:
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def validate_transition(current: SalesStage, target: SalesStage) -> SalesStage:
    if not can_transition(current, target):
        raise InvalidStateTransition(current.value, target.value)
    return target


def is_ai_suggestable(stage: SalesStage) -> bool:
    return stage not in SYSTEM_ONLY_STAGES


class HandlingMode(StrEnum):
    """Who is answering this conversation."""

    AI = "ai"
    HUMAN = "human"
    PAUSED = "paused"


class ConversationStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageType(StrEnum):
    TEXT = "text"
    TEMPLATE = "template"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    STICKER = "sticker"
    LOCATION = "location"
    CONTACTS = "contacts"
    INTERACTIVE = "interactive"
    BUTTON = "button"
    REACTION = "reaction"
    SYSTEM = "system"
    UNSUPPORTED = "unsupported"


class MessageStatus(StrEnum):
    RECEIVED = "received"
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class LeadSource(StrEnum):
    CLICK_TO_WHATSAPP = "click_to_whatsapp"
    LEAD_AD = "lead_ad"
    MANUAL = "manual"


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    RESPONDED = "responded"
    UNREACHABLE = "unreachable"
    CONVERTED = "converted"
    DISQUALIFIED = "disqualified"


class ReminderKind(StrEnum):
    FOLLOW_UP = "follow_up"
    NO_REPLY_NUDGE = "no_reply_nudge"
    DOWNLOAD_CHECK = "download_check"


class ReminderStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    CANCELLED = "cancelled"
    FAILED = "failed"


class WebhookStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    IGNORED = "ignored"


class WebhookEventType(StrEnum):
    WHATSAPP_MESSAGE = "whatsapp_message"
    WHATSAPP_STATUS = "whatsapp_status"
    LEADGEN = "leadgen"
    UNKNOWN = "unknown"


class CustomerIntent(StrEnum):
    """Intent the AI reports for the customer's latest message."""

    GREETING = "greeting"
    INFORMATION_REQUEST = "information_request"
    PRICING_QUESTION = "pricing_question"
    FEATURE_QUESTION = "feature_question"
    COMPARISON = "comparison"
    OBJECTION = "objection"
    BUYING_INTENT = "buying_intent"
    DOWNLOAD_REQUEST = "download_request"
    SUPPORT_ISSUE = "support_issue"
    HUMAN_REQUEST = "human_request"
    NOT_INTERESTED = "not_interested"
    OPT_OUT = "opt_out"
    SMALL_TALK = "small_talk"
    UNCLEAR = "unclear"


class AiAction(StrEnum):
    """Business actions the AI may *request*. The application decides and executes."""

    NONE = "none"
    SEND_DOWNLOAD_LINK = "send_download_link"
    SCHEDULE_FOLLOW_UP = "schedule_follow_up"
    REQUEST_HUMAN_HANDOFF = "request_human_handoff"
    MARK_NOT_INTERESTED = "mark_not_interested"
    OPT_OUT = "opt_out"


#: Substrings that identify each desktop build in something a customer typed.
_PLATFORM_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("macos", ("mac", "osx", "os x", "apple", "imac", "macbook")),
    ("windows", ("windows", "win10", "win 10", "win11", "win 11", "pc", "laptop pc")),
)


def normalise_platform(value: str | None) -> str | None:
    """Map a free-text platform note onto one of the two builds we ship.

    The value reaches us from the model's `customer_notes`, so it can be
    anything from "Windows" to "macbook at work" to "mobile". Anything that is
    not a build we actually ship becomes None - which both keeps free text out
    of the download URL and stops "mobile" counting as a known platform and
    releasing a desktop link to someone holding a phone.
    """
    if not value:
        return None
    lowered = str(value).strip().lower()
    for platform, hints in _PLATFORM_HINTS:
        if any(hint in lowered for hint in hints):
            return platform
    return None

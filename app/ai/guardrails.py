"""Validation of model output before it can influence anything.

Nothing the model returns is trusted. This module is the choke point: it runs
between the AI client and the business logic, and it is where "the AI suggested
X" becomes "the application will do Y".

What it enforces:

  * reply text is present, trimmed and within WhatsApp-friendly length
  * no URLs in AI-written text - the backend owns links, the model never
    invents one (a hallucinated download URL is the worst failure mode here)
  * no claims that an action already happened
  * stage suggestions are real stages, not system-only ones, and are a legal
    transition from the current stage
  * follow-up delays are whole minutes inside sane bounds
"""

from __future__ import annotations

import re

from app.ai.schemas import AiDecision
from app.core.logging import get_logger
from app.domain import (
    SYSTEM_ONLY_STAGES,
    AiAction,
    CustomerIntent,
    SalesStage,
    can_transition,
)

logger = get_logger(__name__)

_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)

#: Phrases where the model claims to have performed a backend action. The
#: backend performs actions; the model must only describe what will happen.
_FALSE_CLAIM_RE = re.compile(
    r"\b(i(?:'ve| have)\s+(?:just\s+)?(?:sent|emailed|added|booked|scheduled|"
    r"forwarded|shared|created|registered|signed you up|passed)"
    r"|(?:link|email|invite)\s+has been sent"
    r"|i(?:'ve| have)\s+asked\s+(?:someone|a colleague|the team))",
    re.IGNORECASE,
)

#: Follow-up delays are whole minutes. The floor exists only to stop a
#: reminder firing inside the turn that created it; it is deliberately small,
#: because "give me five minutes" is a real thing customers say and a one-hour
#: floor turned that promise into a lie.
MIN_FOLLOW_UP_MINUTES = 2
MAX_FOLLOW_UP_MINUTES = 60 * 24 * 14


class ValidationOutcome:
    """A validated decision plus why anything was rejected."""

    def __init__(self, decision: AiDecision, rejected: dict[str, str], usable: bool):
        self.decision = decision
        self.rejected = rejected
        self.usable = usable

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ValidationOutcome(usable={self.usable}, rejected={self.rejected})"


def strip_urls(text: str) -> str:
    """Remove URLs and tidy the whitespace they leave behind."""
    cleaned = _URL_RE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,!?])", r"\1", cleaned)
    return cleaned.strip()


def contains_false_claim(text: str) -> bool:
    return bool(_FALSE_CLAIM_RE.search(text))


def sanitise_reply(text: str, max_characters: int) -> tuple[str, dict[str, str]]:
    rejected: dict[str, str] = {}
    cleaned = (text or "").strip()

    if _URL_RE.search(cleaned):
        rejected["url_in_reply"] = "model wrote a URL; stripped (backend owns links)"
        cleaned = strip_urls(cleaned)

    if contains_false_claim(cleaned):
        # We cannot rewrite the sentence safely, so we flag it. The caller
        # decides whether to send; the decision log keeps the evidence.
        rejected["false_action_claim"] = "reply claims an action already happened"

    if len(cleaned) > max_characters:
        rejected["reply_truncated"] = f"reply exceeded {max_characters} characters"
        cut = cleaned[:max_characters]
        # Prefer to cut at a sentence boundary so the message still reads well.
        boundary = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        if boundary > max_characters * 0.5:
            cleaned = cut[: boundary + 1]
        else:
            # Leave room for the ellipsis so the result still fits the limit.
            cleaned = cleaned[: max_characters - 3].rstrip() + "..."

    return cleaned, rejected


def validate_stage(
    suggested: SalesStage | None, current: SalesStage
) -> tuple[SalesStage | None, dict[str, str]]:
    if suggested is None or suggested == current:
        return None, {}

    if suggested in SYSTEM_ONLY_STAGES:
        return None, {
            "stage_system_only": f"'{suggested}' can only be set by a verified backend event"
        }

    if not can_transition(current, suggested):
        return None, {"stage_illegal_transition": f"{current} -> {suggested} is not allowed"}

    return suggested, {}


def validate_actions(decision: AiDecision) -> tuple[list[AiAction], dict[str, str]]:
    """Drop actions that are incoherent with the rest of the decision."""
    rejected: dict[str, str] = {}
    actions: list[AiAction] = []

    for action in dict.fromkeys(decision.actions):  # de-duplicate, keep order
        if action == AiAction.NONE:
            continue
        if action == AiAction.SCHEDULE_FOLLOW_UP and decision.follow_up_minutes is None:
            rejected["follow_up_without_delay"] = "schedule_follow_up requested with no delay"
            continue
        actions.append(action)

    # Asking to opt out and to keep selling at the same time is nonsense; the
    # customer's wish to stop wins.
    if AiAction.OPT_OUT in actions:
        conflicting = {AiAction.SEND_DOWNLOAD_LINK, AiAction.SCHEDULE_FOLLOW_UP}
        if conflicting & set(actions):
            rejected["conflicting_with_opt_out"] = "dropped sales actions alongside opt_out"
        actions = [a for a in actions if a not in conflicting]

    return actions, rejected


def resolve_deferral(
    stage: SalesStage | None, actions: list[AiAction]
) -> tuple[SalesStage | None, list[AiAction], dict[str, str]]:
    """"Not now" is a deferral. Never let it be recorded as a refusal.

    A customer who says "not right now, I'll have a laptop in five minutes" is
    asking to be contacted later, and the model reports that by asking for a
    follow-up *and* marking them not interested in the same breath. Applying
    both loses the lead: `not_interested` stops every follow-up there will ever
    be, including the one just promised, and the stage is applied before the
    action that would have honoured the promise.

    When the two disagree, the request to come back later wins - it is the one
    the customer actually made, and it is the recoverable choice.
    """
    if AiAction.SCHEDULE_FOLLOW_UP not in actions:
        return stage, actions, {}

    refuses = stage == SalesStage.NOT_INTERESTED or AiAction.MARK_NOT_INTERESTED in actions
    if not refuses:
        return stage, actions, {}

    return (
        None if stage == SalesStage.NOT_INTERESTED else stage,
        [action for action in actions if action != AiAction.MARK_NOT_INTERESTED],
        {"deferral_not_refusal": "customer asked to be contacted later, not to be dropped"},
    )


def clamp_follow_up_minutes(minutes: float | None) -> tuple[int | None, dict[str, str]]:
    """Bring a requested delay inside the schedulable range, in whole minutes."""
    if minutes is None:
        return None, {}
    requested = round(float(minutes))
    clamped = max(MIN_FOLLOW_UP_MINUTES, min(requested, MAX_FOLLOW_UP_MINUTES))
    if clamped != requested:
        return clamped, {"follow_up_clamped": f"{requested}m clamped to {clamped}m"}
    return clamped, {}


def validate_decision(
    decision: AiDecision, current_stage: SalesStage, max_characters: int
) -> ValidationOutcome:
    """Run every check and return a decision that is safe to act on."""
    rejected: dict[str, str] = {}

    reply, reply_rejected = sanitise_reply(decision.reply_text, max_characters)
    rejected.update(reply_rejected)

    stage, stage_rejected = validate_stage(decision.suggested_stage, current_stage)
    rejected.update(stage_rejected)

    actions, action_rejected = validate_actions(decision)
    rejected.update(action_rejected)

    stage, actions, deferral_rejected = resolve_deferral(stage, actions)
    rejected.update(deferral_rejected)

    minutes, minutes_rejected = clamp_follow_up_minutes(decision.follow_up_minutes)
    rejected.update(minutes_rejected)

    notes = {
        str(k)[:64]: str(v)[:256]
        for k, v in (decision.customer_notes or {}).items()
        if k and v
    }

    safe = decision.model_copy(
        update={
            "reply_text": reply,
            "suggested_stage": stage,
            "actions": actions,
            "follow_up_minutes": minutes,
            "confidence": max(0.0, min(float(decision.confidence or 0.0), 1.0)),
            "customer_notes": notes,
            "intent": decision.intent or CustomerIntent.UNCLEAR,
        }
    )

    # A reply that claims a false action is worse than no reply, and an empty
    # reply is nothing to send. Both mean "do not use this".
    usable = bool(safe.reply_text) and "false_action_claim" not in rejected

    if rejected:
        logger.info("ai decision adjusted", extra={"rejected": rejected, "usable": usable})

    return ValidationOutcome(safe, rejected, usable)

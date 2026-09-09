"""Turn a raw Meta webhook body into normalised events.

Pure functions - no I/O, no database. Meta ships malformed / unexpected shapes
occasionally, so every accessor is defensive: an entry we cannot understand is
skipped rather than allowed to fail the whole batch.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.integrations.meta.schemas import (
    InboundMessageEvent,
    LeadgenEvent,
    MessageStatusEvent,
    ParsedEvent,
    Referral,
)

logger = get_logger(__name__)

# Message payload types where the body text lives under a nested key.
_TEXT_EXTRACTORS: dict[str, Any] = {
    "text": lambda m: (m.get("text") or {}).get("body"),
    "button": lambda m: (m.get("button") or {}).get("text"),
    "interactive": lambda m: _interactive_text(m.get("interactive") or {}),
    "image": lambda m: (m.get("image") or {}).get("caption"),
    "video": lambda m: (m.get("video") or {}).get("caption"),
    "document": lambda m: (m.get("document") or {}).get("caption"),
    "reaction": lambda m: (m.get("reaction") or {}).get("emoji"),
}


def _interactive_text(interactive: dict[str, Any]) -> str | None:
    for key in ("button_reply", "list_reply", "nfm_reply"):
        block = interactive.get(key)
        if isinstance(block, dict):
            return block.get("title") or block.get("description") or block.get("id")
    return None


def extract_text(message: dict[str, Any]) -> str | None:
    extractor = _TEXT_EXTRACTORS.get(message.get("type", ""))
    if extractor is None:
        return None
    try:
        value = extractor(message)
    except Exception:  # noqa: BLE001 - malformed payload, not our problem
        return None
    return str(value) if value else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iter_changes(payload: dict[str, Any]):
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if isinstance(change, dict):
                yield entry, change


def _parse_whatsapp_change(value: dict[str, Any]) -> list[ParsedEvent]:
    events: list[ParsedEvent] = []
    metadata = value.get("metadata") or {}
    phone_number_id = metadata.get("phone_number_id")
    display_phone_number = metadata.get("display_phone_number")

    # `contacts` carries the WhatsApp profile name for the sender.
    names: dict[str, str] = {}
    for contact in value.get("contacts") or []:
        if isinstance(contact, dict) and contact.get("wa_id"):
            profile = contact.get("profile") or {}
            if profile.get("name"):
                names[str(contact["wa_id"])] = str(profile["name"])

    for message in value.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message_id = message.get("id")
        wa_id = message.get("from")
        if not message_id or not wa_id:
            logger.warning("skipping whatsapp message without id/from")
            continue

        referral_raw = message.get("referral")
        referral = (
            Referral.model_validate(referral_raw) if isinstance(referral_raw, dict) else None
        )

        events.append(
            InboundMessageEvent(
                event_key=str(message_id),
                provider_message_id=str(message_id),
                wa_id=str(wa_id),
                phone_number_id=str(phone_number_id) if phone_number_id else None,
                display_phone_number=(
                    str(display_phone_number) if display_phone_number else None
                ),
                profile_name=names.get(str(wa_id)),
                message_type=str(message.get("type") or "text"),
                text=extract_text(message),
                timestamp=_as_int(message.get("timestamp")),
                referral=referral,
                raw=message,
            )
        )

    for status in value.get("statuses") or []:
        if not isinstance(status, dict):
            continue
        message_id = status.get("id")
        state = status.get("status")
        if not message_id or not state:
            continue
        events.append(
            MessageStatusEvent(
                # A single message produces sent/delivered/read receipts, so the
                # dedupe key has to include the state.
                event_key=f"{message_id}:{state}",
                provider_message_id=str(message_id),
                status=str(state),
                recipient_id=str(status.get("recipient_id")) if status.get("recipient_id") else None,
                timestamp=_as_int(status.get("timestamp")),
                errors=[e for e in (status.get("errors") or []) if isinstance(e, dict)],
                raw=status,
            )
        )

    return events


def _parse_leadgen_change(entry: dict[str, Any], value: dict[str, Any]) -> list[ParsedEvent]:
    leadgen_id = value.get("leadgen_id")
    if not leadgen_id:
        logger.warning("skipping leadgen change without leadgen_id")
        return []
    return [
        LeadgenEvent(
            event_key=f"leadgen:{leadgen_id}",
            leadgen_id=str(leadgen_id),
            form_id=str(value["form_id"]) if value.get("form_id") else None,
            ad_id=str(value["ad_id"]) if value.get("ad_id") else None,
            adgroup_id=str(value["adgroup_id"]) if value.get("adgroup_id") else None,
            page_id=str(value.get("page_id") or entry.get("id") or "") or None,
            created_time=_as_int(value.get("created_time")),
            raw=value,
        )
    ]


def parse_webhook(payload: dict[str, Any]) -> list[ParsedEvent]:
    """Normalise a Meta webhook body into the events we act on.

    Handles both subscriptions we use:
      * `whatsapp_business_account` -> messages and delivery statuses
      * `page` / `leadgen`          -> lead-ad form submissions
    """
    if not isinstance(payload, dict):
        return []

    events: list[ParsedEvent] = []
    for entry, change in _iter_changes(payload):
        field = change.get("field")
        value = change.get("value")
        if not isinstance(value, dict):
            continue
        if field == "messages":
            events.extend(_parse_whatsapp_change(value))
        elif field == "leadgen":
            events.extend(_parse_leadgen_change(entry, value))
        else:
            logger.debug("ignoring unsubscribed webhook field", extra={"field": field})
    return events

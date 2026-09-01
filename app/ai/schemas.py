"""The contract between the model and the application.

The model returns a *decision*, not just text. The application treats every
field as a suggestion and validates it (see `app.ai.guardrails`) before any of
it becomes business state.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain import AiAction, CustomerIntent, SalesStage


class AiDecision(BaseModel):
    """Validated model output."""

    model_config = ConfigDict(extra="ignore")

    reply_text: str = Field(default="")
    intent: CustomerIntent = CustomerIntent.UNCLEAR
    suggested_stage: SalesStage | None = None
    actions: list[AiAction] = Field(default_factory=list)
    follow_up_minutes: int | None = None
    handoff_reason: str | None = None
    confidence: float = 0.5
    # Durable facts worth remembering about the customer (role, team size,
    # current tool). Persisted on the conversation, not just in the prompt.
    customer_notes: dict[str, str] = Field(default_factory=dict)

    @field_validator("customer_notes", mode="before")
    @classmethod
    def _accept_note_pairs(cls, value: Any) -> Any:
        """Accept the `[{key, value}, ...]` shape strict JSON schema requires.

        Strict structured outputs forbid free-form objects, so the model is
        asked for a list of pairs. The rest of the application still works with
        a plain mapping, so the conversion happens here at the boundary.
        """
        if not isinstance(value, list):
            return value
        pairs = {}
        for item in value:
            if isinstance(item, dict) and item.get("key"):
                pairs[str(item["key"])] = str(item.get("value") or "")
        return pairs

    def wants(self, action: AiAction) -> bool:
        return action in self.actions


class AiCallResult(BaseModel):
    """Decision plus the metadata we log for prompt tuning."""

    model_config = ConfigDict(extra="ignore")

    decision: AiDecision
    model: str | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None


def _enum_values(enum_cls: type) -> list[str]:
    return [member.value for member in enum_cls]


#: JSON Schema handed to OpenAI structured outputs.
#: Written by hand rather than generated from the Pydantic model because strict
#: mode has its own rules (every property required, no `anyOf` unions, explicit
#: `additionalProperties: false`) and mangling a generated schema to fit them is
#: harder to read than just stating it once.
DECISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reply_text",
        "intent",
        "suggested_stage",
        "actions",
        "follow_up_minutes",
        "handoff_reason",
        "confidence",
        "customer_notes",
    ],
    "properties": {
        "reply_text": {
            "type": "string",
            "description": "The WhatsApp message to send. Plain text, no URLs, usually under 60 words.",
        },
        "intent": {
            "type": "string",
            "enum": _enum_values(CustomerIntent),
            "description": "Intent behind the customer's most recent message.",
        },
        "suggested_stage": {
            "type": ["string", "null"],
            "enum": [*_enum_values(SalesStage), None],
            "description": "Sales stage the conversation should now be in, or null if unchanged.",
        },
        "actions": {
            "type": "array",
            # `AiAction.NONE` is deliberately not offered. Advertising a "do
            # nothing" action taught the model to answer `"actions": "none"` -
            # a bare string where a list belongs - which failed validation and
            # took the rest of the decision down with it. An ordinary turn is
            # an empty list. The enum member survives in `AiAction` so the
            # guardrails still strip it from older or hand-built decisions.
            "items": {
                "type": "string",
                "enum": [v for v in _enum_values(AiAction) if v != AiAction.NONE],
            },
            "description": "Business actions requested from the backend. Empty for a normal turn.",
        },
        "follow_up_minutes": {
            "type": ["integer", "null"],
            # Minutes, not hours: a customer who says "give me five minutes"
            # is asking for 0.083 hours, and the model reliably rounded that
            # to zero. Whole minutes span every delay we ever schedule, from a
            # five-minute callback to a fortnight, with no fractions to get
            # wrong.
            "description": (
                "Whole minutes until the follow-up, required when schedule_follow_up "
                "is requested. Use what the customer actually asked for: 5 for "
                "'give me five minutes', 1440 for 'tomorrow'."
            ),
        },
        "handoff_reason": {
            "type": ["string", "null"],
            "description": "Why a human is needed, when request_human_handoff is requested.",
        },
        "confidence": {
            "type": "number",
            "description": "Confidence in this decision, 0 to 1.",
        },
        "customer_notes": {
            "type": "array",
            "description": (
                "Durable facts learned about the customer, as key/value pairs, "
                "e.g. [{'key': 'role', 'value': 'support lead'}]. Empty list if nothing new."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "value"],
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        },
    },
}

#: `strict` makes the provider guarantee the schema rather than merely aim for
#: it, which removes a whole class of malformed-field failures. It requires
#: every property to be required, `additionalProperties: false` on every object,
#: and no free-form maps - which is why `customer_notes` is a list of pairs.
RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "sales_decision",
        "strict": True,
        "schema": DECISION_JSON_SCHEMA,
    },
}

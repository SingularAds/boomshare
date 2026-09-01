"""Stand-ins for Meta and OpenAI.

They implement the same surface the real clients expose and record what they
were asked to do, so tests assert on behaviour ("a template was sent to this
number") rather than on HTTP calls.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from app.ai.schemas import AiCallResult, AiDecision
from app.core.errors import AiUnavailableError, MetaPermanentError, MetaRetryableError
from app.integrations.meta.schemas import LeadDetails, SendResult


@dataclass
class SentMessage:
    to: str
    kind: str  # "text" | "template"
    body: str | None = None
    template: str | None = None
    parameters: list[str] = field(default_factory=list)


class FakeMetaClient:
    """Records outbound messages; can be told to fail."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.read_receipts: list[str] = []
        self.leads: dict[str, LeadDetails] = {}
        self.fail_text_with: Exception | None = None
        self.fail_template_with: Exception | None = None
        self.fail_fetch_with: Exception | None = None
        self._ids = itertools.count(1)

    def _next_id(self) -> str:
        return f"wamid.OUT{next(self._ids):04d}"

    async def send_text(self, to: str, body: str, **_: Any) -> SendResult:
        if self.fail_text_with is not None:
            raise self.fail_text_with
        message_id = self._next_id()
        self.sent.append(SentMessage(to=to, kind="text", body=body))
        return SendResult(provider_message_id=message_id, raw={"messages": [{"id": message_id}]})

    async def send_template(
        self,
        to: str,
        template_name: str,
        language_code: str = "en",
        body_parameters: list[str] | None = None,
        **_: Any,
    ) -> SendResult:
        if self.fail_template_with is not None:
            raise self.fail_template_with
        message_id = self._next_id()
        self.sent.append(
            SentMessage(
                to=to,
                kind="template",
                template=template_name,
                parameters=list(body_parameters or []),
            )
        )
        return SendResult(provider_message_id=message_id, raw={"messages": [{"id": message_id}]})

    async def mark_read(self, provider_message_id: str, phone_number_id: str | None = None) -> None:
        self.read_receipts.append(provider_message_id)

    async def fetch_lead(self, leadgen_id: str) -> LeadDetails:
        if self.fail_fetch_with is not None:
            raise self.fail_fetch_with
        if leadgen_id not in self.leads:
            raise MetaPermanentError(f"unknown lead {leadgen_id}", 404)
        return self.leads[leadgen_id]

    async def aclose(self) -> None:
        return None

    # -- helpers for tests ------------------------------------------------
    def register_lead(self, details: LeadDetails) -> None:
        self.leads[details.id] = details

    @property
    def texts(self) -> list[SentMessage]:
        return [m for m in self.sent if m.kind == "text"]

    @property
    def templates(self) -> list[SentMessage]:
        return [m for m in self.sent if m.kind == "template"]

    def last_body(self) -> str | None:
        return self.sent[-1].body if self.sent else None


class FakeSalesModel:
    """Returns queued decisions instead of calling OpenAI."""

    def __init__(self) -> None:
        self.queue: list[AiDecision] = []
        self.default = AiDecision(
            reply_text="Happy to help - what are you hoping to use it for?",
            intent="information_request",
            confidence=0.8,
        )
        self.calls: list[list[dict[str, str]]] = []
        self.raise_with: Exception | None = None

    def queue_decision(self, decision: AiDecision) -> None:
        self.queue.append(decision)

    async def decide(self, messages: list[dict[str, str]]) -> AiCallResult:
        self.calls.append(messages)
        if self.raise_with is not None:
            raise self.raise_with
        decision = self.queue.pop(0) if self.queue else self.default
        return AiCallResult(
            decision=decision,
            model="fake-model",
            raw_response=decision.model_dump(mode="json"),
            prompt_tokens=100,
            completion_tokens=40,
            latency_ms=12,
        )

    @property
    def last_prompt(self) -> list[dict[str, str]]:
        return self.calls[-1] if self.calls else []

    def system_text(self) -> str:
        return "\n".join(m["content"] for m in self.last_prompt if m["role"] == "system")


def unavailable_model() -> FakeSalesModel:
    model = FakeSalesModel()
    model.raise_with = AiUnavailableError("openai is down")
    return model


def meta_rate_limited() -> MetaRetryableError:
    return MetaRetryableError("rate limited", 429, {"error": {"code": 4}})


def meta_bad_request() -> MetaPermanentError:
    return MetaPermanentError("invalid recipient", 400, {"error": {"code": 100}})


class BrokenRedis:
    """A Redis client where every call fails.

    fakeredis quietly reconnects after `aclose()`, so simulating an outage by
    closing it proves nothing. This raises the way a real unreachable server
    does, which is what the degradation paths need to handle.
    """

    def __getattr__(self, name: str):
        async def _fail(*_args: Any, **_kwargs: Any):
            raise ConnectionError(f"redis unavailable ({name})")

        return _fail

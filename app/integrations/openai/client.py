"""OpenAI integration.

The only module that imports the OpenAI SDK. Everything above it depends on the
`SalesModel` protocol, which is what makes the sales logic testable without a
network call (and what makes swapping providers a one-file change).
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol, runtime_checkable

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from pydantic import ValidationError

from app.ai.schemas import RESPONSE_FORMAT, AiCallResult, AiDecision
from app.core.config import Settings, get_settings
from app.core.errors import AiUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


@runtime_checkable
class SalesModel(Protocol):
    """What the conversation logic needs from an AI provider."""

    async def decide(self, messages: list[dict[str, str]]) -> AiCallResult: ...


class OpenAiSalesModel:
    """Chat Completions with a JSON schema response format."""

    def __init__(self, settings: Settings | None = None, client: AsyncOpenAI | None = None):
        self.settings = settings or get_settings()
        self._client = client

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.settings.openai_api_key.get_secret_value(),
                timeout=self.settings.openai_timeout_seconds,
                # The SDK's own retry handles connection blips and 429s with
                # backoff; we do not add a second retry layer on top.
                max_retries=self.settings.openai_max_retries,
            )
        return self._client

    async def decide(self, messages: list[dict[str, str]]) -> AiCallResult:
        started = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=messages,  # type: ignore[arg-type]
                response_format=RESPONSE_FORMAT,  # type: ignore[arg-type]
                temperature=self.settings.openai_temperature,
                max_tokens=self.settings.openai_max_output_tokens,
            )
        except (APITimeoutError, APIConnectionError, RateLimitError) as exc:
            raise AiUnavailableError(f"openai unavailable: {exc}") from exc
        except APIStatusError as exc:
            # 4xx other than 429 will not fix itself, but the caller's fallback
            # path is the same either way, so we keep one error type.
            raise AiUnavailableError(f"openai rejected the request: {exc.status_code}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise AiUnavailableError("openai returned an empty response")

        decision = _parse_decision(content)
        usage = response.usage

        return AiCallResult(
            decision=decision,
            model=response.model,
            raw_response=_safe_raw(content),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None


#: Fields that may be dropped back to their default when the model returns
#: something unusable for them. `reply_text` is deliberately absent: a decision
#: without a reply is not worth recovering, and defaulting it to "" would turn a
#: malformed response into a silent no-reply.
_RECOVERABLE_FIELDS = frozenset(
    {"intent", "suggested_stage", "actions", "follow_up_minutes", "confidence", "customer_notes"}
)


def _invalid_fields(exc: ValidationError) -> set[str]:
    """The top-level field names a ValidationError actually complained about."""
    fields: set[str] = set()
    for error in exc.errors():
        location = error.get("loc") or ()
        if location:
            fields.add(str(location[0]))
    return fields


def _parse_decision(content: str) -> AiDecision:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AiUnavailableError(f"openai returned invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise AiUnavailableError("openai returned JSON that is not an object")

    try:
        return AiDecision.model_validate(payload)
    except ValidationError as exc:
        # Drop only what actually failed. Clearing every enum-ish field because
        # one of them was malformed used to discard the intent and stage of an
        # otherwise good decision.
        invalid = _invalid_fields(exc)
        recoverable = invalid & _RECOVERABLE_FIELDS
        if not recoverable or invalid - _RECOVERABLE_FIELDS:
            raise AiUnavailableError(f"openai decision failed validation: {exc}") from exc

        logger.warning(
            "dropped invalid ai decision fields",
            extra={"fields": sorted(recoverable), "errors": exc.errors()[:3]},
        )
        cleaned = {key: value for key, value in payload.items() if key not in recoverable}
        try:
            return AiDecision.model_validate(cleaned)
        except ValidationError as exc2:
            raise AiUnavailableError(f"openai decision failed validation: {exc2}") from exc2


def _safe_raw(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {"content": content[:2000]}
    except json.JSONDecodeError:
        return {"content": content[:2000]}


_model: SalesModel | None = None


def get_sales_model() -> SalesModel:
    global _model
    if _model is None:
        _model = OpenAiSalesModel()
    return _model


def set_sales_model(model: SalesModel | None) -> None:
    """Inject a fake model (tests) or a different provider."""
    global _model
    _model = model

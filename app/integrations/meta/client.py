"""HTTP client for the Meta Graph / WhatsApp Cloud API.

The only place in the codebase that knows Meta's URLs, headers and error codes.
Business code calls `send_text` / `send_template` / `fetch_lead` and gets back
plain objects or one of our two error classes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import MetaPermanentError, MetaRetryableError
from app.core.logging import get_logger
from app.integrations.meta.schemas import AdDetails, LeadDetails, SendResult

logger = get_logger(__name__)

# Meta error codes that mean "try again later" rather than "you did it wrong".
_RETRYABLE_META_CODES = {1, 2, 4, 17, 32, 341, 368, 613, 130429, 131048, 131056}
_RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class MetaClient:
    """Thin async wrapper. One instance per process, shared via app state."""

    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    # -- lifecycle --------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0),
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=10,
                    # Without this the pool drops idle connections after 5s, so
                    # the first call of every turn re-handshakes TLS. A stale
                    # connection surfaces as an httpx.TransportError, which
                    # `_request` already retries on a fresh one.
                    keepalive_expiry=self.settings.http_keepalive_seconds,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # -- internals --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.meta_access_token.get_secret_value()}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _raise_for_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:500]}

        if response.is_success:
            return payload if isinstance(payload, dict) else {"data": payload}

        error = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
        code = error.get("code")
        message = error.get("message") or f"Meta returned HTTP {response.status_code}"
        detail = f"{message} (code={code}, status={response.status_code})"

        if response.status_code in _RETRY_STATUS or code in _RETRYABLE_META_CODES:
            raise MetaRetryableError(detail, response.status_code, payload)
        raise MetaPermanentError(detail, response.status_code, payload)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        attempts: int = 3,
    ) -> dict[str, Any]:
        """Send a request, retrying only on transient failures.

        Retries are bounded and only cover errors that are safe to repeat:
        connection problems, timeouts, 5xx and Meta's throttling codes. A
        rejected payload is never retried.
        """
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self.client.request(
                    method, url, json=json_body, params=params, headers=self._headers()
                )
                return self._raise_for_payload(response)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = MetaRetryableError(f"transport error talking to Meta: {exc}")
            except MetaRetryableError as exc:
                last_error = exc
            except MetaPermanentError:
                raise

            if attempt < attempts:
                delay = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "retrying meta request",
                    extra={"attempt": attempt, "url": url, "delay": delay},
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    def _messages_url(self, phone_number_id: str | None = None) -> str:
        """The send endpoint for one of our numbers.

        Callers pass the number the conversation is on; the default is only
        reached by a caller that genuinely has no thread to answer on.
        """
        number = phone_number_id or self.settings.default_phone_number_id
        return f"{self.settings.graph_url}/{number}/messages"

    @staticmethod
    def _send_result(payload: dict[str, Any]) -> SendResult:
        messages = payload.get("messages") or []
        message_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
        return SendResult(provider_message_id=message_id, raw=payload)

    # -- WhatsApp ---------------------------------------------------------
    async def send_text(
        self, to: str, body: str, *, preview_url: bool = True, phone_number_id: str | None = None
    ) -> SendResult:
        """Free-form message. Only valid inside the 24h customer service window."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": preview_url, "body": body},
        }
        result = await self._request("POST", self._messages_url(phone_number_id), json_body=payload)
        return self._send_result(result)

    async def send_template(
        self,
        to: str,
        template_name: str,
        language_code: str = "en",
        body_parameters: list[str] | None = None,
        *,
        phone_number_id: str | None = None,
    ) -> SendResult:
        """Business-initiated message using a pre-approved template."""
        template: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language_code},
        }
        if body_parameters:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_parameters],
                }
            ]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": template,
        }
        result = await self._request("POST", self._messages_url(phone_number_id), json_body=payload)
        return self._send_result(result)

    async def mark_read(self, provider_message_id: str, phone_number_id: str | None = None) -> None:
        """Best-effort read receipt; never let it break message handling."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": provider_message_id,
        }
        try:
            await self._request(
                "POST", self._messages_url(phone_number_id), json_body=payload, attempts=1
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("could not mark message read", extra={"error": str(exc)})

    # -- Lead ads ---------------------------------------------------------
    async def fetch_lead(self, leadgen_id: str) -> LeadDetails:
        """Retrieve a lead-ad submission and its campaign attribution."""
        fields = ",".join(
            [
                "id",
                "created_time",
                "ad_id",
                "ad_name",
                "adset_id",
                "adset_name",
                "campaign_id",
                "campaign_name",
                "form_id",
                "platform",
                "field_data",
            ]
        )
        payload = await self._request(
            "GET", f"{self.settings.graph_url}/{leadgen_id}", params={"fields": fields}
        )
        return LeadDetails.model_validate(payload)


    async def fetch_ad(self, ad_id: str) -> AdDetails:
        """Look up an ad's campaign, for click-to-WhatsApp attribution."""
        fields = "id,name,adset_id,adset{name},campaign_id,campaign{name}"
        payload = await self._request(
            "GET", f"{self.settings.graph_url}/{ad_id}", params={"fields": fields}, attempts=2
        )
        return AdDetails.model_validate(payload)


_client: MetaClient | None = None


def get_meta_client() -> MetaClient:
    global _client
    if _client is None:
        _client = MetaClient()
    return _client


def set_meta_client(client: MetaClient | None) -> None:
    """Swap in a fake during tests."""
    global _client
    _client = client

"""Outbound WhatsApp messaging.

Every message we send goes through here, so four things are guaranteed in one
place: Meta's 24-hour window rule is respected, opted-out customers are never
messaged, every message leaves from the number the conversation is actually on,
and what we sent is persisted whether the send succeeded or failed.

A failed send is still recorded (status `failed`, with the provider error) - a
message that vanished without a trace is the hardest thing to debug later.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import MessagingPolicyError, MetaApiError
from app.core.logging import get_logger
from app.core.trace import trace
from app.domain import MessageStatus, MessageType
from app.integrations.meta.client import MetaClient, get_meta_client
from app.models import Conversation, Customer, Message
from app.services import conversations as conversation_service

logger = get_logger(__name__)


@dataclass(slots=True)
class SendOutcome:
    sent: bool
    message: Message | None
    reason: str | None = None

    @property
    def failed(self) -> bool:
        return not self.sent


async def send_text(
    session: AsyncSession,
    conversation: Conversation,
    customer: Customer,
    body: str,
    *,
    ai_generated: bool = False,
    sent_by: str | None = None,
    client: MetaClient | None = None,
) -> SendOutcome:
    """Send a free-form message. Only valid inside the 24h service window."""
    body = (body or "").strip()
    if not body:
        return SendOutcome(False, None, "empty body")

    blocked = _policy_block(conversation, customer)
    if blocked:
        trace("guard", "REFUSED to send", reason=blocked)
        return SendOutcome(False, None, blocked)

    if not conversation_service.within_service_window(conversation):
        trace("guard", "outside the 24h window - free-form text is not allowed")
        raise MessagingPolicyError(
            "outside the 24h customer service window - a template is required"
        )

    client = client or get_meta_client()
    try:
        result = await client.send_text(
            customer.wa_id or customer.phone,
            body,
            phone_number_id=_sender(conversation),
        )
    except MetaApiError as exc:
        message = await conversation_service.record_outbound_message(
            session,
            conversation,
            content=body,
            status=MessageStatus.FAILED,
            ai_generated=ai_generated,
            sent_by=sent_by,
            error={"message": str(exc), "status_code": exc.status_code},
        )
        logger.warning(
            "whatsapp send failed",
            extra={"conversation_id": str(conversation.id), "error": str(exc)},
        )
        return SendOutcome(False, message, str(exc))

    trace("send", "TEXT sent", to=customer.wa_id or customer.phone, body=body)
    message = await conversation_service.record_outbound_message(
        session,
        conversation,
        content=body,
        provider_message_id=result.provider_message_id,
        status=MessageStatus.SENT,
        ai_generated=ai_generated,
        sent_by=sent_by,
        payload=result.raw,
    )
    return SendOutcome(True, message)


async def send_template(
    session: AsyncSession,
    conversation: Conversation,
    customer: Customer,
    *,
    template_name: str,
    language: str = "en",
    body_parameters: list[str] | None = None,
    preview_text: str | None = None,
    client: MetaClient | None = None,
) -> SendOutcome:
    """Business-initiated message. The only thing allowed outside the window.

    `preview_text` is what we persist as the message body, so the conversation
    history and the AI prompt read naturally - the template id alone is useless
    context.
    """
    blocked = _policy_block(conversation, customer)
    if blocked:
        return SendOutcome(False, None, blocked)

    client = client or get_meta_client()
    content = preview_text or f"[template:{template_name}]"
    try:
        result = await client.send_template(
            customer.wa_id or customer.phone,
            template_name,
            language_code=language,
            body_parameters=body_parameters,
            phone_number_id=_sender(conversation),
        )
    except MetaApiError as exc:
        message = await conversation_service.record_outbound_message(
            session,
            conversation,
            content=content,
            message_type=MessageType.TEMPLATE,
            status=MessageStatus.FAILED,
            error={"message": str(exc), "status_code": exc.status_code},
            payload={"template": template_name, "language": language},
        )
        logger.warning(
            "whatsapp template send failed",
            extra={"conversation_id": str(conversation.id), "template": template_name},
        )
        return SendOutcome(False, message, str(exc))

    trace("send", "TEMPLATE sent", name=template_name, params=body_parameters)
    message = await conversation_service.record_outbound_message(
        session,
        conversation,
        content=content,
        message_type=MessageType.TEMPLATE,
        provider_message_id=result.provider_message_id,
        status=MessageStatus.SENT,
        payload={"template": template_name, "language": language, "response": result.raw},
    )
    return SendOutcome(True, message)


async def send_reply(
    session: AsyncSession,
    conversation: Conversation,
    customer: Customer,
    body: str,
    *,
    ai_generated: bool = True,
    template_fallback: bool = True,
    client: MetaClient | None = None,
) -> SendOutcome:
    """Send `body` free-form, falling back to the follow-up template if the
    24h window has closed."""
    try:
        return await send_text(
            session, conversation, customer, body, ai_generated=ai_generated, client=client
        )
    except MessagingPolicyError as exc:
        if not template_fallback:
            return SendOutcome(False, None, str(exc))
        settings = get_settings()
        trace("send", "falling back to the follow-up template")
        logger.info(
            "service window closed, falling back to template",
            extra={"conversation_id": str(conversation.id)},
        )
        return await send_template(
            session,
            conversation,
            customer,
            template_name=settings.whatsapp_followup_template_name,
            language=settings.whatsapp_followup_template_language,
            body_parameters=[customer.full_name or "there"],
            preview_text=body,
            client=client,
        )


def _sender(conversation: Conversation) -> str | None:
    """Which of our numbers this conversation is answered from.

    `None` lets the client fall back to the configured default. That only
    happens for a row written before the number was recorded; every
    conversation created since carries its own, and routing by the row is what
    keeps a reply in the thread the customer actually opened.
    """
    return conversation.phone_number_id or None


def _policy_block(conversation: Conversation, customer: Customer) -> str | None:
    """Reasons we must not send anything at all."""
    if customer.is_opted_out:
        return "customer opted out"
    if conversation.status != "open":
        return "conversation is closed"
    return None

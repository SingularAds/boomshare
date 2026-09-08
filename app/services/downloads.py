"""Download links and confirmed install/activation events.

Three distinct facts, deliberately never collapsed into one:

    link sent    - we put a URL in front of the customer  (we know this)
    downloaded   - the app was installed                  (the app tells us)
    activated    - the app was signed in and used         (the app tells us)

Each link carries a token, so an install reported by the desktop app can be
attributed back to the exact conversation, ad and campaign that produced it.
"""

from __future__ import annotations

import secrets
import uuid

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain import SalesStage
from app.models import Conversation, Customer, DownloadLink
from app.services import conversations as conversation_service, customers as customer_service

logger = get_logger(__name__)


def _build_url(token: str, platform: str | None) -> str:
    base = get_settings().download_base_url.rstrip("/")
    url = f"{base}?ref={token}"
    return f"{url}&platform={platform}" if platform else url


async def get_active_link(
    session: AsyncSession, customer_id: uuid.UUID
) -> DownloadLink | None:
    stmt = (
        select(DownloadLink)
        .where(DownloadLink.customer_id == customer_id)
        .order_by(desc(DownloadLink.created_at))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def has_link_been_sent(session: AsyncSession, customer_id: uuid.UUID) -> bool:
    link = await get_active_link(session, customer_id)
    return link is not None and link.sent_at is not None


async def create_link(
    session: AsyncSession,
    customer: Customer,
    conversation: Conversation | None = None,
    *,
    platform: str | None = None,
    reuse: bool = True,
) -> DownloadLink:
    """Get the customer's download link, creating one on first use.

    Reusing the token matters: if the customer is sent the link twice and
    installs from the second copy, both are the same attributed link.
    """
    if reuse:
        existing = await get_active_link(session, customer.id)
        if existing is not None:
            return existing

    token = secrets.token_urlsafe(16)
    link = DownloadLink(
        token=token,
        customer_id=customer.id,
        conversation_id=conversation.id if conversation else None,
        url=_build_url(token, platform),
        platform=platform,
    )
    session.add(link)
    await session.flush()
    return link


async def mark_link_sent(session: AsyncSession, link: DownloadLink) -> None:
    if link.sent_at is None:
        link.sent_at = utcnow()
        await session.flush()


async def get_by_token(session: AsyncSession, token: str) -> DownloadLink | None:
    return (
        await session.execute(select(DownloadLink).where(DownloadLink.token == token))
    ).scalar_one_or_none()


async def register_click(
    session: AsyncSession, token: str
) -> tuple[DownloadLink | None, bool]:
    """Record that the download page was opened.

    Returns the link and whether this was its *first* click. Only the first is
    recorded - a page that reports on every load would otherwise keep moving
    `clicked_at` forward and lose the moment they actually followed the link.
    """
    link = await get_by_token(session, token)
    if link is None:
        return None, False
    if link.clicked_at is not None:
        return link, False

    link.clicked_at = utcnow()
    await session.flush()
    return link, True


async def register_download(
    session: AsyncSession,
    customer: Customer,
    *,
    token: str | None = None,
    details: dict | None = None,
) -> bool:
    """Record a confirmed install reported by the Boomshare desktop backend.

    Returns True the first time; later reports for the same customer are no-ops
    so a retried event cannot double-count an install.
    """
    link = await get_by_token(session, token) if token else await get_active_link(session, customer.id)
    if link is not None and link.downloaded_at is None:
        link.downloaded_at = utcnow()
        if details:
            link.details = {**(link.details or {}), **details}

    changed = await customer_service.mark_downloaded(session, customer)
    if changed:
        await _advance_stage(session, customer, SalesStage.DOWNLOADED)
        logger.info("install confirmed", extra={"customer_id": str(customer.id)})
    return changed


async def register_activation(
    session: AsyncSession,
    customer: Customer,
    *,
    token: str | None = None,
    details: dict | None = None,
) -> bool:
    """Record a confirmed activation. Implies the install happened."""
    link = await get_by_token(session, token) if token else await get_active_link(session, customer.id)
    if link is not None:
        link.downloaded_at = link.downloaded_at or utcnow()
        if link.activated_at is None:
            link.activated_at = utcnow()
        if details:
            link.details = {**(link.details or {}), **details}

    changed = await customer_service.mark_activated(session, customer)
    if changed:
        await _advance_stage(session, customer, SalesStage.ACTIVATED)
        logger.info("activation confirmed", extra={"customer_id": str(customer.id)})
    return changed


async def _advance_stage(
    session: AsyncSession, customer: Customer, stage: SalesStage
) -> None:
    """Move the customer's open conversations to a verified system stage.

    Installing is a fact about the person, not about one thread. A customer who
    wrote to two of our numbers has two open conversations, and both should
    stop selling - not whichever one a query happened to return first.
    """
    for conversation in await conversation_service.open_conversations(session, customer.id):
        await conversation_service.set_stage(
            session, conversation, stage, system=True, reason="confirmed by boomshare backend"
        )

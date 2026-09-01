"""Endpoints the Boomshare product calls back into.

This is the seam described in the brief:

    WhatsApp conversation -> download link -> desktop app -> Boomshare backend
    -> installation/activation event -> customer state updated

"We sent a link" is something we know. "They installed it" is something only
Boomshare can tell us, so it arrives here and nowhere else. Nothing in the AI
path can set these states.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_token
from app.api.schemas import EventAck, InstallEvent
from app.core.db import get_db
from app.core.logging import get_logger
from app.models import Customer
from app.services import customers as customer_service, downloads as download_service

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal_token)]
)


async def _resolve_customer(session: AsyncSession, event: InstallEvent) -> Customer:
    if not event.has_identifier():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="either token or phone is required",
        )

    if event.token:
        link = await download_service.get_by_token(session, event.token)
        if link is not None:
            customer = await session.get(Customer, link.customer_id)
            if customer is not None:
                return customer

    if event.phone:
        customer = await customer_service.get_by_phone(session, event.phone)
        if customer is not None:
            return customer

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown customer")


@router.post("/events/download", response_model=EventAck)
async def report_download(
    event: InstallEvent, session: AsyncSession = Depends(get_db)
) -> EventAck:
    """Confirmed install. Idempotent - a repeat report changes nothing."""
    customer = await _resolve_customer(session, event)
    applied = await download_service.register_download(
        session,
        customer,
        token=event.token,
        details={"platform": event.platform, "app_version": event.app_version, **(event.details or {})},
    )
    return EventAck(
        applied=applied,
        customer_id=customer.id,
        detail=None if applied else "already recorded",
    )


@router.post("/events/activation", response_model=EventAck)
async def report_activation(
    event: InstallEvent, session: AsyncSession = Depends(get_db)
) -> EventAck:
    """Confirmed activation. Implies the install, so it backfills it."""
    customer = await _resolve_customer(session, event)
    applied = await download_service.register_activation(
        session,
        customer,
        token=event.token,
        details={"platform": event.platform, "app_version": event.app_version, **(event.details or {})},
    )
    return EventAck(
        applied=applied,
        customer_id=customer.id,
        detail=None if applied else "already recorded",
    )


@router.post("/events/click", response_model=EventAck)
async def report_click(event: InstallEvent, session: AsyncSession = Depends(get_db)) -> EventAck:
    """The download page was opened. Interest, not an install."""
    if not event.token:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="token is required"
        )
    link = await download_service.register_click(session, event.token)
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown token")
    return EventAck(applied=True, customer_id=link.customer_id)

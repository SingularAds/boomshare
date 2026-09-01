"""Customer identity and durable lifecycle facts.

A customer is identified by phone number. That is the one thing both lead flows
have in common, so it is the natural merge key: a person who fills in a lead
form and later messages the WhatsApp ad number is one customer, not two.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.errors import PermanentError
from app.core.logging import get_logger
from app.models import Customer
from app.services.base import apply_if_missing, insert_or_get, merge_json

logger = get_logger(__name__)

_NON_DIGITS = re.compile(r"\D")


def normalise_phone(raw: str) -> str:
    """Reduce a phone number to E.164 digits, the form WhatsApp uses as `wa_id`.

    Lead forms return `+91 98765 43210`, WhatsApp returns `919876543210`. We
    store the second form everywhere so the two flows resolve to one customer.
    """
    digits = _NON_DIGITS.sub("", raw or "")
    return digits.lstrip("0") if digits.startswith("00") else digits


async def get_by_phone(session: AsyncSession, phone: str) -> Customer | None:
    normalised = normalise_phone(phone)
    if not normalised:
        return None
    return (
        await session.execute(select(Customer).where(Customer.phone == normalised))
    ).scalar_one_or_none()


async def get_or_create(
    session: AsyncSession,
    phone: str,
    *,
    wa_id: str | None = None,
    full_name: str | None = None,
    email: str | None = None,
    locale: str | None = None,
) -> tuple[Customer, bool]:
    normalised = normalise_phone(phone)
    if not normalised:
        raise PermanentError("cannot identify a customer without a phone number")

    customer, created = await insert_or_get(
        session,
        Customer,
        defaults={
            "wa_id": wa_id or normalised,
            "full_name": full_name,
            "email": email,
            "locale": locale,
        },
        phone=normalised,
    )

    if not created:
        # Later signals may carry details the first one did not have.
        filled = apply_if_missing(
            customer,
            {"wa_id": wa_id or normalised, "full_name": full_name, "email": email, "locale": locale},
        )
        if filled:
            logger.info(
                "enriched customer", extra={"customer_id": str(customer.id), "fields": filled}
            )

    return customer, created


async def record_attributes(session: AsyncSession, customer: Customer, values: dict) -> None:
    customer.attributes = merge_json(customer.attributes, values)
    await session.flush()


async def mark_downloaded(session: AsyncSession, customer: Customer) -> bool:
    """Record a confirmed install. Idempotent - the first confirmation wins."""
    if customer.downloaded_at is not None:
        return False
    customer.downloaded_at = utcnow()
    await session.flush()
    return True


async def mark_activated(session: AsyncSession, customer: Customer) -> bool:
    """Record a confirmed activation. Implies the install happened."""
    changed = False
    if customer.downloaded_at is None:
        customer.downloaded_at = utcnow()
        changed = True
    if customer.activated_at is None:
        customer.activated_at = utcnow()
        changed = True
    if changed:
        await session.flush()
    return changed


async def opt_out(session: AsyncSession, customer: Customer, reason: str | None = None) -> bool:
    if customer.opted_out_at is not None:
        return False
    customer.opted_out_at = utcnow()
    if reason:
        customer.attributes = merge_json(customer.attributes, {"opt_out_reason": reason})
    await session.flush()
    logger.info("customer opted out", extra={"customer_id": str(customer.id)})
    return True

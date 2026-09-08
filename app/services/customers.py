"""Customer identity and durable lifecycle facts.

A customer is identified by phone number. That is the one thing both lead flows
have in common, so it is the natural merge key: a person who fills in a lead
form and later messages the WhatsApp ad number is one customer, not two.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
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


#: Shortest suffix worth matching on. National numbers run to nine or ten
#: digits, so anything shorter identifies a person only by accident - a
#: seven-digit tail collides across every country code we hold. Countries with
#: shorter national numbers simply do not match, which is the safe direction.
_MIN_SUFFIX_DIGITS = 9

#: Longest country code in E.164. What sits in front of the national number has
#: to be a country code and nothing else: without this a ten-digit tail would
#: also match a fifteen-digit number that merely happens to end the same way.
_MAX_COUNTRY_CODE_DIGITS = 3


async def find_by_phone_suffix(session: AsyncSession, phone: str) -> Customer | None:
    """Resolve a customer from a phone number typed into someone else's form.

    `get_by_phone` needs the number in the same form WhatsApp gave us. A signup
    form takes whatever the user types, so the number often arrives in national
    form (`09905252720`) or with no country code at all (`9905252720`), and
    neither normalises to the stored `919905252720`.

    Matching on the trailing digits recovers those, under two constraints that
    keep it from guessing. The stored number must be the same national number
    with only a country code in front of it - our customers span Portugal, the
    US and Brazil, and without that a tail would also match any longer number
    ending the same way. And exactly one customer must match: two people
    sharing a tail is precisely the case where a guess attributes an install to
    the wrong person, so an ambiguous match is treated as no match at all.

    A heuristic, deliberately: the token is the identifier that is *correct*,
    and every match made here is logged so the rate can be audited.

    Read-only by design. `get_or_create` keeps the exact key, because loosening
    the rule that *creates* customers is how two people become one row.
    """
    normalised = normalise_phone(phone)
    if not normalised:
        return None

    # A leading zero is a national trunk prefix, not part of the number, so it
    # has to come off before the tail will line up with the stored form.
    suffix = normalised.lstrip("0")
    if len(suffix) < _MIN_SUFFIX_DIGITS:
        return None

    # Two rows are enough to tell "exactly one" from "more than one", so an
    # ambiguous suffix costs the same as a unique one. This cannot use the
    # index on `phone`, but it only runs when the exact lookup already missed;
    # revisit if the customer table ever outgrows a scan.
    matches = list(
        (
            await session.execute(
                select(Customer)
                .where(
                    Customer.phone.endswith(suffix),
                    func.length(Customer.phone)
                    <= len(suffix) + _MAX_COUNTRY_CODE_DIGITS,
                )
                .limit(2)
            )
        ).scalars()
    )

    if len(matches) == 1:
        logger.info(
            "customer matched on phone suffix",
            extra={"customer_id": str(matches[0].id), "suffix_digits": len(suffix)},
        )
        return matches[0]

    if len(matches) > 1:
        logger.warning(
            "ambiguous phone suffix - refusing to guess",
            extra={"suffix_digits": len(suffix), "candidates": len(matches)},
        )
    return None


async def resolve_by_phone(session: AsyncSession, phone: str) -> Customer | None:
    """Exact match, then a unique-suffix match. For inbound reports only."""
    return await get_by_phone(session, phone) or await find_by_phone_suffix(session, phone)


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

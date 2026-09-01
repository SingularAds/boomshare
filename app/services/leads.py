"""Lead creation for both acquisition flows.

A lead is never "just a phone number" - it always carries where it came from, so
that campaign / ad performance can be measured later.

    click-to-WhatsApp -> referral block on the customer's first message
    lead ad           -> Graph API fetch of the leadgen submission
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermanentError
from app.core.logging import get_logger
from app.domain import LeadSource, LeadStatus
from app.integrations.meta.schemas import LeadDetails, Referral
from app.models import Customer, Lead
from app.services import attribution, customers as customer_service
from app.services.base import apply_if_missing, insert_or_get

logger = get_logger(__name__)

#: Lead form field names Meta commonly uses for a phone number.
_PHONE_FIELDS = ("phone_number", "phone", "mobile_number", "telephone")
_NAME_FIELDS = ("full_name", "name", "first_name")
_EMAIL_FIELDS = ("email", "email_address")


def _pick(fields: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for key in candidates:
        value = fields.get(key)
        if value:
            return value
    return None


async def create_from_referral(
    session: AsyncSession,
    customer: Customer,
    referral: Referral,
) -> Lead | None:
    """Record the ad that sent a click-to-WhatsApp customer to us.

    Only the first referral for a customer creates a lead; later messages from
    the same ad must not multiply the lead count.
    """
    if not referral.is_ad:
        return None

    existing = (
        await session.execute(
            select(Lead).where(
                Lead.customer_id == customer.id, Lead.source == LeadSource.CLICK_TO_WHATSAPP
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing

    ad = await attribution.get_or_create_ad(
        session,
        referral.source_id,
        details={"headline": referral.headline, "source_url": referral.source_url},
    )

    lead = Lead(
        customer_id=customer.id,
        source=LeadSource.CLICK_TO_WHATSAPP,
        status=LeadStatus.RESPONDED,  # by definition: they messaged us first
        ctwa_clid=referral.ctwa_clid,
        ad_id=ad.id if ad else None,
        campaign_id=ad.campaign_id if ad else None,
        raw_payload=referral.model_dump(),
    )
    session.add(lead)
    await session.flush()

    logger.info(
        "click-to-whatsapp lead recorded",
        extra={"lead_id": str(lead.id), "meta_ad_id": referral.source_id},
    )
    return lead


async def create_from_lead_ad(
    session: AsyncSession,
    details: LeadDetails,
    *,
    page_id: str | None = None,
) -> tuple[Lead, Customer, bool]:
    """Create (or return) the lead for a lead-ad submission.

    Idempotent on `meta_leadgen_id`: a retried webhook returns the existing lead
    and reports `created=False`, so no second welcome message is sent.
    """
    fields = details.fields()
    phone = _pick(fields, _PHONE_FIELDS)
    if not phone:
        # No phone number means we can never message them - retrying the
        # Graph fetch would return the same thing forever.
        raise PermanentError(f"lead {details.id} has no phone number field")

    customer, _ = await customer_service.get_or_create(
        session,
        phone,
        full_name=_pick(fields, _NAME_FIELDS),
        email=_pick(fields, _EMAIL_FIELDS),
    )

    campaign = await attribution.get_or_create_campaign(
        session, details.campaign_id, name=details.campaign_name
    )
    ad = await attribution.get_or_create_ad(
        session,
        details.ad_id,
        name=details.ad_name,
        meta_adset_id=details.adset_id,
        meta_form_id=details.form_id,
        campaign=campaign,
        details={"adset_name": details.adset_name},
    )

    lead, created = await insert_or_get(
        session,
        Lead,
        defaults={
            "customer_id": customer.id,
            "source": LeadSource.LEAD_AD,
            "status": LeadStatus.NEW,
            "meta_form_id": details.form_id,
            "meta_page_id": page_id,
            "campaign_id": campaign.id if campaign else None,
            "ad_id": ad.id if ad else None,
            "field_data": fields,
            "raw_payload": details.model_dump(),
        },
        meta_leadgen_id=details.id,
    )

    if not created:
        apply_if_missing(lead, {"campaign_id": campaign.id if campaign else None})
        logger.info("lead ad already processed", extra={"meta_leadgen_id": details.id})
    else:
        logger.info(
            "lead ad recorded",
            extra={
                "lead_id": str(lead.id),
                "meta_leadgen_id": details.id,
                "campaign_id": details.campaign_id,
            },
        )

    return lead, customer, created


async def set_status(session: AsyncSession, lead: Lead | None, status: LeadStatus) -> None:
    if lead is not None and lead.status != status:
        lead.status = status
        await session.flush()

"""Advertising attribution: campaigns and ads.

Meta gives us ids on the way in (an ad id on a click-to-WhatsApp referral, a
full campaign/adset/ad set on a lead-ad fetch). We normalise them into rows so
that "which campaign produced the most activations" is a join, not a JSON scan.

Rows are created lazily from whatever ids we happen to receive; names get filled
in later when a richer payload arrives.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import Ad, Campaign, Lead
from app.services.base import apply_if_missing, insert_or_get, merge_json

logger = get_logger(__name__)


async def get_or_create_campaign(
    session: AsyncSession,
    meta_campaign_id: str | None,
    *,
    name: str | None = None,
) -> Campaign | None:
    if not meta_campaign_id:
        return None
    campaign, created = await insert_or_get(
        session, Campaign, defaults={"name": name}, meta_campaign_id=str(meta_campaign_id)
    )
    if not created:
        apply_if_missing(campaign, {"name": name})
    return campaign


async def get_or_create_ad(
    session: AsyncSession,
    meta_ad_id: str | None,
    *,
    name: str | None = None,
    meta_adset_id: str | None = None,
    meta_form_id: str | None = None,
    campaign: Campaign | None = None,
    details: dict | None = None,
) -> Ad | None:
    if not meta_ad_id:
        return None
    ad, created = await insert_or_get(
        session,
        Ad,
        defaults={
            "name": name,
            "meta_adset_id": meta_adset_id,
            "meta_form_id": meta_form_id,
            "campaign_id": campaign.id if campaign else None,
            "details": details,
        },
        meta_ad_id=str(meta_ad_id),
    )
    if not created:
        apply_if_missing(
            ad,
            {
                "name": name,
                "meta_adset_id": meta_adset_id,
                "meta_form_id": meta_form_id,
                "campaign_id": campaign.id if campaign else None,
            },
        )
        if details:
            ad.details = merge_json(ad.details, details)
    return ad


async def enrich_ad_from_meta(
    session: AsyncSession,
    meta_ad_id: str,
    client,
) -> bool:
    """Fill in an ad's campaign by asking the Graph API.

    A click-to-WhatsApp referral only tells us which *ad* was clicked, so
    without this the lead has no campaign and campaign-level reporting is
    blind. The lookup happens once per ad - not once per lead - because the
    result is stored on the `ads` row.

    Attribution is valuable but never worth breaking a conversation for, so a
    failure here is logged and swallowed.
    """
    ad = (
        await session.execute(select(Ad).where(Ad.meta_ad_id == str(meta_ad_id)))
    ).scalar_one_or_none()
    if ad is None or ad.campaign_id is not None:
        return False

    try:
        details = await client.fetch_ad(str(meta_ad_id))
    except Exception as exc:  # noqa: BLE001 - attribution is best-effort
        logger.warning(
            "could not resolve campaign for ad",
            extra={"meta_ad_id": meta_ad_id, "error": str(exc)},
        )
        return False

    campaign = await get_or_create_campaign(
        session, details.campaign_id, name=details.campaign_name
    )
    apply_if_missing(
        ad,
        {
            "name": details.name,
            "meta_adset_id": details.adset_id,
            "campaign_id": campaign.id if campaign else None,
        },
    )
    if details.adset_name:
        ad.details = merge_json(ad.details, {"adset_name": details.adset_name})
    await session.flush()

    # Backfill the leads that arrived before we knew the campaign.
    if campaign is not None:
        await session.execute(
            update(Lead)
            .where(Lead.ad_id == ad.id, Lead.campaign_id.is_(None))
            .values(campaign_id=campaign.id)
        )
        logger.info(
            "campaign resolved for ad",
            extra={"meta_ad_id": meta_ad_id, "meta_campaign_id": details.campaign_id},
        )
    return campaign is not None

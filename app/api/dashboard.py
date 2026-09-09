"""The operator dashboard: an overview, a customer list, and one conversation.

Three endpoints, shaped by what the page renders rather than by what the tables
happen to contain. Each answers a whole screen in a single request, because a
dashboard that fans out per row is a dashboard that takes a second per row.

Nothing here estimates. Every number is a COUNT over rows that exist, and a
figure with nothing behind it is returned as zero rather than hidden - "no ads
have run yet" and "ads ran and nobody came" are different answers, and the
operator is entitled to tell them apart.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import Select, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_dashboard_token
from app.api.schemas import (
    CountByKey,
    CustomerDetail,
    CustomerPage,
    CustomerRow,
    DashboardConversation,
    DashboardLink,
    DashboardMessage,
    DashboardOverview,
)
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.domain import ConversationStatus, LeadSource, MessageDirection, SalesStage
from app.models import Campaign, Conversation, Customer, DownloadLink, Lead, Message

logger = get_logger(__name__)

#: The page itself carries no data, so it needs no credential to load - the
#: operator pastes their token into it and every fetch it makes is guarded.
page_router = APIRouter(tags=["dashboard"])

router = APIRouter(
    prefix="/admin/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_dashboard_token)],
)

#: Vite build output. `npm run build` in `dashboard/` writes here, and the
#: Dockerfile runs that build before copying `app/` into the image.
DIST = Path(__file__).parent / "static" / "dashboard"
ASSETS = DIST / "assets"
_INDEX = DIST / "index.html"

#: Lead sources that mean an ad was involved. `manual` is someone we added
#: ourselves, so it is a lead but not an acquisition.
_AD_SOURCES = (LeadSource.CLICK_TO_WHATSAPP, LeadSource.LEAD_AD)

#: Most recent messages returned per conversation. A longer thread is trimmed
#: from the top and flagged, so the modal stays inside its latency budget on a
#: conversation that has been running for months.
_MESSAGE_LIMIT = 300

#: Furthest-first, so a customer with several threads is reported by their best
#: one. Stages off the funnel rank below everything on it.
_STAGE_ORDER: dict[SalesStage, int] = {
    SalesStage.NOT_INTERESTED: -2,
    SalesStage.HUMAN_HANDOFF: -1,
    SalesStage.NEW: 0,
    SalesStage.CONTACTED: 1,
    SalesStage.ENGAGED: 2,
    SalesStage.OBJECTION_HANDLING: 3,
    SalesStage.QUALIFIED: 4,
    SalesStage.PRODUCT_EXPLAINED: 5,
    SalesStage.DOWNLOAD_SUGGESTED: 6,
    SalesStage.LINK_SENT: 7,
    SalesStage.DOWNLOADED: 8,
    SalesStage.ACTIVATED: 9,
    SalesStage.CLOSED: 10,
}


def _test_customer_ids():
    """Subquery of the team's own customer rows, or None when none are configured.

    One definition, used by every aggregate below. A count that forgot to apply
    it would disagree with the rest of the page, which is worse than not
    filtering at all - the operator cannot tell which number to believe.
    """
    numbers = get_settings().test_phone_numbers
    if not numbers:
        return None
    return select(Customer.id).where(Customer.phone.in_(numbers)).scalar_subquery()


def _excluding_tests(stmt, column, exclude):
    """Drop the team's own rows from `stmt`, matched on `column`."""
    return stmt if exclude is None else stmt.where(column.not_in(exclude))


def _messages(exclude, *conditions):
    """Count messages, reaching the customer through their conversation."""
    stmt = (
        select(func.count(Message.id))
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(*conditions)
    )
    return _excluding_tests(stmt, Conversation.customer_id, exclude).scalar_subquery()


def number_label(phone_number_id: str) -> str:
    """Name one of our WhatsApp numbers the way a person would say it.

    Meta identifies a number by an opaque id, which tells an operator nothing.
    `WHATSAPP_NUMBER_LABELS` maps it to the dialable number when that is
    configured; otherwise the position in `WHATSAPP_PHONE_NUMBER_IDS` is used,
    which is stable, needs no configuration, and is still enough to tell two
    numbers apart. A number we no longer answer on keeps its id visible rather
    than being silently relabelled.
    """
    settings = get_settings()
    label = settings.whatsapp_number_labels.get(phone_number_id)
    if label:
        return label
    ids = settings.whatsapp_phone_number_ids
    if phone_number_id in ids:
        return f"Number {ids.index(phone_number_id) + 1}"
    return f"Retired ({phone_number_id[-4:]})"


def thread_number(thread: Conversation) -> str:
    """Name the number this thread is on, best source first.

    What Meta told us when the thread opened beats anything configured by hand:
    it is the number itself, it arrives on every webhook, and it stays correct
    when a number is added without anyone remembering to update the config.
    """
    return thread.display_phone_number or number_label(thread.phone_number_id)


def _numbers_written_to(threads: list[Conversation]) -> list[str]:
    """Every number this person has written to, first contact first."""
    seen: dict[str, None] = {}
    for thread in sorted(threads, key=lambda t: t.created_at):
        seen.setdefault(thread_number(thread), None)
    return list(seen)


def _latest(*values):
    """The most recent of several nullable timestamps."""
    present = [value for value in values if value is not None]
    return max(present) if present else None


@page_router.get("/dashboard", include_in_schema=False)
async def dashboard_page() -> FileResponse:
    """The single-page app shell.

    It carries no data, so it needs no credential to load - the operator pastes
    their token into it and every request it then makes is guarded. Serving it
    from the API keeps the dashboard on one origin with the endpoints it calls,
    which is what removes any need for CORS.
    """
    if not _INDEX.is_file():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="dashboard is not built - run `npm run build` in dashboard/",
        )
    # The shell is tiny and its asset names are content-hashed, so it must not
    # be cached: a stale shell would point at a build that no longer exists.
    return FileResponse(
        _INDEX, media_type="text/html", headers={"Cache-Control": "no-store"}
    )


@router.get("/overview", response_model=DashboardOverview)
async def overview(
    session: AsyncSession = Depends(get_db),
    include_test: bool = Query(
        default=False,
        description="Include the team's own test numbers. Off by default, so the "
        "figures describe people who came from outside the team.",
    ),
) -> DashboardOverview:
    """Every headline number, in four round trips.

    The scalar counts are independent of one another, so they go out as one
    statement of subqueries rather than one statement each. That is invisible
    when the database is a millisecond away, and it is the whole latency budget
    when it is not - an operator reading production over the Cloud SQL proxy
    pays the round trip once instead of ten times.

    Only the acquisition split, which returns rows rather than a single value,
    still needs a statement of its own.
    """

    exclude = None if include_test else _test_customer_ids()

    def total(stmt, column=Customer.id):
        """One aggregate, with the test filter already applied."""
        return _excluding_tests(stmt, column, exclude).scalar_subquery()

    counts = (
        await session.execute(
            select(
                total(select(func.count(Customer.id))),
                total(select(func.count(Customer.id)).where(Customer.downloaded_at.is_not(None))),
                total(select(func.count(Customer.id)).where(Customer.activated_at.is_not(None))),
                total(select(func.count(Customer.id)).where(Customer.opted_out_at.is_not(None))),
                total(select(func.count(DownloadLink.id)).where(DownloadLink.sent_at.is_not(None)),
                     DownloadLink.customer_id),
                total(
                    select(func.count(DownloadLink.id)).where(DownloadLink.clicked_at.is_not(None)),
                    DownloadLink.customer_id,
                ),
                total(
                    select(func.count(func.distinct(DownloadLink.customer_id))).where(
                        DownloadLink.sent_at.is_not(None)
                    ),
                    DownloadLink.customer_id,
                ),
                # Customers reached through an ad, counted once each however
                # many leads they have. The rest have no lead row at all.
                total(
                    select(func.count(func.distinct(Lead.customer_id))).where(
                        Lead.source.in_(_AD_SOURCES)
                    ),
                    Lead.customer_id,
                ),
                total(select(func.count(func.distinct(Lead.customer_id))), Lead.customer_id),
                total(select(func.count(Conversation.id)), Conversation.customer_id),
                total(
                    select(func.count(Conversation.id)).where(
                        Conversation.status == ConversationStatus.OPEN
                    ),
                    Conversation.customer_id,
                ),
                _messages(exclude),
                _messages(exclude, Message.direction == MessageDirection.INBOUND),
                _messages(exclude, Message.direction == MessageDirection.OUTBOUND),
                _messages(exclude, Message.ai_generated.is_(True)),
                # Campaigns belong to no customer, so the test filter does not
                # apply - joining one on would multiply the count.
                select(func.count(Campaign.id)).scalar_subquery(),
            )
        )
    ).one()

    (
        customer_count,
        downloaded,
        activated,
        opted_out,
        links_sent,
        links_clicked,
        customers_with_link,
        from_ads,
        with_any_lead,
        conversation_count,
        conversations_open,
        message_count,
        inbound,
        outbound,
        ai_written,
        campaigns,
    ) = counts

    # Threads on a number we do not answer on. The pipeline logs an error when
    # one arrives, but a log nobody reads is not a signal - this puts the count
    # where the operator is already looking.
    configured = get_settings().whatsapp_phone_number_ids
    unanswerable = (
        (
            await session.execute(
                _excluding_tests(
                    select(
                    func.coalesce(
                        func.max(Conversation.display_phone_number),
                        Conversation.phone_number_id,
                    ),
                    func.count(Conversation.id),
                ),
                    Conversation.customer_id,
                    exclude,
                )
                .where(Conversation.phone_number_id.not_in(configured))
                .group_by(Conversation.phone_number_id)
            )
        ).all()
        if configured
        else []
    )

    leads_by_source = (
        await session.execute(
            _excluding_tests(
                select(Lead.source, func.count(Lead.id)), Lead.customer_id, exclude
            ).group_by(Lead.source)
        )
    ).all()

    return DashboardOverview(
        customers=customer_count,
        customers_downloaded=downloaded,
        customers_activated=activated,
        customers_opted_out=opted_out,
        customers_from_ads=from_ads,
        customers_direct=customer_count - with_any_lead,
        leads_by_source=[CountByKey(key=str(source), count=n) for source, n in leads_by_source],
        links_sent=links_sent,
        links_clicked=links_clicked,
        customers_with_link=customers_with_link,
        conversations=conversation_count,
        conversations_open=conversations_open,
        messages=message_count,
        messages_inbound=inbound,
        messages_outbound=outbound,
        messages_ai_generated=ai_written,
        campaigns=campaigns,
        unanswerable_by_number=[
            CountByKey(key=number, count=n) for number, n in unanswerable
        ],
        generated_at=utcnow(),
    )


def _customer_filter(
    stmt: Select, search: str | None, outcome: str | None, exclude=None
) -> Select:
    if search:
        term = f"%{search.strip()}%"
        stmt = stmt.where(or_(Customer.phone.ilike(term), Customer.full_name.ilike(term)))
    if outcome == "activated":
        stmt = stmt.where(Customer.activated_at.is_not(None))
    elif outcome == "downloaded":
        stmt = stmt.where(Customer.downloaded_at.is_not(None))
    elif outcome == "not_downloaded":
        stmt = stmt.where(Customer.downloaded_at.is_(None))
    elif outcome == "opted_out":
        stmt = stmt.where(Customer.opted_out_at.is_not(None))
    return _excluding_tests(stmt, Customer.id, exclude)


@router.get("/customers", response_model=CustomerPage)
async def customer_page(
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=64),
    outcome: str | None = Query(default=None),
    include_test: bool = Query(
        default=False,
        description="Include the team's own test numbers. Off by default.",
    ),
) -> CustomerPage:
    """One page of customers, with the facts the table shows.

    Related counts are fetched for the page's ids rather than per row: four
    bounded queries whatever the page size, instead of one query per customer.
    """
    # The row count rides along on the page query as a window function, so
    # paging costs one round trip rather than two.
    exclude = None if include_test else _test_customer_ids()

    page = (
        await session.execute(
            _customer_filter(
                select(Customer, func.count().over().label("total")),
                search,
                outcome,
                exclude,
            )
            .order_by(desc(Customer.created_at))
            .limit(limit)
            .offset(offset)
        )
    ).all()

    rows = [row[0] for row in page]
    if not rows:
        # An empty page carries no window value, so the count needs asking for.
        total = (
            await session.execute(
                _customer_filter(select(func.count(Customer.id)), search, outcome, exclude)
            )
        ).scalar_one()
        return CustomerPage(rows=[], total=total, limit=limit, offset=offset)

    total = page[0][1]

    ids = [row.id for row in rows]

    threads_by_customer: dict[uuid.UUID, list[Conversation]] = {}
    for thread in (
        await session.execute(select(Conversation).where(Conversation.customer_id.in_(ids)))
    ).scalars():
        threads_by_customer.setdefault(thread.customer_id, []).append(thread)

    origins: dict[uuid.UUID, tuple[str, str | None]] = {}
    for customer_id, source, campaign_name in (
        await session.execute(
            select(Lead.customer_id, Lead.source, Campaign.name)
            .outerjoin(Campaign, Lead.campaign_id == Campaign.id)
            .where(Lead.customer_id.in_(ids))
            .order_by(Lead.created_at)
        )
    ).all():
        # The first lead wins: it is the one that brought them in.
        origins.setdefault(customer_id, (str(source), campaign_name))

    message_counts = dict(
        (
            await session.execute(
                select(Conversation.customer_id, func.count(Message.id))
                .join(Message, Message.conversation_id == Conversation.id)
                .where(Conversation.customer_id.in_(ids))
                .group_by(Conversation.customer_id)
            )
        ).all()
    )

    out: list[CustomerRow] = []
    for customer in rows:
        threads = threads_by_customer.get(customer.id, [])
        source, campaign_name = origins.get(customer.id, ("direct", None))
        out.append(
            CustomerRow(
                id=customer.id,
                phone=customer.phone,
                full_name=customer.full_name,
                created_at=customer.created_at,
                downloaded_at=customer.downloaded_at,
                activated_at=customer.activated_at,
                opted_out_at=customer.opted_out_at,
                source=source,
                campaign_name=campaign_name,
                numbers=_numbers_written_to(threads),
                stage=max(
                    (thread.sales_stage for thread in threads),
                    key=lambda stage: _STAGE_ORDER.get(stage, 0),
                    default=None,
                ),
                last_activity_at=_latest(
                    *[thread.last_inbound_at for thread in threads],
                    *[thread.last_outbound_at for thread in threads],
                ),
                messages=message_counts.get(customer.id, 0),
            )
        )

    return CustomerPage(rows=out, total=total, limit=limit, offset=offset)


@router.get("/customers/{customer_id}", response_model=CustomerDetail)
async def customer_detail(
    customer_id: uuid.UUID, session: AsyncSession = Depends(get_db)
) -> CustomerDetail:
    """One customer with every thread and its messages, for the modal."""
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown customer")

    threads = list(
        (
            await session.execute(
                select(Conversation)
                .where(Conversation.customer_id == customer_id)
                .order_by(desc(Conversation.created_at))
            )
        ).scalars()
    )

    by_thread: dict[uuid.UUID, list[Message]] = {}
    if threads:
        for message in (
            await session.execute(
                select(Message)
                .where(Message.conversation_id.in_([thread.id for thread in threads]))
                .order_by(Message.created_at)
            )
        ).scalars():
            by_thread.setdefault(message.conversation_id, []).append(message)

    origin = (
        await session.execute(
            select(Lead.source, Campaign.name)
            .outerjoin(Campaign, Lead.campaign_id == Campaign.id)
            .where(Lead.customer_id == customer_id)
            .order_by(Lead.created_at)
            .limit(1)
        )
    ).first()

    links = list(
        (
            await session.execute(
                select(DownloadLink)
                .where(DownloadLink.customer_id == customer_id)
                .order_by(desc(DownloadLink.created_at))
            )
        ).scalars()
    )

    return CustomerDetail(
        id=customer.id,
        phone=customer.phone,
        full_name=customer.full_name,
        email=customer.email,
        locale=customer.locale,
        created_at=customer.created_at,
        downloaded_at=customer.downloaded_at,
        activated_at=customer.activated_at,
        opted_out_at=customer.opted_out_at,
        source=str(origin[0]) if origin else "direct",
        campaign_name=origin[1] if origin else None,
        numbers=_numbers_written_to(threads),
        conversations=[
            DashboardConversation(
                id=thread.id,
                status=str(thread.status),
                handling_mode=thread.handling_mode,
                sales_stage=thread.sales_stage,
                phone_number_id=thread.phone_number_id,
                number_label=thread_number(thread),
                created_at=thread.created_at,
                last_inbound_at=thread.last_inbound_at,
                last_outbound_at=thread.last_outbound_at,
                messages=[
                    DashboardMessage(
                        id=message.id,
                        direction=str(message.direction),
                        message_type=str(message.message_type),
                        status=str(message.status),
                        content=message.content,
                        ai_generated=message.ai_generated,
                        sent_by=message.sent_by,
                        created_at=message.created_at,
                        sent_at=message.sent_at,
                        delivered_at=message.delivered_at,
                        read_at=message.read_at,
                    )
                    for message in by_thread.get(thread.id, [])[-_MESSAGE_LIMIT:]
                ],
                truncated=len(by_thread.get(thread.id, [])) > _MESSAGE_LIMIT,
            )
            for thread in threads
        ],
        links=[
            DashboardLink(
                token=link.token,
                url=link.url,
                platform=link.platform,
                sent_at=link.sent_at,
                clicked_at=link.clicked_at,
                downloaded_at=link.downloaded_at,
                activated_at=link.activated_at,
                details=link.details,
            )
            for link in links
        ],
    )

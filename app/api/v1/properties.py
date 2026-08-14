"""Property endpoints (Section XII /properties).

GET  /api/v1/properties                       — list/filter properties
POST /api/v1/properties/identify              — pin drop -> existing | ambiguous | created
GET  /api/v1/properties/{property_id}         — canonical record + rating breakdown
GET  /api/v1/properties/{property_id}/rent-history
GET  /api/v1/properties/{property_id}/environment
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.ratelimit import registration_limit
from app.core import property_id as pid_core
from app.db.models import Agent, DataSource, FloodEvent, Property, RentHistory, Review
from app.db.session import get_session
from app.schemas.property import (
    EnvironmentOut,
    FloodEventOut,
    IdentifyRequest,
    IdentifyResponse,
    PropertyOut,
    RatingBreakdown,
    RentPointOut,
    SourceOut,
)
from app.services import identity

router = APIRouter(prefix="/properties", tags=["properties"])

_RATING_DIMS = (
    "landlord", "agent", "property", "water", "power",
    "security", "noise", "flooding", "neighbourhood", "value",
)


async def _agent_slugs(
    session: AsyncSession, property_pks: list[int]
) -> dict[int, str]:
    """Most-cited agent per property, from approved reviews. One query for all.

    The client used to hardcode a single slug for every property.
    """
    if not property_pks:
        return {}
    rows = (
        await session.execute(
            select(Review.property_id, Agent.slug, func.count().label("n"))
            .join(Agent, Agent.id == Review.agent_id)
            .where(
                Review.property_id.in_(property_pks),
                Review.moderation_status == "approved",
                Agent.slug.is_not(None),
            )
            .group_by(Review.property_id, Agent.slug)
            .order_by(Review.property_id, func.count().desc())
        )
    ).all()
    out: dict[int, str] = {}
    for property_pk, slug, _n in rows:
        out.setdefault(property_pk, slug)  # first row per property is the top one
    return out


def _to_out(prop: Property, agent_slug: str | None = None) -> PropertyOut:
    """Serialise a property whose `lga` and `neighbourhood` are already loaded.

    The rating breakdown is read from the cached column rather than recomputed:
    ``services.reviews.recompute_property_scores`` writes the same weighted
    figures that produce ``avg_rating``, so the bars and the headline score can
    no longer disagree.
    """
    out = PropertyOut.model_validate(prop)
    out.lga_name = prop.lga.name if prop.lga else None
    out.neighbourhood_name = prop.neighbourhood.name if prop.neighbourhood else None
    out.agent_slug = agent_slug
    cached = prop.rating_breakdown or {}
    out.rating_breakdown = RatingBreakdown(
        **{dim: cached.get(dim, 0.0) for dim in _RATING_DIMS}
    )
    return out


def _with_relations(stmt):
    """Eager-load the two lookup relationships so listing stays a single query."""
    return stmt.options(selectinload(Property.lga), selectinload(Property.neighbourhood))


@router.get("", response_model=list[PropertyOut])
async def list_properties(
    session: AsyncSession = Depends(get_session),
    q: str | None = Query(default=None, description="Address or PropertyID search"),
    lga: str | None = Query(default=None),
    area: str | None = Query(default=None),
    min_rating: float | None = Query(default=None, ge=0, le=5),
    flood_risk: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = 0,
) -> list[PropertyOut]:
    stmt = select(Property).where(Property.status == "active")
    if q and (term := q.strip()):
        # Matches the three things a tenant would actually type: the street
        # address, the area name, or a PropertyID from a listing or a friend.
        like = f"%{term.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Property.address_local).like(like),
                func.lower(Property.address_formal).like(like),
                func.lower(Property.property_id).like(like),
                func.lower(Property.neighbourhood_code).like(like),
            )
        )
    if lga:
        stmt = stmt.where(Property.lga_code == lga.upper())
    if area:
        stmt = stmt.where(Property.neighbourhood_code == area.upper())
    if min_rating is not None:
        stmt = stmt.where(Property.avg_rating >= min_rating)
    if flood_risk:
        stmt = stmt.where(Property.flood_zone == flood_risk)
    stmt = _with_relations(stmt).order_by(Property.total_reviews.desc())
    props = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    slugs = await _agent_slugs(session, [p.id for p in props])
    return [_to_out(p, slugs.get(p.id)) for p in props]


@router.post(
    "/identify",
    response_model=IdentifyResponse,
    # Open by design — see app/api/ratelimit.py — so it carries a quota.
    dependencies=[Depends(registration_limit)],
)
async def identify(
    payload: IdentifyRequest, session: AsyncSession = Depends(get_session)
) -> IdentifyResponse:
    return await identity.identify_or_create(
        session,
        lat=payload.lat,
        lng=payload.lng,
        lga_code=payload.lga_code,
        area_code=payload.area_code,
        address=payload.address,
        photo_hash=payload.photo_hash,
        location_approximate=payload.location_approximate,
    )


async def _get_or_404(
    session: AsyncSession, property_id: str, *, with_relations: bool = False
) -> Property:
    try:
        pid_core.parse(property_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    stmt = select(Property).where(Property.property_id == property_id.upper())
    if with_relations:
        stmt = _with_relations(stmt)
    prop = (await session.execute(stmt)).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=404, detail="Property not found")
    return prop


@router.get("/{property_id}", response_model=PropertyOut)
async def get_property(
    property_id: str, session: AsyncSession = Depends(get_session)
) -> PropertyOut:
    prop = await _get_or_404(session, property_id, with_relations=True)
    slugs = await _agent_slugs(session, [prop.id])
    return _to_out(prop, slugs.get(prop.id))


@router.get("/{property_id}/rent-history", response_model=list[RentPointOut])
async def rent_history(
    property_id: str, session: AsyncSession = Depends(get_session)
) -> list[RentPointOut]:
    prop = await _get_or_404(session, property_id)
    rows = (
        await session.execute(
            select(RentHistory)
            .where(RentHistory.property_id == prop.id)
            .order_by(RentHistory.period_year)
        )
    ).scalars().all()
    return [
        RentPointOut(year=r.period_year, rent_kobo=r.amount_kobo, area_avg_kobo=r.area_avg_kobo)
        for r in rows
    ]


@router.get("/{property_id}/sources", response_model=list[SourceOut])
async def property_sources(
    property_id: str, session: AsyncSession = Depends(get_session)
) -> list[SourceOut]:
    """Where each imported figure on this property came from.

    A platform that asks people to trust its numbers should be able to show its
    working. It also discharges the ODbL attribution requirement for anything
    derived from OpenStreetMap.
    """
    prop = await _get_or_404(session, property_id)
    rows = (
        await session.execute(
            select(DataSource)
            .where(
                DataSource.subject_type == "property",
                DataSource.subject_id == prop.property_id,
            )
            .order_by(DataSource.field)
        )
    ).scalars().all()
    return [
        SourceOut(
            field=r.field,
            source=r.source,
            licence=r.licence,
            url=r.url,
            fetched_at=r.fetched_at,
        )
        for r in rows
    ]


@router.get("/{property_id}/environment", response_model=EnvironmentOut)
async def environment(
    property_id: str, session: AsyncSession = Depends(get_session)
) -> EnvironmentOut:
    prop = await _get_or_404(session, property_id)
    events = (
        await session.execute(
            select(FloodEvent)
            .where(FloodEvent.property_id == prop.id)
            .order_by(FloodEvent.id)
        )
    ).scalars().all()
    return EnvironmentOut(
        flood_zone=prop.flood_zone,
        flood_report_count=len(events),
        elevation_m=float(prop.elevation_m) if prop.elevation_m is not None else None,
        drainage_dist_m=float(prop.drainage_dist_m) if prop.drainage_dist_m is not None else None,
        power_hours_avg=prop.power_hours_avg,
        flood_events=[
            FloodEventOut(
                when=e.when_label, severity=e.severity, quote=e.quote, evidence=e.evidence
            )
            for e in events
        ],
    )

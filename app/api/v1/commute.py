"""Commute endpoints (Section VII /commute).

The product's claim is that tenant-reported door-to-door times beat a routing
API's estimate for Lagos. So tenant reports are the source of truth here, and
anything we haven't actually measured is returned as null rather than filled in
with a plausible-looking number.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core import property_id as pid_core
from app.db.models import (
    CommuteDestination,
    CommuteReport,
    Neighbourhood,
    Property,
    TransitOption,
    User,
)
from app.db.session import get_session
from app.services import routing

router = APIRouter(tags=["commute"])

# Display order and labels for the departure windows we collect.
WINDOWS: dict[str, str] = {
    "am_rush": "Weekday morning rush",
    "midday": "Weekday midday",
    "pm_rush": "Weekday evening rush",
    "weekend": "Weekend",
}


class DestinationOut(BaseModel):
    code: str
    name: str


class WindowStat(BaseModel):
    window: str
    label: str
    typical_min: int          # median of tenant reports
    worst_min: int            # slowest reported
    best_min: int
    report_count: int


class TransitOut(BaseModel):
    kind: str
    label: str
    distance_m: int | None
    available: bool


class BottleneckOut(BaseModel):
    title: str
    detail: str


class CommuteOut(BaseModel):
    destination_code: str
    destination_name: str
    report_count: int
    # Null when nobody has reported this trip yet — the UI says so rather than
    # showing an invented figure.
    typical_min: int | None = None
    fastest_min: int | None = None
    slowest_min: int | None = None
    # A routing engine's drive time, for comparison against what tenants
    # report. Null when the provider is unreachable or there is no drivable
    # route — both mean "we don't know", and neither may render as a number.
    google_estimate_min: int | None = None
    # What kind of number that is: "traffic" (a provider's model of current
    # conditions, what a phone shows) or "free_flow" (the road network at its
    # speed limits — 4am with nobody about). They are not interchangeable and
    # the UI must not describe them with the same words: calling a free-flow
    # figure "what your maps app says" would be false, and would make the
    # tenant reports look absurd rather than informative.
    routing_kind: str | None = None
    routing_configured: bool = False
    by_window: list[WindowStat] = Field(default_factory=list)
    modes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    transit: list[TransitOut] = Field(default_factory=list)
    bottleneck: BottleneckOut | None = None


class CommuteReportIn(BaseModel):
    destination_code: str = Field(..., min_length=2, max_length=10)
    departure_window: str = Field(..., description="am_rush|midday|pm_rush|weekend")
    mode: str = Field(..., min_length=2, max_length=20)
    minutes: int = Field(..., gt=0, le=600)
    note: str | None = Field(default=None, max_length=280)


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return round((ordered[mid - 1] + ordered[mid]) / 2)


async def _get_property(session: AsyncSession, property_id: str) -> Property:
    try:
        pid_core.parse(property_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    prop = (
        await session.execute(
            select(Property).where(Property.property_id == property_id.upper())
        )
    ).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=404, detail="Property not found")
    return prop


@router.get("/commute/destinations", response_model=list[DestinationOut])
async def list_destinations(
    session: AsyncSession = Depends(get_session),
) -> list[DestinationOut]:
    rows = (
        await session.execute(
            select(CommuteDestination).order_by(
                CommuteDestination.sort_order, CommuteDestination.name
            )
        )
    ).scalars().all()
    return [DestinationOut(code=d.code, name=d.name) for d in rows]


@router.get("/properties/{property_id}/commute", response_model=CommuteOut)
async def property_commute(
    property_id: str,
    destination: str = Query(..., min_length=2, max_length=10),
    session: AsyncSession = Depends(get_session),
) -> CommuteOut:
    prop = await _get_property(session, property_id)
    dest_code = destination.upper()

    dest = (
        await session.execute(
            select(CommuteDestination).where(CommuteDestination.code == dest_code)
        )
    ).scalar_one_or_none()
    if dest is None:
        raise HTTPException(status_code=404, detail="Unknown destination")

    reports = (
        await session.execute(
            select(CommuteReport).where(
                CommuteReport.property_id == prop.id,
                CommuteReport.destination_code == dest_code,
            )
        )
    ).scalars().all()

    transit = (
        await session.execute(
            select(TransitOption)
            .where(TransitOption.property_id == prop.id)
            .order_by(TransitOption.available.desc(), TransitOption.distance_m)
        )
    ).scalars().all()

    hood = (
        await session.execute(
            select(Neighbourhood).where(
                Neighbourhood.code == prop.neighbourhood_code
            )
        )
    ).scalar_one_or_none()

    out = CommuteOut(
        destination_code=dest_code,
        destination_name=dest.name,
        report_count=len(reports),
        transit=[
            TransitOut(
                kind=t.kind,
                label=t.label,
                distance_m=t.distance_m,
                available=t.available,
            )
            for t in transit
        ],
        bottleneck=(
            BottleneckOut(title=hood.bottleneck_title, detail=hood.bottleneck_detail)
            if hood and hood.bottleneck_title and hood.bottleneck_detail
            else None
        ),
    )

    # A live routing estimate is fetched whether or not tenants have reported
    # this trip. It is deliberately kept separate from the reported figures
    # rather than substituting for them: the gap between what a maps app
    # predicts and what tenants actually experience is the finding, so
    # collapsing the two would destroy the only thing this number is for.
    if dest.lat is not None and dest.lng is not None:
        out.google_estimate_min, out.routing_kind = await routing.drive_estimate_min(
            (float(prop.lat), float(prop.lng)), (float(dest.lat), float(dest.lng))
        )
    out.routing_configured = routing.is_configured()

    if not reports:
        return out

    minutes = [r.minutes for r in reports]
    out.typical_min = _median(minutes)
    out.fastest_min = min(minutes)
    out.slowest_min = max(minutes)
    out.modes = sorted({r.mode for r in reports})
    out.notes = [r.note for r in reports if r.note][:3]

    # Estimates captured at report time, if any, are a better comparison than a
    # live lookup: they are what that tenant's phone said on that trip. Prefer
    # them, and fall back to the live figure fetched above.
    estimates = [r.google_estimate_min for r in reports if r.google_estimate_min]
    if estimates:
        out.google_estimate_min = _median(estimates)
        # Captured from the tenant's own phone at the time of the trip, so it
        # is a traffic figure whatever the live fallback happens to be.
        out.routing_kind = routing.TRAFFIC

    by_window: list[WindowStat] = []
    for key, label in WINDOWS.items():
        vals = [r.minutes for r in reports if r.departure_window == key]
        if vals:
            by_window.append(
                WindowStat(
                    window=key,
                    label=label,
                    typical_min=_median(vals),
                    worst_min=max(vals),
                    best_min=min(vals),
                    report_count=len(vals),
                )
            )
    out.by_window = by_window
    return out


@router.post(
    "/properties/{property_id}/commute", response_model=CommuteOut, status_code=201
)
async def add_commute_report(
    property_id: str,
    payload: CommuteReportIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CommuteOut:
    """Add your own commute time for this property."""
    prop = await _get_property(session, property_id)

    if payload.departure_window not in WINDOWS:
        raise HTTPException(
            status_code=422,
            detail=f"departure_window must be one of {sorted(WINDOWS)}",
        )
    dest_code = payload.destination_code.upper()
    exists = (
        await session.execute(
            select(func.count())
            .select_from(CommuteDestination)
            .where(CommuteDestination.code == dest_code)
        )
    ).scalar_one()
    if not exists:
        raise HTTPException(status_code=404, detail="Unknown destination")

    session.add(
        CommuteReport(
            property_id=prop.id,
            user_id=user.id,
            destination_code=dest_code,
            departure_window=payload.departure_window,
            mode=payload.mode,
            minutes=payload.minutes,
            note=payload.note,
        )
    )
    await session.commit()
    return await property_commute(property_id, dest_code, session)

"""Neighbourhood endpoints (Section XII /neighbourhoods)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Neighbourhood, Property, RentBenchmark, Review
from app.db.session import get_session

router = APIRouter(prefix="/neighbourhoods", tags=["neighbourhoods"])


class NeighbourhoodOut(BaseModel):
    code: str
    name: str
    lga_code: str | None
    avg_rent_1bed: int | None
    avg_rent_2bed: int | None
    avg_rent_3bed: int | None
    avg_rating: float | None
    avg_power_hours: int | None
    avg_security: float | None
    avg_agent_fee_pct: float | None
    commute_vi_min: int | None
    flood_risk: str | None
    total_properties: int
    total_reviews: int

    model_config = {"from_attributes": True}


class CompareOut(BaseModel):
    areas: list[NeighbourhoodOut]


async def _live_counts(session: AsyncSession) -> dict[str, tuple[int, int]]:
    """Property and approved-review counts per area, counted rather than cached.

    The stored `total_properties` / `total_reviews` columns were seeded to zero
    and never maintained, so anything reading them reported no activity.
    """
    rows = (
        await session.execute(
            select(
                Property.neighbourhood_code,
                func.count(func.distinct(Property.id)),
                func.count(Review.id),
            )
            .outerjoin(
                Review,
                (Review.property_id == Property.id)
                & (Review.moderation_status == "approved"),
            )
            .where(Property.status == "active")
            .group_by(Property.neighbourhood_code)
        )
    ).all()
    return {code: (props, reviews) for code, props, reviews in rows if code}


def _with_counts(
    n: Neighbourhood, counts: dict[str, tuple[int, int]]
) -> NeighbourhoodOut:
    out = NeighbourhoodOut.model_validate(n)
    props, reviews = counts.get(n.code, (0, 0))
    out.total_properties = props
    out.total_reviews = reviews
    return out


@router.get("", response_model=list[NeighbourhoodOut])
async def list_neighbourhoods(
    session: AsyncSession = Depends(get_session),
) -> list[NeighbourhoodOut]:
    rows = (
        await session.execute(select(Neighbourhood).order_by(Neighbourhood.name))
    ).scalars().all()
    counts = await _live_counts(session)
    return [_with_counts(n, counts) for n in rows]


@router.get("/compare", response_model=CompareOut)
async def compare(
    codes: str = Query(..., description="Comma-separated area codes, e.g. LEK,YAB"),
    session: AsyncSession = Depends(get_session),
) -> CompareOut:
    wanted = [c.strip().upper() for c in codes.split(",") if c.strip()][:3]
    if len(wanted) < 2:
        raise HTTPException(status_code=422, detail="Provide 2–3 area codes")
    rows = (
        await session.execute(
            select(Neighbourhood).where(Neighbourhood.code.in_(wanted))
        )
    ).scalars().all()
    by_code = {n.code: n for n in rows}
    missing = [c for c in wanted if c not in by_code]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown areas: {missing}")
    counts = await _live_counts(session)
    return CompareOut(areas=[_with_counts(by_code[c], counts) for c in wanted])


class RentBenchmarkOut(BaseModel):
    """Official rent inflation, for comparison against tenant reports."""

    # Null until the series has twelve months in it — a year-on-year figure
    # computed from less than a year is not a year-on-year figure.
    yoy_pct: float | None = None
    period_year: int | None = None
    period_month: int | None = None
    # National, because NBS does not publish the rent index by state. The UI has
    # to say so rather than let a reader assume it describes Lagos.
    scope: str = "national"
    source: str = "NBS Consumer Price Index — HOUSING (RENT) INDEX"
    url: str = "https://microdata.nigerianstat.gov.ng/index.php/catalog/154"


@router.get("/rent-benchmark", response_model=RentBenchmarkOut)
async def rent_benchmark(
    session: AsyncSession = Depends(get_session),
) -> RentBenchmarkOut:
    """The most recent official rent inflation figure.

    Everything else in this app reports what tenants paid. This is the one
    number that comes from outside, and its whole job is to give those reports
    something to be measured against.
    """
    row = (
        await session.execute(
            select(RentBenchmark)
            .where(RentBenchmark.scope == "national", RentBenchmark.yoy_pct.is_not(None))
            .order_by(RentBenchmark.period_year.desc(), RentBenchmark.period_month.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if row is None:
        return RentBenchmarkOut()
    return RentBenchmarkOut(
        yoy_pct=round(float(row.yoy_pct), 1),
        period_year=row.period_year,
        period_month=row.period_month,
    )

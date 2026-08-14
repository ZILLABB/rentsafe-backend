"""Address lookup for registering a property (Section II, step 1).

The identity flow (`POST /properties/identify`) needs a point plus an LGA and
area code. A tenant knows their address, not a geohash and not an internal area
code — so this bridges the two: search an address, get back candidate points
already resolved to the LGA and neighbourhood they fall in.

Without this the registration endpoint existed but was unreachable: the review
wizard could only pick from properties that were already in the database.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ratelimit import search_limit
from app.core import address as address_mod
from app.db.models import LGA, Neighbourhood
from app.db.session import get_session
from app.services import opendata

router = APIRouter(prefix="/places", tags=["places"])

# Beyond this a "nearest area" match stops being meaningful — better to say we
# don't cover it than to file someone's home under a suburb 12km away.
MAX_AREA_DISTANCE_M = 8_000


class ResolvedArea(BaseModel):
    lga_code: str | None
    lga_name: str | None
    area_code: str | None
    area_name: str | None
    distance_m: float | None


class PlaceOut(BaseModel):
    label: str
    lat: float
    lng: float
    road: str | None
    suburb: str | None
    resolved: ResolvedArea
    # How closely this hit matches what was typed: "exact" if the full address
    # matched, "street" if only the street did, "area" if only the
    # neighbourhood. Lagos street data in OSM is thin, so an area-level match is
    # the common case and the UI has to say so rather than imply a rooftop pin.
    precision: str = "exact"


async def resolve_area(
    session: AsyncSession, lat: float, lng: float
) -> ResolvedArea:
    """Map a point to the nearest known neighbourhood and its LGA.

    Nearest-centroid rather than point-in-polygon: we hold centroids, not
    boundaries. It's accurate enough to file an address under the right area,
    and the distance is returned so the caller can judge.
    """
    areas = (
        await session.execute(
            select(Neighbourhood).where(Neighbourhood.centroid_lat.is_not(None))
        )
    ).scalars().all()
    if not areas:
        return ResolvedArea(
            lga_code=None, lga_name=None, area_code=None, area_name=None,
            distance_m=None,
        )

    nearest = min(
        areas,
        key=lambda a: opendata.haversine_m(
            lat, lng, float(a.centroid_lat), float(a.centroid_lng)
        ),
    )
    distance = opendata.haversine_m(
        lat, lng, float(nearest.centroid_lat), float(nearest.centroid_lng)
    )
    if distance > MAX_AREA_DISTANCE_M:
        return ResolvedArea(
            lga_code=None, lga_name=None, area_code=None, area_name=None,
            distance_m=round(distance),
        )

    lga = None
    if nearest.lga_code:
        lga = (
            await session.execute(select(LGA).where(LGA.code == nearest.lga_code))
        ).scalar_one_or_none()

    return ResolvedArea(
        lga_code=nearest.lga_code,
        lga_name=lga.name if lga else None,
        area_code=nearest.code,
        area_name=nearest.name,
        distance_m=round(distance),
    )


async def _local_area(session: AsyncSession, q: str) -> Neighbourhood | None:
    """Find the point of a known Lagos neighbourhood named in the query.

    We already hold every Lagos neighbourhood with a centroid, so recognising
    "Magodo" inside "16 Salako Street Magodo Phase 1" needs no network call and
    beats asking the geocoder about a guessed suffix — "Phase 1" alone resolves
    to an estate in Eti-Osa, the wrong side of the city.

    Longest name wins, so "Lekki Phase 1" is preferred over "Lekki".
    """
    haystack = f" {address_mod.normalise(q)} "
    areas = (
        await session.execute(
            select(Neighbourhood).where(Neighbourhood.centroid_lat.is_not(None))
        )
    ).scalars().all()

    best: Neighbourhood | None = None
    for area in areas:
        name = address_mod.normalise(area.name)
        if len(name) < 3 or f" {name} " not in haystack:
            continue
        if best is None or len(name) > len(address_mod.normalise(best.name)):
            best = area
    return best


@router.get(
    "/search",
    response_model=list[PlaceOut],
    # Reaches Nominatim and writes to disk; both need a ceiling.
    dependencies=[Depends(search_limit)],
)
async def search_places(
    q: str = Query(..., min_length=3, description="Street, estate or landmark"),
    session: AsyncSession = Depends(get_session),
) -> list[PlaceOut]:
    """Find a Lagos address and resolve each hit to an LGA + area."""
    try:
        # Must be the async client: the blocking one holds the event loop for
        # the full round trip plus the 1s courtesy delay, stalling every other
        # request in the process.
        # Progressive rather than all-or-nothing. A single query for
        # "16 Salako Street Magodo" returns nothing — OSM has no house number
        # and no such street in that area — even though "Magodo" resolves fine.
        # Failing there told a tenant their own address didn't exist.
        known_area = await _local_area(session, q)
        anchor = (
            (float(known_area.centroid_lat), float(known_area.centroid_lng))
            if known_area
            else None
        )
        hits, precision = await opendata.geocode_progressive(q, anchor=anchor)
    except Exception as exc:  # network or upstream failure
        raise HTTPException(
            status_code=503,
            detail="Address lookup is unavailable right now. Try again shortly.",
        ) from exc

    if not hits and known_area:
        # OSM has nothing, but we do know the neighbourhood the tenant named.
        # Offering it beats a dead end: they can register at area precision and
        # their typed address is kept verbatim. Inventing a street would not be.
        hits = [
            {
                "label": known_area.name,
                "lat": anchor[0],
                "lng": anchor[1],
                "road": None,
                "suburb": known_area.name,
            }
        ]
        precision = "area"

    out = []
    for h in hits:
        out.append(
            PlaceOut(
                label=h["label"],
                lat=h["lat"],
                lng=h["lng"],
                road=h["road"],
                suburb=h["suburb"],
                resolved=await resolve_area(session, h["lat"], h["lng"]),
                precision=precision,
            )
        )
    return out


@router.get("/resolve", response_model=ResolvedArea)
async def resolve_point(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    session: AsyncSession = Depends(get_session),
) -> ResolvedArea:
    """Resolve a dropped pin to an LGA + area, for map-based registration."""
    return await resolve_area(session, lat, lng)

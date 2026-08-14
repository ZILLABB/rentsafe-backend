"""The Property Identity Service — Section II registration & dedup flow.

Given a pin drop it:

  1. Computes the geohash pair and the candidate PropertyID prefix.
  2. Finds existing properties within ``dedup_radius_m``. Candidate rows are
     pre-filtered by a geohash-5 prefix (a ~4.9km cell plus its neighbours would
     be needed for edge cases; at our radii a 5-char prefix over the pin's own
     cell catches everything within ~150m of the cell interior, and the exact
     haversine check below is authoritative). On PostgreSQL the same code runs
     unchanged; swapping in ST_DWithin is a production optimisation.
  3. If a photo pHash was supplied, also pulls properties within
     ``phash_radius_m`` and flags any within Hamming distance <= threshold.
  4. Decides: attach to an existing record, ask the user to disambiguate, or
     mint a brand-new Canonical Property Record with the next free sequence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import address as address_mod
from app.core import geohash, phash, property_id
from app.db.models import Property
from app.schemas.property import IdentifyResponse, PropertyCandidate

settings = get_settings()

_EARTH_R = 6_371_000.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(a))


@dataclass
class NearbyProperty:
    property_id: str
    distance_m: float
    address_formal: str | None
    address_normalised: str | None
    total_reviews: int
    avg_rating: float | None
    photo_hash: str | None


async def _query_nearby(
    session: AsyncSession, lat: float, lng: float, radius_m: float
) -> list[NearbyProperty]:
    """Exact radius search: coarse lat/lng bounding box in SQL, haversine in Python."""
    dlat = radius_m / 111_320.0
    dlng = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    stmt = select(Property).where(
        Property.status == "active",
        Property.lat.between(lat - dlat, lat + dlat),
        Property.lng.between(lng - dlng, lng + dlng),
    )
    rows = (await session.execute(stmt)).scalars().all()

    out: list[NearbyProperty] = []
    for p in rows:
        d = haversine_m(lat, lng, float(p.lat), float(p.lng))
        if d <= radius_m:
            out.append(
                NearbyProperty(
                    property_id=p.property_id,
                    distance_m=d,
                    address_formal=p.address_formal,
                    address_normalised=p.address_normalised,
                    total_reviews=p.total_reviews,
                    avg_rating=float(p.avg_rating) if p.avg_rating is not None else None,
                    photo_hash=p.photo_hash,
                )
            )
    out.sort(key=lambda n: n.distance_m)
    return out


def _phash_matches(
    photo_hash: str | None, candidates: list[NearbyProperty]
) -> set[str]:
    """PropertyIDs whose stored pHash is within Hamming threshold of the new photo."""
    if not photo_hash:
        return set()
    matches: set[str] = set()
    for c in candidates:
        if c.photo_hash and phash.is_same_building(photo_hash, c.photo_hash):
            matches.add(c.property_id)
    return matches


async def identify_or_create(
    session: AsyncSession,
    *,
    lat: float,
    lng: float,
    lga_code: str,
    area_code: str,
    address: str | None = None,
    photo_hash: str | None = None,
    location_approximate: bool = False,
) -> IdentifyResponse:
    """Run the full identity flow and return the resolution.

    ``location_approximate`` marks a point that is an area centroid rather than
    the building — the usual outcome for a Lagos street OSM hasn't mapped. Every
    tenant in that area then submits the *same* coordinate, so a zero-metre
    distance means "same neighbourhood", not "same building". Auto-attaching on
    distance there would quietly fold a whole neighbourhood into one record and
    hang one landlord's reviews on another's building.
    """
    # --- Step 5a: dedup radius lookup ---------------------------------------
    near = await _query_nearby(session, lat, lng, settings.dedup_radius_m)

    # --- Step 5b: wider pHash lookup (only if a photo was supplied) ----------
    phash_match_ids: set[str] = set()
    if photo_hash:
        wider = await _query_nearby(session, lat, lng, settings.phash_radius_m)
        phash_match_ids = _phash_matches(photo_hash, wider)
        known = {n.property_id for n in near}
        for w in wider:
            if w.property_id in phash_match_ids and w.property_id not in known:
                near.append(w)

    if near:
        candidates = [
            PropertyCandidate(
                property_id=n.property_id,
                distance_m=round(n.distance_m, 2),
                address_formal=n.address_formal,
                address_normalised=n.address_normalised,
                total_reviews=n.total_reviews,
                avg_rating=n.avg_rating,
                phash_match=n.property_id in phash_match_ids,
            )
            for n in near
        ]

        # A single very-close match (or a confident pHash hit) -> attach directly.
        if location_approximate:
            # Proximity proves nothing here, so require corroboration: a photo
            # of the same building, or the same normalised street address.
            typed = address_mod.normalise(address) if address else None
            confident = [
                c
                for c in candidates
                if c.phash_match
                or (typed and c.address_normalised and c.address_normalised == typed)
            ]
        else:
            confident = [
                c
                for c in candidates
                if c.phash_match or c.distance_m <= settings.dedup_radius_m
            ]
        if len(confident) == 1:
            return IdentifyResponse(
                match="existing",
                property_id=confident[0].property_id,
                candidates=candidates,
                message="Is this the property you're reviewing?",
            )

        # Otherwise ask the user to disambiguate (Step 5: same-compound case).
        return IdentifyResponse(
            match="ambiguous",
            candidates=candidates,
            message="We found nearby properties. Confirm which one, or add a new building.",
        )

    # --- No match: create a new Canonical Property Record -------------------
    new_pid = await create_property(
        session,
        lat=lat,
        lng=lng,
        lga_code=lga_code,
        area_code=area_code,
        address=address,
        photo_hash=photo_hash,
        location_approximate=location_approximate,
    )
    return IdentifyResponse(
        match="created",
        property_id=new_pid,
        message="New property registered.",
    )


async def create_property(
    session: AsyncSession,
    *,
    lat: float,
    lng: float,
    lga_code: str,
    area_code: str,
    address: str | None = None,
    photo_hash: str | None = None,
    location_approximate: bool = False,
) -> str:
    """Mint a new PropertyID (handling geohash-collision sequencing) and persist."""
    gh8, gh7 = geohash.encode_pair(lat, lng)
    bucket_prefix = property_id.prefix(lga_code, area_code, lat, lng)

    # Existing IDs sharing the same LGA-AREA-GPSHASH bucket -> pick next free seq.
    stmt = select(Property.property_id).where(
        Property.property_id.like(f"{bucket_prefix}-%")
    )
    existing_ids = [row[0] for row in (await session.execute(stmt)).all()]
    seq = property_id.next_seq(existing_ids)
    new_pid = property_id.format_property_id(
        lga_code, area_code, gh8[:6].upper(), seq
    )

    prop = Property(
        property_id=new_pid,
        geohash_7=gh7,
        geohash_8=gh8,
        lat=lat,
        lng=lng,
        location_wkt=f"POINT({lng} {lat})",
        location_precision="area" if location_approximate else "exact",
        address_formal=address,
        # `address_local` is what every screen displays. Leaving it null meant a
        # freshly registered property showed its PropertyID where its address
        # should be. The two diverge later — a formal address can be corrected
        # while the local one stays how tenants actually refer to the place.
        address_local=address,
        address_normalised=address_mod.normalise(address) if address else None,
        lga_code=lga_code.upper(),
        neighbourhood_code=area_code.upper(),
        photo_hash=photo_hash,
    )
    session.add(prop)
    await session.commit()
    return new_pid

#!/usr/bin/env python
"""Import real Lagos reference data from open datasets.

    python -m scripts.import_reference_data --what all
    python -m scripts.import_reference_data --what elevation --offline
    python -m scripts.import_reference_data --what all --dry-run

What this imports, and why it's allowed to:

  LGAs, neighbourhoods, transit   OpenStreetMap via Overpass    ODbL 1.0
  Ground elevation                Open-Elevation (SRTM)         public domain
  Flood banding                   derived from elevation        NIHSA thresholds

What it deliberately does NOT import:

  Rents      Listing sites publish *asking* prices set by agents. RentSafe
             exists because that number and the number tenants actually pay
             diverge — importing it would launder the distortion the product
             is trying to expose. Their terms also forbid scraping. Rent comes
             from tenant reports only.
  Reviews    First-party by definition. Sourcing opinions about landlords from
             elsewhere would be both defamation exposure and a lie about
             provenance.
  Commutes   No open dataset of real door-to-door Lagos times exists. A routing
             API gives you its model's estimate, which is the thing tenant
             reports are meant to correct, so it can only ever be a comparison
             column — never the primary figure.

Every write records provenance in `data_sources`, and responses are cached
under backend/data/cache so runs are reproducible and CI never needs network.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    LGA,
    DataSource,
    Neighbourhood,
    Property,
    TransitOption,
)
from app.db.session import SessionLocal
from app.services import opendata

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("import")

OSM = ("OpenStreetMap", "ODbL 1.0", "https://www.openstreetmap.org/copyright")
SRTM = (
    "Open-Elevation (SRTM)",
    "public domain",
    "https://open-elevation.com",
)


async def _record(
    session: AsyncSession,
    subject_type: str,
    subject_id: str,
    field: str,
    source: tuple[str, str, str],
) -> None:
    """Replace any prior provenance for this field, then record the new one."""
    await session.execute(
        delete(DataSource).where(
            DataSource.subject_type == subject_type,
            DataSource.subject_id == subject_id,
            DataSource.field == field,
        )
    )
    session.add(
        DataSource(
            subject_type=subject_type,
            subject_id=subject_id,
            field=field,
            source=source[0],
            licence=source[1],
            url=source[2],
            fetched_at=dt.datetime.now(dt.UTC),
        )
    )


async def import_lgas(session: AsyncSession, *, offline: bool, dry_run: bool) -> int:
    """Refresh LGA names and centroids from OSM admin boundaries."""
    rows = opendata.lagos_lgas(offline=offline)
    log.info("OSM returned %d Lagos LGAs", len(rows))

    existing = {
        lga.name.lower(): lga
        for lga in (await session.execute(select(LGA))).scalars().all()
    }
    matched = 0
    for row in rows:
        # OSM writes "Ajeromi/Ifelodun"; the seed uses "Ajeromi-Ifelodun".
        key = row["name"].lower().replace("/", "-").replace(" ", "-")
        lga = existing.get(row["name"].lower()) or existing.get(key)
        if lga is None:
            continue
        matched += 1
        if not dry_run:
            # The centroid is what lets a dropped pin resolve to an LGA.
            lga.centroid_lat = row["lat"]
            lga.centroid_lng = row["lng"]
            await _record(session, "lga", lga.code, "centroid", OSM)
    log.info("  matched %d/%d against seeded LGAs", matched, len(rows))
    return matched


def _area_code(name: str, taken: set[str]) -> str:
    """A short, stable code for a neighbourhood (PropertyIDs embed it)."""
    letters = "".join(ch for ch in name.upper() if ch.isalpha())[:3] or "XXX"
    code = letters
    n = 1
    while code in taken:
        n += 1
        code = f"{letters[:2]}{n}"
    return code


async def import_neighbourhoods(
    session: AsyncSession, *, offline: bool, dry_run: bool
) -> int:
    """Import Lagos suburbs from OSM so addresses across the city can resolve.

    Six hand-written areas covered almost none of Lagos, which meant a tenant
    outside those six had no area to register their building under.
    """
    rows = opendata.lagos_neighbourhoods(offline=offline)
    log.info("OSM returned %d Lagos neighbourhoods", len(rows))

    existing = (await session.execute(select(Neighbourhood))).scalars().all()
    by_name = {n.name.lower(): n for n in existing}
    taken = {n.code for n in existing}

    lgas = (await session.execute(select(LGA))).scalars().all()

    added = 0
    for row in rows:
        if row["name"].lower() in by_name:
            continue
        # Attach to the nearest LGA centroid we know about.
        nearest = min(
            (lga for lga in lgas if lga.centroid_lat is not None),
            key=lambda lga: opendata.haversine_m(
                row["lat"], row["lng"], float(lga.centroid_lat), float(lga.centroid_lng)
            ),
            default=None,
        )
        code = _area_code(row["name"], taken)
        taken.add(code)
        if not dry_run:
            session.add(
                Neighbourhood(
                    code=code,
                    name=row["name"],
                    lga_code=nearest.code if nearest else None,
                    centroid_lat=row["lat"],
                    centroid_lng=row["lng"],
                )
            )
            await _record(session, "neighbourhood", code, "name", OSM)
        added += 1

    log.info("  added %d new areas (kept %d existing)", added, len(existing))
    return added


async def import_elevation(
    session: AsyncSession, *, offline: bool, dry_run: bool
) -> int:
    """Replace hand-written elevations with measured ground height.

    Elevation drives the flood banding, which is the single most consequential
    number on a property page — it belongs to a measurement, not an estimate.
    """
    props = (
        await session.execute(select(Property).order_by(Property.id))
    ).scalars().all()
    if not props:
        log.info("No properties to update")
        return 0

    points = [(float(p.lat), float(p.lng)) for p in props]
    values = opendata.elevations(points, offline=offline)
    if len(values) != len(props):
        log.warning("Elevation API returned %d values for %d points", len(values), len(props))
        return 0

    changed = 0
    for prop, elevation in zip(props, values, strict=True):
        before_elev = float(prop.elevation_m) if prop.elevation_m is not None else None
        before_zone = prop.flood_zone
        zone = opendata.flood_zone_from_elevation(
            elevation,
            float(prop.drainage_dist_m) if prop.drainage_dist_m is not None else None,
        )
        log.info(
            "  %s  %5.1fm -> %5.1fm   flood %-9s -> %-9s%s",
            prop.property_id,
            before_elev if before_elev is not None else -1,
            elevation,
            before_zone,
            zone,
            "  (changed)" if zone != before_zone else "",
        )
        if not dry_run:
            prop.elevation_m = elevation
            prop.flood_zone = zone
            await _record(session, "property", prop.property_id, "elevation_m", SRTM)
            await _record(session, "property", prop.property_id, "flood_zone", SRTM)
        changed += 1
    return changed


async def import_transit(
    session: AsyncSession, *, offline: bool, dry_run: bool
) -> int:
    """Replace hand-written transit lists with mapped stops near each property."""
    props = (
        await session.execute(select(Property).order_by(Property.id))
    ).scalars().all()
    total = 0
    for prop in props:
        key = f"osm_transit_{prop.property_id}"
        try:
            found = opendata.transit_near(
                float(prop.lat), float(prop.lng), key=key, offline=offline
            )
        except (opendata.OpenDataError, httpx.HTTPError) as exc:
            # One unreachable property shouldn't abandon the whole import; the
            # cache means a re-run picks up only what's still missing.
            log.warning("  %s: skipped — %s", prop.property_id, str(exc)[:90])
            continue

        log.info("  %s: %d mapped stops", prop.property_id, len(found))
        if dry_run or not found:
            continue

        await session.execute(
            delete(TransitOption).where(TransitOption.property_id == prop.id)
        )
        for f in found[:6]:
            session.add(
                TransitOption(
                    property_id=prop.id,
                    kind=f["kind"],
                    label=f["name"],
                    distance_m=f["distance_m"],
                    available=True,
                )
            )
        await _record(session, "property", prop.property_id, "transit", OSM)
        total += len(found[:6])
    return total


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--what",
        choices=["all", "lgas", "areas", "elevation", "transit"],
        default="all",
    )
    ap.add_argument(
        "--offline",
        action="store_true",
        help="Use only the committed cache; never hit the network.",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Report what would change, write nothing."
    )
    args = ap.parse_args()

    async with SessionLocal() as session:
        if args.what in ("all", "lgas"):
            log.info("\n== LGAs (OpenStreetMap, ODbL) ==")
            await import_lgas(session, offline=args.offline, dry_run=args.dry_run)

        if args.what in ("all", "areas"):
            log.info("\n== Neighbourhoods (OpenStreetMap, ODbL) ==")
            await import_neighbourhoods(
                session, offline=args.offline, dry_run=args.dry_run
            )

        if args.what in ("all", "elevation"):
            log.info("\n== Elevation & flood banding (SRTM, public domain) ==")
            await import_elevation(session, offline=args.offline, dry_run=args.dry_run)

        if args.what in ("all", "transit"):
            log.info("\n== Transit (OpenStreetMap, ODbL) ==")
            await import_transit(session, offline=args.offline, dry_run=args.dry_run)

        if args.dry_run:
            log.info("\nDry run — nothing written.")
            await session.rollback()
        else:
            await session.commit()
            log.info("\nCommitted. Provenance recorded in data_sources.")


if __name__ == "__main__":
    asyncio.run(main())

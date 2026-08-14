"""Idempotent dev/demo seed — Lagos reference data + flagship properties.

Runs at startup when the database is empty (see app.main). Mirrors the design
doc's realistic Lagos data so the frontend renders real API responses.
"""

from __future__ import annotations

import datetime as dt
import random

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import geohash, security
from app.core.scoring import DIMENSIONS
from app.db.models import (
    LGA,
    Agent,
    CommuteDestination,
    CommuteReport,
    FloodEvent,
    Neighbourhood,
    Property,
    RentHistory,
    Review,
    TransitOption,
    User,
)
from app.services.reviews import recompute_property_scores

# Review copy pools, indexed by rough score band. Kept realistic to Lagos
# tenancy: water, power, drainage, agent fees, service charge, gate security.
_POSITIVES = {
    "high": [
        "Borehole runs clean and constant, and the estate actually enforces gate passes.",
        "Landlord fixed the pump within days each time it failed. Service charge is accounted for.",
        "Quiet street, good neighbours, and the generator is shared fairly on a proper rota.",
        "Compound is well kept and the drainage was upgraded before I moved in.",
    ],
    "mid": [
        "Decent space for the money and the neighbourhood is convenient for work.",
        "Water supply is reliable. Power is average for the area but the estate gen helps.",
        "Landlord is reachable, though repairs take a couple of weeks.",
    ],
    "low": [
        "The location is convenient and transport is easy to find.",
        "Rooms are a reasonable size and rent is below the area average.",
    ],
}
_WARNINGS = {
    "high": [
        "Renewal notice came late — ask for it in writing three months ahead.",
        "Parking is tight if you have more than one car.",
        "Negotiate the agent fee. I got mine down by asking for a receipt upfront.",
    ],
    "mid": [
        "Power averages well below what the agent quoted. Budget for diesel.",
        "The street pools after heavy rain — passable, but wear boots in October.",
        "Service charge went up twice with no breakdown provided.",
    ],
    "low": [
        "The street floods badly every rainy season and it damaged our furniture.",
        "Rent went up sharply at renewal with barely any notice.",
        "Power is unreliable and the estate generator is often out of diesel.",
        "Security at the gate is inconsistent — there were break-ins on our row.",
    ],
}
_DISPLAY_NAMES = ["Tola A.", "Chidinma A.", "Anonymous tenant", "Bola O.", "Emeka N."]

LGAS: list[tuple[str, str, str]] = [
    ("AGE", "Agege", "Moderate"),
    ("AJE", "Ajeromi-Ifelodun", "High"),
    ("ALI", "Alimosho", "High"),
    ("AMU", "Amuwo-Odofin", "VeryHigh"),
    ("APA", "Apapa", "High"),
    ("BAD", "Badagry", "Moderate"),
    ("EPE", "Epe", "Moderate"),
    ("ETI", "Eti-Osa", "VeryHigh"),
    ("IBE", "Ibeju-Lekki", "High"),
    ("IFA", "Ifako-Ijaiye", "Low"),
    ("IKE", "Ikeja", "Low"),
    ("IKO", "Ikorodu", "Moderate"),
    ("KOS", "Kosofe", "VeryHigh"),
    ("LAG", "Lagos Island", "High"),
    ("LAM", "Lagos Mainland", "Moderate"),
    ("MUS", "Mushin", "Moderate"),
    ("OJO", "Ojo", "High"),
    ("OSH", "Oshodi-Isolo", "Moderate"),
    ("SHO", "Shomolu", "Moderate"),
    ("SUR", "Surulere", "Moderate"),
]

NEIGHBOURHOODS = [
    # code, name, lga, lat, lng, rent2bed(kobo), rating, power, security, fee%, vi_min, flood
    ("LEK", "Lekki Phase 1", "ETI", 6.4478, 3.4723, 280_000_000, 3.9, 15, 4.1, 11.2, 35, "High"),
    ("YAB", "Yaba", "LAM", 6.5095, 3.3711, 120_000_000, 3.6, 11, 3.8, 9.8, 75, "Moderate"),
    ("JAK", "Jakande", "ETI", 6.4568, 3.5342, 48_000_000, 2.9, 9, 3.2, 12.5, 55, "VeryHigh"),
    ("OGU", "Ogudu", "KOS", 6.5621, 3.3895, 90_000_000, 3.2, 10, 3.5, 10.4, 65, "VeryHigh"),
    ("IKJ", "Ikeja GRA", "IKE", 6.5764, 3.3554, 250_000_000, 4.1, 18, 4.4, 10.0, 70, "Low"),
    ("SUR", "Surulere Central", "SUR", 6.4926, 3.3549, 95_000_000, 3.4, 12, 3.6, 10.8, 50, "Moderate"),
]

# Corridor risk, per area (Section VII).
BOTTLENECKS = {
    "LEK": (
        "Single-corridor risk: Lekki–Epe Expressway",
        (
            "Almost all traffic out of Lekki Phase 1 funnels onto the Lekki–Epe "
            "Expressway and over the Lekki–Ikoyi link. One incident on either "
            "can gridlock the whole area for hours."
        ),
    ),
    "JAK": (
        "Single-corridor risk: Lekki–Epe Expressway",
        (
            "Ilasan and Jakande sit behind the same expressway with no "
            "practical alternative route westward."
        ),
    ),
    "SUR": (
        "Bridge dependency: Eko and Carter",
        (
            "Journeys to the Island depend on Eko or Carter bridge. Maintenance "
            "closures on either add an hour or more each way."
        ),
    ),
}

# Work destinations tenants commute to. code, name, lat, lng.
DESTINATIONS = [
    ("VI", "Victoria Island", 6.4281, 3.4219),
    ("IKY", "Ikoyi", 6.4550, 3.4350),
    ("YAB", "Yaba", 6.5095, 3.3711),
    ("IKJ", "Ikeja GRA", 6.5764, 3.3554),
    ("APA", "Apapa", 6.4488, 3.3596),
    ("LIS", "Lagos Island CBD", 6.4541, 3.3947),
]

# Public transport within reach, per property.
TRANSIT = {
    "ETI-LEK-7F3A2B-0041": [
        ("brt", "BRT stop · Admiralty", 400, True),
        ("keke", "Keke accessible", None, True),
        ("ferry", "Ferry terminal · Ikoyi", 2100, True),
        ("rail", "Rail station", None, False),
    ],
    "ETI-LEK-9C1D4E-0007": [
        ("bus", "Danfo stop · Fola Osibo", 250, True),
        ("keke", "Keke accessible", None, True),
        ("rail", "Rail station", None, False),
    ],
    "ETI-JAK-2B8F1A-0113": [
        ("bus", "Danfo stop · Ilasan gate", 150, True),
        ("keke", "Keke accessible", None, True),
        ("brt", "BRT stop", None, False),
    ],
    "LAM-YAB-Q8MN4C-0007": [
        ("rail", "Yaba rail station · 900m", 900, True),
        ("brt", "BRT stop · Herbert Macaulay", 300, True),
        ("bus", "Danfo stop", 120, True),
    ],
    "SUR-SUR-M2KP7D-0019": [
        ("brt", "BRT stop · Ojuelegba", 850, True),
        ("bus", "Danfo stop · Adelabu", 200, True),
        ("ferry", "Ferry terminal", None, False),
    ],
    "IKE-IKJ-T5RB2N-0003": [
        ("rail", "Ikeja rail station · 1.8km", 1800, True),
        ("brt", "BRT stop · Ikeja Along", 1200, True),
        ("keke", "Keke accessible", None, True),
    ],
}

# Tenant-reported commute times, by property. Each entry is
# (destination, window, mode, minutes, note). These are the product's whole
# point: lived door-to-door times, not a routing API's guess.
COMMUTES = {
    "ETI-LEK-7F3A2B-0041": [
        ("VI", "am_rush", "car", 95, "Leave before 6:20 or it doubles."),
        ("VI", "am_rush", "car", 105, None),
        ("VI", "am_rush", "bus", 120, "Danfo to Obalende then walk."),
        ("VI", "midday", "car", 30, None),
        ("VI", "pm_rush", "car", 115, "Friday evenings are the worst."),
        ("VI", "weekend", "car", 25, None),
        ("IKY", "am_rush", "car", 55, None),
        ("IKY", "midday", "car", 22, None),
        ("IKJ", "am_rush", "car", 150, "Only do this trip if you must."),
    ],
    "ETI-LEK-9C1D4E-0007": [
        ("VI", "am_rush", "car", 80, None),
        ("VI", "am_rush", "car", 72, "Back roads through Ikate help."),
        ("VI", "midday", "car", 28, None),
        ("VI", "pm_rush", "car", 95, None),
        ("IKY", "am_rush", "car", 45, None),
    ],
    "ETI-JAK-2B8F1A-0113": [
        ("VI", "am_rush", "bus", 140, "Two danfos and a keke."),
        ("VI", "am_rush", "car", 125, None),
        ("VI", "pm_rush", "bus", 160, "Standing room only after 5."),
        ("VI", "midday", "car", 45, None),
    ],
    "LAM-YAB-Q8MN4C-0007": [
        ("VI", "am_rush", "car", 85, "Third Mainland decides your morning."),
        ("VI", "am_rush", "rail", 60, "Train is slower to board but predictable."),
        ("VI", "midday", "car", 35, None),
        ("LIS", "am_rush", "car", 55, None),
        ("IKJ", "am_rush", "car", 50, None),
        ("IKJ", "midday", "rail", 40, None),
    ],
    "SUR-SUR-M2KP7D-0019": [
        ("VI", "am_rush", "car", 70, "Eko bridge, leave by 6:30."),
        ("VI", "am_rush", "brt", 80, None),
        ("VI", "pm_rush", "car", 90, None),
        ("LIS", "am_rush", "car", 40, None),
        ("YAB", "am_rush", "car", 25, None),
    ],
    "IKE-IKJ-T5RB2N-0003": [
        ("IKJ", "am_rush", "car", 12, "Work is in the same GRA."),
        ("VI", "am_rush", "car", 110, "Only worth it if you leave at 5:45."),
        ("VI", "midday", "car", 55, None),
        ("YAB", "am_rush", "car", 45, None),
    ],
}

PROPERTIES = [
    {
        "property_id": "ETI-LEK-7F3A2B-0041",
        "lat": 6.4474, "lng": 3.4736,
        "address": "12A Admiralty Way, Lekki Phase 1",
        "lga": "ETI", "area": "LEK",
        "type": "flat", "bedrooms": 3,
        "w3w": "///lofty.wings.pledge",
        "flood_zone": "High", "elevation_m": 2.4, "drainage_dist_m": 60,
        "rating": 3.7, "total_reviews": 23, "verified_reviews": 14,
        "rent": 175_000_000, "velocity": 87, "area_velocity": 54, "percentile": 88,
        "power": 12, "security": 4.2, "turnover": True, "traffic": "red",
        "history": [
            (2022, 93_500_000, 96_000_000),
            (2023, 118_000_000, 108_000_000),
            (2024, 150_000_000, 128_000_000),
            (2025, 175_000_000, 148_000_000),
        ],
        "floods": [
            ("OCT 2024", "major", "Major flooding, cars submerged on the street", "video"),
            ("OCT 2023", "moderate", "Ankle-deep, lasted 3 days", "photo"),
            ("JUL 2022", "minor", "Minor pooling in the compound", None),
        ],
    },
    {
        "property_id": "ETI-LEK-9C1D4E-0007",
        "lat": 6.4442, "lng": 3.4681,
        "address": "4 Fola Osibo Road, Lekki Phase 1",
        "lga": "ETI", "area": "LEK",
        "type": "flat", "bedrooms": 2,
        "w3w": None,
        "flood_zone": "Moderate", "elevation_m": 3.8, "drainage_dist_m": 180,
        "rating": 4.3, "total_reviews": 11, "verified_reviews": 9,
        "rent": 120_000_000, "velocity": 50, "area_velocity": 54, "percentile": 42,
        "power": 17, "security": 4.0, "turnover": False, "traffic": "yellow",
        "history": [
            (2022, 80_000_000, 96_000_000),
            (2023, 92_000_000, 108_000_000),
            (2024, 105_000_000, 128_000_000),
            (2025, 120_000_000, 148_000_000),
        ],
        "floods": [
            ("OCT 2023", "minor", "Brief pooling near the gate, drained same day", None),
        ],
    },
    {
        "property_id": "LAM-YAB-Q8MN4C-0007",
        "lat": 6.5095, "lng": 3.3711,
        "address": "23 Herbert Macaulay Way, Yaba",
        "lga": "LAM", "area": "YAB",
        "type": "flat", "bedrooms": 2,
        "w3w": None,
        "flood_zone": "Moderate", "elevation_m": 12.0, "drainage_dist_m": 220,
        "rating": 4.1, "total_reviews": 14, "verified_reviews": 10,
        "rent": 120_000_000, "velocity": 41, "area_velocity": 45, "percentile": 48,
        "power": 16, "security": 3.9, "turnover": False, "traffic": "yellow",
        "history": [
            (2022, 85_000_000, 82_000_000),
            (2023, 95_000_000, 94_000_000),
            (2024, 108_000_000, 106_000_000),
            (2025, 120_000_000, 119_000_000),
        ],
        "floods": [
            ("SEP 2023", "minor", "Gutter overflow after heavy rain, cleared overnight", None),
        ],
    },
    {
        "property_id": "SUR-SUR-M2KP7D-0019",
        "lat": 6.4926, "lng": 3.3549,
        "address": "14 Adelabu Street, Surulere",
        "lga": "SUR", "area": "SUR",
        "type": "flat", "bedrooms": 3,
        "w3w": None,
        "flood_zone": "Moderate", "elevation_m": 18.0, "drainage_dist_m": 300,
        "rating": 3.4, "total_reviews": 9, "verified_reviews": 6,
        "rent": 95_000_000, "velocity": 58, "area_velocity": 51, "percentile": 61,
        "power": 12, "security": 3.6, "turnover": False, "traffic": "yellow",
        "history": [
            (2022, 60_000_000, 62_000_000),
            (2023, 72_000_000, 71_000_000),
            (2024, 84_000_000, 80_000_000),
            (2025, 95_000_000, 90_000_000),
        ],
        "floods": [],
    },
    {
        "property_id": "IKE-IKJ-T5RB2N-0003",
        "lat": 6.5764, "lng": 3.3554,
        "address": "7 Oduduwa Crescent, Ikeja GRA",
        "lga": "IKE", "area": "IKJ",
        "type": "duplex", "bedrooms": 4,
        "w3w": None,
        "flood_zone": "Low", "elevation_m": 39.0, "drainage_dist_m": 500,
        "rating": 4.5, "total_reviews": 7, "verified_reviews": 6,
        "rent": 450_000_000, "velocity": 36, "area_velocity": 40, "percentile": 66,
        "power": 19, "security": 4.6, "turnover": False, "traffic": "green",
        "history": [
            (2022, 330_000_000, 300_000_000),
            (2023, 360_000_000, 330_000_000),
            (2024, 400_000_000, 370_000_000),
            (2025, 450_000_000, 410_000_000),
        ],
        "floods": [],
    },
    {
        "property_id": "ETI-JAK-2B8F1A-0113",
        "lat": 6.4568, "lng": 3.5342,
        "address": "Block C, Ilasan Housing Estate, Jakande",
        "lga": "ETI", "area": "JAK",
        "type": "flat", "bedrooms": 2,
        "w3w": None,
        "flood_zone": "VeryHigh", "elevation_m": 1.9, "drainage_dist_m": 40,
        "rating": 2.2, "total_reviews": 8, "verified_reviews": 5,
        "rent": 48_000_000, "velocity": 60, "area_velocity": 47, "percentile": 51,
        "power": 9, "security": 3.1, "turnover": True, "traffic": "red",
        "history": [
            (2022, 30_000_000, 34_000_000),
            (2023, 36_000_000, 39_000_000),
            (2024, 42_000_000, 44_000_000),
            (2025, 48_000_000, 50_000_000),
        ],
        "floods": [
            ("OCT 2024", "major", "Whole estate access road under water for a week", "video"),
            ("JUN 2023", "major", "Ground-floor flats flooded, property damaged", "photo"),
        ],
    },
]


def _band(score: float) -> str:
    return "high" if score >= 4.0 else "mid" if score >= 3.0 else "low"


def _generate_reviews(p: dict) -> list[dict]:
    """Build a review set whose weighted mean lands near the property's target.

    Deterministic (seeded per property) so the demo data is stable across runs
    while still varying between properties. The flagship keeps its two
    hand-written reviews on top of these.
    """
    target = p["rating"]
    count = p["total_reviews"]
    verified_count = p["verified_reviews"]
    rng = random.Random(p["property_id"])
    today = dt.datetime.now(dt.UTC).date()

    out: list[dict] = []
    for i in range(count):
        # Spread ratings around the target, clamped to the 1-5 scale.
        base = min(5.0, max(1.0, rng.gauss(target, 0.55)))
        ratings = {}
        for dim in DIMENSIONS:
            v = base
            # Dimensions the property record already tells us about track it.
            if dim == "power" and p["power"] is not None:
                v = 1 + (p["power"] / 24) * 4
            elif dim == "security" and p["security"] is not None:
                v = p["security"]
            elif dim == "flooding":
                v = {"VeryHigh": 1.4, "High": 2.1, "Moderate": 3.4, "Low": 4.6}[
                    p["flood_zone"]
                ]
            ratings[dim] = int(min(5, max(1, round(rng.gauss(v, 0.5)))))

        # Spread over the last three years so the recency weighting in
        # scoring.py has something to actually do. Each property gets its own
        # starting offset so the activity feed doesn't show every property's
        # newest review landing on the same day.
        offset_days = rng.randint(0, 21)
        months_ago = int(i * (34 / max(count - 1, 1)))
        created = today - dt.timedelta(days=30 * months_ago + offset_days)
        start = created - dt.timedelta(days=rng.randint(400, 900))
        still_living = i == 0
        band = _band(base)

        out.append(
            {
                "start": start,
                "end": None if still_living else created - dt.timedelta(days=20),
                "rent": int(p["rent"] * rng.uniform(0.72, 1.0)),
                "ratings": ratings,
                "positives": rng.choice(_POSITIVES[band]),
                "warnings": rng.choice(_WARNINGS[band]),
                "tier": 3 if i < verified_count else 1,
                "verified": i < verified_count,
                "anonymous": i % 4 == 0,
                "display_name": _DISPLAY_NAMES[i % len(_DISPLAY_NAMES)],
                "created_at": dt.datetime.combine(
                    created, dt.time(12, 0), tzinfo=dt.UTC
                ),
            }
        )
    return out


async def seed_if_empty(session: AsyncSession) -> bool:
    """Populate reference + demo data when the DB is empty. Returns True if seeded."""
    count = (await session.execute(select(func.count()).select_from(LGA))).scalar_one()
    if count:
        return False

    for code, name, risk in LGAS:
        session.add(LGA(code=code, name=name, flood_risk=risk))

    for code, name, lga, lat, lng, rent2, rating, power, sec, fee, vi, flood in NEIGHBOURHOODS:
        session.add(
            Neighbourhood(
                code=code, name=name, lga_code=lga,
                centroid_lat=lat, centroid_lng=lng,
                avg_rent_1bed=int(rent2 * 0.6),
                avg_rent_2bed=rent2,
                avg_rent_3bed=int(rent2 * 1.45),
                avg_rating=rating, avg_power_hours=power, avg_security=sec,
                avg_agent_fee_pct=fee, commute_vi_min=vi, flood_risk=flood,
                bottleneck_title=BOTTLENECKS.get(code, (None, None))[0],
                bottleneck_detail=BOTTLENECKS.get(code, (None, None))[1],
            )
        )

    for code, name, lat, lng in DESTINATIONS:
        session.add(
            CommuteDestination(
                code=code, name=name, lat=lat, lng=lng,
                sort_order=[d[0] for d in DESTINATIONS].index(code),
            )
        )

    agent = Agent(
        name="Chidi Okonkwo",
        name_normalised="chidi okonkwo",
        slug="chidi-okonkwo",
        company_name="Chidi Okonkwo Properties",
        operating_areas=["Lekki", "Ajah", "VI"],
        lasrera_verified=True,
        # Not claimed: no real person owns this demo profile, and claiming it
        # requires a phone_hash that the right-of-reply check authorises
        # against. Seeding it as claimed with no owner made the profile say it
        # was "managed by the agent" while nobody could reply as them — and
        # blocked anyone from claiming it either.
        profile_claimed=False,
        avg_rating_transparency=2.0,
        avg_rating_honesty=2.8,
        avg_rating_fee_fairness=2.2,
        avg_rating_responsiveness=3.1,
        avg_rating_professionalism=3.6,
        avg_rating_overall=2.8,
        avg_fee_pct=17.4,
        total_reviews=41,
        flagged=True,
        flag_reason=(
            "Multiple tenants have reported fee disputes with this agent. "
            "Read the reviews carefully and get all fees in writing."
        ),
    )
    session.add(agent)

    admin_user = User(
        phone_hash=security.hash_phone("+2348000000001"),
        phone_last4="0001",
        display_name="RentSafe Admin",
        role="admin",
        trust_score=0.99,
    )
    session.add(admin_user)

    demo_user = User(
        phone_hash=security.hash_phone("+2348012345678"),
        phone_last4="5678",
        display_name="Tola A.",
        trust_score=0.62,
    )
    anon_user = User(
        phone_hash=security.hash_phone("+2348098765432"),
        phone_last4="5432",
        display_name="Chidinma A.",
        is_anonymous_default=True,
        trust_score=0.71,
    )
    session.add_all([demo_user, anon_user])
    await session.flush()

    prop_rows: dict[str, Property] = {}
    for p in PROPERTIES:
        gh8, gh7 = geohash.encode_pair(p["lat"], p["lng"])
        prop = Property(
            property_id=p["property_id"],
            geohash_7=gh7, geohash_8=gh8,
            lat=p["lat"], lng=p["lng"],
            location_wkt=f"POINT({p['lng']} {p['lat']})",
            address_formal=p["address"], address_local=p["address"],
            w3w_address=p["w3w"],
            lga_code=p["lga"], neighbourhood_code=p["area"],
            property_type=p["type"], bedrooms=p["bedrooms"],
            flood_zone=p["flood_zone"], elevation_m=p["elevation_m"],
            drainage_dist_m=p["drainage_dist_m"],
            # avg_rating / total_reviews / verified_reviews / rating_breakdown are
            # deliberately NOT set here — they are derived from the seeded reviews
            # by recompute_property_scores() at the end of this function, so the
            # headline number and the reviews behind it can't disagree.
            latest_rent_kobo=p["rent"],
            rent_velocity_pct=p["velocity"], area_velocity_pct=p["area_velocity"],
            rent_percentile=p["percentile"],
            power_hours_avg=p["power"], security_rating=p["security"],
            high_turnover=p["turnover"], traffic_score=p["traffic"],
        )
        session.add(prop)
        prop_rows[p["property_id"]] = prop
    await session.flush()

    for p in PROPERTIES:
        prop = prop_rows[p["property_id"]]
        for year, amount, area_avg in p["history"]:
            session.add(
                RentHistory(
                    property_id=prop.id, period_year=year,
                    amount_kobo=amount, area_avg_kobo=area_avg,
                    payment_type="annual",
                )
            )
        for when, sev, quote, ev in p["floods"]:
            session.add(
                FloodEvent(
                    property_id=prop.id, when_label=when,
                    severity=sev, quote=quote, evidence=ev,
                )
            )
        for kind, label, dist, avail in TRANSIT.get(p["property_id"], []):
            session.add(
                TransitOption(
                    property_id=prop.id, kind=kind, label=label,
                    distance_m=dist, available=avail,
                )
            )
        for dest, window, mode, minutes, note in COMMUTES.get(p["property_id"], []):
            session.add(
                CommuteReport(
                    property_id=prop.id, destination_code=dest,
                    departure_window=window, mode=mode,
                    minutes=minutes, note=note,
                )
            )

    # Every property gets real review rows. Previously only the flagship did,
    # so the other five showed a confident headline score above a rating
    # breakdown of all zeros.
    reviewers = [demo_user, anon_user, admin_user]
    for p in PROPERTIES:
        prop = prop_rows[p["property_id"]]
        for i, r in enumerate(_generate_reviews(p)):
            session.add(
                Review(
                    property_id=prop.id,
                    user_id=reviewers[i % len(reviewers)].id,
                    agent_id=agent.id if p["lga"] == "ETI" and i == 0 else None,
                    tenancy_start=r["start"],
                    tenancy_end=r["end"],
                    still_living=r["end"] is None,
                    rent_amount_kobo=r["rent"],
                    rent_period="annual",
                    **{f"rating_{d}": v for d, v in r["ratings"].items()},
                    text_positives=r["positives"],
                    text_warnings=r["warnings"],
                    verification_tier=r["tier"],
                    verified_tenant=r["verified"],
                    is_anonymous=r["anonymous"],
                    display_name=r["display_name"],
                    created_at=r["created_at"],
                    moderation_status="approved",
                )
            )

    flagship = prop_rows["ETI-LEK-7F3A2B-0041"]
    session.add_all(
        [
            Review(
                property_id=flagship.id,
                user_id=anon_user.id,
                agent_id=agent.id,
                tenancy_start=dt.date(2023, 3, 1),
                tenancy_end=dt.date(2025, 2, 28),
                rent_amount_kobo=150_000_000,
                rent_period="annual",
                agent_fee_kobo=25_000_000,
                rating_landlord=3, rating_agent=2, rating_property=4,
                rating_water=4, rating_power=3, rating_security=5,
                rating_noise=3, rating_flooding=2, rating_neighbourhood=4,
                rating_value=3,
                text_positives=(
                    "Solid building, borehole water is clean and constant, estate "
                    "security is serious about gate passes."
                ),
                text_warnings=(
                    "The street floods badly every October — we lost a rug and a "
                    "generator. Landlord raised rent 25% at renewal with two weeks' notice."
                ),
                verification_tier=3,
                verified_tenant=True,
                is_anonymous=True,
                display_name="Anonymous tenant",
                moderation_status="approved",
            ),
            Review(
                property_id=flagship.id,
                user_id=demo_user.id,
                tenancy_start=dt.date(2021, 1, 1),
                tenancy_end=dt.date(2022, 12, 31),
                rent_amount_kobo=110_000_000,
                rent_period="annual",
                rating_landlord=4, rating_agent=3, rating_property=4,
                rating_water=5, rating_power=3, rating_security=5,
                rating_noise=4, rating_flooding=3, rating_neighbourhood=4,
                rating_value=4,
                text_positives=(
                    "Landlord fixed the pump within days whenever it failed. Estate "
                    "association actually works."
                ),
                text_warnings=(
                    "Negotiate the agent fee — I got it from ₦300K down to ₦200K by "
                    "asking for a receipt upfront."
                ),
                verification_tier=1,
                verified_tenant=False,
                display_name="Tola A.",
                owner_response=(
                    "The drainage on Admiralty Way was upgraded by the estate in 2025. "
                    "Renewal notices now go out 3 months ahead."
                ),
                owner_response_from="landlord",
                moderation_status="approved",
            ),
        ]
    )
    await session.flush()

    # Derive every cached aggregate from the reviews just written.
    for prop in prop_rows.values():
        await recompute_property_scores(session, prop.id, commit=False)

    await session.commit()
    return True
